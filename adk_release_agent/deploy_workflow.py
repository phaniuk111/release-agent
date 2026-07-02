"""Deterministic deploy path expressed as an ADK Workflow graph.

The release-defining deploy flow is intentionally NOT LLM-driven. Previously it
lived as imperative branching inside the FastAPI chat adapter. Here it is a real
ADK ``Workflow`` graph (``google.adk.workflow``) so the preview -> confirm ->
apply sequence is declarative, testable, and enforced by the graph topology:

    START ─▶ deploy_gate ─┬─(confirmed)─▶ apply_deploy
                          └─(rejected)──▶ cancel_deploy

``deploy_gate`` is a human-in-the-loop node: on the first pass it builds the exact
deployment-JSON preview (minting a ``CONFIRM-xxxxxx`` token) and pauses on an ADK
``RequestInput`` interrupt. When the run is resumed with the user's confirmation,
the node re-runs (``rerun_on_resume=True``) and routes to ``apply_deploy`` or
``cancel_deploy``. All three nodes are plain Python functions that reuse the
existing :mod:`adk_release_agent.deploy` helpers, so the graph runs without an LLM
and is fully unit-testable through ``InMemoryRunner``.
"""
from __future__ import annotations

import json
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from . import deploy

try:
    from google.adk.apps import App, ResumabilityConfig
    from google.adk.events.event import Event
    from google.adk.events.request_input import RequestInput
    from google.adk.workflow import FunctionNode, Workflow
    from google.genai import types

    _ADK_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised only without google-adk
    _ADK_AVAILABLE = False


class DeployOutcome(BaseModel):
    """Typed terminal output of the deploy Workflow (used as its ``output_schema``).

    ``extra="allow"`` preserves the arbitrary fields the underlying
    ``open_release_pr`` tool returns (PR url, number, etc.) while giving the known
    fields real types for downstream formatting.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    status: str = ""
    token: str = ""
    note: Optional[str] = None
    confirmed_token: Optional[str] = None
    error: Optional[str] = None


# State key used to carry the minted confirmation token from the first (preview)
# pass of the gate node to its resumed (decision) pass.
_TOKEN_STATE_KEY = "deploy_confirm_token"

# App name for the standalone deploy Workflow runner.
DEPLOY_APP_NAME = "release_deploy_workflow"


def _change_request_preview(change_request: Any) -> str:
    """Compact change-request block for the prod deploy preview (empty if none)."""
    if not isinstance(change_request, dict) or not change_request:
        return ""
    cr = change_request
    summary = cr.get("chg_summary") or cr.get("summary") or ""
    description = cr.get("description") or cr.get("change_description") or ""
    start = cr.get("start_date") or cr.get("start_time") or "?"
    end = cr.get("end_date") or cr.get("end_time") or "?"
    return (
        "\n\n**Change request** (→ change-request.json):\n"
        f"- Summary: {summary or '(none)'}\n"
        f"- Window: {start} → {end}\n"
        f"- Description: {description or '(none)'}"
    )


def _preview_text(
    preview: dict[str, Any], token: str, env: str, image_tags: str, change_request: Any = None,
    deployment_repo: str = "",
) -> str:
    """Human-readable preview shown to the user before confirmation."""
    repo_line = f"\n\n**Deployment repo:** `{deployment_repo}`" if deployment_repo else ""
    return (
        f"**Deploy {image_tags} to {str(env).upper()}**\n\n"
        "```json\n" + json.dumps(preview, indent=2) + "\n```"
        + repo_line
        + _change_request_preview(change_request)
        + f"\n\nReply `{token}` to confirm."
    )


async def _deploy_gate(ctx: Any, node_input: str):
    """HITL gate: preview + request confirmation, then route on resume.

    First pass (no ``resume_inputs``): build the preview, emit the preview text,
    and pause on a ``RequestInput`` keyed by the confirmation token.
    Resumed pass: decide ``confirmed`` vs ``rejected`` from the reply payload.
    """
    if not ctx.resume_inputs:
        result = deploy.prepare_deploy_preview(message=node_input)
        if not result.get("ok"):
            # Nothing previewable — route to cancel carrying the error.
            yield Event(
                output={"ok": False, "error": result.get("error"), "token": ""},
                route="rejected",
            )
            return

        token = result["token"]
        text = _preview_text(
            result["proposed"],
            token,
            result["environment"],
            result["image_tags"],
            result.get("change_request"),
            result.get("deployment_repo") or "",
        )
        yield Event(
            content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
            state={_TOKEN_STATE_KEY: token},
        )
        yield RequestInput(
            interrupt_id=token,
            message=f"Reply with exactly {token} to apply this deploy.",
        )
        return

    # Resumed: recover the token (prefer state; fall back to the sole interrupt id).
    token = ctx.state.get(_TOKEN_STATE_KEY)
    if not token and ctx.resume_inputs:
        token = next(iter(ctx.resume_inputs))
    reply = ctx.resume_inputs.get(token) if token else None
    confirmed = bool(isinstance(reply, dict) and reply.get("confirmed"))
    yield Event(
        output={"token": token or "", "confirmed": confirmed},
        route="confirmed" if confirmed else "rejected",
    )


def _apply_deploy(node_input: dict[str, Any]) -> "DeployOutcome":
    """Apply the confirmed deploy via the existing token-gated apply helper."""
    token = (node_input or {}).get("token") or ""
    return DeployOutcome(**deploy.apply_confirmed_deploy(token))


def _cancel_deploy(node_input: dict[str, Any]) -> "DeployOutcome":
    """Discard the pending preview and report a non-mutating cancellation."""
    payload = node_input or {}
    token = payload.get("token") or ""
    if token:
        deploy._PENDING_PREVIEWS.pop(token, None)
    return DeployOutcome(
        ok=False, status="cancelled", token=token, error=payload.get("error")
    )


def build_deploy_workflow() -> "Workflow":
    """Build the deploy Workflow graph. Requires google-adk to be installed."""
    if not _ADK_AVAILABLE:
        raise RuntimeError("google-adk is not installed; cannot build deploy workflow")

    gate = FunctionNode(func=_deploy_gate, rerun_on_resume=True, name="deploy_gate")
    apply_node = FunctionNode(func=_apply_deploy, name="apply_deploy")
    cancel_node = FunctionNode(func=_cancel_deploy, name="cancel_deploy")

    return Workflow(
        name="release_deploy",
        edges=[("START", gate, {"confirmed": apply_node, "rejected": cancel_node})],
        output_schema=DeployOutcome,
    )


def build_deploy_app() -> "App":
    """Wrap the deploy Workflow in a resumable ``App`` for the HITL interrupt."""
    if not _ADK_AVAILABLE:
        raise RuntimeError("google-adk is not installed; cannot build deploy app")

    return App(
        name=DEPLOY_APP_NAME,
        root_agent=build_deploy_workflow(),
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
