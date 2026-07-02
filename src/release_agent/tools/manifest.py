"""Manifest / image-catalog / workflow-dispatch tools (build repo)."""

from typing import Any

from ._common import (
    tool,
    BaseModel,
    Field,
    json,
    base64,
    itertools,
    GithubException,
    _get_github_client,
    _parse_pairs,
    active_build_repo,
    CONFIG_PATH,
    MANIFEST_PATH,
    ALLOWED_WORKFLOWS,
)


class ImageTagsInput(BaseModel):
    image_tags: str = Field(
        ...,
        description="Comma-separated image:tag pairs, e.g. 'payments-api:2.0.33,orders-api:v1.2.3'",
    )


class ApplyJsonUpdateInput(BaseModel):
    image_tags: str = Field(..., description="Comma-separated image:tag pairs")
    commit_message: str = Field(
        default="chore(release): update image tags via release-agent chat",
        description="Commit message for the update",
    )


class DispatchWorkflowInput(BaseModel):
    workflow: str = Field(
        default="image-tag-step-report.yml", description="Workflow filename to dispatch"
    )
    image_tags: str = Field(default="", description="Comma-separated image:tag pairs to pass")
    extra_inputs: str = Field(
        default="", description="Optional JSON string with additional workflow inputs"
    )


class GetRecentRunsInput(BaseModel):
    limit: int = Field(default=5, ge=1, le=50, description="Max number of runs to return")


class GetWorkflowStatusInput(BaseModel):
    run_id: str = Field(..., description="Workflow run ID (databaseId)")


@tool
def list_allowed_images() -> str:
    """Return the list of known images and their build workflows from the config JSON."""
    try:
        g = _get_github_client()
        repo = g.get_repo(active_build_repo())
        content_file = repo.get_contents(CONFIG_PATH)
        content = base64.b64decode(content_file.content).decode()
        cfg = json.loads(content)
        images = list(cfg.get("images", {}).keys())
        return json.dumps({"allowed_images": images, "config": cfg}, indent=2)
    except Exception as e:
        return f"ERROR listing images: {e}"


def _fetch_current_manifest() -> str:
    """Plain helper so other tools can reuse manifest reading without invoking a
    tool wrapper."""
    path = MANIFEST_PATH
    try:
        g = _get_github_client()
        repo = g.get_repo(active_build_repo())
        content_file = repo.get_contents(path)
        content = base64.b64decode(content_file.content).decode()
        return content
    except Exception as e:
        # 404 means file doesn't exist yet
        if "404" in str(e) or "Not Found" in str(e):
            skeleton = {
                "last_updated": None,
                "requested_by": "chat-agent",
                "images": {},
                "promote_to": "prod",
                "status": "empty",
            }
            return json.dumps(skeleton, indent=2)
        return f"ERROR reading manifest: {e}"


@tool
def get_current_manifest() -> str:
    """Fetch the current release-manifest.json (creates a skeleton if missing)."""
    return _fetch_current_manifest()


