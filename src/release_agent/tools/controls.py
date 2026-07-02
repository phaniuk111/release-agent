"""Build-pipeline verification + RLFT/RFTL release-control tools."""

from urllib.parse import urlparse

from ._common import (
    settings,
    tool,
    BaseModel,
    Field,
    json,
    base64,
    itertools,
    _resolve_github_token,
    _get_github_client,
    active_build_repo,
    CONFIG_PATH,
)


class VerifyImageTagInput(BaseModel):
    image: str = Field(..., description="Image name (must be in image-workflows.json)")
    tag: str = Field(..., description="Git tag that was built, e.g. v1.2.3")
    repo: str = Field(
        default="", description="owner/repo where the build ran. Defaults to the target repo."
    )
    tag_generation_step: str = Field(
        default="Generate Git tag", description="Step name that generates the git tag"
    )
    tag_marker_prefix: str = Field(
        default="TAG_GENERATED=", description="Log marker prefix emitted by the tag step"
    )


def _image_build_workflow(repo_obj, image: str) -> str | None:
    """Look up an image's build workflow from image-workflows.json. Accepts either
    the 'build_workflow' or 'workflow' key (different repos use different names)."""
    try:
        cfg = json.loads(base64.b64decode(repo_obj.get_contents(CONFIG_PATH).content).decode())
        entry = cfg.get("images", {}).get(image)
        if isinstance(entry, dict):
            return entry.get("build_workflow") or entry.get("workflow")
        return None
    except Exception:
        return None


def _resolve_tag_commit(repo_obj, tag: str) -> str | None:
    """Resolve a git tag to the commit SHA it points to (handles annotated tags)."""
    try:
        ref = repo_obj.get_git_ref(f"tags/{tag}")
    except Exception:
        return None
    obj = ref.object
    if obj.type == "tag":  # annotated tag -> dereference to the underlying commit
        try:
            return repo_obj.get_git_tag(obj.sha).object.sha
        except Exception:
            return obj.sha
    return obj.sha


def _fetch_job_log(repo_full: str, job_id: int) -> str:
    """Download a job's full log text via the REST API (PyGithub has no helper for this)."""
    token = _resolve_github_token()
    if not token:
        return ""
    try:
        import requests

        r = requests.get(
            f"https://api.github.com/repos/{repo_full}/actions/jobs/{job_id}/logs",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            allow_redirects=True,
            timeout=30,
        )
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


@tool(args_schema=VerifyImageTagInput)
def verify_image_tag_build(
    image: str,
    tag: str,
    repo: str = "",
    tag_generation_step: str = "Generate Git tag",
    tag_marker_prefix: str = "TAG_GENERATED=",
) -> str:
    """
    Verify that image:tag was actually built correctly BEFORE promoting it.

    Resolves the git tag -> commit, finds the image's build-workflow run at that commit,
    confirms the tag-generation step succeeded AND the job log contains the
    '<tag_marker_prefix><tag>' marker, and reports the run's RLFT release-control steps.
    verified=true only when a matching successful run is found.
    """
    repo_full = repo or active_build_repo()
    try:
        g = _get_github_client()
        repo_obj = g.get_repo(repo_full)
    except Exception as e:
        return f"ERROR verifying build: {e}"

    workflow = _image_build_workflow(repo_obj, image)
    if not workflow:
        return f"ERROR verifying build: image '{image}' is not configured in {CONFIG_PATH} (repo {repo_full})."

    commit = _resolve_tag_commit(repo_obj, tag)
    if not commit:
        return f"ERROR verifying build: tag '{tag}' not found in {repo_full}."

    try:
        wf = repo_obj.get_workflow(workflow)
        runs = list(itertools.islice(wf.get_runs(head_sha=commit), 20))
    except Exception as e:
        return f"ERROR verifying build: {e}"

    if not runs:
        return json.dumps(
            {
                "verified": False,
                "image": image,
                "tag": tag,
                "tag_commit": commit,
                "workflow": workflow,
                "repo": repo_full,
                "reason": f"No '{workflow}' run found at commit {commit[:7]}.",
            },
            indent=2,
        )

    marker = f"{tag_marker_prefix}{tag}"

    def _inspect(run):
        tag_step, rlft = None, []
        try:
            for job in run.jobs():
                for step in getattr(job, "steps", None) or []:
                    name = getattr(step, "name", "") or ""
                    rec = {
                        "job": job.name,
                        "job_id": job.id,
                        "number": getattr(step, "number", None),
                        "name": name,
                        "status": getattr(step, "status", None),
                        "conclusion": getattr(step, "conclusion", None),
                    }
                    if name == tag_generation_step and tag_step is None:
                        tag_step = rec
                    if name.startswith("RLFT"):
                        rlft.append(
                            {k: rec[k] for k in ("job", "number", "name", "status", "conclusion")}
                        )
        except Exception:
            pass
        log_found = bool(
            tag_step
            and tag_step.get("conclusion") == "success"
            and tag_step.get("job_id")
            and marker in _fetch_job_log(repo_full, tag_step["job_id"])
        )
        return tag_step, rlft, log_found

    # Pick the newest run whose tag-gen step succeeded AND the log marker is present.
    selected = None
    for run in sorted(runs, key=lambda r: r.created_at, reverse=True):
        tag_step, rlft, log_found = _inspect(run)
        if tag_step and tag_step.get("conclusion") == "success" and log_found:
            selected = (run, tag_step, rlft, log_found)
            break
        if selected is None:
            selected = (run, tag_step, rlft, log_found)

    run, tag_step, rlft, log_found = selected
    verified = bool(tag_step and tag_step.get("conclusion") == "success" and log_found)
    return json.dumps(
        {
            "verified": verified,
            "image": image,
            "tag": tag,
            "tag_commit": commit,
            "workflow": workflow,
            "repo": repo_full,
            "run": {
                "id": run.id,
                "name": run.name,
                "url": run.html_url,
                "headSha": run.head_sha,
                "status": run.status,
                "conclusion": run.conclusion,
            },
            "tag_generation": (
                {
                    "step": tag_generation_step,
                    "job": tag_step.get("job"),
                    "status": tag_step.get("status"),
                    "conclusion": tag_step.get("conclusion"),
                    "marker": marker,
                    "log_marker_found": log_found,
                }
                if tag_step
                else {"step": tag_generation_step, "found": False, "marker": marker}
            ),
            "rlft_controls": rlft,
            "note": "verified=true means the tag was built by a successful run whose tag-gen step logged "
            "the marker. Check the RLFT control steps before promoting.",
        },
        indent=2,
    )


