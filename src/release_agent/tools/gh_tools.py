"""GitHub tools for the release LangGraph agent using PyGithub.

All operations are performed via the GitHub REST API (PyGithub library).
Works great with a Personal Access Token (set via GH_TOKEN env var).
"""

import base64
import itertools
import json
import os
import subprocess
import uuid
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
# Dispatchable-workflow allow-list — driven by config (env / Helm ConfigMap), not
# hardcoded. The default workflow is always allowed so a promote never self-blocks.
ALLOWED_WORKFLOWS = set(settings.allowed_workflows) | {settings.default_workflow}
# Workflow used to (re)run the deployment simulation in DEPLOY_REPO.
ON_MERGE_WORKFLOW = settings.on_merge_workflow

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
        tokens = [t for t in image_tags.lower().replace(":", " ").replace(",", " ").split() if t]
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
        tokens = [t for t in search_term.lower().replace(":", " ").replace(",", " ").split() if t]
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
        workflow = repo.get_workflow(ON_MERGE_WORKFLOW)

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


# ============ Image-tag build verification (PyGithub refactor of gh-image-tag-steps.sh) ============

class VerifyImageTagInput(BaseModel):
    image: str = Field(..., description="Image name (must be in image-workflows.json)")
    tag: str = Field(..., description="Git tag that was built, e.g. v1.2.3")
    repo: str = Field(default="", description="owner/repo where the build ran. Defaults to the target repo.")
    tag_generation_step: str = Field(default="Generate Git tag", description="Step name that generates the git tag")
    tag_marker_prefix: str = Field(default="TAG_GENERATED=", description="Log marker prefix emitted by the tag step")


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
            allow_redirects=True, timeout=30,
        )
        return r.text if r.status_code == 200 else ""
    except Exception:
        return ""


