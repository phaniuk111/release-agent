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
    """PRD-branch PRs created today (UTC). GitHub is the cross-session source of
    truth, so any session sees the same 'is there a release today?' answer."""
    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date()
    g = _get_github_client()
    repo = g.get_repo(DEPLOY_REPO)
    out = []
    for pr in itertools.islice(
        repo.get_pulls(state="all", base=settings.prd_branch, sort="created", direction="desc"), 40
    ):
        d = pr.created_at.astimezone(timezone.utc).date()
        if d == today:
            out.append(pr)
        elif d < today:
            break
    return out


def get_release_status() -> dict:
    """Today's PRD release status: whether a PRD PR already exists today, whether
    the daily UTC cutoff has passed, and whether a new PRD release can be created."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    cutoff = settings.prd_cutoff_hour_utc
    cutoff_passed = now.hour >= cutoff
    base = {"date_utc": now.date().isoformat(), "now_utc": now.strftime("%H:%M"),
            "cutoff_utc": f"{cutoff:02d}:00", "cutoff_passed": cutoff_passed}
    try:
        prs = _todays_prd_prs()
    except Exception as e:
        return {**base, "error": str(e), "can_create_prd": True, "prd_pr_today": None}
    today_pr = prs[0] if prs else None
    can_create = (today_pr is None or not settings.prd_once_per_day) and not cutoff_passed
    if today_pr is not None and settings.prd_once_per_day:
        reason = f"A PRD release PR was already created today (#{today_pr.number})."
    elif cutoff_passed:
        reason = f"Past the {cutoff:02d}:00 UTC cutoff — no more PRD releases today."
    else:
        reason = f"PRD window open until {cutoff:02d}:00 UTC."
    return {
        **base,
        "can_create_prd": can_create,
        # Release-train: when a PRD PR exists today, more images can be ADDED to it
        # (up to the cutoff) rather than opening a new PR.
        "can_add_prd": bool(today_pr) and not cutoff_passed,
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


def _add_images_to_pr(repo, pr_number: int, pairs: list, cr: dict) -> str:
    """Release-train: add image:tag pairs to today's existing PRD PR instead of
    opening a new one (one release per day; multiple developers contribute)."""
    image_map = {i: t for i, t in pairs}
    image_str = ",".join(f"{i}:{t}" for i, t in pairs)
    images_path, cr_path = settings.env_config_path, settings.change_request_path
    try:
        pr = repo.get_pull(int(pr_number))
        if pr.state != "open":
            return (f"ERROR adding to PRD release PR #{pr_number}: it is {pr.state}, not open. "
                    "Today's release is already closed.")
        branch = pr.head.ref

        # 1) merge into the images config on the release branch
        cfg = _read_json_file(repo, branch, images_path)
        cfg.setdefault("images", {})
        cfg["images"].update(image_map)
        _upsert_json_file(repo, branch, images_path, cfg)

        # 2) merge into the change-request images list (same CHG covers the train)
        crdoc = _read_json_file(repo, branch, cr_path)
        if crdoc:
            crdoc.setdefault("images", {})
            crdoc["images"].update(image_map)
            _upsert_json_file(repo, branch, cr_path, crdoc)

        # 3) PR comment recording the addition
        note = (cr.get("short_description") or cr.get("summary")) if cr else None
        lines = [f"➕ **Added to this release:** `{image_str}`",
                 f"- `{images_path}` now carries these tags (same change request / CHG)."]
        if note:
            lines.append(f"- Note: {note}")
        pr.create_issue_comment("\n".join(lines))

        return json.dumps({
            "ok": True, "environment": "prod", "action": "added_to_existing",
            "image_tags": image_str, "pr_number": pr.number, "pr_url": pr.html_url,
            "branch": branch,
            "note": (f"Added {image_str} to today's PRD release PR #{pr.number} — one release per day, "
                     "merged into the same change request / CHG."),
        }, indent=2)
    except Exception as e:
        return f"ERROR adding to PRD release PR #{pr_number}: {e}"


@tool(args_schema=OpenReleasePRInput)
def open_release_pr(environment: str, image_tags: str, change_request_json: str = "") -> str:
    """
    Branch-based promotion in the deploy repo:
      - uat : open a PR INTO the UAT branch (updates the images config).
      - prod: open a PR from the UAT branch INTO the PRD branch. The pasted change
        request (change_request_json) updates the change-request TEMPLATE file; the
        CHG (and RMG) are auto-created from it and posted as PR comments.
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

    cr: dict = {}
    if change_request_json.strip():
        try:
            cr = json.loads(change_request_json)
        except Exception:
            return "ERROR opening release PR: change_request_json is not valid JSON."
    # Prod create-vs-add (release-train: one PRD PR per day; additional images are
    # ADDED to that PR rather than opening a new one). After the cutoff, nothing.
    prod_add_to = None
    if env == "prod":
        status = get_release_status()
        if status.get("cutoff_passed"):
            return f"ERROR opening release PR: {status.get('reason', 'past the PRD cutoff for today')}"
        today = status.get("prd_pr_today")
        if today:
            prod_add_to = today["number"]  # contribute to today's existing release
        elif not cr:
            return "ERROR opening release PR: prod promotion requires a change_request block (drives the CHG)."

    # Build-control gate (fail-closed): block a PRD release if any release control
    # failed in the build pipeline for any image:tag. If a build run can't be
    # located we don't hard-block — the conversational flow asks for the run id.
    if env == "prod" and settings.prd_require_controls:
        try:
            bg = _get_github_client()
            brepo = bg.get_repo(_build_repo_full())
        except Exception:
            brepo = None
        if brepo is not None:
            failures = []
            for image, tag in pairs:
                run, _err = _find_build_run(brepo, image, tag)
                if run is None:
                    continue  # unverifiable here; chat flow asks for the run id
                rep = _controls_report(_build_repo_full(), image, tag, run)
                if rep["summary"]["failed"]:
                    failures.append(
                        f"{image}:{tag} → FAILED controls: {', '.join(rep['summary']['failed'])} "
                        f"(build run {rep['run']['url']})"
                    )
            if failures:
                return ("ERROR opening release PR: build controls failed — cannot promote to PRD.\n"
                        + "\n".join(failures))

    uat_branch, prd_branch = settings.uat_branch, settings.prd_branch
    images_path, cr_path = settings.env_config_path, settings.change_request_path
    base = uat_branch if env == "uat" else prd_branch
    source = uat_branch  # both flows branch off UAT (prod = promote UAT -> PRD)

    g = _get_github_client()
    try:
        repo = g.get_repo(DEPLOY_REPO)
    except Exception as e:
        return f"ERROR opening release PR: {e}"

    # Release-train: add to today's existing PRD PR instead of opening a new one.
    if env == "prod" and prod_add_to is not None:
        return _add_images_to_pr(repo, prod_add_to, pairs, cr)

    try:
        source_ref = repo.get_git_ref(f"heads/{source}")
    except Exception:
        return (f"ERROR opening release PR: source branch '{source}' not found in {DEPLOY_REPO}. "
                f"Create the '{uat_branch}' and '{prd_branch}' env branches first.")
    try:
        repo.get_branch(base)
    except Exception:
        return f"ERROR opening release PR: base branch '{base}' not found in {DEPLOY_REPO}."

    image_map = {i: t for i, t in pairs}
    image_str = ",".join(f"{i}:{t}" for i, t in pairs)
    branch = f"release/{env}/{uuid.uuid4().hex[:8]}"

    chg = rmg = None
    try:
        repo.create_git_ref(f"refs/heads/{branch}", source_ref.object.sha)

        # 1) images config (carried on the env branch)
        cfg = _read_json_file(repo, branch, images_path)
        cfg["environment"] = env
        cfg.setdefault("images", {})
        cfg["images"].update(image_map)
        cfg["requested_by"] = "release-copilot"
        cfg["status"] = "pr-open"
        _upsert_json_file(repo, branch, images_path, cfg)

        # 2) change-request template (prod) — the pasted JSON updates it
        if env == "prod":
            cr_doc = {"environment": "prod", "images": image_map,
                      "change_request": cr, "status": "pending-chg"}
            _upsert_json_file(repo, branch, cr_path, cr_doc)

        # PR
        if env == "uat":
            title = f"Promote to UAT: {image_str}"
            body = f"Promote `{image_str}` into the **{uat_branch}** branch.\n\n- Images config: `{images_path}`"
        else:
            title = f"Promote UAT → PRD: {image_str}"
            body = (f"Promote `{image_str}` from **{uat_branch}** into **{prd_branch}**.\n\n"
                    f"- Images config: `{images_path}`\n- Change request: `{cr_path}`\n\n"
                    "CHG/RMG are auto-created from the change request (see PR comments).")
        pr = repo.create_pull(title=title, body=body, head=branch, base=base)

        # 3) auto-create CHG/RMG from the change request, posted as PR comments
        if env == "prod":
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
                "",
                f"- **CHG:** {chg}",
                f"- **RMG:** {rmg}",
                f"- **Summary:** {sd}",
                f"- **Images:** {image_str}{window}",
                "",
                "**Control gates (RLFT):**",
                "- RLFT approval gate: open",
                "- RLFT deploy control: open",
            ]))
    except Exception as e:
        return f"ERROR opening release PR: {e}"

    result = {
        "ok": True, "environment": env, "image_tags": image_str,
        "images_config": images_path, "branch": branch,
        "base_branch": base, "source_branch": source,
        "pr_number": pr.number, "pr_url": pr.html_url,
        "note": (f"UAT→PRD PR opened; CHG {chg} / RMG {rmg} auto-created from the change request "
                 "and posted as PR comments." if env == "prod"
                 else f"PR opened into '{uat_branch}'. Review and merge to apply."),
    }
    if env == "prod":
        result["change_request_template"] = cr_path
        result["chg"], result["rmg"] = chg, rmg
    return json.dumps(result, indent=2)


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
    check_release_window,
]