# ============ Build-pipeline release controls (RLFT/RFTL pass/fail) ============


class BuildControlsInput(BaseModel):
    image: str = Field(
        default="",
        description="Image name (to find the build workflow + resolve the tag). Optional if run_id is given.",
    )
    tag: str = Field(
        default="", description="Git tag that was built, e.g. v1.2.3. Optional if run_id is given."
    )
    repo: str = Field(
        default="",
        description="owner/repo where the build ran. Defaults to the configured build repo / target repo.",
    )
    run_id: int = Field(
        default=0,
        description="GitHub Actions run id that generated the tag. Pass it to skip tag->run discovery, or when discovery can't find the run.",
    )


# Step conclusions that count as a failed control gate.
_FAIL_CONCLUSIONS = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}


def _is_control_step(name: str) -> bool:
    return any(name.startswith(p) for p in settings.control_prefixes)


def _collect_controls(run) -> list[dict]:
    """Enumerate a build run's release-control steps (RLFT/RFTL...) with pass/fail."""
    controls = []
    for job in run.jobs():
        for step in getattr(job, "steps", None) or []:
            name = getattr(step, "name", "") or ""
            if not _is_control_step(name):
                continue
            concl = getattr(step, "conclusion", None)
            controls.append(
                {
                    "control": name,
                    "job": job.name,
                    "status": getattr(step, "status", None),
                    "conclusion": concl,
                    "passed": concl == "success",
                    "failed": concl in _FAIL_CONCLUSIONS,
                }
            )
    return controls


def _build_repo_full(repo: str = "") -> str:
    return repo or active_build_repo()


def _find_build_run(repo_obj, image: str, tag: str):
    """Return (newest build run for image at tag's commit, None) or (None, reason)."""
    workflow = _image_build_workflow(repo_obj, image)
    if not workflow:
        return None, f"image '{image}' has no build workflow in {CONFIG_PATH}"
    commit = _resolve_tag_commit(repo_obj, tag)
    if not commit:
        return None, f"tag '{tag}' not found"
    try:
        wf = repo_obj.get_workflow(workflow)
        # Filter by head_sha server-side (GitHub) instead of scanning 100 runs client-side.
        runs = list(itertools.islice(wf.get_runs(head_sha=commit), 20))
    except Exception as e:
        return None, str(e)
    if not runs:
        return None, f"no '{workflow}' run found at commit {commit[:7]}"
    # Multiple tags can point at the same commit, so head_sha alone is ambiguous.
    # Tag-triggered runs carry the tag name in head_branch — prefer an exact match.
    exact = [r for r in runs if (getattr(r, "head_branch", "") or "") == tag]
    return sorted(exact or runs, key=lambda r: r.created_at, reverse=True)[0], None


