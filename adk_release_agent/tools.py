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


def _build_repo_for(repo: str, dataflow: bool) -> str:
    """Explicit repo wins; else Dataflow images resolve to DF_BUILD_REPO (their
    builds live in a separate repo from the GKE services'); else config default."""
    if repo:
        return repo
    if dataflow:
        from release_agent.config import settings

        return settings.df_build_repo
    return ""


def verify_image_tag_build(
    image: str, tag: str, repo: str = "", dataflow: bool = False
) -> dict[str, Any]:
    """Verify whether an image tag can be traced to a build workflow run.
    Set dataflow=true for Dataflow images — they are built in a different repo
    (DF_BUILD_REPO) than the GKE services."""
    return _invoke_tool(
        "verify_image_tag_build",
        {"image": image, "tag": tag, "repo": _build_repo_for(repo, dataflow)},
    )


def get_build_controls(
    image: str = "", tag: str = "", repo: str = "", run_id: int = 0, dataflow: bool = False
) -> dict[str, Any]:
    """Read release build controls for an image tag or explicit workflow run id.
    Set dataflow=true for Dataflow images (built in DF_BUILD_REPO)."""
    return _invoke_tool(
        "get_build_controls",
        {"image": image, "tag": tag, "repo": _build_repo_for(repo, dataflow), "run_id": run_id},
    )