@tool(args_schema=ImageTagsInput)
def propose_update(image_tags: str) -> str:
    """
    Propose changes for a comma-separated list of image:tag.
    Does NOT mutate anything. Returns a proposed manifest diff.
    Example input: "payments-api:2.0.33,orders-api:v1.2.3"
    """
    try:
        pairs = []
        for part in image_tags.split(","):
            part = part.strip()
            if not part or ":" not in part:
                return f"ERROR: bad pair '{part}'. Use image:tag format."
            name, tag = part.split(":", 1)
            pairs.append((name.strip(), tag.strip()))

        current_raw = _fetch_current_manifest()
        if current_raw.startswith("ERROR"):
            return current_raw
        current = json.loads(current_raw)
        proposed = current.copy()
        proposed["images"] = current.get("images", {}).copy()
        changes = []
        for name, tag in pairs:
            old = proposed["images"].get(name)
            proposed["images"][name] = tag
            changes.append({"image": name, "old": old, "new": tag})

        proposed["last_updated"] = "proposed"
        proposed["status"] = "proposed"

        return json.dumps(
            {
                "current": current,
                "proposed": proposed,
                "changes": changes,
                "note": "This is a proposal only. Reply with the confirmation token to apply.",
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR proposing update: {e}"


@tool(args_schema=ApplyJsonUpdateInput)
def apply_json_update(
    image_tags: str,
    commit_message: str = "chore(release): update image tags via release-agent chat",
) -> str:
    """
    Apply image:tag updates to the release manifest in the GitHub repo.
    This MUTATES the repository. Only call after user confirmation.

    Idempotent + conflict-safe: on a 409 SHA conflict (e.g. a concurrent edit or
    an HTTP retry that already landed the write) it re-reads the file; if the
    desired tags are already present it reports success, otherwise it retries
    the update against the fresh SHA.
    """
    try:
        pairs = _parse_pairs(image_tags)
    except ValueError as e:
        return f"ERROR applying update: {e}"

    g = _get_github_client()
    try:
        repo = g.get_repo(active_build_repo())
    except Exception as e:
        return f"ERROR applying update: {e}"

    def _desired_already_present() -> dict | None:
        try:
            cur = json.loads(repo.get_contents(MANIFEST_PATH).decoded_content.decode())
        except Exception:
            return None
        imgs = cur.get("images", {})
        return cur if all(imgs.get(i) == t for i, t in pairs) else None

    last_err: Exception | None = None
    for _attempt in range(3):
        # (Re-)read current state to obtain a fresh SHA each attempt.
        try:
            contents = repo.get_contents(MANIFEST_PATH)
            sha = contents.sha
            current = json.loads(contents.decoded_content.decode())
        except Exception:
            contents = None
            sha = None
            current = {}

        for img, tag in pairs:
            current.setdefault("images", {})[img] = tag
        current["last_updated"] = "updated-by-agent"
        current["status"] = "applied"
        current["requested_by"] = "release-copilot-chat"
        new_content = json.dumps(current, indent=2)

        try:
            if contents:
                commit = repo.update_file(MANIFEST_PATH, commit_message, new_content, sha)
            else:
                commit = repo.create_file(MANIFEST_PATH, commit_message, new_content)
            return json.dumps(
                {
                    "ok": True,
                    "updated_file": MANIFEST_PATH,
                    "commit": commit["commit"].sha,
                    "url": commit["commit"].html_url,
                    "new_manifest": current,
                },
                indent=2,
            )
        except GithubException as e:
            last_err = e
            if e.status == 409:
                # SHA conflict. The write may have already succeeded (HTTP retry)
                # or another commit landed first — re-check before retrying.
                applied = _desired_already_present()
                if applied is not None:
                    return json.dumps(
                        {
                            "ok": True,
                            "updated_file": MANIFEST_PATH,
                            "commit": None,
                            "url": f"https://github.com/{active_build_repo()}/blob/main/{MANIFEST_PATH}",
                            "new_manifest": applied,
                            "note": "Desired tags already present (409 conflict resolved idempotently).",
                        },
                        indent=2,
                    )
                continue  # stale SHA — retry with a freshly-read SHA
            return f"ERROR applying update: {e}"
        except Exception as e:
            return f"ERROR applying update: {e}"

    return f"ERROR applying update: {last_err}"


@tool(args_schema=DispatchWorkflowInput)
def dispatch_workflow(
    workflow: str = "image-tag-step-report.yml", image_tags: str = "", extra_inputs: str = ""
) -> str:
    """
    Dispatch a workflow_dispatch event.
    workflow: filename in .github/workflows (must be in the configured allow-list)
    image_tags: "img1:tag1,img2:tag2"
    extra_inputs: optional JSON string of other inputs
    """
    # Enforce the config-driven allow-list (safety gate). Set ALLOWED_WORKFLOWS
    # (env / Helm ConfigMap) to permit additional workflows.
    if workflow not in ALLOWED_WORKFLOWS:
        return (
            f"ERROR dispatching workflow: '{workflow}' is not in the allowed list "
            f"{sorted(ALLOWED_WORKFLOWS)}. Add it via the ALLOWED_WORKFLOWS config to permit it."
        )

    inputs: dict[str, Any] = {}
    if image_tags:
        inputs["image_tags"] = image_tags
    if extra_inputs:
        try:
            extra = json.loads(extra_inputs)
            inputs.update(extra)
        except Exception:
            inputs["extra"] = extra_inputs

    try:
        g = _get_github_client()
        repo = g.get_repo(active_build_repo())
        workflow_obj = repo.get_workflow(workflow)
        # Dispatch against the repo's actual default branch (not a hardcoded
        # "main") so repos on master/develop/etc. still fire — and so the ref
        # matches the default branch the manifest is read/written on.
        workflow_obj.create_dispatch(ref=repo.default_branch, inputs=inputs)

        return json.dumps(
            {
                "dispatched": True,
                "workflow": workflow,
                "repo": active_build_repo(),
                "inputs": inputs,
                "note": "Workflow dispatched. Use get_recent_runs or get_workflow_status to check progress.",
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR dispatching workflow: {e}"


@tool(args_schema=GetRecentRunsInput)
def get_recent_runs(limit: int = 5) -> str:
    """List recent workflow runs for the repo (good for status after dispatch)."""
    try:
        g = _get_github_client()
        repo = g.get_repo(active_build_repo())
        # islice over the PaginatedList: lazy (only fetches the page(s) needed,
        # unlike list(...) which pulls the ENTIRE history) AND empty-safe (a bare
        # [:limit] slice raises IndexError on an empty PaginatedList in PyGithub).
        runs = list(itertools.islice(repo.get_workflow_runs(), limit))

        result = []
        for run in runs:
            result.append(
                {
                    "databaseId": run.id,
                    "workflowName": run.name or "unknown",
                    "event": run.event,
                    "status": run.status,
                    "conclusion": run.conclusion,
                    "createdAt": str(run.created_at),
                    "url": run.html_url,
                }
            )
        return json.dumps(result, indent=2)
    except Exception as e:
        return f"ERROR listing runs: {e}"


@tool(args_schema=GetWorkflowStatusInput)
def get_workflow_status(run_id: str) -> str:
    """Get status of a specific workflow run, including per-step details for failures.
    Note: The rendered GitHub summary (GITHUB_STEP_SUMMARY) is not directly available via the API.
    Step conclusions (success/failure) are available and usually more useful.
    """
    try:
        g = _get_github_client()
        repo = g.get_repo(active_build_repo())
        run = repo.get_workflow_run(int(run_id))

        jobs_data = []
        try:
            for job in run.jobs():
                steps_data = []
                for step in getattr(job, "steps", []) or []:
                    steps_data.append(
                        {
                            "number": getattr(step, "number", None),
                            "name": getattr(step, "name", None),
                            "status": getattr(step, "status", None),
                            "conclusion": getattr(step, "conclusion", None),
                            "started_at": str(getattr(step, "started_at", ""))
                            if getattr(step, "started_at", None)
                            else None,
                            "completed_at": str(getattr(step, "completed_at", ""))
                            if getattr(step, "completed_at", None)
                            else None,
                        }
                    )
                jobs_data.append(
                    {
                        "name": job.name,
                        "status": job.status,
                        "conclusion": job.conclusion,
                        "steps": steps_data,
                    }
                )
        except Exception as job_err:
            jobs_data = [{"error": str(job_err)}]

        return json.dumps(
            {
                "databaseId": run.id,
                "workflowName": run.name or "unknown",
                "event": run.event,
                "status": run.status,
                "conclusion": run.conclusion,
                "createdAt": str(run.created_at),
                "url": run.html_url,
                "jobs": jobs_data,
                "note": "Step conclusions are available. The free-text GITHUB_STEP_SUMMARY markdown is not exposed by the GitHub API.",
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR getting run {run_id}: {e}"


# ==================== PR Tracking Tools (for deployment repo) ====================

