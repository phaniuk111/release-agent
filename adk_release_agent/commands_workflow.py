"""The chat's fixed commands as an ADK Workflow graph (LLM_ENABLED=false).

With no model, the chat answers a fixed set of commands (release_agent/commands:
what they are, and how their results read). Running them is this graph's job, so
every pause and resume in the portal stays in ADK — the same machinery as the
deploy Workflow, not a second mechanism beside it:

    START ─▶ command_gate ─┬─(done)─────▶ finish_command
                           ├─(approved)─▶ run_command
                           └─(rejected)─▶ reject_command

``command_gate`` recognises the command. One that needs a person's yes/no — a
promotion to PRD/PRL1, a removal from production, by the SAME rules the chat
agent uses (approvals.py) — pauses on an ADK ``RequestInput``; everything else
runs at once. The tools are the chat agent's own (tools.py), so turning the
model back on changes who chooses the tool, never what the tool does.

As in the deploy Workflow: no node raises (ADK 2.9 re-runs a failed node on
resume, repeating its side effects), blocking work runs on a worker thread, and
the pending command lives in session state so any replica can resume it.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict

from . import approvals

try:
    from google.adk.apps import App, ResumabilityConfig
    from google.adk.events.event import Event
    from google.adk.events.request_input import RequestInput
    from google.adk.workflow import FunctionNode, Workflow

    _ADK_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised only without google-adk
    _ADK_AVAILABLE = False

COMMANDS_APP_NAME = "release_commands_workflow"

# The approval the gate paused on, and the command it is for — in session state
# so the answer can be applied by a process that never served the question.
# FunctionNode binds a node parameter of the pending key's name from ctx.state.
_APPROVAL_STATE_KEY = "command_approval_id"
_PENDING_STATE_KEY = "command_pending"

# Tools that change a release or an environment: the page re-reads the banner.
MUTATING = {"promote_release", "promote_df_release", "remove_from_release"}


class CommandOutcome(BaseModel):
    """Terminal output of the commands Workflow."""

    model_config = ConfigDict(extra="allow")

    ok: bool = True
    text: str = ""
    mutated: bool = False


def _run(command) -> str:
    from release_agent import commands

    return commands.run(command)


async def _command_gate(ctx: Any, node_input: str):
    """First pass: recognise and either answer or pause. Resumed pass: route the
    person's yes/no. Never raises. ``str``, not ``Any``: FunctionNode hands the
    user's message over as the annotated type — untyped it is the Content object."""
    from release_agent import commands

    if not ctx.resume_inputs:
        try:
            command = commands.parse(str(node_input or ""))
            if command is None:
                yield Event(output={"ok": True, "text": commands.HELP}, route="done")
                return
            if not approvals.needs_approval(command.name, command.args):
                text = await asyncio.to_thread(_run, command)
                yield Event(output={"ok": True, "text": text, "mutated": command.name in MUTATING},
                            route="done")
                return
            approval_id = f"APPROVE-{uuid.uuid4().hex[:8].upper()}"
            yield Event(state={_APPROVAL_STATE_KEY: approval_id, _PENDING_STATE_KEY: {
                "name": command.name, "args": command.args, "label": command.label}})
            yield RequestInput(interrupt_id=approval_id, message=f"{command.label}?")
        except Exception as e:  # noqa: BLE001 — an answer, never a re-run
            yield Event(output={"ok": False, "text": f"That command could not run: {e}"}, route="done")
        return

    approval_id = ctx.state.get(_APPROVAL_STATE_KEY) or next(iter(ctx.resume_inputs))
    reply = ctx.resume_inputs.get(approval_id)
    approved = bool(isinstance(reply, dict) and reply.get("confirmed"))
    yield Event(output={"approval_id": approval_id, "approved": approved},
                route="approved" if approved else "rejected")


async def _finish_command(node_input: dict[str, Any]):
    yield Event(output=CommandOutcome(**(node_input or {})).model_dump())


async def _run_command(node_input: dict[str, Any], command_pending: dict | None = None):
    """Run the approved command. The approval is cleared from state BEFORE the
    tool runs: a yes is single-use, like a CONFIRM token."""
    from release_agent import commands

    yield Event(state={_APPROVAL_STATE_KEY: None, _PENDING_STATE_KEY: None})
    pending = command_pending or {}
    if not pending.get("name"):
        yield Event(output=CommandOutcome(ok=False, text="That approval is no longer waiting — nothing "
                                                         "was done. Ask again.").model_dump())
        return
    command = commands.Command(pending["name"], dict(pending.get("args") or {}), pending.get("label") or "")
    try:
        text = await asyncio.to_thread(_run, command)
    except Exception as e:  # noqa: BLE001
        text = f"**{command.label}** — not done: {e}"
    yield Event(output=CommandOutcome(text=text, mutated=command.name in MUTATING).model_dump())


async def _reject_command(node_input: dict[str, Any], command_pending: dict | None = None):
    label = (command_pending or {}).get("label") or "The operation"
    yield Event(
        output=CommandOutcome(text=f"You rejected it — **{label}** was not done. "
                                   "Ask again when you're ready.").model_dump(),
        state={_APPROVAL_STATE_KEY: None, _PENDING_STATE_KEY: None},
    )


def build_commands_workflow() -> "Workflow":
    if not _ADK_AVAILABLE:
        raise RuntimeError("google-adk is not installed; cannot build the commands workflow")
    gate = FunctionNode(func=_command_gate, rerun_on_resume=True, name="command_gate")
    return Workflow(
        name="release_commands",
        edges=[("START", gate, {
            "done": FunctionNode(func=_finish_command, name="finish_command"),
            "approved": FunctionNode(func=_run_command, name="run_command"),
            "rejected": FunctionNode(func=_reject_command, name="reject_command"),
        })],
        output_schema=CommandOutcome,
    )


def build_commands_app() -> "App":
    """The commands Workflow in a resumable App — the pause survives in session state."""
    if not _ADK_AVAILABLE:
        raise RuntimeError("google-adk is not installed; cannot build the commands app")
    return App(
        name=COMMANDS_APP_NAME,
        root_agent=build_commands_workflow(),
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
