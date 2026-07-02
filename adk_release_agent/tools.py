"""ADK-friendly function tools over the existing Release Copilot GitHub tools.

The existing repo tool layer is still the source of truth for GitHub reads and
mutations. These wrappers give ADK plain Python functions with typed signatures
and dictionary returns, which ADK can expose as Function Tools.
"""
from __future__ import annotations

import json
from typing import Any

from release_agent.tools import gh_tools


def _coerce_tool_result(result: Any) -> dict[str, Any]:
    """Return a dictionary for ADK, preserving structured JSON tool results."""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return {"result": result}
        return parsed if isinstance(parsed, dict) else {"result": parsed}
    return {"result": result}


def _invoke_tool(tool_name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    tool = getattr(gh_tools, tool_name)
    payload = args or {}
    if hasattr(tool, "invoke"):
        return _coerce_tool_result(tool.invoke(payload))
    return _coerce_tool_result(tool(**payload))


def check_release_window() -> dict[str, Any]:
    """Read live UAT/PRD deployment state and today's PRD release window."""
    return _invoke_tool("check_release_window")


def list_allowed_images() -> dict[str, Any]:
    """List the allowed image/chart catalog from the configured build repo."""
    return _invoke_tool("list_allowed_images")


def get_recent_runs(limit: int = 5) -> dict[str, Any]:
    """List recent release-related GitHub Actions runs."""
    return _invoke_tool("get_recent_runs", {"limit": limit})


def get_workflow_status(run_id: str) -> dict[str, Any]:
    """Get status and summary information for a GitHub Actions run id."""
    return _invoke_tool("get_workflow_status", {"run_id": run_id})


def find_prs(search_term: str = "", limit: int = 5) -> dict[str, Any]:
    """Find deployment PRs matching an image, tag, ticket, branch, or text query."""
    return _invoke_tool("find_prs", {"search_term": search_term, "limit": limit})


def get_pr_details(pr_number: int) -> dict[str, Any]:
    """Read details for a deployment PR number."""
    return _invoke_tool("get_pr_details", {"pr_number": pr_number})


def get_pr_comments(pr_number: int, limit: int = 30) -> dict[str, Any]:
    """Read recent comments from a deployment PR."""
    return _invoke_tool("get_pr_comments", {"pr_number": pr_number, "limit": limit})


def summarize_pr_controls(pr_number: int) -> dict[str, Any]:
    """Summarize CHG/RMG tickets and RLFT/RFTL control status from PR comments."""
    return _invoke_tool("summarize_pr_controls", {"pr_number": pr_number})


def verify_image_tag_build(image: str, tag: str, repo: str = "") -> dict[str, Any]:
    """Verify whether an image tag can be traced to a build workflow run."""
    return _invoke_tool("verify_image_tag_build", {"image": image, "tag": tag, "repo": repo})


def get_build_controls(
    image: str = "", tag: str = "", repo: str = "", run_id: int = 0
) -> dict[str, Any]:
    """Read RLFT/RFTL build controls for an image tag or explicit workflow run id."""
    return _invoke_tool(
        "get_build_controls",
        {"image": image, "tag": tag, "repo": repo, "run_id": run_id},
    )


def remove_from_release(
    image_names: str, environment: str = "staging", deployment_repo: str = ""
) -> dict[str, Any]:
    """Unstage chart names from today's PRD release PR (environment='staging', the
    default) or remove them from a live environment ('uat' or 'prod' — only when the
    user explicitly names it). deployment_repo (owner/repo) targets a non-default
    deployment repo — pass it only when the user names one."""
    return _invoke_tool(
        "remove_from_release",
        {"image_names": image_names, "environment": environment, "deployment_repo": deployment_repo},
    )


def retrigger_deployment_workflow(
    pr_number: int, simulate_closed_controls: str = ""
) -> dict[str, Any]:
    """Retrigger deployment workflow for an existing deployment PR."""
    return _invoke_tool(
        "retrigger_deployment_workflow",
        {"pr_number": pr_number, "simulate_closed_controls": simulate_closed_controls},
    )


def merge_prod_release(deployment_repo: str = "") -> dict[str, Any]:
    """Promote today's PRD release after the configured cutoff, if eligible.
    deployment_repo (owner/repo) targets a non-default deployment repo — pass it
    only when the user names one (e.g. the repo their deploy was staged in)."""
    return _invoke_tool("merge_prod_release", {"deployment_repo": deployment_repo})


STATUS_TOOLS = [
    check_release_window,
    list_allowed_images,
    get_recent_runs,
    get_workflow_status,
]

PR_TOOLS = [
    find_prs,
    get_pr_details,
    get_pr_comments,
    summarize_pr_controls,
    get_recent_runs,
    get_workflow_status,
]

CONTROLS_TOOLS = [
    verify_image_tag_build,
    get_build_controls,
    get_recent_runs,
]

OPS_TOOLS = [
    remove_from_release,
    retrigger_deployment_workflow,
    merge_prod_release,
    find_prs,
    get_pr_details,
]

ADK_CHAT_TOOLS = list(
    {id(tool): tool for tool in (STATUS_TOOLS + PR_TOOLS + CONTROLS_TOOLS + OPS_TOOLS)}.values()
)

# These remain in the deterministic confirmed path, not the ADK free-form toolset.
RELEASE_DEFINING_MUTATIONS = {
    "apply_json_update",
    "dispatch_workflow",
    "open_release_pr",
}

