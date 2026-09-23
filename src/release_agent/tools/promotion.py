"""SIT->UAT->PRD promotion of Helm-chart entries via the protected-branch PR chain.

The deploy repo carries an env-pathed deployment JSON per environment
(uat/deployment.json, prd/deployment.json), each shaped {"include": [entry, ...]}
where an entry is a Helm chart:
    {helm_chart_name, helm_chart_version, helm_chart_dir, helm_values_file_name, gke_namespace}

The dev supplies only chart_name:version (+ optional namespace); the constants and the
env-specific values-file + namespace are filled from config.

- UAT deploy : OVERRIDE uat/deployment.json via targeted per-branch edits (SIT, UAT).
- PROD       : reachable ONLY through a release — queue the chart (intake queue),
               raise the CARE or DF release, then promote it. There is no
               single-chart prod deploy; ``adk_release_agent.deploy.prepare_deploy_preview``
               refuses a chart deploy whose environment resolves to prod before any
               GitHub work happens, so this module never opens a PR against prod for
               a bare chart:version.
Entries are keyed by helm_chart_name (one entry per chart per env file).
"""

import itertools
import json
import uuid

from pydantic import BaseModel, Field

from ._common import (
    settings,
    tool,
    _get_github_client,
    _read_json_file,
    _upsert_json_file,
    _parse_pairs,
    active_deploy_repo,
)
from .release_window import (  # noqa: F401
    _release_guard_branches,
    _open_prd_pr_blocker,
)


# --- env helpers + entry assembly -------------------------------------------
def _env_key(env: str) -> str:
    """Canonical file-model env: prod/prd/production -> 'prd', everything else 'uat'.
    The deployment files/paths/values/namespace all key on 'prd' (not 'prod')."""
    return "prd" if str(env).lower() in ("prod", "prd", "production") else "uat"


def _deployment_path(env: str) -> str:
    return settings.deployment_path_pattern.format(env=_env_key(env))


def _values_file(env: str) -> str:
    return settings.helm_values_pattern.format(env=_env_key(env))


def _namespace_for(env: str, override: str = "") -> str:
    if override and override.strip():
        return override.strip()
    return settings.prd_namespace if _env_key(env) == "prd" else settings.uat_namespace


def assemble_entry(
    name: str, version: str, env: str, namespace: str = "", chart_dir: str = "", values_file: str = ""
) -> dict:
    """Build a full deployment.json entry. The dev gives name + version; the
    helm_chart_dir constant and the env-specific values-file + namespace come from
    config. Any of namespace / chart_dir / values_file may be overridden per request
    (e.g. when the user edits the JSON in the UI)."""
    return {
        "helm_chart_name": name,
        "helm_chart_version": version,
        "helm_chart_dir": chart_dir.strip() if (chart_dir and chart_dir.strip()) else settings.helm_chart_dir,
        "helm_values_file_name": values_file.strip() if (values_file and values_file.strip()) else _values_file(env),
        "gke_namespace": _namespace_for(env, namespace),
    }


# --- include[] list ops (keyed by helm_chart_name) --------------------------
def _upsert_entry(include: list, entry: dict) -> bool:
    """Replace-or-append by helm_chart_name, keeping the file's one-entry-per-chart
    invariant: the first match is replaced and any further entries with the same name
    are dropped (stale duplicates left behind by earlier whole-branch git merges), so
    a promotion self-heals a polluted include[]. Returns True if the list changed."""
    name = entry["helm_chart_name"]
    matches = [i for i, e in enumerate(include) if e.get("helm_chart_name") == name]
    if not matches:
        include.append(entry)
        return True
    changed = len(matches) > 1
    for i in reversed(matches[1:]):
        del include[i]
    if include[matches[0]] != entry:
        include[matches[0]] = entry
        changed = True
    return changed


def _remove_entry(include: list, name: str) -> bool:
    """Drop every entry with this helm_chart_name (duplicates included).
    Returns True if any was removed."""
    matches = [i for i, e in enumerate(include) if e.get("helm_chart_name") == name]
    for i in reversed(matches):
        del include[i]
    return bool(matches)


def _read_include(repo, branch: str, path: str) -> list:
    """Read the include[] list from a deployment JSON on a branch (empty if absent)."""
    doc = _read_json_file(repo, branch, path)
    inc = doc.get("include") if isinstance(doc, dict) else None
    return inc if isinstance(inc, list) else []


