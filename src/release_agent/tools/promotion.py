"""SIT->UAT->PRD promotion of Helm-chart entries via the protected-branch PR chain.

The deploy repo carries an env-pathed deployment JSON per environment
(uat/deployment.json, prd/deployment.json), each shaped {"include": [entry, ...]}
where an entry is a Helm chart:
    {helm_chart_name, helm_chart_version, helm_chart_dir, helm_values_file_name, gke_namespace}

The dev supplies only chart_name:version (+ optional namespace); the constants and the
env-specific values-file + namespace are filled from config.

- UAT deploy  : OVERRIDE uat/deployment.json (chain working->SIT->UAT).
- PROD deploy : accumulate the chart into today's PRD release PR (a day-long PR on a
               release/prd/<date> branch holding BOTH uat & prd deployment.json). At the
               cutoff, `release prod` promotes the staged charts through the FULL chain
               working->SIT->UAT->PRD (merge_prod_release) — prod never skips SIT/UAT.
Entries are keyed by helm_chart_name (one entry per chart per env file).
"""

from ._common import (
    settings,
    tool,
    BaseModel,
    Field,
    json,
    itertools,
    uuid,
    _get_github_client,
    _read_json_file,
    _upsert_json_file,
    _parse_pairs,
    active_deploy_repo,
)
from .release_window import _today_prd_pr, _prd_release_branch  # noqa: F401


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
        return False, f"awaiting review/checks ({pr.mergeable_state})"
    try:
        pr.merge(merge_method=method)
        return True, "merged"
    except Exception as e:
        return False, f"could not auto-merge (likely branch protection): {e}"


def _open_pr_on_file(repo, base_branch: str, path: str):
    """Return the first OPEN PR into base_branch that changes `path`, or None.
    Lets a deploy say 'a PR is already open' instead of stacking a duplicate — the
    same guard Dependabot/Renovate use to avoid concurrent writes to one file."""
    try:
        prs = repo.get_pulls(state="open", base=base_branch, sort="created", direction="desc")
    except Exception:
        return None
    for pr in itertools.islice(prs, 30):
        try:
            if any(f.filename == path for f in pr.get_files()):
                return pr
        except Exception:
            continue
    return None


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


def _apply_via_pr_chain(repo, file_mutations: list, summary: str) -> dict:
    """Apply per-file mutations along the protected-branch chain (never commit directly).

    file_mutations is a list of (path, mutate_fn); mutate_fn(include_list) mutates the
    include[] list in place and returns True if it changed anything. The chain runs
    working -> SIT -> UAT (this is the UAT deploy path). Promotion on to PRD is done by
    _promote_targeted, not by a whole-branch UAT->PRD merge. Returns
    {changed, prs, deploy_run, blocked_pr?}."""
    sit, uat = settings.sit_branch, settings.uat_branch
    prs: list = []

    # Guard: if a PR touching any target file is already open into SIT, don't stack another.
    for path, _ in file_mutations:
        existing = _open_pr_on_file(repo, sit, path)
        if existing is not None:
            return {
                "changed": False,
                "prs": [],
                "blocked_pr": {"number": existing.number, "url": existing.html_url, "path": path},
            }

    sit_ref = repo.get_git_ref(f"heads/{sit}")
    work = f"change/sit/{uuid.uuid4().hex[:8]}"
    repo.create_git_ref(f"refs/heads/{work}", sit_ref.object.sha)

    changed_any = False
    for path, mutate_fn in file_mutations:
        doc = _read_json_file(repo, work, path)
        if not isinstance(doc, dict):
            doc = {}
        include = doc.get("include")
        if not isinstance(include, list):
            include = []
        if mutate_fn(include):
            doc["include"] = include
            doc["updated_by"] = "release-copilot"
            _upsert_json_file(repo, work, path, doc)
            changed_any = True

    if not changed_any:
        try:
            repo.get_git_ref(f"heads/{work}").delete()
        except Exception:
            pass
        return {"changed": False, "prs": []}

    # 1) working branch -> SIT
    pr_sit = repo.create_pull(title=f"{summary} (→ {sit})", body=summary, head=work, base=sit)
    ok, detail = _merge_pr(pr_sit, "squash")
    prs.append(
        {"stage": f"→{sit}", "number": pr_sit.number, "url": pr_sit.html_url, "merged": ok, "detail": detail}
    )

    # 2) SIT -> UAT
    if ok:
        try:
            pr_uat = repo.create_pull(
                title=f"Promote {sit} → {uat}: {summary}", body=summary, head=sit, base=uat
            )
            ok2, detail2 = _merge_pr(pr_uat, "merge")
            entry = {
                "stage": f"{sit}→{uat}",
                "number": pr_uat.number,
                "url": pr_uat.html_url,
                "merged": ok2,
                "detail": detail2,
            }
            if ok2:
                try:
                    pr_uat.update()
                    msha = pr_uat.merge_commit_sha
                    run = _find_deploy_run(repo, msha, uat) if msha else None
                    if run:
                        entry["deploy_run"] = run
                except Exception:
                    pass
            prs.append(entry)
        except Exception as e:
            prs.append({"stage": f"{sit}→{uat}", "error": str(e)})

    deploy_run = next((p.get("deploy_run") for p in prs if p.get("deploy_run")), None)
    return {"changed": True, "prs": prs, "deploy_run": deploy_run, "deploy_run_prd": None}


