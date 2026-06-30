"""SIT->UAT->PRD promotion: stage/remove/raise via the protected-branch PR chain."""

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
from .release_window import get_release_status  # noqa: F401
from .controls import _find_build_run, _controls_report, _build_repo_full  # noqa: F401


class OpenReleasePRInput(BaseModel):
    environment: str = Field(..., description="Target environment: uat or prod")
    image_tags: str = Field(..., description="Comma-separated image:tag pairs (supports multiple)")
    change_request_json: str = Field(
        default="",
        description="JSON object of the change_request block (required for prod) — drives the auto-created CHG.",
    )


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
    Lets a promote say 'a PR is already open' instead of stacking a duplicate — the
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


def _find_deploy_run(repo, head_sha: str, tries: int = 4, delay: float = 1.5):
    """Find the workflow run that the UAT merge triggered, by the merge commit sha.
    Polls briefly because GitHub Actions takes a few seconds to register the run."""
    import time

    uat = settings.uat_branch
    for i in range(tries):
        runs = []
        try:
            runs = list(itertools.islice(repo.get_workflow_runs(head_sha=head_sha), 5))
        except TypeError:
            # older PyGithub without the head_sha kwarg → filter recent UAT runs
            try:
                runs = [
                    r for r in itertools.islice(repo.get_workflow_runs(branch=uat), 15)
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


def _apply_via_pr_chain(repo, mutate_fn, summary: str) -> dict:
    """Branches are protected — never commit to them directly. Apply a change to the
    images config via PRs along the chain: a fresh working branch -> SIT -> UAT.
    Each step is a PR (merged when protection allows). mutate_fn(images: dict) -> bool
    mutates the images map in place and returns True if it changed anything."""
    sit, uat = settings.sit_branch, settings.uat_branch
    images_path = settings.env_config_path
    prs: list = []

    # Guard: if a promote PR touching this file is already open, don't open another —
    # report it so the caller can tell the user to wait for it to merge.
    existing = _open_pr_on_file(repo, sit, images_path)
    if existing is not None:
        return {
            "changed": False,
            "prs": [],
            "blocked_pr": {"number": existing.number, "url": existing.html_url},
        }

    sit_ref = repo.get_git_ref(f"heads/{sit}")
    work = f"change/sit/{uuid.uuid4().hex[:8]}"
    repo.create_git_ref(f"refs/heads/{work}", sit_ref.object.sha)
    cfg = _read_json_file(repo, work, images_path)
    if not isinstance(cfg, dict):
        cfg = {}
    cfg.setdefault("images", {})
    changed = mutate_fn(cfg["images"])
    if not changed:
        try:
            repo.get_git_ref(f"heads/{work}").delete()
        except Exception:
            pass
        return {"changed": False, "prs": []}
    cfg["updated_by"] = "release-copilot"
    _upsert_json_file(repo, work, images_path, cfg)

    # 1) working branch -> SIT
    pr_sit = repo.create_pull(title=f"{summary} (→ {sit})", body=summary, head=work, base=sit)
    ok, detail = _merge_pr(pr_sit, "squash")
    prs.append(
        {
            "stage": f"→{sit}",
            "number": pr_sit.number,
            "url": pr_sit.html_url,
            "merged": ok,
            "detail": detail,
        }
    )
    # 2) SIT -> UAT (promote) — only if SIT actually advanced
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
            # On a successful UAT merge, capture the deploy workflow run it triggered.
            if ok2:
                try:
                    pr_uat.update()
                    msha = pr_uat.merge_commit_sha
                    run = _find_deploy_run(repo, msha) if msha else None
                    if run:
                        entry["deploy_run"] = run
                except Exception:
                    pass
            prs.append(entry)
        except Exception as e:
            prs.append({"stage": f"{sit}→{uat}", "error": str(e)})
    deploy_run = next((p.get("deploy_run") for p in prs if p.get("deploy_run")), None)
    return {"changed": True, "prs": prs, "deploy_run": deploy_run}


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


def _deploy_run_note(dr) -> str:
    """One-line note pointing at the UAT deploy workflow run, if captured."""
    if not dr:
        return ""
    return f" UAT deploy run #{dr['id']} ({dr['url']})."


def _blocked_pr_result(res: dict):
    """If the PR chain was blocked because a promote PR is already open on the file,
    return a user-facing JSON string; otherwise None."""
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
                f"A promote PR is already open — #{b['number']} ({b['url']}) — touching the images "
                "config. Merge or close it first, then retry. (One promote at a time per file.)"
            ),
        },
        indent=2,
    )