def get_build_report(
    image: str = "", tag: str = "", workflow_url: str = "", repo: str = "", dataflow: bool = False
) -> dict[str, Any]:
    """Full build diagnosis for an image:tag OR a GitHub Actions run URL: which
    STEPS failed, which controls (RCTLDEF…/RLFT) passed or
    failed (gate verdict), and whether the tag was built from the default
    branch. Use when a developer asks WHAT failed in their build/run and what
    to fix. A workflow_url carries its own repo; for image+tag lookups of
    Dataflow images set dataflow=true (they build in DF_BUILD_REPO)."""
    return _invoke_tool(
        "get_build_report",
        {"image": image, "tag": tag, "workflow_url": workflow_url,
         "repo": _build_repo_for(repo, dataflow)},
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


def promote_release(
    target: str, release_branch: str = "", deployment_repo: str = ""
) -> dict[str, Any]:
    """Promote the current release's file-set to the next environment branch
    (uat, prd or prl1). Copies the release's changed files verbatim via a
    change-branch PR — use after a release has been created. deployment_repo
    (owner/repo) targets a non-default deployment repo — pass it only when the
    user names one."""
    return _invoke_tool(
        "promote_release",
        {"target": target, "release_branch": release_branch, "deployment_repo": deployment_repo},
    )


def queue_release_intent(
    artifact: str,
    requested_by: str,
    prl1_only: bool = False,
    df_only: bool = False,
    note: str = "",
    jira_ticket: str = "",
    change_details: str = "",
    build_run_url: str = "",
) -> dict[str, Any]:
    """Register an artifact for the NEXT release (the intake queue): chart:version,
    the requester's email, PRL1-only / Dataflow-only routing, optional note for
    DevOps, the change context — jira_ticket (e.g. REL-1234) and change_details
    (what changed and why) — and build_run_url, the GitHub Actions run that
    built the tag. build_run_url is REQUIRED: nothing is queued without it —
    the run is checked NOW, and a failed build or failed RLFT/RFTL control
    makes the chart INELIGIBLE (eligible=false with failed_controls and
    failed_steps listed) so the dev fixes and re-runs first. A clean run
    queues as eligible (build_verified=true). Re-queuing a chart replaces
    its version."""
    from release_agent.tools import release_queue as _rq

    name, version = _rq._split_artifact(artifact)
    verified: bool | None = None
    warnings: list[str] = []
    run_url = str(build_run_url or "").strip()
    if not run_url:
        return {
            "ok": False,
            "error": (
                "The GitHub Actions run URL that built this tag is required — "
                "I check the build and RLFT/RFTL controls before anything is "
                "queued. Ask the developer for the run URL (…/actions/runs/<id>)."
            ),
        }
    # Eligibility gate: the dev pointed at the exact run — judge it.
    try:
        report = _invoke_tool("get_build_report", {"workflow_url": run_url})
    except Exception as e:
        report = {"found": False, "reason": str(e)}
    if not report.get("found"):
        return {
            "ok": False,
            "error": (
                f"Could not inspect that run ({report.get('reason')}). "
                "Check the URL — it must be a GitHub Actions run "
                "(…/actions/runs/<id>) in the build repo. Nothing was queued."
            ),
        }
    failed_controls = [c.get("control") for c in report.get("controls") or [] if c.get("failed")]
    failed_steps = report.get("failed_steps") or []
    if failed_controls or not report.get("run_succeeded"):
        return {
            "ok": False,
            "eligible": False,
            "artifact": f"{name}:{version}",
            "run_url": (report.get("run") or {}).get("url") or run_url,
            "run_conclusion": (report.get("run") or {}).get("conclusion"),
            "failed_controls": failed_controls,
            "failed_steps": failed_steps,
            "gate": report.get("gate"),
            "reason": (
                "This build is NOT eligible for the release — fix the failures, "
                "re-run the build, then queue again with the new run."
            ),
        }
    verified = report.get("gate") == "PASS"
    if report.get("gate") == "UNKNOWN":
        warnings.append("Run succeeded but no RLFT/RFTL control steps were found in it.")
    run_tag = str(report.get("tag") or "")
    if version and run_tag and version not in run_tag and name not in run_tag:
        warnings.append(
            f"The run built '{run_tag}', which doesn't obviously match {name}:{version} — double-check the URL."
        )
    result = _rq.add_intent(
        artifact=artifact,
        requested_by=requested_by,
        prl1_only=prl1_only,
        df_only=df_only,
        note=note,
        build_verified=verified,
        jira_ticket=jira_ticket,
        change_details=change_details,
        build_run_url=run_url,
    )
    if result.get("ok"):
        result["eligible"] = True if verified else None
        if warnings:
            result["warnings"] = warnings
    if result.get("ok"):
        result["build_verified"] = verified
        try:
            events = _rq._fetch_events()
            result["last_shipped"] = _rq.last_shipped(events, name)
            result["last_time_flags"] = _rq.last_queued_flags(events, name)
        except Exception:
            pass
    return result


def withdraw_release_intent(artifact_name: str, requested_by: str = "") -> dict[str, Any]:
    """Withdraw a chart from the next-release intake queue (e.g. 'remove my
    risk-fetcher from the queue'). Only touches the queue — never a live
    environment or an open release."""
    from release_agent.tools import release_queue as _rq

    return _rq.withdraw_intent(artifact_name, requested_by)


def list_release_queue() -> dict[str, Any]:
    """List everything queued for the NEXT release: chart, version, requester,
    when, PRL1-only/DF flags, note, and whether the build was verified at queue
    time. This is what DevOps reviews before creating the release."""
    from release_agent.tools import release_queue as _rq

    return _rq.current_queue()


def release_stats(pattern: str = "", days: int = 90, event_type: str = "released") -> dict[str, Any]:
    """Stats over the release/deployment history event log. Answers questions
    like 'which images were released this month?' or 'how many acme-capability*
    images were released?'. pattern: empty = all charts; supports * globs
    (acme-capability*) or a plain substring (capability). event_type:
    'released' (shipped in a release), 'deployed' (UAT/PRD/PRL1/DF deploys and
    promotions), 'queued', 'all' — or 'state' for the CURRENT per-environment
    deployed picture ('how many capability images are on uat/prd/prl1?'):
    latest deployed/removed event per artifact per environment wins, returning
    each environment's distinct image count and list."""
    from release_agent.tools import release_queue as _rq

    return _rq.history_stats(pattern=pattern, days=days, event_type=event_type)


def merge_prod_release(deployment_repo: str = "") -> dict[str, Any]:
    """Release today's staged PRD release now (any time). Releasing finalizes it —
    no new charts can be added afterwards; later prod deploys start a new release.
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
    get_build_report,
    get_recent_runs,
]

OPS_TOOLS = [
    remove_from_release,
    retrigger_deployment_workflow,
    merge_prod_release,
    promote_release,
    find_prs,
    get_pr_details,
]

# Next-release intake queue (BigQuery-backed) — conversational adds/withdraws/list,
# plus stats over the release/deploy history event log.
QUEUE_TOOLS = [
    queue_release_intent,
    withdraw_release_intent,
    list_release_queue,
    release_stats,
    verify_image_tag_build,
    list_allowed_images,
]

ADK_CHAT_TOOLS = list(
    {
        id(tool): tool
        for tool in (STATUS_TOOLS + PR_TOOLS + CONTROLS_TOOLS + OPS_TOOLS + QUEUE_TOOLS)
    }.values()
)

# These remain in the deterministic confirmed path, not the ADK free-form toolset.
RELEASE_DEFINING_MUTATIONS = {
    "apply_json_update",
    "dispatch_workflow",
    "open_release_pr",
}