def _controls_report(repo_full, image, tag, run) -> dict:
    controls = _collect_controls(run)
    passed = [c["control"] for c in controls if c["passed"]]
    failed = [c["control"] for c in controls if c["failed"]]
    other = [c["control"] for c in controls if not c["passed"] and not c["failed"]]
    gate_pass = bool(controls) and not failed and not other
    return {
        "image": image,
        "tag": tag,
        "repo": repo_full,
        "run": {
            "id": run.id,
            "name": run.name,
            "url": run.html_url,
            "head_sha": run.head_sha,
            "conclusion": run.conclusion,
            "created_at": str(run.created_at),
        },
        "controls": controls,
        "summary": {"total": len(controls), "passed": passed, "failed": failed, "other": other},
        "gate": "PASS" if gate_pass else ("FAIL" if failed else "UNKNOWN"),
        "all_controls_passed": gate_pass,
    }


@tool(args_schema=BuildControlsInput)
def get_build_controls(image: str = "", tag: str = "", repo: str = "", run_id: int = 0) -> str:
    """
    Fetch the release CONTROLS (RLFT/RFTL gates) recorded in the build pipeline for
    an image:tag and report which PASSED and which FAILED — run this BEFORE a PRD
    release. Either pass run_id (the GitHub Actions run that generated the tag), OR
    pass image+tag and it locates the run from the tag's commit automatically. If it
    can't find the run from image+tag, it returns need_run_id and you must ask the
    developer for the run id that generated the tag.
    """
    repo_full = _build_repo_full(repo)
    try:
        g = _get_github_client()
        repo_obj = g.get_repo(repo_full)
    except Exception as e:
        return f"ERROR fetching controls: {e}"

    if run_id:
        try:
            run = repo_obj.get_workflow_run(int(run_id))
        except Exception:
            return (
                f"ERROR fetching controls: run id {run_id} not found in {repo_full}. "
                "Check the run id and that the repo is the build-pipeline repo."
            )
    else:
        if not (image and tag):
            return (
                "NEED_INPUT: provide a run_id, or both image and tag, so I can locate the "
                "build-pipeline run that generated the tag."
            )
        run, err = _find_build_run(repo_obj, image, tag)
        if run is None:
            return json.dumps(
                {
                    "need_run_id": True,
                    "image": image,
                    "tag": tag,
                    "repo": repo_full,
                    "reason": err,
                    "ask": (
                        f"I couldn't locate the build run for {image}:{tag} in {repo_full} ({err}). "
                        "Please provide the GitHub Actions run id that generated this tag."
                    ),
                },
                indent=2,
            )

    report = _controls_report(repo_full, image, tag, run)
    if not report["controls"]:
        report["note"] = (
            f"No control steps matched prefixes {settings.control_prefixes} in this run — "
            "verify the run id / build pipeline."
        )
    else:
        report["note"] = (
            "gate=PASS only when every control passed. Do NOT promote to PRD with any FAILED control."
        )
    return json.dumps(report, indent=2)


# ============ Build report: step failures + built-from-main (live, PyGithub) ============


def _parse_run_url(url: str):
    """Extract (owner/repo, run_id) from a GitHub Actions run URL, e.g.
    https://github.com/<owner>/<repo>/actions/runs/<run_id>[/job/<id>|/attempts/<n>].
    Returns (repo_full_or_None, run_id_or_None). No regex — plain path split."""
    try:
        parts = [p for p in urlparse(url).path.split("/") if p]
        if "runs" in parts:
            i = parts.index("runs")
            run_id = int(parts[i + 1])
            repo_full = f"{parts[0]}/{parts[1]}" if i >= 2 else ""
            return (repo_full or None, run_id)
    except Exception:
        pass
    return (None, None)


def _check_built_from_main(repo_obj, commit: str) -> dict:
    """Is `commit` reachable from the repo's default branch (i.e. built from main)?
    Uses the compare API: comparing default_branch -> commit, status 'identical'/'behind'
    means the commit is an ancestor of the default branch (built from main); 'ahead'/
    'diverged' means it carries commits not on the default branch (not built from main)."""
    default = repo_obj.default_branch
    try:
        status = repo_obj.compare(default, commit).status  # identical|behind|ahead|diverged
        return {"result": status in ("identical", "behind"), "default_branch": default, "status": status}
    except Exception as e:
        return {"result": None, "default_branch": default, "reason": str(e)}


def _failed_steps(run) -> list[dict]:
    """Failed NON-control steps across the run's jobs. Control steps (RLFT/RFTL...) are
    excluded here because they're reported separately in the `controls` array — this keeps
    a failed control from showing up twice in the rendered table."""
    out: list[dict] = []
    try:
        for job in run.jobs():
            for step in getattr(job, "steps", None) or []:
                name = getattr(step, "name", "") or ""
                concl = getattr(step, "conclusion", None)
                if concl in _FAIL_CONCLUSIONS and not _is_control_step(name):
                    out.append(
                        {
                            "job": job.name,
                            "name": name,
                            "number": getattr(step, "number", None),
                            "conclusion": concl,
                        }
                    )
    except Exception:
        pass
    return out