def _doc_changed(existing: dict, new_doc: dict, ignore=("updated_at",)) -> bool:
    """True if new_doc differs from existing, ignoring volatile keys (e.g. updated_at)."""
    def _strip(d):
        return {k: v for k, v in (d or {}).items() if k not in ignore}

    return _strip(existing) != _strip(new_doc)


def _promote_targeted(
    repo, file_mutations: list, summary: str, extra_files: dict | None = None,
    branches: tuple | None = None,
) -> dict:
    """Promote a change to PRD through SIT -> UAT -> PRD by applying the SAME targeted
    file mutation to each branch in order, each via its own working-branch PR.

    Unlike _apply_via_pr_chain (which git-merges whole branches and so drags an env's
    unrelated charts — e.g. UAT-only entries — into the next env and conflicts), this
    edits only the include[] of the named files on each branch. uat/deployment.json keeps
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
            prs.append({"stage": f"→{branch}", "skipped": "already in desired state"})
            continue  # this env already matches; keep promoting to the next

        pr = repo.create_pull(title=f"{summary} (→ {branch})", body=summary, head=work, base=branch)
        ok, detail = _merge_pr(pr, "squash")
        entry = {"stage": f"→{branch}", "number": pr.number, "url": pr.html_url, "merged": ok, "detail": detail}
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

    delivered = any(p.get("stage") == f"→{chain[-1]}" and p.get("merged") for p in prs)
    changed_any = any(p.get("number") for p in prs)
    return {
        "changed": changed_any,
        "prs": prs,
        "deploy_run": deploy_run_uat,
        "deploy_run_prd": deploy_run_prd,
        "delivered": delivered,
    }


def _pr_chain_note(prs: list) -> str:
    """One-line human summary of the PRs raised by _apply_via_pr_chain."""
    if not prs:
        return "No change (already in that state)."
    bits = []
    for p in prs:
        if p.get("number"):
            bits.append(
                f"PR #{p['number']} {p['stage']} ({'merged' if p.get('merged') else p.get('detail', 'open')})"
            )
        elif p.get("error"):
            bits.append(f"{p['stage']} failed: {p['error']}")
    return "; ".join(bits) + "."


def _deploy_run_note(res: dict) -> str:
    """Note pointing at the deploy workflow run(s) captured by the chain."""
    bits = []
    if res.get("deploy_run"):
        bits.append(f"UAT deploy run #{res['deploy_run']['id']} ({res['deploy_run']['url']})")
    if res.get("deploy_run_prd"):
        bits.append(f"PRD deploy run #{res['deploy_run_prd']['id']} ({res['deploy_run_prd']['url']})")
    return (" " + "; ".join(bits) + ".") if bits else ""


def _blocked_pr_result(res: dict):
    """If the chain was blocked by an already-open PR on a target file, return a
    user-facing JSON string; otherwise None."""
    b = res.get("blocked_pr")
    if not b:
        return None
    return json.dumps(
        {
            "ok": False,
            "action": "pr_already_open",
            "pr_number": b["number"],
            "pr_url": b["url"],
            "note": (
                f"A deploy PR is already open — #{b['number']} ({b['url']}) — touching "
                f"`{b.get('path', 'the deployment config')}`. Merge or close it first, then retry. "
                "(One deploy at a time per file.)"
            ),
        },
        indent=2,
    )


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
    """mutate_fn for _apply_via_pr_chain that OVERRIDES include[] with `entries`
    (complete replace, no upsert). Returns True if the list changed."""
    def _mut(include):
        old = list(include)
        include.clear()
        include.extend(entries)
        return include != old
    return _mut


def _upsert_each(entries: list):
    """mutate_fn for _apply_via_pr_chain that UPSERTS each entry (by helm_chart_name),
    preserving charts already present. Returns True if anything changed. Used by the
    PRD release so promoting to current PRD/UAT adds today's charts without dropping
    what's already live."""
    def _mut(include):
        changed = False
        for e in entries:
            changed = _upsert_entry(include, e) or changed
        return changed
    return _mut


