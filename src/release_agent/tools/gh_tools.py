"""GitHub tools for the release LangGraph agent using PyGithub.

All operations are performed via the GitHub REST API (PyGithub library).
Works great with a Personal Access Token (set via GH_TOKEN env var).
"""

import base64
import itertools
import json
import os
import re
import subprocess
from typing import Any

from github import Github, Auth, GithubException
from langchain_core.tools import tool
from pydantic import BaseModel, Field

# Config - using Pydantic settings for consistency
from ..config import settings

TARGET_REPO = settings.target_repo
DEPLOY_REPO = getattr(settings, 'deploy_repo', settings.target_repo)
CONFIG_PATH = settings.config_path
MANIFEST_PATH = settings.manifest_path
ALLOWED_WORKFLOWS = {
    "image-tag-step-report.yml",
    "build-payments-api.yml",
    "build-orders-api.yml",
    # Add your real promote workflow(s) here
    "release-promote.yml",
}

def _resolve_github_token() -> str | None:
    """Resolve a GitHub token from the environment, falling back to the `gh` CLI.

    Order: GH_TOKEN -> GITHUB_TOKEN -> `gh auth token` (keyring login).
    The CLI fallback means a developer who is logged in via `gh auth login`
    doesn't have to export a PAT manually (the previous behavior caused 404 /
    auth failures whenever GH_TOKEN was unset).
    """
    token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    if token:
        return token.strip()
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return None


# Initialize PyGithub client (PAT via GH_TOKEN/GITHUB_TOKEN, or the gh CLI login)
def _get_github_client() -> Github:
    token = _resolve_github_token()
    if token:
        return Github(auth=Auth.Token(token))
    # Fallback - unauthenticated (will hit rate limits / 404s on private repos)
    return Github()


# Pydantic schemas for tool inputs (better validation + schema generation)
class ImageTagsInput(BaseModel):
    image_tags: str = Field(
        ..., description="Comma-separated image:tag pairs, e.g. 'payments-api:2.0.33,orders-api:v1.2.3'"
    )


class ApplyJsonUpdateInput(BaseModel):
    image_tags: str = Field(..., description="Comma-separated image:tag pairs")
    commit_message: str = Field(
        default="chore(release): update image tags via release-agent chat",
        description="Commit message for the update"
    )


class DispatchWorkflowInput(BaseModel):
    workflow: str = Field(
        default="image-tag-step-report.yml",
        description="Workflow filename to dispatch"
    )
    image_tags: str = Field(default="", description="Comma-separated image:tag pairs to pass")
    extra_inputs: str = Field(
        default="",
        description="Optional JSON string with additional workflow inputs"
    )


class GetRecentRunsInput(BaseModel):
    limit: int = Field(default=5, ge=1, le=50, description="Max number of runs to return")


class GetWorkflowStatusInput(BaseModel):
    run_id: str = Field(..., description="Workflow run ID (databaseId)")


class FindPrsInput(BaseModel):
    search_term: str = Field(
        default="",
        description="Search term for PRs (e.g. image name, tag, or 'CHG')"
    )
    limit: int = Field(default=5, ge=1, le=20)


class PrNumberInput(BaseModel):
    pr_number: int = Field(..., description="Pull request number")


class PrCommentsInput(BaseModel):
    pr_number: int = Field(..., description="Pull request number")
    limit: int = Field(default=100, ge=1, le=300, description="Max comments to fetch (defaults high so the agent sees all PR comments, e.g. CHG/RMG tickets and RLFT gates)")


class RetriggerDeploymentWorkflowInput(BaseModel):
    pr_number: int = Field(..., description="PR number in the deployment repo to simulate for")
    simulate_closed_controls: str = Field(
        default="",
        description="Comma-separated list of controls to mark as closed (e.g. 'RLFT approval gate,RLFT deploy control'). Use this to simulate external actions."
    )


@tool
def list_allowed_images() -> str:
    """Return the list of known images and their build workflows from the config JSON."""
    try:
        g = _get_github_client()
        repo = g.get_repo(TARGET_REPO)
        content_file = repo.get_contents(CONFIG_PATH)
        content = base64.b64decode(content_file.content).decode()
        cfg = json.loads(content)
        images = list(cfg.get("images", {}).keys())
        return json.dumps({"allowed_images": images, "config": cfg}, indent=2)
    except Exception as e:
        return f"ERROR listing images: {e}"


