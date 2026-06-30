"""SIT->UAT->PRD promotion of Helm-chart entries via the protected-branch PR chain.

The deploy repo carries an env-pathed deployment JSON per environment
(uat/deployment.json, prd/deployment.json), each shaped {"include": [entry, ...]}
where an entry is a Helm chart:
    {helm_chart_name, helm_chart_version, helm_chart_dir, helm_values_file_name, gke_namespace}

The dev supplies only chart_name:version (+ optional namespace); the constants and the
env-specific values-file + namespace are filled from config.

- UAT deploy  : upsert the chart into uat/deployment.json (chain working->SIT->UAT).
- PROD deploy : upsert the chart into BOTH uat/deployment.json and prd/deployment.json
               (chain working->SIT->UAT->PRD), each with its env-specific values file
               + namespace.
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
    """Replace-or-append by helm_chart_name. Returns True if the list changed."""
    name = entry["helm_chart_name"]
    for i, e in enumerate(include):
        if e.get("helm_chart_name") == name:
            if e == entry:
                return False
            include[i] = entry
            return True
    include.append(entry)
    return True


def _remove_entry(include: list, name: str) -> bool:
    """Drop the entry with this helm_chart_name. Returns True if one was removed."""
    for i, e in enumerate(include):
        if e.get("helm_chart_name") == name:
            del include[i]
            return True
    return False


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


def _apply_via_pr_chain(repo, file_mutations: list, summary: str, to_prd: bool = False) -> dict:
    """Apply per-file mutations along the protected-branch chain (never commit directly).

    file_mutations is a list of (path, mutate_fn); mutate_fn(include_list) mutates the
    include[] list in place and returns True if it changed anything. The chain runs
    working -> SIT -> UAT, and on to -> PRD when to_prd. Returns
    {changed, prs, deploy_run, blocked_pr?}."""
    sit, uat, prd = settings.sit_branch, settings.uat_branch, settings.prd_branch
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

            # 3) UAT -> PRD (prod deploys only)
            if to_prd and ok2:
                pr_prd = repo.create_pull(
                    title=f"Promote {uat} → {prd}: {summary}", body=summary, head=uat, base=prd
                )
                ok3, detail3 = _merge_pr(pr_prd, "merge")
                pentry = {
                    "stage": f"{uat}→{prd}",
                    "number": pr_prd.number,
                    "url": pr_prd.html_url,
                    "merged": ok3,
                    "detail": detail3,
                }
                if ok3:
                    try:
                        pr_prd.update()
                        msha = pr_prd.merge_commit_sha
                        run = _find_deploy_run(repo, msha, prd) if msha else None
                        if run:
                            pentry["deploy_run_prd"] = run
                    except Exception:
                        pass
                prs.append(pentry)
        except Exception as e:
            prs.append({"stage": f"{sit}→{uat}", "error": str(e)})

    deploy_run = next((p.get("deploy_run") for p in prs if p.get("deploy_run")), None)
    deploy_run_prd = next((p.get("deploy_run_prd") for p in prs if p.get("deploy_run_prd")), None)
    return {"changed": True, "prs": prs, "deploy_run": deploy_run, "deploy_run_prd": deploy_run_prd}


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


@tool(args_schema=DeployInput)
def open_release_pr(
    environment: str,
    image_tags: str = "",
    deployment_json: str = "",
    namespace: str = "",
    chart_dir: str = "",
    values_file: str = "",
) -> str:
    """Deploy Helm chart(s) by OVERRIDING the deployment JSON (complete replace, no
    upsert) via the protected-branch PR chain. The submitted include[] becomes the
    entire file.

      uat : overrides uat/deployment.json (working->SIT->UAT).
      prod: overrides BOTH prd/deployment.json and uat/deployment.json
            (working->SIT->UAT->PRD), each with its env-specific values file.

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
        repo = _get_github_client().get_repo(settings.deploy_repo)
    except Exception as e:
        return f"ERROR deploying: {e}"

    plan = plan_deploy(env, entries)
    file_mutations = [(path, _replace_with(ents)) for path, ents in plan.items()]
    res = _apply_via_pr_chain(repo, file_mutations, f"Deploy {chart_str} to {env}", to_prd=(env == "prod"))
    blocked = _blocked_pr_result(res)
    if blocked:
        return blocked
    if not res["changed"]:
        return json.dumps(
            {"ok": True, "action": "no_change", "environment": env, "image_tags": chart_str,
             "note": f"No change — {env} deployment.json already matches the submitted charts."},
            indent=2,
        )

    uat_now = _read_include(repo, settings.uat_branch, _deployment_path("uat"))
    written = list(plan.keys())
    note = (
        f"Deployed {chart_str} to {env} via PR chain (override). {_pr_chain_note(res['prs'])} "
        f"Replaced {', '.join(written)}. {len(uat_now)} chart(s) on UAT." + _deploy_run_note(res)
    )
    return json.dumps(
        {
            "ok": True,
            "environment": env,
            "action": "deployed",
            "image_tags": chart_str,
            "files_updated": written,
            "uat_charts": uat_now,
            "prs": res["prs"],
            "deploy_run": res.get("deploy_run"),
            "deploy_run_prd": res.get("deploy_run_prd"),
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
        default="uat", description="Which env to remove from: uat (default) or prod (both files)."
    )


@tool(args_schema=RemoveFromReleaseInput)
def remove_from_release(image_names: str, environment: str = "uat") -> str:
    """Remove chart(s) from the deployment JSON by helm_chart_name, via the protected-
    branch PR chain. environment=uat drops them from uat/deployment.json (unstage);
    environment=prod drops them from BOTH uat and prd/deployment.json."""
    names = []
    for tok in image_names.replace(",", " ").split():
        tok = tok.strip()
        if tok:
            names.append(tok.split(":", 1)[0])
    if not names:
        return "ERROR: no chart names provided to remove."

    raw = (environment or "uat").strip().lower()
    env = "prod" if raw in ("prod", "prd", "production") else "uat"

    try:
        repo = _get_github_client().get_repo(settings.deploy_repo)
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
    if env == "prod":
        file_mutations = [(uat_path, _mut), (_deployment_path("prd"), _mut)]
        to_prd = True
    else:
        file_mutations = [(uat_path, _mut)]
        to_prd = False

    res = _apply_via_pr_chain(repo, file_mutations, "Remove " + ",".join(names) + f" from {env}", to_prd=to_prd)
    blocked = _blocked_pr_result(res)
    if blocked:
        return blocked
    if not res["changed"]:
        return json.dumps(
            {"ok": True, "action": "no_change", "environment": env,
             "note": f"No change — {', '.join(names)} not deployed to {env}; nothing to remove."},
            indent=2,
        )
    note = (
        f"Removed {', '.join(removed)} from {env} via PR chain. {_pr_chain_note(res['prs'])}"
        + _deploy_run_note(res)
    )
    return json.dumps(
        {"ok": True, "action": "removed", "environment": env, "removed": removed,
         "prs": res["prs"], "note": note},
        indent=2,
    )