def _add_images_to_uat(repo, pairs: list) -> dict:
    """Stage image:tag pairs for the day's release via the protected-branch PR chain
    (working -> SIT -> UAT). Returns {uat_images, prs, changed}."""
    image_map = {i: t for i, t in pairs}

    def _mut(images):
        before = dict(images)
        images.update(image_map)
        return images != before

    summary = "Add " + ",".join(f"{i}:{t}" for i, t in pairs) + " to release"
    res = _apply_via_pr_chain(repo, _mut, summary)
    uat_images = (_read_json_file(repo, settings.uat_branch, settings.env_config_path) or {}).get(
        "images", {}
    ) or {}
    return {"uat_images": uat_images, "prs": res["prs"], "changed": res["changed"],
            "blocked_pr": res.get("blocked_pr"), "deploy_run": res.get("deploy_run")}


def _remove_images_via_chain(repo, names: list) -> dict:
    """Remove image(s) from the release via the protected-branch PR chain
    (working -> SIT, then promote SIT -> UAT), merging both so the removal reaches
    UAT: revert each image to PRD's current tag, or drop it if it's new."""
    prd_images = (_read_json_file(repo, settings.prd_branch, settings.env_config_path) or {}).get(
        "images", {}
    ) or {}
    reverted, removed, not_found = [], [], []

    def _mut(images):
        for n in names:
            if n not in images:
                not_found.append(n)
            elif n in prd_images:
                images[n] = prd_images[n]
                reverted.append(f"{n}→{prd_images[n]} (PRD)")
            else:
                del images[n]
                removed.append(n)
        return bool(reverted or removed)

    res = _apply_via_pr_chain(repo, _mut, "Remove " + ",".join(names) + " from release")
    return {
        "reverted": reverted,
        "removed": removed,
        "not_found": not_found,
        "prs": res["prs"],
        "changed": res["changed"],
        "blocked_pr": res.get("blocked_pr"),
    }


class RemoveFromReleaseInput(BaseModel):
    image_names: str = Field(
        ...,
        description="Comma-separated image names to remove from today's release. Tags are "
        "optional/ignored (e.g. 'orders-api' or 'orders-api:v1.1.0').",
    )


@tool(args_schema=RemoveFromReleaseInput)
def remove_from_release(image_names: str) -> str:
    """Remove image(s) from the release via the protected-branch PR chain: open a PR
    from a working branch into SIT that drops the image, then a PR promoting SIT->UAT,
    merging both so the removal reaches UAT (it flows on to PRD with the release).
    Each image is reverted to PRD's current tag, or dropped if it's new. Branches are
    never edited directly."""
    names = []
    for tok in image_names.replace(",", " ").split():
        tok = tok.strip()
        if tok:
            names.append(tok.split(":", 1)[0])
    if not names:
        return "ERROR: no image names provided to remove."
    try:
        repo = _get_github_client().get_repo(settings.deploy_repo)
    except Exception as e:
        return f"ERROR removing from release: {e}"

    res = _remove_images_via_chain(repo, names)
    blocked = _blocked_pr_result(res)
    if blocked:
        return blocked
    if not res["changed"]:
        return (
            f"No change — {', '.join(res['not_found']) or ','.join(names)} not found in the release "
            "config; nothing to remove."
        )
    parts = []
    if res["reverted"]:
        parts.append("reverted to PRD: " + ", ".join(res["reverted"]))
    if res["removed"]:
        parts.append("dropped (was new): " + ", ".join(res["removed"]))
    if res["not_found"]:
        parts.append("not in release: " + ", ".join(res["not_found"]))
    note = "Removed from the release — " + "; ".join(parts) + ". Via " + _pr_chain_note(res["prs"])
    return json.dumps({"ok": True, "action": "removed", **res, "note": note}, indent=2)