@tool(args_schema=VerifyImageTagInput)
def verify_image_tag_build(image: str, tag: str, repo: str = "",
                           tag_generation_step: str = "Generate Git tag",
                           tag_marker_prefix: str = "TAG_GENERATED=") -> str:
    """
    Verify that image:tag was actually built correctly BEFORE promoting it.

    Resolves the git tag -> commit, finds the image's build-workflow run at that commit,
    confirms the tag-generation step succeeded AND the job log contains the
    '<tag_marker_prefix><tag>' marker, and reports the run's RLFT release-control steps.
    verified=true only when a matching successful run is found.
    """
    repo_full = repo or TARGET_REPO
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
        return json.dumps({
            "verified": False, "image": image, "tag": tag, "tag_commit": commit,
            "workflow": workflow, "repo": repo_full,
            "reason": f"No '{workflow}' run found at commit {commit[:7]}.",
        }, indent=2)

    marker = f"{tag_marker_prefix}{tag}"

    def _inspect(run):
        tag_step, rlft = None, []
        try:
            for job in run.jobs():
                for step in (getattr(job, "steps", None) or []):
                    name = getattr(step, "name", "") or ""
                    rec = {"job": job.name, "job_id": job.id,
                           "number": getattr(step, "number", None), "name": name,
                           "status": getattr(step, "status", None),
                           "conclusion": getattr(step, "conclusion", None)}
                    if name == tag_generation_step and tag_step is None:
                        tag_step = rec
                    if name.startswith("RLFT"):
                        rlft.append({k: rec[k] for k in ("job", "number", "name", "status", "conclusion")})
        except Exception:
            pass
        log_found = bool(
            tag_step and tag_step.get("conclusion") == "success" and tag_step.get("job_id")
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
    return json.dumps({
        "verified": verified,
        "image": image, "tag": tag, "tag_commit": commit, "workflow": workflow, "repo": repo_full,
        "run": {"id": run.id, "name": run.name, "url": run.html_url,
                "headSha": run.head_sha, "status": run.status, "conclusion": run.conclusion},
        "tag_generation": ({
            "step": tag_generation_step, "job": tag_step.get("job"),
            "status": tag_step.get("status"), "conclusion": tag_step.get("conclusion"),
            "marker": marker, "log_marker_found": log_found,
        } if tag_step else {"step": tag_generation_step, "found": False, "marker": marker}),
        "rlft_controls": rlft,
        "note": "verified=true means the tag was built by a successful run whose tag-gen step logged "
                "the marker. Check the RLFT control steps before promoting.",
    }, indent=2)


# ============ Build-pipeline release controls (RLFT/RFTL pass/fail) ============

class BuildControlsInput(BaseModel):
    image: str = Field(default="", description="Image name (to find the build workflow + resolve the tag). Optional if run_id is given.")
    tag: str = Field(default="", description="Git tag that was built, e.g. v1.2.3. Optional if run_id is given.")
    repo: str = Field(default="", description="owner/repo where the build ran. Defaults to the configured build repo / target repo.")
    run_id: int = Field(default=0, description="GitHub Actions run id that generated the tag. Pass it to skip tag->run discovery, or when discovery can't find the run.")


# Step conclusions that count as a failed control gate.
_FAIL_CONCLUSIONS = {"failure", "timed_out", "cancelled", "startup_failure", "action_required"}


def _is_control_step(name: str) -> bool:
    return any(name.startswith(p) for p in settings.control_prefixes)


def _collect_controls(run) -> list[dict]:
    """Enumerate a build run's release-control steps (RLFT/RFTL...) with pass/fail."""
    controls = []
    for job in run.jobs():
        for step in (getattr(job, "steps", None) or []):
            name = getattr(step, "name", "") or ""
            if not _is_control_step(name):
                continue
            concl = getattr(step, "conclusion", None)
            controls.append({
                "control": name, "job": job.name,
                "status": getattr(step, "status", None), "conclusion": concl,
                "passed": concl == "success", "failed": concl in _FAIL_CONCLUSIONS,
            })
    return controls


def _build_repo_full(repo: str = "") -> str:
    return repo or settings.build_repo or TARGET_REPO


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
        "image": image, "tag": tag, "repo": repo_full,
        "run": {"id": run.id, "name": run.name, "url": run.html_url,
                "head_sha": run.head_sha, "conclusion": run.conclusion, "created_at": str(run.created_at)},
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
            return (f"ERROR fetching controls: run id {run_id} not found in {repo_full}. "
                    "Check the run id and that the repo is the build-pipeline repo.")
    else:
        if not (image and tag):
            return ("NEED_INPUT: provide a run_id, or both image and tag, so I can locate the "
                    "build-pipeline run that generated the tag.")
        run, err = _find_build_run(repo_obj, image, tag)
        if run is None:
            return json.dumps({
                "need_run_id": True, "image": image, "tag": tag, "repo": repo_full, "reason": err,
                "ask": (f"I couldn't locate the build run for {image}:{tag} in {repo_full} ({err}). "
                        "Please provide the GitHub Actions run id that generated this tag."),
            }, indent=2)

    report = _controls_report(repo_full, image, tag, run)
    if not report["controls"]:
        report["note"] = (f"No control steps matched prefixes {settings.control_prefixes} in this run — "
                          "verify the run id / build pipeline.")
    else:
        report["note"] = "gate=PASS only when every control passed. Do NOT promote to PRD with any FAILED control."
    return json.dumps(report, indent=2)


# ============ Environment promotion: update config JSON + open a PR (PyGithub) ============

class OpenReleasePRInput(BaseModel):
    environment: str = Field(..., description="Target environment: uat or prod")
    image_tags: str = Field(..., description="Comma-separated image:tag pairs (supports multiple)")
    change_request_json: str = Field(
        default="",
        description="JSON object of the change_request block (required for prod) — drives the auto-created CHG.",
    )


def _upsert_json_file(repo, branch: str, path: str, new_doc: dict) -> None:
    """Create or update a JSON file on a branch."""
    try:
        c = repo.get_contents(path, ref=branch)
        sha = c.sha
    except Exception:
        sha = None
    content = json.dumps(new_doc, indent=2)
    msg = f"chore(release): update {path}"
    if sha:
        repo.update_file(path, msg, content, sha, branch=branch)
    else:
        repo.create_file(path, msg, content, branch=branch)


def _read_json_file(repo, branch: str, path: str) -> dict:
    try:
        c = repo.get_contents(path, ref=branch)
        return json.loads(c.decoded_content.decode())
    except Exception:
        return {}


# ---- Today's PRD release window (shared across sessions via GitHub) ----

def _todays_prd_prs() -> list:
    """Today's (UTC) UAT->PRD release PRs that LOCK the day — open or merged. A
    closed-unmerged PR is abandoned and does not lock. GitHub is the cross-session
    source of truth, so any session sees the same answer."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    g = _get_github_client()
    repo = g.get_repo(DEPLOY_REPO)
    out = []
    for pr in itertools.islice(
        repo.get_pulls(state="all", base=settings.prd_branch, sort="created", direction="desc"), 40
    ):
        d = pr.created_at.astimezone(timezone.utc).date()
        if d < today:
            break
        if d == today and (pr.state == "open" or pr.merged_at is not None):
            out.append(pr)
    return out


def get_release_status() -> dict:
    """Today's PRD release status under the SIT->UAT->PRD model.

    Images accumulate on UAT through the day; the single UAT->PRD release PR is
    raised ONLY after the cutoff (and raising it locks the day). Reports the UAT
    accumulation, whether the cutoff has passed, whether the day is locked, and
    whether images can still be added / the release can be raised."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cutoff = settings.prd_cutoff_hour_utc
    cutoff_passed = now.hour >= cutoff
    base = {"date_utc": now.date().isoformat(), "now_utc": now.strftime("%H:%M"),
            "cutoff_utc": f"{cutoff:02d}:00", "cutoff_passed": cutoff_passed}
    try:
        g = _get_github_client()
        repo = g.get_repo(DEPLOY_REPO)
        uat_cfg = _read_json_file(repo, settings.uat_branch, settings.env_config_path)
        uat_images = uat_cfg.get("images", {}) if isinstance(uat_cfg, dict) else {}
        prd_cfg = _read_json_file(repo, settings.prd_branch, settings.env_config_path)
        prd_images = prd_cfg.get("images", {}) if isinstance(prd_cfg, dict) else {}
        prs = _todays_prd_prs()
    except Exception as e:
        return {**base, "error": str(e), "can_add": True, "can_raise_prod": False,
                "locked": False, "uat_images": {}, "pending_changes": {}, "prd_pr_today": None}

    # Pending = images on UAT whose tag differs from PRD (what an UAT->PRD PR would
    # actually promote). UAT config persists across days, so "has any images" is NOT
    # the same as "has something to release today" — compare against PRD.
    pending = {i: t for i, t in uat_images.items() if prd_images.get(i) != t}

    today_pr = prs[0] if prs else None         # today's UAT->PRD release PR (post-cutoff)
    locked = today_pr is not None              # release raised -> no more adds today
    can_add = not locked                       # adds land on UAT until the PR is raised
    can_raise_prod = cutoff_passed and not locked and bool(pending)

    if locked:
        reason = f"Today's UAT→PRD release PR #{today_pr.number} is raised — the day is locked."
    elif not pending:
        reason = ("No changes on UAT vs PRD — nothing to release"
                  + (" (cutoff passed)." if cutoff_passed else f"; UAT→PRD opens after {cutoff:02d}:00 UTC."))
    elif cutoff_passed:
        reason = f"Cutoff passed — raise the UAT→PRD release PR ({len(pending)} image(s) to promote)."
    else:
        reason = (f"Collecting on UAT ({len(pending)} image(s) pending vs PRD); the UAT→PRD PR opens "
                  f"after {cutoff:02d}:00 UTC.")
    return {
        **base,
        "uat_images": uat_images,
        "pending_changes": pending,
        "locked": locked,
        "can_add": can_add,
        "can_raise_prod": can_raise_prod,
        "reason": reason,
        "prd_pr_today": ({
            "number": today_pr.number, "url": today_pr.html_url, "title": today_pr.title,
            "state": today_pr.state, "author": today_pr.user.login if today_pr.user else None,
            "created_at": str(today_pr.created_at),
        } if today_pr else None),
    }


@tool
def check_release_window() -> str:
    """Report today's PRD release status (UTC): whether a PRD release PR already
    exists today, whether the daily cutoff has passed, and whether a new PRD
    release can still be created. Shared across all sessions/developers via GitHub."""
    return json.dumps(get_release_status(), indent=2)


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


def _apply_via_pr_chain(repo, mutate_fn, summary: str) -> dict:
    """Branches are protected — never commit to them directly. Apply a change to the
    images config via PRs along the chain: a fresh working branch -> SIT -> UAT.
    Each step is a PR (merged when protection allows). mutate_fn(images: dict) -> bool
    mutates the images map in place and returns True if it changed anything."""
    sit, uat = settings.sit_branch, settings.uat_branch
    images_path = settings.env_config_path
    prs: list = []

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
    prs.append({"stage": f"→{sit}", "number": pr_sit.number, "url": pr_sit.html_url,
                "merged": ok, "detail": detail})
    # 2) SIT -> UAT (promote) — only if SIT actually advanced
    if ok:
        try:
            pr_uat = repo.create_pull(title=f"Promote {sit} → {uat}: {summary}", body=summary,
                                      head=sit, base=uat)
            ok2, detail2 = _merge_pr(pr_uat, "merge")
            prs.append({"stage": f"{sit}→{uat}", "number": pr_uat.number, "url": pr_uat.html_url,
                        "merged": ok2, "detail": detail2})
        except Exception as e:
            prs.append({"stage": f"{sit}→{uat}", "error": str(e)})
    return {"changed": True, "prs": prs}


def _pr_chain_note(prs: list) -> str:
    """One-line human summary of the PRs raised by _apply_via_pr_chain."""
    if not prs:
        return "No change (already in that state)."
    bits = []
    for p in prs:
        if p.get("number"):
            bits.append(f"PR #{p['number']} {p['stage']} ({'merged' if p.get('merged') else p.get('detail', 'open')})")
        elif p.get("error"):
            bits.append(f"{p['stage']} failed: {p['error']}")
    return "; ".join(bits) + "."


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
    uat_images = (_read_json_file(repo, settings.uat_branch, settings.env_config_path) or {}).get("images", {}) or {}
    return {"uat_images": uat_images, "prs": res["prs"], "changed": res["changed"]}


def _unstage_images_from_uat(repo, names: list) -> dict:
    """Remove image(s) from the day's release via the protected-branch PR chain
    (working -> SIT -> UAT): revert each to PRD's current tag, or drop it if new."""
    prd_images = (_read_json_file(repo, settings.prd_branch, settings.env_config_path) or {}).get("images", {}) or {}
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

    summary = "Remove " + ",".join(names) + " from release"
    res = _apply_via_pr_chain(repo, _mut, summary)
    uat_images = (_read_json_file(repo, settings.uat_branch, settings.env_config_path) or {}).get("images", {}) or {}
    pending = {i: t for i, t in uat_images.items() if prd_images.get(i) != t}
    return {"reverted": reverted, "removed": removed, "not_found": not_found,
            "pending_after": pending, "prs": res["prs"], "changed": res["changed"]}


class RemoveFromReleaseInput(BaseModel):
    image_names: str = Field(
        ..., description="Comma-separated image names to remove from today's release. Tags are "
        "optional/ignored (e.g. 'orders-api' or 'orders-api:v1.1.0').")


@tool(args_schema=RemoveFromReleaseInput)
def remove_from_release(image_names: str) -> str:
    """Remove (unstage) image(s) from TODAY'S pending PRD release on the UAT branch —
    use before the cutoff / before the UAT->PRD PR is raised. Each named image is
    reverted to the tag currently on PRD (or dropped if it's new), so it no longer
    ships in today's release. Reversible (just promote it again). Refused once the
    day is locked (the UAT->PRD PR has been raised)."""
    names = []
    for tok in image_names.replace(",", " ").split():
        tok = tok.strip()
        if tok:
            names.append(tok.split(":", 1)[0])
    if not names:
        return "ERROR: no image names provided to remove."

    status = get_release_status()
    if status.get("locked"):
        p = status.get("prd_pr_today") or {}
        return (f"ERROR: today's UAT→PRD release PR #{p.get('number')} is already raised — UAT is locked. "
                f"Amend or close {p.get('url','')} to change the release.")
    try:
        repo = _get_github_client().get_repo(DEPLOY_REPO)
    except Exception as e:
        return f"ERROR removing from release: {e}"

    res = _unstage_images_from_uat(repo, names)
    changed = res["reverted"] or res["removed"]
    note_parts = []
    if res["reverted"]:
        note_parts.append("reverted to PRD: " + ", ".join(res["reverted"]))
    if res["removed"]:
        note_parts.append("dropped (was new): " + ", ".join(res["removed"]))
    if res["not_found"]:
        note_parts.append("not staged: " + ", ".join(res["not_found"]))
    pr_str = "; ".join(
        f"PR #{p['number']} {p['stage']} ({'merged' if p.get('merged') else p.get('detail', 'open')})"
        for p in res.get("prs", []) if p.get("number"))
    note = ("Removed from today's release — " if changed else "No changes — ") + (
        "; ".join(note_parts) or "nothing matched") + f". {len(res['pending_after'])} image(s) still pending."
    if pr_str:
        note += f" Via {pr_str}."
    return json.dumps({"ok": True, "action": "unstaged", **res, "note": note}, indent=2)


def _lead_time_ok(cr: dict):
    """Production changes need lead time: the change request's start_date must be at
    least `prd_lead_time_days` ahead (default 1 -> tomorrow or later).
    Returns (ok: bool, message: str)."""
    from datetime import datetime, timezone, timedelta
    raw = str(cr.get("start_date") or "").strip()
    lead = settings.prd_lead_time_days
    if not raw:
        return False, ("the change request needs a start_date — production releases need lead time, so "
                       f"the start date must be at least {lead} day(s) out.")
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
        return False, (f"start_date {dt.date().isoformat()} is too soon — production changes need "
                       f"{lead} day(s) lead time, so the start date must be {earliest.isoformat()} "
                       "(tomorrow) or later.")
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
            return ("NOTE: nothing to release — UAT already matches PRD (no new images staged). "
                    "No UAT→PRD PR was raised.")
        ok, msg = _lead_time_ok(cr)
        if not ok:
            return f"ERROR raising release: {msg}"
        image_str = ",".join(f"{i}:{t}" for i, t in pending.items())
        try:
            source_ref = repo.get_git_ref(f"heads/{uat}")
        except Exception:
            return f"ERROR raising release: UAT branch '{uat}' not found in {DEPLOY_REPO}."

        branch = f"release/prod/{uuid.uuid4().hex[:8]}"
        repo.create_git_ref(f"refs/heads/{branch}", source_ref.object.sha)
        cr_doc = {"environment": "prod", "images": pending, "promoting_to_state": uat_images,
                  "change_request": cr, "status": "pending-chg"}
        _upsert_json_file(repo, branch, cr_path, cr_doc)

        title = f"Release UAT → PRD: {image_str}"
        body = (f"Daily production release — promote **{uat}** → **{prd}**.\n\n"
                f"- Images: `{image_str}`\n- Change request: `{cr_path}`\n\n"
                "CHG/RMG auto-created from the change request (see comments).")
        pr = repo.create_pull(title=title, body=body, head=branch, base=prd)

        from datetime import datetime, timezone
        ym = datetime.now(timezone.utc).strftime("%Y%m")
        seq = f"{uuid.uuid4().int % 100000:05d}"
        chg, rmg = f"CHG-{ym}-{seq}", f"RMG-{ym}-{seq}"
        sd = cr.get("short_description") or cr.get("summary") or image_str
        window = ""
        if cr.get("start_date") or cr.get("end_date"):
            window = f"\n- **Window:** {cr.get('start_date','?')} → {cr.get('end_date','?')}"
        pr.create_issue_comment("\n".join([
            "📋 **Change management & release controls** (auto-created from the change request)",
            "", f"- **CHG:** {chg}", f"- **RMG:** {rmg}", f"- **Summary:** {sd}",
            f"- **Images:** {image_str}{window}", "",
            "**Control gates (RLFT):**", "- RLFT approval gate: open", "- RLFT deploy control: open",
        ]))
        return json.dumps({
            "ok": True, "environment": "prod", "action": "raised_uat_to_prd",
            "image_tags": image_str, "branch": branch, "base_branch": prd, "source_branch": uat,
            "pr_number": pr.number, "pr_url": pr.html_url,
            "change_request_template": cr_path, "chg": chg, "rmg": rmg,
            "note": (f"Daily UAT→PRD release PR #{pr.number} raised promoting {len(pending)} image(s); "
                     f"CHG {chg} / RMG {rmg} created. The day is now locked."),
        }, indent=2)
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
            failures.append(f"{image}:{tag} → FAILED controls: {', '.join(rep['summary']['failed'])} "
                            f"(build run {rep['run']['url']})")
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
        return f"ERROR opening release PR: unsupported environment '{environment}' (use uat or prod)."

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
        repo = g.get_repo(DEPLOY_REPO)
    except Exception as e:
        return f"ERROR opening release PR: {e}"

    # --- UAT: stage onto UAT via the protected-branch PR chain (working->SIT->UAT) ---
    if env == "uat":
        res = _add_images_to_uat(repo, pairs)
        imgs = res["uat_images"]
        return json.dumps({
            "ok": True, "environment": "uat", "action": "staged_to_uat",
            "image_tags": image_str, "uat_images": imgs, "prs": res["prs"],
            "note": f"Staged {image_str} via PR chain (working→SIT→UAT). {_pr_chain_note(res['prs'])} "
                    f"{len(imgs)} image(s) on UAT.",
        }, indent=2)

    # --- PROD path ---
    status = get_release_status()
    if status.get("locked"):
        p = status.get("prd_pr_today") or {}
        return (f"ERROR: today's UAT→PRD release PR #{p.get('number')} is already raised — the day is "
                f"locked, no more images can be added. {p.get('url','')}")

    # Build-control gate (fail-closed) for the requested images.
    failures = _prod_controls_failures(pairs)
    if failures:
        return ("ERROR: build controls failed — cannot stage for PRD release.\n" + "\n".join(failures))

    # Stage the requested images for today's release via the PR chain (working->SIT->UAT).
    res = _add_images_to_uat(repo, pairs)
    imgs = res["uat_images"]

    if not status.get("cutoff_passed"):
        cutoff = settings.prd_cutoff_hour_utc
        return json.dumps({
            "ok": True, "environment": "prod", "action": "staged_to_uat",
            "image_tags": image_str, "uat_images": imgs, "prs": res["prs"],
            "note": (f"Staged {image_str} for today's release via PR chain (working→SIT→UAT). "
                     f"{_pr_chain_note(res['prs'])} {len(imgs)} image(s) on UAT. The single UAT→PRD PR "
                     f"is raised after {cutoff:02d}:00 UTC — until then more images can be added."),
        }, indent=2)

    # Cutoff passed → raise the day's UAT→PRD release PR.
    if not cr:
        return ("ERROR: the cutoff has passed — raising the UAT→PRD release requires a change_request "
                "block (drives the CHG).")
    return _raise_uat_to_prd_pr(repo, cr)


class RaiseReleaseInput(BaseModel):
    change_request_json: str = Field(
        default="", description="change_request block (required) the CHG is created from.")


@tool(args_schema=RaiseReleaseInput)
def raise_prod_release(change_request_json: str = "") -> str:
    """Raise today's single UAT->PRD production release PR with everything currently
    staged on UAT. Allowed ONLY after the daily cutoff (raising it locks the day).
    Requires a change_request block to drive the CHG/RMG."""
    status = get_release_status()
    if status.get("locked"):
        p = status.get("prd_pr_today") or {}
        return f"ERROR: today's release PR #{p.get('number')} is already raised — locked. {p.get('url','')}"
    if not status.get("cutoff_passed"):
        return (f"ERROR: the UAT→PRD release can only be raised after {status.get('cutoff_utc')} UTC. "
                "Until then images keep accumulating on UAT.")
    cr: dict = {}
    if change_request_json.strip():
        try:
            cr = json.loads(change_request_json)
        except Exception:
            return "ERROR: change_request_json is not valid JSON."
    if not cr:
        return "ERROR: raising the release requires a change_request block (drives the CHG)."
    try:
        repo = _get_github_client().get_repo(DEPLOY_REPO)
    except Exception as e:
        return f"ERROR raising release: {e}"
    return _raise_uat_to_prd_pr(repo, cr)


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
    verify_image_tag_build,
    get_build_controls,
    open_release_pr,
    raise_prod_release,
    remove_from_release,
    check_release_window,
]