# --- PR plumbing ------------------------------------------------------------
# Returned by _merge_pr when another change landed on the base branch while this
# PR was being raised — the one refusal that rebuilding on the new head fixes.
MERGE_CONFLICT = "conflicts with a change that landed on the branch meanwhile"
_CONFLICT_REBUILDS = 2


def _merge_pr(pr, method: str = "squash"):
    """Merge a PR once GitHub has computed mergeability. Returns (merged, detail).
    On protected branches that require review, the merge is refused — we report it
    and leave the PR open for approval."""
    import time

    for _ in range(8):
        try:
            pr.update()
        except Exception:
            pass
        if pr.mergeable is not None:
            break
        time.sleep(1)
    if pr.mergeable is False:
        # mergeable is about conflicts only — a PR held for review is still
        # mergeable and is refused at merge() below, in GitHub's own words.
        if pr.mergeable_state == "dirty":
            return False, MERGE_CONFLICT
        return False, f"awaiting review/checks ({pr.mergeable_state})"
    from . import attribution

    try:
        pr.merge(merge_method=method, **attribution.merge_kwargs())
        return True, "merged"
    except Exception as e:
        reason = _merge_refusal_reason(e)
        # The base moved between GitHub computing mergeability and the merge:
        # someone else's change landed in that window.
        if getattr(e, "status", None) in (405, 409) and "branch was modified" in reason.lower():
            return False, MERGE_CONFLICT
        return False, reason


def _merge_refusal_reason(e: Exception) -> str:
    """Why GitHub refused a merge, in GitHub's own words.

    A protected branch answers 405 with a JSON body whose "message" already says
    what is needed ("Waiting on code owner review from <team>. Required status
    check <name> is queued.") — which is exactly what the person has to act on.
    Dumping the whole exception buried that sentence inside a status code, the
    JSON again and a docs URL.
    """
    data = getattr(e, "data", None)
    message = data.get("message") if isinstance(data, dict) else None
    return str(message or e).strip() or "merge refused by branch protection"


def _find_deploy_run(repo, head_sha: str, branch: str = "", tries: int = 4, delay: float = 1.5):
    """Find the workflow run a merge triggered, by the merge commit sha. Polls briefly
    because GitHub Actions takes a few seconds to register the run."""
    import time

    branch = branch or settings.uat_branch
    for i in range(tries):
        runs = []
        try:
            runs = list(itertools.islice(repo.get_workflow_runs(head_sha=head_sha), 5))
        except TypeError:
            try:
                runs = [
                    r for r in itertools.islice(repo.get_workflow_runs(branch=branch), 15)
                    if r.head_sha == head_sha
                ]
            except Exception:
                runs = []
        except Exception:
            runs = []
        if runs:
            r = runs[0]
            return {
                "id": r.id,
                "url": r.html_url,
                "name": r.name,
                "status": r.status,
                "conclusion": r.conclusion,
            }
        if i < tries - 1:
            time.sleep(delay)
    return None


def _doc_changed(existing: dict, new_doc: dict, ignore=("updated_at",)) -> bool:
    """True if new_doc differs from existing, ignoring volatile keys (e.g. updated_at)."""
    def _strip(d):
        return {k: v for k, v in (d or {}).items() if k not in ignore}

    return _strip(existing) != _strip(new_doc)


def _raise_hop_pr(repo, branch: str, file_mutations: list, extra_files: dict | None, summary: str):
    """One hop's PR, built on the branch's CURRENT head. Returns (pr, work
    branch), or (None, None) when the branch already matches."""
    work = f"change/promote/{uuid.uuid4().hex[:8]}"
    ref = repo.get_git_ref(f"heads/{branch}")
    repo.create_git_ref(f"refs/heads/{work}", ref.object.sha)

    changed = False
    for path, mutate_fn in file_mutations:
        doc = _read_json_file(repo, work, path)
        if not isinstance(doc, dict):
            doc = {}
        include = doc.get("include") if isinstance(doc.get("include"), list) else []
        if mutate_fn(include):
            doc["include"] = include
            doc["updated_by"] = "release-copilot"
            _upsert_json_file(repo, work, path, doc)
            changed = True

    # Whole-file docs (e.g. change-request.json): write verbatim, created if missing.
    for path, doc in (extra_files or {}).items():
        if _doc_changed(_read_json_file(repo, work, path), doc):
            _upsert_json_file(repo, work, path, doc)
            changed = True

    if not changed:
        try:
            repo.get_git_ref(f"heads/{work}").delete()
        except Exception:
            pass
        return None, None
    from . import attribution

    return repo.create_pull(title=f"{summary} (→ {branch})", body=attribution.with_trailer(summary),
                            head=work, base=branch), work


