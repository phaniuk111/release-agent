"""Multi-agent free-form lane: a supervisor delegates read-mostly chat queries to
specialized sub-agents, each scoped to a NARROW toolset (LangGraph supervisor
pattern, built on ``create_react_agent``).

Why this exists
---------------
The promote/apply path is deterministic (parse → propose → HITL gate → apply) and
the LLM is never on it. Free-form questions ("which PR has my CHG?", "verify this
tag", "remove my image") used to fall into a single ReAct loop bound to the ENTIRE
tool set — including the high-impact mutations apply_json_update, dispatch_workflow
and open_release_pr. A confused or prompt-injected model could therefore trigger a
production release from a chat question.

This module replaces that one loop with a supervisor + five specialists, each a
``langchain.agents.create_agent`` (the LangGraph V1 prebuilt agent):

    supervisor ─┬─▶ status_agent     (release window / catalog / manifest / runs)   READ-ONLY
                ├─▶ pr_agent          (find + track deploy PRs, CHG/RMG/RLFT)        READ-ONLY
                ├─▶ controls_agent    (verify a build, report RLFT/RFTL controls)    READ-ONLY
                ├─▶ ops_agent         (remove/unstage an image, retrigger a deploy)  SCOPED MUTATE
                └─▶ general_agent     (mixed / ambiguous read-only)                  READ-ONLY

The three release-defining mutations are bound to NONE of these agents — they remain
exclusively in the deterministic pipeline behind the human-confirmation gate. The
only mutating tools reachable from free-form chat are remove_from_release and
retrigger_deployment_workflow, isolated in the single ``ops_agent`` (and both were
already user-initiated chat actions, so behaviour is preserved, not widened).
"""

from __future__ import annotations

from typing import Literal

from langchain.agents import create_agent
from langchain.agents.middleware import after_model
from pydantic import BaseModel, Field

from .budget import get_budget_tracker
from .tools.gh_tools import (
    BUILD_REPO,
    DEPLOY_REPO,
    # read-only
    check_release_window,
    list_allowed_images,
    get_recent_runs,
    get_workflow_status,
    find_prs,
    get_pr_details,
    get_pr_comments,
    summarize_pr_controls,
    verify_image_tag_build,
    get_build_controls,
    # scoped mutations (ops only)
    remove_from_release,
    retrigger_deployment_workflow,
)

# --- Scoped tool sets -------------------------------------------------------
# Each specialist sees only what its job needs. Disjoint where it matters: the
# three release-defining mutations (apply_json_update, dispatch_workflow,
# open_release_pr) appear in NONE of these lists.
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
    find_prs,
    get_pr_details,
]
# General fallback: the read-only union (no mutations), de-duplicated by identity.
GENERAL_TOOLS = list(
    {id(t): t for t in (STATUS_TOOLS + PR_TOOLS + CONTROLS_TOOLS)}.values()
)


# --- Shared prompt fragments ------------------------------------------------
_GLOSSARY = """Deployment governance glossary (these appear in deployment-repo PR comments):
- CHG = a Change ticket authorizing the change (id like CHG-<yyyymm>-<digits>).
- RMG = a Release Management ticket/approval (id like RMG-<yyyymm>-<digits>).
- RLFT / RFTL = release control gates that must pass/close before a prod deploy.
CRITICAL — never fabricate ticket numbers, PR numbers, or gate states; the real values live ONLY in
the PR comments and build runs, NOT in these instructions. Call the tools and report EXACTLY what you
find. If you cannot find the PR/comments/run, say so plainly."""

_FOOTER = (
    f"Be concise and precise; always show the GitHub URLs you produce. "
    f"Build/source repo: {BUILD_REPO}. Deployment / PR repo: {DEPLOY_REPO}."
)

STATUS_PROMPT = f"""You are the Deploy Status specialist for Release Copilot (READ-ONLY).
The source of truth for what is deployed is the env-pathed deployment JSON in the deploy repo
(uat/deployment.json and prd/deployment.json) — read it with check_release_window. ALWAYS use
check_release_window to answer "what's deployed to UAT/PROD", "which charts/versions", or "what's
pending to prod"; it returns the charts on UAT vs PRD and the diff. Do NOT use any release-manifest tool.
You also report the allowed image catalog (list_allowed_images) and recent workflow runs / a run's
status (get_recent_runs / get_workflow_status).
You CANNOT change anything: no deploy, remove, or merge. If the user wants to act, tell them to phrase it
as a "deploy ..." or "remove ..." request. {_FOOTER}"""

PR_PROMPT = f"""You are the PR Tracking specialist for Release Copilot (READ-ONLY).
Locate deployment-repo PRs with find_prs (NEVER ask the user for a PR number — derive the image:tag
from the conversation and search). Read comments with get_pr_comments and summarize the CHG/RMG tickets
and RLFT control gates with summarize_pr_controls. If several PRs match, default to the newest (highest
number) and mention the others so the user can pick. Report exactly the ticket numbers and gate states
found in the comments.
{_GLOSSARY}
You CANNOT promote, remove, or dispatch anything. {_FOOTER}"""