def _fetch_current_manifest() -> str:
    """Plain helper so other tools can reuse manifest reading without invoking a
    StructuredTool (which is not directly callable under langchain-core 1.x)."""
    path = MANIFEST_PATH
    try:
        g = _get_github_client()
        repo = g.get_repo(TARGET_REPO)
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
                "status": "empty"
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

        return json.dumps({
            "current": current,
            "proposed": proposed,
            "changes": changes,
            "note": "This is a proposal only. Reply with the confirmation token to apply."
        }, indent=2)
    except Exception as e:
        return f"ERROR proposing update: {e}"


def _parse_pairs(image_tags: str) -> list[tuple[str, str]]:
    pairs = []
    for p in (x.strip() for x in image_tags.split(",")):
        if not p:
            continue
        if ":" not in p:
            raise ValueError(f"Bad image:tag {p}")
        img, tag = p.split(":", 1)
        pairs.append((img.strip(), tag.strip()))
    return pairs


@tool(args_schema=ApplyJsonUpdateInput)
def apply_json_update(image_tags: str, commit_message: str = "chore(release): update image tags via release-agent chat") -> str:
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
        repo = g.get_repo(TARGET_REPO)
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
            return json.dumps({
                "ok": True,
                "updated_file": MANIFEST_PATH,
                "commit": commit["commit"].sha,
                "url": commit["commit"].html_url,
                "new_manifest": current,
            }, indent=2)
        except GithubException as e:
            last_err = e
            if e.status == 409:
                # SHA conflict. The write may have already succeeded (HTTP retry)
                # or another commit landed first — re-check before retrying.
                applied = _desired_already_present()
                if applied is not None:
                    return json.dumps({
                        "ok": True,
                        "updated_file": MANIFEST_PATH,
                        "commit": None,
                        "url": f"https://github.com/{TARGET_REPO}/blob/main/{MANIFEST_PATH}",
                        "new_manifest": applied,
                        "note": "Desired tags already present (409 conflict resolved idempotently).",
                    }, indent=2)
                continue  # stale SHA — retry with a freshly-read SHA
            return f"ERROR applying update: {e}"
        except Exception as e:
            return f"ERROR applying update: {e}"

    return f"ERROR applying update: {last_err}"


@tool(args_schema=DispatchWorkflowInput)
def dispatch_workflow(workflow: str = "image-tag-step-report.yml", image_tags: str = "", extra_inputs: str = "") -> str:
    """
    Dispatch a workflow_dispatch event.
    workflow: filename in .github/workflows (must be in ALLOWED_WORKFLOWS)
    image_tags: "img1:tag1,img2:tag2"
    extra_inputs: optional JSON string of other inputs
    """
    if workflow not in ALLOWED_WORKFLOWS and not workflow.endswith(".yml"):
        # Allow but warn
        pass
    if workflow not in ALLOWED_WORKFLOWS:
        # Still permit for PoV flexibility but document it
        print(f"[WARN] dispatching non-allowlisted workflow: {workflow}")

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
        repo = g.get_repo(TARGET_REPO)
        workflow_obj = repo.get_workflow(workflow)
        # Dispatch against the repo's actual default branch (not a hardcoded
        # "main") so repos on master/develop/etc. still fire — and so the ref
        # matches the default branch the manifest is read/written on.
        workflow_obj.create_dispatch(ref=repo.default_branch, inputs=inputs)

        return json.dumps({
            "dispatched": True,
            "workflow": workflow,
            "repo": TARGET_REPO,
            "inputs": inputs,
            "note": "Workflow dispatched. Use get_recent_runs or get_workflow_status to check progress."
        }, indent=2)
    except Exception as e:
        return f"ERROR dispatching workflow: {e}"


@tool(args_schema=GetRecentRunsInput)
def get_recent_runs(limit: int = 5) -> str:
    """List recent workflow runs for the repo (good for status after dispatch)."""
    try:
        g = _get_github_client()
        repo = g.get_repo(TARGET_REPO)
        # islice over the PaginatedList: lazy (only fetches the page(s) needed,
        # unlike list(...) which pulls the ENTIRE history) AND empty-safe (a bare
        # [:limit] slice raises IndexError on an empty PaginatedList in PyGithub).
        runs = list(itertools.islice(repo.get_workflow_runs(), limit))

        result = []
        for run in runs:
            result.append({
                "databaseId": run.id,
                "workflowName": run.name or "unknown",
                "event": run.event,
                "status": run.status,
                "conclusion": run.conclusion,
                "createdAt": str(run.created_at),
                "url": run.html_url,
            })
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
        repo = g.get_repo(TARGET_REPO)
        run = repo.get_workflow_run(int(run_id))

        jobs_data = []
        try:
            for job in run.jobs():
                steps_data = []
                for step in getattr(job, 'steps', []) or []:
                    steps_data.append({
                        "number": getattr(step, 'number', None),
                        "name": getattr(step, 'name', None),
                        "status": getattr(step, 'status', None),
                        "conclusion": getattr(step, 'conclusion', None),
                        "started_at": str(getattr(step, 'started_at', '')) if getattr(step, 'started_at', None) else None,
                        "completed_at": str(getattr(step, 'completed_at', '')) if getattr(step, 'completed_at', None) else None,
                    })
                jobs_data.append({
                    "name": job.name,
                    "status": job.status,
                    "conclusion": job.conclusion,
                    "steps": steps_data,
                })
        except Exception as job_err:
            jobs_data = [{"error": str(job_err)}]

        return json.dumps({
            "databaseId": run.id,
            "workflowName": run.name or "unknown",
            "event": run.event,
            "status": run.status,
            "conclusion": run.conclusion,
            "createdAt": str(run.created_at),
            "url": run.html_url,
            "jobs": jobs_data,
            "note": "Step conclusions are available. The free-text GITHUB_STEP_SUMMARY markdown is not exposed by the GitHub API."
        }, indent=2)
    except Exception as e:
        return f"ERROR getting run {run_id}: {e}"