def _supersede(repo, pr, work: str, branch: str) -> None:
    """Close a PR that conflicted with a concurrent change; it is rebuilt next."""
    try:
        pr.create_issue_comment(
            f"Superseded: another change landed on `{branch}` while this was merging, "
            "so the change is being rebuilt on the new head in a fresh PR.")
        pr.edit(state="closed")
        repo.get_git_ref(f"heads/{work}").delete()
    except Exception:
        pass


def _promote_targeted(
    repo, file_mutations: list, summary: str, extra_files: dict | None = None,
    branches: tuple | None = None,
) -> dict:
    """Promote a change to PRD through SIT -> UAT -> PRD by applying the SAME targeted
    file mutation to each branch in order, each via its own working-branch PR.

    Unlike a whole-branch git merge (which drags an env's unrelated charts — e.g.
    UAT-only entries — into the next env, and conflicts permanently once branch
    histories diverge), this edits only the include[] of the named files on each
    branch. uat/deployment.json keeps
    its own per-env contents; only the promoted charts move. Returns
    {changed, prs, deploy_run, deploy_run_prd, delivered}.

    ``extra_files`` maps a path to a whole-file JSON doc written verbatim on each branch
    (created if missing) — used to promote flat, non-include files like change-request.json
    alongside the deployment files, so the standard file set travels through the chain.

    ``branches`` limits the hop list — e.g. (SIT, UAT) for a change that must stop at
    UAT; the default is the full SIT -> UAT -> PRD chain. ``delivered`` means the LAST
    hop in the list merged.

    The chain stops if a hop's PR fails to merge (branch protection/review) so a change
    can't reach a downstream env without clearing the upstream one."""
    sit, uat, prd = settings.sit_branch, settings.uat_branch, settings.prd_branch
    chain = tuple(branches) if branches else (sit, uat, prd)
    prs: list = []
    deploy_run_uat = deploy_run_prd = None

    for branch in chain:
        # Several people deploy at once: another change can land on this branch
        # between our read and our merge. Our PR then conflicts — rebuild it on
        # the new head (both changes survive) rather than leave it for someone
        # to untangle. Review/branch-protection refusals are never retried.
        superseded: list[int] = []
        for attempt in range(_CONFLICT_REBUILDS + 1):
            pr, work = _raise_hop_pr(repo, branch, file_mutations, extra_files, summary)
            if pr is None:
                break
            ok, detail = _merge_pr(pr, "squash")
            if ok or detail != MERGE_CONFLICT or attempt == _CONFLICT_REBUILDS:
                break
            _supersede(repo, pr, work, branch)
            superseded.append(pr.number)
        if pr is None:
            prs.append({"stage": f"→{branch}", "skipped": "already in desired state"})
            continue  # this env already matches; keep promoting to the next

        entry = {"stage": f"→{branch}", "number": pr.number, "url": pr.html_url, "merged": ok, "detail": detail}
        if superseded:
            entry["rebuilt_after_conflict"] = superseded
        if ok and branch in (uat, prd):
            try:
                pr.update()
                msha = pr.merge_commit_sha
                run = _find_deploy_run(repo, msha, branch) if msha else None
                if run:
                    entry["deploy_run"] = run
                    if branch == uat:
                        deploy_run_uat = run
                    else:
                        deploy_run_prd = run
            except Exception:
                pass
        prs.append(entry)
        if not ok:
            break  # don't promote further until this env's PR is merged

    # Landed = the LAST hop is in the desired state: merged now, or already
    # matching. The chain breaks on the first unmerged PR, so a last hop that is
    # present at all means every hop before it cleared.
    delivered = any(
        p.get("stage") == f"→{chain[-1]}" and (p.get("merged") or p.get("skipped"))
        for p in prs
    )
    changed_any = any(p.get("number") for p in prs)
    return {
        "changed": changed_any,
        "prs": prs,
        "deploy_run": deploy_run_uat,
        "deploy_run_prd": deploy_run_prd,
        "delivered": delivered,
    }