CONTROLS_PROMPT = f"""You are the Build Controls specialist for Release Copilot (READ-ONLY).
For an image:tag, verify it was actually built with verify_image_tag_build, and report the release
CONTROLS (RLFT/RFTL gates) recorded in that tag's build pipeline run with get_build_controls. Report
each control as PASSED or FAILED (e.g. "RFTL0001: FAILED, RFTL0002: PASSED"). If the build run cannot
be located from image+tag (need_run_id), ASK the developer for the GitHub Actions run id that generated
the tag, then call get_build_controls(run_id=<id>).
{_GLOSSARY}
You only verify and report — you CANNOT promote or stage an image. If controls FAILED, tell the
developer they must be resolved and the build re-run before any PRD release. {_FOOTER}"""

OPS_PROMPT = f"""You are the Release Ops specialist for Release Copilot. You perform exactly TWO scoped
actions and nothing else:
1. REMOVE / UNSTAGE an image from today's release: call remove_from_release(image_names="<name>[,<name>...]").
   Like an add, it goes through the protected-branch PR chain (a PR into SIT dropping the image, then
   SIT → UAT), both merged so the removal reaches UAT. Each image is reverted to PRD's current tag (or
   dropped if new). Report the PR links.
2. RETRIGGER a deployment workflow after controls are closed: retrigger_deployment_workflow. Use find_prs
   / get_pr_details to locate the target PR if you only have an image:tag.
You CANNOT add or promote images, stage onto UAT, or raise the UAT → PRD release PR — those go through
the confirmed promote flow. If asked to do those, say so and tell the user to use a "promote ..." request.
{_FOOTER}"""

GENERAL_PROMPT = f"""You are Release Copilot's general assistant for READ-ONLY questions. For what is
deployed to UAT/PROD (charts, versions, what's pending to prod) the source of truth is the deployment
JSON — use check_release_window (never a release-manifest tool). You can also look up the allowed image
catalog, deployment PRs and their CHG/RMG tickets and RLFT control gates, recent workflow runs, and verify
image builds. Choose the right tool and report exactly what the tools return.
{_GLOSSARY}
You do NOT perform mutations — no deploy, remove, or merge. If the user wants to deploy or remove a chart,
tell them to phrase it as a "deploy ..." or "remove ..." request. {_FOOTER}"""


# --- Supervisor routing -----------------------------------------------------
class Route(BaseModel):
    """Structured routing decision emitted by the supervisor LLM."""

    route: Literal["status", "pr", "controls", "ops", "general"] = Field(
        description="Which specialist should handle this request."
    )


SUPERVISOR_PROMPT = """You are the supervisor of Release Copilot's specialist agents. Route the user's
latest message to the SINGLE best specialist:
- status   → today's release window / cutoff / lead-time, the allowed image catalog, the current
             manifest, or recent workflow runs.
- pr       → find or track a deployment PR, read its comments, or summarize CHG/RMG tickets and RLFT
             control gates.
- controls → verify that an image:tag was built, or report the RLFT/RFTL build-pipeline controls for it.
- ops      → REMOVE / unstage an image from today's release, or RETRIGGER a deployment workflow.
- general  → anything else, mixed, or ambiguous read-only questions.
Pick exactly one."""

# Specialist label → graph node name (nodes are registered under these names in agent.build_graph).
ROUTE_TO_NODE = {
    "status": "status_agent",
    "pr": "pr_agent",
    "controls": "controls_agent",
    "ops": "ops_agent",
    "general": "general_agent",
}


@after_model
def _record_usage(state, runtime):
    """after_model middleware for every specialist: fold each model call's token
    usage into the shared budget tracker so multi-agent turns stay cost-accounted."""
    messages = state["messages"] if isinstance(state, dict) else getattr(state, "messages", [])
    last = messages[-1] if messages else None
    usage = getattr(last, "usage_metadata", None) or {}
    input_t = usage.get("input_tokens", 0) or 0
    output_t = usage.get("output_tokens", 0) or 0
    if input_t or output_t:
        try:
            get_budget_tracker().add_usage(input_t, output_t)
        except Exception:
            pass
    return None


_SPECS = [
    ("status_agent", STATUS_TOOLS, STATUS_PROMPT),
    ("pr_agent", PR_TOOLS, PR_PROMPT),
    ("controls_agent", CONTROLS_TOOLS, CONTROLS_PROMPT),
    ("ops_agent", OPS_TOOLS, OPS_PROMPT),
    ("general_agent", GENERAL_TOOLS, GENERAL_PROMPT),
]


def build_specialists(model):
    """Compile the five scoped specialist sub-agents (READ-ONLY except ops).

    Returns {node_name: compiled_agent}. Each is a ``create_agent`` graph over the
    standard messages state, so it slots straight into the parent ReleaseState graph
    as a node (both share the ``messages`` channel). The usage middleware keeps every
    specialist model call inside the shared budget accounting.
    """
    return {
        name: create_agent(
            model,
            tools,
            system_prompt=prompt,
            middleware=[_record_usage],
            name=name,
        )
        for name, tools, prompt in _SPECS
    }