class BuildReportInput(BaseModel):
    image: str = Field(
        default="", description="Image name (resolves the tag's build run). Provide image+tag, OR workflow_url."
    )
    tag: str = Field(
        default="", description="Git tag that was built, e.g. v1.2.3. Provide image+tag, OR workflow_url."
    )
    workflow_url: str = Field(
        default="",
        description="A GitHub Actions run URL (…/actions/runs/<id>) to inspect directly, instead of image+tag.",
    )
    repo: str = Field(
        default="",
        description="owner/repo where the build ran. Defaults to the build repo; auto-derived from workflow_url.",
    )


@tool(args_schema=BuildReportInput)
def get_build_report(image: str = "", tag: str = "", workflow_url: str = "", repo: str = "") -> str:
    """Report a build's outcome for an image:tag (or a GitHub Actions run URL): which STEPS failed,
    which RLFT/RFTL controls passed/failed, and whether the tag was built from the build repo's
    main/default branch. Read-only, resolved live from GitHub (tag -> commit -> run -> steps).

    PRESENT THE RESULT TO THE USER AS A MARKDOWN TABLE — a summary line with the clickable run URL
    + conclusion + built-from-main verdict, then a table of Step/Control | Job | Result rows with
    ✅/❌ markers. Do NOT show the raw JSON to the user."""
    repo_full = _build_repo_full(repo)

    # 1) Resolve the run — directly from a URL, or by image+tag -> commit -> run.
    if workflow_url.strip():
        url_repo, run_id = _parse_run_url(workflow_url)
        repo_full = repo or url_repo or repo_full
        if not run_id:
            return json.dumps(
                {"found": False, "reason": f"could not parse a run id from '{workflow_url}'."}, indent=2
            )
        try:
            repo_obj = _get_github_client().get_repo(repo_full)
            run = repo_obj.get_workflow_run(int(run_id))
        except Exception as e:
            return json.dumps(
                {"found": False, "repo": repo_full, "reason": f"run {run_id} not found in {repo_full}: {e}"},
                indent=2,
            )
    else:
        if not (image and tag):
            return json.dumps(
                {"found": False, "reason": "provide a workflow_url, or both image and tag."}, indent=2
            )
        try:
            repo_obj = _get_github_client().get_repo(repo_full)
        except Exception as e:
            return f"ERROR building report: {e}"
        run, err = _find_build_run(repo_obj, image, tag)
        if run is None:
            return json.dumps(
                {
                    "found": False,
                    "image": image,
                    "tag": tag,
                    "repo": repo_full,
                    "reason": err,
                    "hint": "If you have the build's workflow run URL, pass it as workflow_url.",
                },
                indent=2,
            )

    # 2) Assemble the report from permanent run/step metadata (no log downloads).
    commit = getattr(run, "head_sha", None)
    controls = _collect_controls(run)
    failed_controls = [c["control"] for c in controls if c["failed"]]
    other = [c["control"] for c in controls if not c["passed"] and not c["failed"]]
    gate = "PASS" if (controls and not failed_controls and not other) else ("FAIL" if failed_controls else "UNKNOWN")
    failed_steps = _failed_steps(run)
    run_succeeded = run.conclusion == "success"

    if failed_steps or failed_controls:
        note = (
            "Render for the user as a markdown table — a summary line (run URL, conclusion, "
            "built-from-main) then Step/Control | Job | Result rows with ✅/❌. Do not show JSON."
        )
    elif run_succeeded:
        note = (
            "The build SUCCEEDED — no failed steps. Give a one-line success summary with the run "
            "link and the built-from-main verdict; a table isn't needed. Do not show JSON."
        )
    else:
        note = (
            f"The run's overall conclusion is '{run.conclusion}' but NO individual step or control "
            "failure was recorded (e.g. a workflow-level / startup / config failure, or a job that "
            "never ran). Tell the user the run failed at the workflow level with no per-step detail, "
            "and link the run so they can inspect it. Do not show JSON."
        )

    report = {
        "found": True,
        "image": image or None,
        "tag": tag or getattr(run, "head_branch", None),
        "repo": repo_full,
        "commit": commit,
        "run": {
            "id": run.id,
            "url": run.html_url,
            "name": run.name,
            "status": run.status,
            "conclusion": run.conclusion,
        },
        "run_succeeded": run_succeeded,
        "failed_steps": failed_steps,
        "controls": controls,
        "gate": gate,
        "built_from_main": (
            _check_built_from_main(repo_obj, commit) if commit else {"result": None, "reason": "no commit on run"}
        ),
        "note": note,
    }
    return json.dumps(report, indent=2)