def _pr_link(p: dict) -> str:
    """Markdown link to a raised PR — the chat renders it clickable, and a PR
    number without its URL sends someone searching the repo for it."""
    label = f"PR #{p['number']}"
    return f"[{label}]({p['url']})" if p.get("url") else label


def _pr_chain_note(prs: list) -> str:
    """One-line human summary of the PRs raised by _promote_targeted."""
    if not prs:
        return "No change (already in that state)."
    bits = []
    for p in prs:
        if p.get("number"):
            state = "merged" if p.get("merged") else f"open — {p.get('detail', 'awaiting review')}"
            bits.append(f"{_pr_link(p)} {p['stage']} ({state})")
        elif p.get("error"):
            bits.append(f"{p['stage']} failed: {p['error']}")
    return "; ".join(bits) + "."


def _open_prs(prs: list) -> list:
    """The PRs a human still has to approve — raised, not merged."""
    return [p for p in prs if p.get("number") and not p.get("merged")]


def _pending_prs(prs: list, final_branch: str) -> list:
    """Open PRs as reported to the caller. `final` marks the PR whose merge
    COMPLETES the change — only that one can be recorded as pending."""
    return [
        {"number": p["number"], "url": p.get("url"), "stage": p["stage"],
         "reason": p.get("detail"), "final": p["stage"] == f"→{final_branch}"}
        for p in _open_prs(prs)
    ]


def _pending_note(what: str, prs: list, final_branch: str, env_label: str,
                  done: str = "done") -> str:
    """What to tell someone whose change stopped at a PR awaiting review.

    The chain breaks at the FIRST unmerged hop. When that is the final hop,
    merging it lands the change. When it is an earlier one, merging it lands
    nothing downstream — the next hop's PR has not been raised — so the
    instruction has to be "merge it, then run this again", not "merge it".
    """
    waiting = _open_prs(prs)
    links = ", ".join(_pr_link(p) for p in waiting) or "the raised PR"
    head = f"{what} — awaiting approval, NOT {done} yet. {_pr_chain_note(prs)} "
    if waiting and waiting[-1]["stage"] == f"→{final_branch}":
        return head + f"Approve and merge {links}; {env_label} is unchanged until it merges."
    return head + (f"Approve and merge {links}, then run this again — the PR into "
                   f"{final_branch} is only raised once that one has merged.")


def _deploy_run_note(res: dict) -> str:
    """Note pointing at the deploy workflow run(s) captured by the chain."""
    bits = []
    if res.get("deploy_run"):
        bits.append(f"UAT deploy run #{res['deploy_run']['id']} ({res['deploy_run']['url']})")
    if res.get("deploy_run_prd"):
        bits.append(f"PRD deploy run #{res['deploy_run_prd']['id']} ({res['deploy_run_prd']['url']})")
    return (" " + "; ".join(bits) + ".") if bits else ""


# --- deploy planning (OVERRIDE, not upsert) --------------------------------
def _normalize_entry(e: dict, env: str) -> dict:
    """Coerce a (possibly partial) entry into a full deployment.json entry, filling
    any missing constant from config (env-appropriate)."""
    return {
        "helm_chart_name": e.get("helm_chart_name"),
        "helm_chart_version": e.get("helm_chart_version"),
        "helm_chart_dir": e.get("helm_chart_dir") or settings.helm_chart_dir,
        "helm_values_file_name": e.get("helm_values_file_name") or _values_file(env),
        "gke_namespace": e.get("gke_namespace") or _namespace_for(env),
    }


def _entries_for_deploy(env, image_tags, deployment_json, namespace, chart_dir, values_file) -> list:
    """Build the target-env entry list from either a full deployment.json paste (the UI
    editor: {"include":[...]}) or <chart>:<version> pairs (NL / CLI)."""
    if deployment_json and deployment_json.strip():
        try:
            doc = json.loads(deployment_json)
        except Exception:
            return []
        inc = doc.get("include") if isinstance(doc, dict) else (doc if isinstance(doc, list) else [])
        return [
            _normalize_entry(e, env)
            for e in (inc or [])
            if isinstance(e, dict) and e.get("helm_chart_name") and e.get("helm_chart_version")
        ]
    return [assemble_entry(n, v, env, namespace, chart_dir, values_file) for n, v in _parse_pairs(image_tags)]