def _utc_now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def change_request_doc(change_request, now_iso: str) -> dict | None:
    """Normalize a prod deploy's change_request into the change-request.json doc.

    The stored file uses the canonical CHG keys (chg_summary / start_date / end_date)
    plus a free-form ``description``. Accepts either those keys or the form's semantic
    aliases (summary / start_time / end_time / change_description). Returns None when no
    usable change-request content is supplied.
    """
    if isinstance(change_request, str):
        try:
            change_request = json.loads(change_request)
        except (json.JSONDecodeError, ValueError):
            return None
    if not isinstance(change_request, dict) or not change_request:
        return None
    cr = change_request
    doc = {
        "chg_summary": cr.get("chg_summary") or cr.get("summary") or "",
        "description": cr.get("description") or cr.get("change_description") or "",
        "start_date": cr.get("start_date") or cr.get("start_time") or "",
        "end_date": cr.get("end_date") or cr.get("end_time") or "",
        "updated_by": "release-copilot",
        "updated_at": now_iso,
    }
    if not any(doc[k] for k in ("chg_summary", "description", "start_date", "end_date")):
        return None
    return doc


# --- PRD release PR (accumulate through the day, merge at cutoff) ------------
# _today_prd_pr / _prd_release_branch live in release_window (the lower module) so both
# this module and the status reader share one definition without a circular import.
def _accumulate_into_prd_pr(repo, entries: list, change_request=None):
    """Upsert chart(s) into today's PRD release branch — BOTH uat/deployment.json and
    prd/deployment.json (each with its env values file) — and ensure the open PR exists.
    Accumulates (upsert by helm_chart_name) so charts pile up through the day. When a prod
    deploy carries a ``change_request`` (change summary/description + start/end time), the
    day's change-request.json (settings.change_request_path) is written on the same branch.
    Returns (pr, branch, created, changed_paths)."""
    prd, branch = settings.prd_branch, _prd_release_branch()
    pr = _today_prd_pr(repo)

    # Ensure the day's branch exists (cut from PRD's current state).
    if pr is None:
        try:
            repo.get_git_ref(f"heads/{branch}")
        except Exception:
            prd_ref = repo.get_git_ref(f"heads/{prd}")
            repo.create_git_ref(f"refs/heads/{branch}", prd_ref.object.sha)

    # Accumulate into both files on the branch (commit before creating the PR so there's a diff).
    plan = plan_deploy("prd", entries)  # {prd_path: prd_entries, uat_path: uat_entries}
    changed = []
    for path, ents in plan.items():
        doc = _read_json_file(repo, branch, path)
        if not isinstance(doc, dict):
            doc = {}
        include = doc.get("include") if isinstance(doc.get("include"), list) else []
        ch = False
        for e in ents:
            ch = _upsert_entry(include, e) or ch
        if ch:
            doc["include"] = include
            doc["updated_by"] = "release-copilot"
            _upsert_json_file(repo, branch, path, doc)
            changed.append(path)

    # Change request (prod only): write/update change-request.json on the release branch so
    # the CHG travels in the same PR as the deployment changes.
    cr_doc = change_request_doc(change_request, _utc_now_iso())
    if cr_doc is not None:
        cr_path = settings.change_request_path
        if _doc_changed(_read_json_file(repo, branch, cr_path), cr_doc):
            _upsert_json_file(repo, branch, cr_path, cr_doc)
            changed.append(cr_path)

    created = False
    if pr is None:
        if not changed:
            return None, branch, False, []  # nothing to release (already matches PRD)
        date = branch.rsplit("/", 1)[-1]
        body = (
            "Daily PRD release — charts accumulate here through the day. After the cutoff, "
            f"`release prod` promotes the staged charts through the chain "
            f"**{settings.sit_branch} → {settings.uat_branch} → {prd}** (do not merge this PR "
            "directly; it's the staging view and is retired once the release ships)."
        )
        if cr_doc is not None:
            body += (
                f"\n\n**Change request:** {cr_doc['chg_summary'] or '(no summary)'}\n"
                f"- Window: {cr_doc['start_date'] or '?'} → {cr_doc['end_date'] or '?'}\n"
                f"- {cr_doc['description'] or ''}"
            )
        pr = repo.create_pull(title=f"PRD release {date}", body=body, head=branch, base=prd)
        created = True
    else:
        try:
            pr.update()
        except Exception:
            pass
    return pr, branch, created, changed


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
    change_request: dict | None = Field(
        default=None,
        description=(
            "PROD only: change-request details (chg_summary, description, start_date, "
            "end_date) written to change-request.json on the release PR. Ignored for uat."
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
    change_request: dict | None = None,
) -> str:
    """Deploy Helm chart(s) into the deployment JSON.

      uat : OVERRIDE uat/deployment.json (complete replace) via working->SIT->UAT.
      prod: ADD the chart(s) to today's PRD release PR — a single day-long PR that
            accumulates (upsert by chart name) BOTH uat/deployment.json and
            prd/deployment.json on a release/prd/<date> branch. It stays OPEN; after the
            cutoff merge_prod_release ("release prod") promotes the staged charts through
            the chain SIT->UAT->PRD (prod never skips SIT/UAT).

    Accepts a full {"include":[...]} payload (the UI editor — supports multiple charts)
    or <chart_name>:<version> pairs (NL/CLI); constants are filled from config."""
    raw = (environment or "").strip().lower()
    if raw == "uat":
        env = "uat"
    elif raw in ("prod", "prd", "production"):
        env = "prod"
    else:
        return f"ERROR deploying: unsupported environment '{environment}' (use uat or prod)."

    try:
        entries = _entries_for_deploy(env, image_tags, deployment_json, namespace, chart_dir, values_file)
    except ValueError as e:
        return f"ERROR deploying: {e}"
    if not entries:
        return "ERROR deploying: no charts provided (need a {\"include\":[...]} or <chart>:<version>)."
    chart_str = ", ".join(f"{e['helm_chart_name']}:{e['helm_chart_version']}" for e in entries)

    try:
        repo = _get_github_client().get_repo(active_deploy_repo())
    except Exception as e:
        return f"ERROR deploying: {e}"

    # --- PROD: accumulate into today's PRD release PR (merged later at the cutoff) ---
    if env == "prod":
        pr, branch, created, changed = _accumulate_into_prd_pr(repo, entries, change_request)
        if pr is None:
            return json.dumps(
                {"ok": True, "action": "no_change", "environment": "prod", "image_tags": chart_str,
                 "note": f"No change — {chart_str} already matches PRD; nothing to add to the release."},
                indent=2,
            )
        cutoff = settings.prd_cutoff_hour_utc
        in_release = _read_include(repo, branch, _deployment_path("prd"))
        note = (
            f"Added {chart_str} to today's PRD release PR #{pr.number} ({pr.html_url}). "
            f"It now holds {len(in_release)} chart(s) and stays open — say 'release prod' to merge it "
            f"after {cutoff:02d}:00 UTC."
        )
        return json.dumps(
            {
                "ok": True,
                "environment": "prod",
                "action": "staged_to_prd_pr",
                "image_tags": chart_str,
                "pr_number": pr.number,
                "pr_url": pr.html_url,
                "pr_created": created,
                "files_updated": changed,
                "charts_in_release": in_release,
                "cutoff_utc": f"{cutoff:02d}:00",
                "note": note,
            },
            indent=2,
        )

    # --- UAT: OVERRIDE uat/deployment.json via working -> SIT -> UAT ---
    uat_path = _deployment_path("uat")
    res = _apply_via_pr_chain(repo, [(uat_path, _replace_with(entries))], f"Deploy {chart_str} to uat")
    blocked = _blocked_pr_result(res)
    if blocked:
        return blocked
    if not res["changed"]:
        return json.dumps(
            {"ok": True, "action": "no_change", "environment": "uat", "image_tags": chart_str,
             "note": f"No change — uat/deployment.json already matches {chart_str}."},
            indent=2,
        )
    uat_now = _read_include(repo, settings.uat_branch, uat_path)
    note = (
        f"Deployed {chart_str} to UAT (override). {_pr_chain_note(res['prs'])} "
        f"Replaced uat/deployment.json. {len(uat_now)} chart(s) on UAT." + _deploy_run_note(res)
    )
    return json.dumps(
        {
            "ok": True,
            "environment": "uat",
            "action": "deployed",
            "image_tags": chart_str,
            "files_updated": ["uat/deployment.json"],
            "uat_charts": uat_now,
            "prs": res["prs"],
            "deploy_run": res.get("deploy_run"),
            "note": note,
        },
        indent=2,
    )


def _retire_staging_pr(repo, pr, branch: str) -> None:
    """Close the day's staging PR and delete its branch once the release has shipped
    through the chain (its diff vs PRD is now empty). Best-effort."""
    try:
        pr.edit(state="closed")
    except Exception:
        pass
    try:
        repo.get_git_ref(f"heads/{branch}").delete()
    except Exception:
        pass


def _staging_pending_vs_prd(repo, branch: str) -> list:
    """Entries staged on the release branch whose version differs from live PRD —
    i.e. what would actually ship at the cutoff."""
    prd_path = _deployment_path("prd")
    prd_now = {
        e.get("helm_chart_name"): e.get("helm_chart_version")
        for e in _read_include(repo, settings.prd_branch, prd_path)
    }
    return [
        e for e in _read_include(repo, branch, prd_path)
        if e.get("helm_chart_name") and prd_now.get(e.get("helm_chart_name")) != e.get("helm_chart_version")
    ]


def _unstage_from_prd_pr(repo, names: list) -> dict | None:
    """Drop chart(s) by helm_chart_name from today's open PRD release PR — BOTH
    uat/deployment.json and prd/deployment.json on the release/prd/<date> branch — so
    they don't ship at the cutoff. Live env branches are NOT touched. If nothing left
    on the branch differs from PRD, the staging PR is retired (same as after a release).
    Returns None when no staging PR is open today; otherwise
    {pr_number, pr_url, removed, retired, still_pending}."""
    pr = _today_prd_pr(repo)
    if pr is None:
        return None
    branch = pr.head.ref
    removed: list = []
    for path in (_deployment_path("prd"), _deployment_path("uat")):
        doc = _read_json_file(repo, branch, path)
        if not isinstance(doc, dict):
            continue
        include = doc.get("include") if isinstance(doc.get("include"), list) else []
        changed = False
        for n in names:
            if _remove_entry(include, n):
                changed = True
                if n not in removed:
                    removed.append(n)
        if changed:
            doc["include"] = include
            doc["updated_by"] = "release-copilot"
            _upsert_json_file(repo, branch, path, doc)
    pending = _staging_pending_vs_prd(repo, branch)
    retired = False
    if removed and not pending:
        _retire_staging_pr(repo, pr, branch)
        retired = True
    return {
        "pr_number": pr.number,
        "pr_url": pr.html_url,
        "removed": removed,
        "retired": retired,
        "still_pending": [
            f"{e.get('helm_chart_name')}:{e.get('helm_chart_version')}" for e in pending
        ],
    }


@tool
def merge_prod_release() -> str:
    """Release today's accumulated PRD release to production by promoting the staged charts
    through the FULL chain SIT -> UAT -> PRD (prod never skips SIT/UAT). Allowed ONLY after
    the daily cutoff (PRD_CUTOFF_HOUR_UTC). Use when a developer says 'release prod'."""
    from datetime import datetime, timezone

    try:
        repo = _get_github_client().get_repo(active_deploy_repo())
    except Exception as e:
        return f"ERROR releasing prod: {e}"

    pr = _today_prd_pr(repo)
    if pr is None:
        return "No PRD release PR is open today — nothing to release. Deploy chart(s) to prod first."

    now = datetime.now(timezone.utc)
    cutoff = settings.prd_cutoff_hour_utc
    if now.hour < cutoff:
        return (
            f"ERROR: today's PRD release can only be released after {cutoff:02d}:00 UTC "
            f"(now {now.strftime('%H:%M')} UTC). Release PR #{pr.number} stays open until then: {pr.html_url}"
        )

    branch = pr.head.ref
    uat_path, prd_path = _deployment_path("uat"), _deployment_path("prd")

    # The accumulated final state staged on the release branch (cut from PRD + today's
    # upserts). "Today's charts" = whatever differs from what's currently live in PRD.
    staged_uat = _read_include(repo, branch, uat_path)
    today_prd = _staging_pending_vs_prd(repo, branch)
    today_names = {e["helm_chart_name"] for e in today_prd}
    today_uat = [e for e in staged_uat if e.get("helm_chart_name") in today_names]

    if not today_prd:
        _retire_staging_pr(repo, pr, branch)
        return json.dumps(
            {"ok": True, "action": "nothing_to_release", "pr_number": pr.number, "pr_url": pr.html_url,
             "note": f"PRD release PR #{pr.number} had no changes vs PRD; retired it. Nothing to release."},
            indent=2,
        )

    chart_str = ", ".join(f"{e['helm_chart_name']}:{e['helm_chart_version']}" for e in today_prd)
    date = branch.rsplit("/", 1)[-1]

    # The day's change request (staged on the release branch) travels with the release so
    # the live SIT/UAT/PRD branches carry it as part of the standard file set.
    staged_cr = _read_json_file(repo, branch, settings.change_request_path)
    extra_files = {settings.change_request_path: staged_cr} if staged_cr else None

    # Promote the staged charts through SIT -> UAT -> PRD, upserting into BOTH the prd and
    # uat deployment files on each branch (targeted edits — no whole-branch merge, so
    # UAT-only charts never leak into PRD and there's nothing to conflict). change-request.json
    # is promoted verbatim alongside them.
    res = _promote_targeted(
        repo,
        [(prd_path, _upsert_each(today_prd)), (uat_path, _upsert_each(today_uat))],
        f"PRD release {date}: {chart_str}",
        extra_files=extra_files,
    )
    if not res["changed"]:
        _retire_staging_pr(repo, pr, branch)
        return json.dumps(
            {"ok": True, "action": "nothing_to_release", "pr_number": pr.number, "pr_url": pr.html_url,
             "note": "PRD already matches today's release; retired the staging PR."},
            indent=2,
        )

    delivered = res["delivered"]
    if delivered:
        _retire_staging_pr(repo, pr, branch)
        note = (
            f"Released {chart_str} to PROD through {settings.sit_branch} → {settings.uat_branch} → "
            f"{settings.prd_branch}. {_pr_chain_note(res['prs'])} Retired staging PR #{pr.number}."
            + _deploy_run_note(res)
        )
        action = "prod_released"
    else:
        note = (
            f"Promoting {chart_str} to PROD — raised the chain {settings.sit_branch} → "
            f"{settings.uat_branch} → {settings.prd_branch}, but the final PRD merge is pending "
            f"(review/branch protection). {_pr_chain_note(res['prs'])} Staging PR #{pr.number} stays "
            f"open until PRD merges: {pr.html_url}" + _deploy_run_note(res)
        )
        action = "release_pending_prd_merge"

    return json.dumps(
        {
            "ok": True,
            "action": action,
            "released": today_prd,
            "staging_pr": pr.number,
            "staging_pr_url": pr.html_url,
            "prs": res["prs"],
            "deploy_run_prd": res.get("deploy_run_prd"),
            "deploy_run_uat": res.get("deploy_run"),
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
        default="staging",
        description=(
            "Where to remove from. 'staging' (default): unstage from today's PRD release "
            "PR only — live environments are untouched. 'uat': also remove from the live "
            "uat/deployment.json. 'prod': also remove from BOTH live files (uat + prd). "
            "Use uat/prod ONLY when the user explicitly names that live environment."
        ),
    )


@tool(args_schema=RemoveFromReleaseInput)
def remove_from_release(image_names: str, environment: str = "staging") -> str:
    """Remove/unstage chart(s) by helm_chart_name.

    environment='staging' (default): drop the chart(s) from today's PRD release PR
      (release/prd/<date>, both deployment files) so they don't ship at the cutoff.
      Live environments are NOT touched; if nothing else is left to release, the
      staging PR is retired. Use this for "remove X from the release / unstage X".
    environment='uat' : ALSO remove from the live uat/deployment.json via targeted
      per-branch edits (working->SIT, working->UAT — no whole-branch merge).
    environment='prod': ALSO remove from BOTH live deployment files through the
      targeted chain SIT -> UAT -> PRD.
    Live removals always unstage from today's release PR first, so a removed chart
    can't ship again at the cutoff."""
    names = []
    for tok in image_names.replace(",", " ").split():
        tok = tok.strip()
        if tok:
            names.append(tok.split(":", 1)[0])
    if not names:
        return "ERROR: no chart names provided to remove."

    raw = (environment or "staging").strip().lower()
    if raw in ("prod", "prd", "production"):
        env = "prod"
    elif raw == "uat":
        env = "uat"
    elif raw in ("", "staging", "stage", "release"):
        env = "staging"
    else:
        return (
            f"ERROR removing from release: unsupported environment '{environment}' "
            "(use staging, uat or prod)."
        )

    try:
        repo = _get_github_client().get_repo(active_deploy_repo())
    except Exception as e:
        return f"ERROR removing from release: {e}"

    # Whatever the target env, unstage from today's PRD release PR first — a chart left
    # staged there would ship again at the cutoff.
    staging = _unstage_from_prd_pr(repo, names)
    staged_removed = staging["removed"] if staging else []
    unstage_note = ""
    if staged_removed:
        unstage_note = (
            f"Unstaged {', '.join(staged_removed)} from today's PRD release PR "
            f"#{staging['pr_number']} ({staging['pr_url']})"
        )
        if staging["retired"]:
            unstage_note += " — nothing left to release, so the staging PR was retired"
        unstage_note += ". "

    if env == "staging":
        if not staged_removed:
            if staging is None:
                note = (
                    f"No PRD release PR is open today — {', '.join(names)} is not staged for "
                    "release. To remove from a live environment instead, say so explicitly "
                    "(environment=uat or prod)."
                )
            else:
                note = (
                    f"No change — {', '.join(names)} is not staged in today's PRD release PR "
                    f"#{staging['pr_number']} ({staging['pr_url']})."
                )
            return json.dumps(
                {"ok": True, "action": "no_change", "environment": "staging", "note": note},
                indent=2,
            )
        note = unstage_note + "Live environments were not touched."
        return json.dumps(
            {"ok": True, "action": "unstaged", "environment": "staging",
             "removed": staged_removed, "staging_pr": staging, "note": note},
            indent=2,
        )

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
        # Targeted per-branch edits stopping at UAT — _apply_via_pr_chain's SIT->UAT
        # whole-branch merge conflicts whenever UAT has moved independently of SIT.
        res = _promote_targeted(
            repo, [(uat_path, _mut)], summary,
            branches=(settings.sit_branch, settings.uat_branch),
        )
    if not res["changed"]:
        if staged_removed:
            note = unstage_note + f"{', '.join(names)} was not deployed to live {env}."
            return json.dumps(
                {"ok": True, "action": "unstaged", "environment": env,
                 "removed": staged_removed, "staging_pr": staging, "note": note},
                indent=2,
            )
        return json.dumps(
            {"ok": True, "action": "no_change", "environment": env,
             "note": f"No change — {', '.join(names)} not deployed to {env}; nothing to remove."},
            indent=2,
        )
    note = (
        unstage_note
        + f"Removed {', '.join(removed)} from live {env} via PR chain. {_pr_chain_note(res['prs'])}"
        + _deploy_run_note(res)
    )
    return json.dumps(
        {"ok": True, "action": "removed", "environment": env,
         "removed": sorted(set(removed) | set(staged_removed)), "staging_pr": staging,
         "prs": res["prs"], "note": note},
        indent=2,
    )