# ==================== PR Tracking Tools (for deployment repo) ====================

def _find_prs_for_images(image_tags: str, limit: int = 20) -> list[dict]:
    """Return deployment-repo PRs whose title/branch matches ALL tokens of the
    given image tags, newest first. Empty list on error or no match."""
    try:
        g = _get_github_client()
        repo = g.get_repo(DEPLOY_REPO)
        tokens = [t for t in re.split(r"[\s:,]+", image_tags.lower()) if t]
        if not tokens:
            return []
        out: list[dict] = []
        for pr in itertools.islice(repo.get_pulls(state="all", sort="created", direction="desc"), 60):
            hay = f"{pr.title} {pr.head.ref or ''}".lower()
            if all(tok in hay for tok in tokens):
                out.append({
                    "number": pr.number,
                    "url": pr.html_url,
                    "title": pr.title,
                    "state": pr.state,
                })
                if len(out) >= limit:
                    break
        return out
    except Exception:
        return []


def _find_pr_for_images(image_tags: str) -> dict | None:
    """Newest deployment-repo PR matching the image tags, or None."""
    prs = _find_prs_for_images(image_tags, limit=1)
    return prs[0] if prs else None


@tool(args_schema=FindPrsInput)
def find_prs(search_term: str = "", limit: int = 5) -> str:
    """
    Find recent PRs in the deployment repo.
    Use search_term like image name, tag, or 'CHG' to filter.
    Example: search_term="payments-api:2.0.33" or "CHG-12345"
    """
    def _pr_dict(pr):
        return {
            "number": pr.number,
            "title": pr.title,
            "url": pr.html_url,
            "state": pr.state,
            "createdAt": str(pr.created_at),
            "author": pr.user.login if pr.user else None,
        }

    try:
        g = _get_github_client()
        repo = g.get_repo(DEPLOY_REPO)

        if not search_term:
            pulls = list(itertools.islice(repo.get_pulls(state="all", sort="created", direction="desc"), limit))
            return json.dumps(
                {"repo": DEPLOY_REPO, "search_term": "recent", "prs": [_pr_dict(p) for p in pulls]},
                indent=2,
            )

        # Token-based scan of recent PRs (reliable; no search-index delay and
        # tolerant of ':' vs ' ' between image and tag). A PR matches if every
        # token of the search term appears in its title or head branch.
        tokens = [t for t in re.split(r"[\s:,]+", search_term.lower()) if t]
        results: dict[int, dict] = {}
        for pr in itertools.islice(repo.get_pulls(state="all", sort="created", direction="desc"), 80):
            hay = f"{pr.title} {pr.head.ref or ''}".lower()
            if tokens and all(tok in hay for tok in tokens):
                results[pr.number] = _pr_dict(pr)
                if len(results) >= limit:
                    break

        # Supplement with GitHub search (catches matches in body/comments, e.g. a
        # CHG/RMG number) — best-effort, since the search index can lag.
        if len(results) < limit:
            try:
                query = f"{search_term} repo:{DEPLOY_REPO} is:pr"
                for issue in itertools.islice(g.search_issues(query), limit):
                    if issue.pull_request and issue.number not in results:
                        results[issue.number] = {
                            "number": issue.number,
                            "title": issue.title,
                            "url": issue.html_url,
                            "state": issue.state,
                            "createdAt": str(issue.created_at),
                            "author": issue.user.login if issue.user else None,
                        }
            except Exception:
                pass

        return json.dumps(
            {"repo": DEPLOY_REPO, "search_term": search_term, "prs": list(results.values())[:limit]},
            indent=2,
        )
    except Exception as e:
        return f"ERROR finding PRs: {e}"


