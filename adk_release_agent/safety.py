"""App-level safety guardrails for the ADK release copilot.

The free-form chat agent is only ever wired with read-only and tightly-scoped
ops tools. Release-*defining* mutations (opening a release PR, writing deployment
JSON, dispatching arbitrary workflows) must only happen through the deterministic,
confirmation-gated deploy Workflow — never as a side effect of a free-form chat
turn.

``MutationGuardPlugin`` enforces that boundary in code rather than trusting the
model to obey the system instruction. Registered on the ``App``, its
``before_tool_callback`` runs before every tool the chat agent invokes; if the
tool is a blocked release-defining mutation, it short-circuits the call by
returning an error dict (the tool never executes).
"""
from __future__ import annotations

from typing import Any

try:
    from google.adk.plugins.base_plugin import BasePlugin
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext

    _ADK_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised only without google-adk
    BasePlugin = object  # type: ignore[assignment,misc]
    BaseTool = Any  # type: ignore[assignment,misc]
    ToolContext = Any  # type: ignore[assignment,misc]
    _ADK_AVAILABLE = False


# Release-defining mutations that must never run from the free-form chat path.
# Kept in sync with ``adk_release_agent.tools.RELEASE_DEFINING_MUTATIONS`` plus the
# confirmed-apply entrypoint, which belongs to the deterministic deploy Workflow.
BLOCKED_FREEFORM_TOOLS = frozenset(
    {
        "open_release_pr",
        "apply_json_update",
        "dispatch_workflow",
        "apply_confirmed_deploy",
    }
)


def _blocked_response(tool_name: str) -> dict[str, Any]:
    return {
        "error": (
            f"Tool '{tool_name}' is blocked in free-form chat. Release-defining "
            "mutations only run through the deterministic, confirmation-gated "
            "deploy Workflow."
        ),
        "error_code": "MUTATION_BLOCKED",
        "blocked_tool": tool_name,
    }


class MutationGuardPlugin(BasePlugin):
    """Blocks release-defining mutation tools from the free-form chat path."""

    def __init__(self, name: str = "mutation_guard") -> None:
        super().__init__(name=name)

    async def before_tool_callback(  # type: ignore[override]
        self,
        *,
        tool: "BaseTool",
        tool_args: dict[str, Any],
        tool_context: "ToolContext",
    ) -> dict[str, Any] | None:
        """Return a block result (short-circuit) for blocked tools, else None."""
        tool_name = getattr(tool, "name", "") or ""
        if tool_name in BLOCKED_FREEFORM_TOOLS:
            return _blocked_response(tool_name)
        return None
