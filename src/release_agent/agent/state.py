"""Shared graph state + the re-runnable step vocabulary."""
from __future__ import annotations

from typing import Annotated, List, Optional

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field

from ..tools.gh_tools import BUILD_REPO, DEPLOY_REPO


class ReleaseState(BaseModel):
    """Shared state for the release agent using Pydantic for structure and validation."""

    model_config = {"arbitrary_types_allowed": True}
    messages: Annotated[List[AnyMessage], add_messages] = Field(default_factory=list)
    release_request: Optional[dict] = None
    proposed: Optional[dict] = None
    confirmation_token: Optional[str] = None
    last_action: Optional[dict] = None
    # Per-step status of the last apply phase (for reporting + named re-run).
    steps: Optional[List[dict]] = None
    # Set by parse when the user asks to re-run step(s); routes to the rerun node.
    rerun_steps: Optional[List[str]] = None
    repo: str = Field(default=BUILD_REPO)
    deploy_repo: str = Field(default=DEPLOY_REPO)


# ---- Re-runnable apply-phase steps -----------------------------------------
STEP_APPLY = "apply_manifest"
STEP_DISPATCH = "dispatch_workflow"
STEP_RELEASE_PR = "release_pr"  # env promote: update env config JSON + open a PR
ALL_STEPS = [STEP_APPLY, STEP_DISPATCH]

# Steps used for an environment (uat/prod) promote vs. the legacy dispatch path.
_ENV_STEPS = [STEP_RELEASE_PR]

# Map the underlying tool name -> canonical step label (used to attribute each
# ToolMessage back to a step via its tool_call).
_STEP_BY_TOOL = {
    "apply_json_update": STEP_APPLY,
    "dispatch_workflow": STEP_DISPATCH,
    "open_release_pr": STEP_RELEASE_PR,
}

# User-facing words that select a step in a "re-run ..." request.
_STEP_ALIASES = {
    STEP_APPLY: ["apply_manifest", "apply-manifest", "apply", "commit", "manifest"],
    STEP_DISPATCH: ["dispatch_workflow", "dispatch-workflow", "dispatch", "workflow", "trigger"],
    STEP_RELEASE_PR: ["release_pr", "release-pr", "pr", "open_pr", "open-pr", "raise_pr"],
}