def plan_deploy(env: str, entries: list) -> dict:
    """Map the target-env entries to the deployment-file writes for an OVERRIDE deploy.
    uat  -> {uat/deployment.json: entries}.
    prod -> {prd/deployment.json: entries, uat/deployment.json: same entries with the
             uat values-file} so a prod deploy lands declaratively on both files."""
    uat_path, prd_path = _deployment_path("uat"), _deployment_path("prd")
    if _env_key(env) == "uat":
        return {uat_path: entries}
    uat_copy = [{**e, "helm_values_file_name": _values_file("uat")} for e in entries]
    return {prd_path: entries, uat_path: uat_copy}


def _replace_with(entries: list):
    """mutate_fn for _promote_targeted that OVERRIDES include[] with `entries`
    (complete replace, no upsert). Returns True if the list changed."""
    def _mut(include):
        old = list(include)
        include.clear()
        include.extend(entries)
        return include != old
    return _mut


def _upsert_each(entries: list):
    """mutate_fn for _promote_targeted that UPSERTS each entry (by helm_chart_name),
    preserving charts already present. Returns True if anything changed. Used by
    promotions that must ADD to an env's include[] without dropping what's already
    live there."""
    def _mut(include):
        changed = False
        for e in entries:
            changed = _upsert_entry(include, e) or changed
        return changed
    return _mut


# --- tools ------------------------------------------------------------------
class DeployInput(BaseModel):
    environment: str = Field(..., description="Target environment: uat or prod")
    image_tags: str = Field(
        default="", description="Comma-separated <helm_chart_name>:<version> (one or more charts)"
    )
    deployment_json: str = Field(
        default="",
        description='Full {"include":[...]} file content to write (OVERRIDE). Takes precedence over image_tags.',
    )
    namespace: str = Field(
        default="", description="GKE namespace (optional; defaults per environment)"
    )
    chart_dir: str = Field(
        default="", description="helm_chart_dir override (optional; defaults from config)"
    )
    values_file: str = Field(
        default="", description="helm_values_file_name override (optional; defaults per environment)"
    )
    deployment_repo: str = Field(
        default="",
        description=(
            "Target deployment repository (owner/repo or GitHub URL) for THIS deploy. "
            "Empty = the configured/session default."
        ),
    )


