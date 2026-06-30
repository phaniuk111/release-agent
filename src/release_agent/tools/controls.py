"""Build-pipeline verification + RLFT/RFTL release-control tools."""

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
    repo_full = repo or settings.build_repo
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
        runs = [r for r in itertools.islice(wf.get_runs(), 100) if r.head_sha == commit]
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
    return repo or settings.build_repo


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
        runs = [r for r in itertools.islice(wf.get_runs(), 100) if r.head_sha == commit]
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


# ============ Environment promotion: update config JSON + open a PR (PyGithub) ============