def _lead_time_ok(cr: dict):
    """Production changes need lead time: the change request's start_date must be at
    least `prd_lead_time_days` ahead (default 1 -> tomorrow or later).
    Returns (ok: bool, message: str)."""
    from datetime import datetime, timezone, timedelta

    raw = str(cr.get("start_date") or "").strip()
    lead = settings.prd_lead_time_days
    if not raw:
        return False, (
            "the change request needs a start_date — production releases need lead time, so "
            f"the start date must be at least {lead} day(s) out."
        )
    dt = None
    for candidate in (raw, raw[:10]):  # accept 'YYYY-MM-DDThh:mm' or 'YYYY-MM-DD'
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except Exception:
            continue
    if dt is None:
        return False, f"could not parse start_date '{raw}' (use YYYY-MM-DD or YYYY-MM-DDThh:mm)."
    earliest = datetime.now(timezone.utc).date() + timedelta(days=lead)
    if dt.date() < earliest:
        return False, (
            f"start_date {dt.date().isoformat()} is too soon — production changes need "
            f"{lead} day(s) lead time, so the start date must be {earliest.isoformat()} "
            "(tomorrow) or later."
        )
    return True, ""


def _raise_uat_to_prd_pr(repo, cr: dict) -> str:
    """Post-cutoff: raise the single UAT->PRD release PR with everything accumulated
    on UAT, auto-creating the CHG/RMG. Raising it locks the day. The change's
    start_date must satisfy the production lead time."""
    uat, prd = settings.uat_branch, settings.prd_branch
    images_path, cr_path = settings.env_config_path, settings.change_request_path
    try:
        uat_cfg = _read_json_file(repo, uat, images_path)
        uat_images = uat_cfg.get("images", {}) if isinstance(uat_cfg, dict) else {}
        prd_cfg = _read_json_file(repo, prd, images_path)
        prd_images = prd_cfg.get("images", {}) if isinstance(prd_cfg, dict) else {}
        # Only the images that actually differ from PRD get promoted. If nothing
        # changed (a quiet day), do NOT raise an empty release PR.
        pending = {i: t for i, t in uat_images.items() if prd_images.get(i) != t}
        if not pending:
            return (
                "NOTE: nothing to release — UAT already matches PRD (no new images staged). "
                "No UAT→PRD PR was raised."
            )
        ok, msg = _lead_time_ok(cr)
        if not ok:
            return f"ERROR raising release: {msg}"
        image_str = ",".join(f"{i}:{t}" for i, t in pending.items())
        try:
            source_ref = repo.get_git_ref(f"heads/{uat}")
        except Exception:
            return f"ERROR raising release: UAT branch '{uat}' not found in {settings.deploy_repo}."

        branch = f"release/prod/{uuid.uuid4().hex[:8]}"
        repo.create_git_ref(f"refs/heads/{branch}", source_ref.object.sha)
        cr_doc = {
            "environment": "prod",
            "images": pending,
            "promoting_to_state": uat_images,
            "change_request": cr,
            "status": "pending-chg",
        }
        _upsert_json_file(repo, branch, cr_path, cr_doc)

        title = f"Release UAT → PRD: {image_str}"
        body = (
            f"Daily production release — promote **{uat}** → **{prd}**.\n\n"
            f"- Images: `{image_str}`\n- Change request: `{cr_path}`\n\n"
            "CHG/RMG auto-created from the change request (see comments)."
        )
        pr = repo.create_pull(title=title, body=body, head=branch, base=prd)

        from datetime import datetime, timezone

        ym = datetime.now(timezone.utc).strftime("%Y%m")
        seq = f"{uuid.uuid4().int % 100000:05d}"
        chg, rmg = f"CHG-{ym}-{seq}", f"RMG-{ym}-{seq}"
        sd = cr.get("short_description") or cr.get("summary") or image_str
        window = ""
        if cr.get("start_date") or cr.get("end_date"):
            window = f"\n- **Window:** {cr.get('start_date', '?')} → {cr.get('end_date', '?')}"
        pr.create_issue_comment(
            "\n".join(
                [
                    "📋 **Change management & release controls** (auto-created from the change request)",
                    "",
                    f"- **CHG:** {chg}",
                    f"- **RMG:** {rmg}",
                    f"- **Summary:** {sd}",
                    f"- **Images:** {image_str}{window}",
                    "",
                    "**Control gates (RLFT):**",
                    "- RLFT approval gate: open",
                    "- RLFT deploy control: open",
                ]
            )
        )
        return json.dumps(
            {
                "ok": True,
                "environment": "prod",
                "action": "raised_uat_to_prd",
                "image_tags": image_str,
                "branch": branch,
                "base_branch": prd,
                "source_branch": uat,
                "pr_number": pr.number,
                "pr_url": pr.html_url,
                "change_request_template": cr_path,
                "chg": chg,
                "rmg": rmg,
                "note": (
                    f"Daily UAT→PRD release PR #{pr.number} raised promoting {len(pending)} image(s); "
                    f"CHG {chg} / RMG {rmg} created. The day is now locked."
                ),
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR raising release: {e}"


def _prod_controls_failures(pairs: list) -> list:
    """Return human-readable failure strings for any requested image whose build
    controls FAILED (empty list = none failed or unverifiable)."""
    if not settings.prd_require_controls:
        return []
    try:
        brepo = _get_github_client().get_repo(_build_repo_full())
    except Exception:
        return []
    failures = []
    for image, tag in pairs:
        run, _err = _find_build_run(brepo, image, tag)
        if run is None:
            continue  # unverifiable here; the chat flow asks for the run id
        rep = _controls_report(_build_repo_full(), image, tag, run)
        if rep["summary"]["failed"]:
            failures.append(
                f"{image}:{tag} → FAILED controls: {', '.join(rep['summary']['failed'])} "
                f"(build run {rep['run']['url']})"
            )
    return failures


@tool(args_schema=OpenReleasePRInput)
def open_release_pr(environment: str, image_tags: str, change_request_json: str = "") -> str:
    """
    SIT -> UAT -> PRD promotion in the deploy repo.
      - uat : stage image:tag(s) onto the UAT branch (the day's release accumulates).
      - prod: BEFORE the daily cutoff this also just stages onto UAT (the UAT->PRD PR
        is NOT raised yet, so more images can keep being added). AFTER the cutoff it
        stages onto UAT and raises the single UAT->PRD release PR (change_request
        required) which auto-creates the CHG/RMG and LOCKS the day.
    Supports multiple image:tag pairs.
    """
    raw = (environment or "").strip().lower()
    if raw == "uat":
        env = "uat"
    elif raw in ("prod", "prd", "production"):
        env = "prod"
    else:
        return (
            f"ERROR opening release PR: unsupported environment '{environment}' (use uat or prod)."
        )

    try:
        pairs = _parse_pairs(image_tags)
    except ValueError as e:
        return f"ERROR opening release PR: {e}"
    if not pairs:
        return "ERROR opening release PR: no image:tag pairs provided."
    image_str = ",".join(f"{i}:{t}" for i, t in pairs)

    cr: dict = {}
    if change_request_json.strip():
        try:
            cr = json.loads(change_request_json)
        except Exception:
            return "ERROR opening release PR: change_request_json is not valid JSON."

    g = _get_github_client()
    try:
        repo = g.get_repo(settings.deploy_repo)
    except Exception as e:
        return f"ERROR opening release PR: {e}"

    # --- UAT: stage onto UAT via the protected-branch PR chain (working->SIT->UAT) ---
    if env == "uat":
        res = _add_images_to_uat(repo, pairs)
        blocked = _blocked_pr_result(res)
        if blocked:
            return blocked
        imgs = res["uat_images"]
        return json.dumps(
            {
                "ok": True,
                "environment": "uat",
                "action": "staged_to_uat",
                "image_tags": image_str,
                "uat_images": imgs,
                "prs": res["prs"],
                "deploy_run": res.get("deploy_run"),
                "note": f"Staged {image_str} via PR chain (working→SIT→UAT). {_pr_chain_note(res['prs'])} "
                f"{len(imgs)} image(s) on UAT." + _deploy_run_note(res.get("deploy_run")),
            },
            indent=2,
        )

    # --- PROD path ---
    status = get_release_status()
    if status.get("locked"):
        p = status.get("prd_pr_today") or {}
        return (
            f"ERROR: today's UAT→PRD release PR #{p.get('number')} is already raised — the day is "
            f"locked, no more images can be added. {p.get('url', '')}"
        )

    # Build-control gate (fail-closed) for the requested images.
    failures = _prod_controls_failures(pairs)
    if failures:
        return "ERROR: build controls failed — cannot stage for PRD release.\n" + "\n".join(
            failures
        )

    # Stage the requested images for today's release via the PR chain (working->SIT->UAT).
    res = _add_images_to_uat(repo, pairs)
    blocked = _blocked_pr_result(res)
    if blocked:
        return blocked
    imgs = res["uat_images"]

    if not status.get("cutoff_passed"):
        cutoff = settings.prd_cutoff_hour_utc
        return json.dumps(
            {
                "ok": True,
                "environment": "prod",
                "action": "staged_to_uat",
                "image_tags": image_str,
                "uat_images": imgs,
                "prs": res["prs"],
                "deploy_run": res.get("deploy_run"),
                "note": (
                    f"Staged {image_str} for today's release via PR chain (working→SIT→UAT). "
                    f"{_pr_chain_note(res['prs'])} {len(imgs)} image(s) on UAT." + _deploy_run_note(res.get("deploy_run"))
                    + f" The single UAT→PRD PR is raised after {cutoff:02d}:00 UTC — until then more images can be added."
                ),
            },
            indent=2,
        )

    # Cutoff passed → raise the day's UAT→PRD release PR.
    if not cr:
        return (
            "ERROR: the cutoff has passed — raising the UAT→PRD release requires a change_request "
            "block (drives the CHG)."
        )
    return _raise_uat_to_prd_pr(repo, cr)


class RaiseReleaseInput(BaseModel):
    change_request_json: str = Field(
        default="", description="change_request block (required) the CHG is created from."
    )


@tool(args_schema=RaiseReleaseInput)
def raise_prod_release(change_request_json: str = "") -> str:
    """Raise today's single UAT->PRD production release PR with everything currently
    staged on UAT. Allowed ONLY after the daily cutoff (raising it locks the day).
    Requires a change_request block to drive the CHG/RMG."""
    status = get_release_status()
    if status.get("locked"):
        p = status.get("prd_pr_today") or {}
        return f"ERROR: today's release PR #{p.get('number')} is already raised — locked. {p.get('url', '')}"
    if not status.get("cutoff_passed"):
        return (
            f"ERROR: the UAT→PRD release can only be raised after {status.get('cutoff_utc')} UTC. "
            "Until then images keep accumulating on UAT."
        )
    cr: dict = {}
    if change_request_json.strip():
        try:
            cr = json.loads(change_request_json)
        except Exception:
            return "ERROR: change_request_json is not valid JSON."
    if not cr:
        return "ERROR: raising the release requires a change_request block (drives the CHG)."
    try:
        repo = _get_github_client().get_repo(settings.deploy_repo)
    except Exception as e:
        return f"ERROR raising release: {e}"
    return _raise_uat_to_prd_pr(repo, cr)


# Export all tools for the agent