@tool(args_schema=DeployInput)
def open_release_pr(
    environment: str,
    image_tags: str = "",
    deployment_json: str = "",
    namespace: str = "",
    chart_dir: str = "",
    values_file: str = "",
    deployment_repo: str = "",
) -> str:
    """Deploy Helm chart(s) into uat/deployment.json (OVERRIDE — complete replace)
    via targeted working->SIT->UAT edits.

    PROD is NOT reachable here: it is reached only through a release (queue the
    chart, raise the CARE or DF release, then promote it) — callers refuse a
    prod-targeted chart deploy before ever calling this tool
    (``adk_release_agent.deploy.prepare_deploy_preview``).

    Accepts a full {"include":[...]} payload (the UI editor — supports multiple charts)
    or <chart_name>:<version> pairs (NL/CLI); constants are filled from config."""
    raw = (environment or "").strip().lower()
    if raw != "uat":
        return (
            f"ERROR deploying: unsupported environment '{environment}' — single-chart "
            "deploys only reach uat. PROD is reached through a release: queue the "
            "chart, raise the CARE or DF release, then promote it."
        )
    env = "uat"

    try:
        entries = _entries_for_deploy(env, image_tags, deployment_json, namespace, chart_dir, values_file)
    except ValueError as e:
        return f"ERROR deploying: {e}"
    if not entries:
        return "ERROR deploying: no charts provided (need a {\"include\":[...]} or <chart>:<version>)."
    chart_str = ", ".join(f"{e['helm_chart_name']}:{e['helm_chart_version']}" for e in entries)

    # Per-deploy repo from the form payload wins; else the configured/session default.
    from ..session_creds import _normalize_repo
    target_repo = _normalize_repo(deployment_repo) if deployment_repo else ""
    if deployment_repo and not target_repo:
        return f"ERROR deploying: could not parse deployment_repo '{deployment_repo}' (use owner/repo)."
    try:
        repo = _get_github_client().get_repo(target_repo or active_deploy_repo())
    except Exception as e:
        return f"ERROR deploying: {e}"

    # --- UAT: OVERRIDE uat/deployment.json via targeted per-branch edits ---
    # (SIT then UAT, each via its own short-lived change branch. The old
    # working->SIT->UAT whole-branch merge conflicted permanently once SIT/UAT
    # histories diverged — observed live on deployment-repo PRs #93, #96, #103.)
    uat_path = _deployment_path("uat")
    # The override REPLACES the file: a chart on UAT now and not in `entries`
    # leaves UAT. Reported so it is logged as removed — otherwise the derived
    # per-environment state would show it deployed forever.
    before = _read_include(repo, settings.uat_branch, uat_path)
    kept = {e.get("helm_chart_name") for e in entries if isinstance(e, dict)}
    dropped = [{"name": b.get("helm_chart_name"), "tag": b.get("helm_chart_version")}
               for b in before if isinstance(b, dict) and b.get("helm_chart_name")
               and b.get("helm_chart_name") not in kept]
    res = _promote_targeted(
        repo,
        [(uat_path, _replace_with(entries))],
        f"Deploy {chart_str} to uat",
        branches=(settings.sit_branch, settings.uat_branch),
    )
    if not res["changed"]:
        return json.dumps(
            {"ok": True, "action": "no_change", "environment": "uat", "image_tags": chart_str,
             "note": f"No change — uat/deployment.json already matches {chart_str}."},
            indent=2,
        )
    if res["delivered"]:
        uat_now = _read_include(repo, settings.uat_branch, uat_path)
        note = (
            f"Deployed {chart_str} to UAT (override). {_pr_chain_note(res['prs'])} "
            f"Replaced uat/deployment.json. {len(uat_now)} chart(s) on UAT." + _deploy_run_note(res)
        )
        action = "deployed"
    else:
        # The chain stopped at a PR branch protection would not let us merge.
        # Nothing has reached UAT: saying "Deployed" here — or quoting the live
        # chart count, which is the PRE-deploy file — told people a change had
        # landed when it was sitting in review.
        note = _pending_note(f"Raised {chart_str} for UAT", res["prs"],
                             settings.uat_branch, "UAT", done="deployed") + _deploy_run_note(res)
        action = "pending_review"
    return json.dumps(
        {
            "ok": True,
            "environment": "uat",
            "action": action,
            "pending_prs": _pending_prs(res["prs"], settings.uat_branch),
            "image_tags": chart_str,
            "files_updated": ["uat/deployment.json"],
            # Only when it landed: before the merge the live file is the OLD one.
            "uat_charts": uat_now if res["delivered"] else None,
            "dropped": dropped,
            "prs": res["prs"],
            "deploy_run": res.get("deploy_run"),
            "note": note,
        },
        indent=2,
    )


class RemoveFromReleaseInput(BaseModel):
    image_names: str = Field(
        ...,
        description="Comma-separated helm chart names to remove (version optional/ignored, "
        "e.g. 'abc-client-api-svc').",
    )
    environment: str = Field(
        default="uat",
        description=(
            "Where to remove from — a LIVE environment only. 'uat' (default): remove "
            "from the live uat/deployment.json. 'prod': also remove from BOTH live "
            "files (uat + prd) through the targeted chain SIT -> UAT -> PRD. To take a "
            "chart out of a release that hasn't shipped yet, edit the release itself "
            "(it hasn't reached a live environment)."
        ),
    )
    deployment_repo: str = Field(
        default="",
        description=(
            "Target deployment repository (owner/repo or GitHub URL) when the release was "
            "staged in a non-default repo. Empty = the configured/session default."
        ),
    )


