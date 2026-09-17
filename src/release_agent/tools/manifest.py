"""Manifest / image-catalog / workflow tools (build repo)."""

import base64
import itertools
import json

from pydantic import BaseModel, Field

from ._common import tool, _get_github_client, active_build_repo, CONFIG_PATH


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