def _fetch_pr_details(pr_number: int) -> str:
    """Plain helper (reusable without StructuredTool invocation)."""
    try:
        g = _get_github_client()
        repo = g.get_repo(DEPLOY_REPO)
        pr = repo.get_pull(pr_number)
        return json.dumps({
            "number": pr.number,
            "title": pr.title,
            "url": pr.html_url,
            "state": pr.state,
            "headRefName": pr.head.ref,
            "baseRefName": pr.base.ref,
            "author": pr.user.login if pr.user else None,
            "createdAt": str(pr.created_at),
            "updatedAt": str(pr.updated_at),
            "mergedAt": str(pr.merged_at) if pr.merged_at else None,
        }, indent=2)
    except Exception as e:
        return f"ERROR getting PR #{pr_number}: {e}"


@tool(args_schema=PrNumberInput)
def get_pr_details(pr_number: int) -> str:
    """Get basic details of a PR (title, state, URL, branch, etc.)."""
    return _fetch_pr_details(pr_number)


def _fetch_pr_comments(pr_number: int, limit: int = 100) -> str:
    """Plain helper (reusable without StructuredTool invocation)."""
    try:
        g = _get_github_client()
        repo = g.get_repo(DEPLOY_REPO)
        pr = repo.get_pull(pr_number)
        comments = list(itertools.islice(pr.get_issue_comments(), limit))

        simplified = []
        for c in comments:
            simplified.append({
                "id": c.id,
                "user": c.user.login if c.user else None,
                "created_at": str(c.created_at),
                "body": c.body[:2000] if c.body else "",
            })
        return json.dumps({
            "repo": DEPLOY_REPO,
            "pr": pr_number,
            "comment_count": len(simplified),
            "comments": simplified
        }, indent=2)
    except Exception as e:
        return f"ERROR getting comments for PR #{pr_number}: {e}"


@tool(args_schema=PrCommentsInput)
def get_pr_comments(pr_number: int, limit: int = 30) -> str:
    """
    Get recent comments on a PR.
    These usually contain CHG ticket references and control status
    (e.g. "CHG-12345 created", "RLFT approval gate closed", "controls opened").
    """
    return _fetch_pr_comments(pr_number, limit)


@tool(args_schema=PrNumberInput)
def summarize_pr_controls(pr_number: int) -> str:
    """
    Fetch the PR + its comments and provide a summary focused on:
    - CHG/change ticket references
    - Release control states (RLFT gates, closed/opened)
    - Overall readiness
    """
    try:
        details = json.loads(_fetch_pr_details(pr_number))
        comments_data = json.loads(_fetch_pr_comments(pr_number, limit=100))

        return json.dumps({
            "pr_details": details,
            "comments": comments_data.get("comments", []),
            "note": "Look for CHG and RMG tickets, 'RLFT', 'closed', 'opened', 'approved', 'gate' in the comments."
        }, indent=2)
    except Exception as e:
        return f"ERROR summarizing PR #{pr_number}: {e}"


@tool(args_schema=RetriggerDeploymentWorkflowInput)
def retrigger_deployment_workflow(pr_number: int, simulate_closed_controls: str = "") -> str:
    """
    Retrigger the deployment simulation workflow in the DEPLOY_REPO.
    This is useful when you have closed some controls manually (outside the automation)
    and want the deployment comments / status to be re-generated with the updated control state.
    """
    try:
        g = _get_github_client()
        repo = g.get_repo(DEPLOY_REPO)
        workflow = repo.get_workflow("on-merge-deploy.yml")

        inputs = {"pr_number": str(pr_number)}
        if simulate_closed_controls:
            inputs["simulate_closed_controls"] = simulate_closed_controls

        workflow.create_dispatch(ref=repo.default_branch, inputs=inputs)

        return json.dumps({
            "triggered": True,
            "repo": DEPLOY_REPO,
            "pr_number": pr_number,
            "simulate_closed_controls": simulate_closed_controls,
            "note": "Workflow retriggered. Use summarize_pr_controls or get_pr_comments to see the updated status."
        }, indent=2)
    except Exception as e:
        return f"ERROR retriggering deployment workflow for PR #{pr_number}: {e}"


# Export all tools for the agent
GH_TOOLS = [
    list_allowed_images,
    get_current_manifest,
    propose_update,
    apply_json_update,
    dispatch_workflow,
    get_recent_runs,
    get_workflow_status,
    find_prs,
    get_pr_details,
    get_pr_comments,
    summarize_pr_controls,
    retrigger_deployment_workflow,
]