@tool(args_schema=RemoveFromReleaseInput)
def remove_from_release(image_names: str, environment: str = "uat", deployment_repo: str = "") -> str:
    """Remove chart(s) by helm_chart_name from a LIVE environment.

    environment='uat' (default): remove from the live uat/deployment.json via targeted
      per-branch edits (working->SIT, working->UAT — no whole-branch merge).
    environment='prod': ALSO remove from BOTH live deployment files through the
      targeted chain SIT -> UAT -> PRD.
    Use when a chart is already deployed and needs to come OFF an environment —
    not for taking a chart out of a release that hasn't shipped yet."""
    names = []
    for tok in image_names.replace(",", " ").split():
        tok = tok.strip()
        if tok:
            names.append(tok.split(":", 1)[0])
    if not names:
        return "ERROR: no chart names provided to remove."

    raw = (environment or "uat").strip().lower()
    if raw in ("prod", "prd", "production"):
        env = "prod"
    elif raw == "uat":
        env = "uat"
    else:
        return (
            f"ERROR removing from release: unsupported environment '{environment}' "
            "(use uat or prod)."
        )

    from ..session_creds import _normalize_repo

    target_repo = _normalize_repo(deployment_repo) if deployment_repo else ""
    if deployment_repo and not target_repo:
        return (
            f"ERROR removing from release: could not parse deployment_repo "
            f"'{deployment_repo}' (use owner/repo)."
        )
    try:
        repo = _get_github_client().get_repo(target_repo or active_deploy_repo())
    except Exception as e:
        return f"ERROR removing from release: {e}"

    removed: list = []

    def _mut(include):
        changed = False
        for n in names:
            if _remove_entry(include, n):
                if n not in removed:
                    removed.append(n)
                changed = True
        return changed

    uat_path = _deployment_path("uat")
    summary = "Remove " + ",".join(names) + f" from {env}"
    if env == "prod":
        # Targeted SIT -> UAT -> PRD so the removal reaches PRD without a whole-branch merge.
        res = _promote_targeted(repo, [(_deployment_path("prd"), _mut), (uat_path, _mut)], summary)
    else:
        # Targeted per-branch edits stopping at UAT (whole-branch SIT->UAT merges
        # conflict whenever UAT has moved independently of SIT).
        res = _promote_targeted(
            repo, [(uat_path, _mut)], summary,
            branches=(settings.sit_branch, settings.uat_branch),
        )
    if not res["changed"]:
        return json.dumps(
            {"ok": True, "action": "no_change", "environment": env,
             "note": f"No change — {', '.join(names)} not deployed to {env}; nothing to remove."},
            indent=2,
        )
    if not res["delivered"]:
        # Same as a UAT deploy: the chain stopped at a PR awaiting review, so the
        # chart is STILL live. Reporting it removed — and writing 'removed'
        # events — would make "what's deployed where" lie until someone merges.
        final_branch = settings.prd_branch if env == "prod" else settings.uat_branch
        pending = _pending_prs(res["prs"], final_branch)
        final_pr = next((p for p in pending if p["final"]), None)
        if final_pr:
            # Merged in GitHub later, outside any chat turn — pr_reconcile writes
            # the 'removed' events then. Best-effort.
            try:
                from .pr_reconcile import record_pending

                for ev_env in (("prd", "uat") if env == "prod" else ("uat",)):
                    record_pending(ev_env, [{"name": n} for n in removed],
                                   target_repo or active_deploy_repo(), final_pr["number"],
                                   on_merge="removed", tag="removed_from_live")
            except Exception:
                pass
        return json.dumps(
            {"ok": True, "action": "removal_pending_review", "environment": env,
             "removed": [], "requested": sorted(set(removed)),
             "pending_prs": pending, "prs": res["prs"],
             "note": _pending_note(
                 f"Raised removal of {', '.join(removed)} from live {env}",
                 res["prs"], final_branch, env, done="removed")},
            indent=2,
        )
    note = (
        f"Removed {', '.join(removed)} from live {env} via PR chain. {_pr_chain_note(res['prs'])}"
        + _deploy_run_note(res)
    )
    # BQ capture: live removals write 'removed' events so the per-environment
    # deployed state derived from the event log stays accurate. A prod removal
    # edits both prd and uat deployment files. Best-effort telemetry. Only once
    # the chain has LANDED — see the pending branch above.
    try:
        from . import release_queue as _rq

        terminal_pr = next((p.get("number") for p in reversed(res["prs"]) if p.get("number")), None)
        for ev_env in (("prd", "uat") if env == "prod" else ("uat",)):
            _rq.record_deployment(
                environment=ev_env,
                artifacts=[{"name": n} for n in removed],
                deployment_repo=target_repo or active_deploy_repo(),
                pr_number=terminal_pr,
                note="removed_from_live",
                event_type="removed",
            )
    except Exception:
        pass
    return json.dumps(
        {"ok": True, "action": "removed", "environment": env,
         "removed": sorted(set(removed)), "prs": res["prs"], "note": note},
        indent=2,
    )
