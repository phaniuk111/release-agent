"""JSONL trace collection for the release copilot.

Two sources write one append-only JSONL stream (``TRACE_LOG_PATH``, default
``traces/agent-traces.jsonl``):

* ``TraceLoggerPlugin`` — an App plugin whose ``before_tool`` / ``after_tool`` /
  ``on_tool_error`` callbacks record every chat-lane tool call with args,
  a result preview, latency, and the session id. (The deterministic deploy
  Workflow calls its functions directly, not via tools, so its activity shows
  up as router decisions + its own result events instead.)
* ``log_event`` — used by the service layer to record routing decisions
  (deterministic parser vs classifier fallback vs chat lane), so misroutes can
  be diagnosed from the trace instead of by hand.

Tracing must never break the agent: every write is best-effort and swallows
its own errors. Nothing here mutates tool behavior — callbacks always return
None so the framework proceeds normally. Set ``TRACE_LOG_PATH=""`` to disable.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger("release_copilot")

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

_PREVIEW_CHARS = 2000


def _trace_path() -> str:
    return os.getenv("TRACE_LOG_PATH", os.path.join("traces", "agent-traces.jsonl"))


def log_event(event: dict[str, Any]) -> None:
    """Append one JSON object to the trace stream. Best-effort, never raises."""
    path = _trace_path()
    if not path:
        return
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        event = {"ts": round(time.time(), 3), **event}
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:  # tracing must never break the agent
        logger.debug("trace write failed", exc_info=True)


def log_router_decision(thread_id: str, message: str, route: str, detail: str = "") -> None:
    """Record which lane a chat turn was routed to (and why)."""
    log_event(
        {
            "type": "router_decision",
            "thread_id": thread_id,
            "message_preview": message[:300],
            "route": route,
            "detail": detail[:500],
        }
    )


def _preview(value: Any) -> str:
    try:
        text = value if isinstance(value, str) else json.dumps(value, default=str)
    except Exception:
        text = repr(value)
    return text[:_PREVIEW_CHARS]


def _session_id(tool_context: Any) -> str:
    # Best-effort: walk the public-ish attributes; fall back to empty.
    for path in (("session", "id"), ("_invocation_context", "session", "id")):
        obj = tool_context
        for attr in path:
            obj = getattr(obj, attr, None)
            if obj is None:
                break
        else:
            return str(obj)
    return ""


class TraceLoggerPlugin(BasePlugin):
    """Records every chat-lane tool call (args, result preview, latency)."""

    def __init__(self, name: str = "trace_logger") -> None:
        super().__init__(name=name)
        self._started: dict[int, float] = {}

    async def before_tool_callback(  # type: ignore[override]
        self,
        *,
        tool: "BaseTool",
        tool_args: dict[str, Any],
        tool_context: "ToolContext",
    ) -> dict[str, Any] | None:
        self._started[id(tool_context)] = time.monotonic()
        log_event(
            {
                "type": "tool_call",
                "session_id": _session_id(tool_context),
                "tool": getattr(tool, "name", "") or "",
                "args": _preview(tool_args),
            }
        )
        return None  # never short-circuit

    async def after_tool_callback(  # type: ignore[override]
        self,
        *,
        tool: "BaseTool",
        tool_args: dict[str, Any],
        tool_context: "ToolContext",
        result: dict,
    ) -> dict[str, Any] | None:
        started = self._started.pop(id(tool_context), None)
        log_event(
            {
                "type": "tool_result",
                "session_id": _session_id(tool_context),
                "tool": getattr(tool, "name", "") or "",
                "latency_ms": round((time.monotonic() - started) * 1000) if started else None,
                "result_preview": _preview(result),
            }
        )
        return None  # never modify the result

    async def on_tool_error_callback(  # type: ignore[override]
        self,
        *,
        tool: "BaseTool",
        tool_args: dict[str, Any],
        tool_context: "ToolContext",
        error: Exception,
    ) -> dict[str, Any] | None:
        started = self._started.pop(id(tool_context), None)
        log_event(
            {
                "type": "tool_error",
                "session_id": _session_id(tool_context),
                "tool": getattr(tool, "name", "") or "",
                "latency_ms": round((time.monotonic() - started) * 1000) if started else None,
                "args": _preview(tool_args),
                "error": f"{type(error).__name__}: {error}",
            }
        )
        return None  # let the framework's error handling proceed
