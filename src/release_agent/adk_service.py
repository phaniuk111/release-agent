"""ADK-backed chat service used by the FastAPI app and CLI.

This is the runtime bridge from the UI/API into the ADK refactor. Two ADK runtimes
back it, both sharing in-memory session/artifact/memory services:

* the **chat App** — a single skills-routed ``Agent`` wrapped in an ``App`` with the
  ``MutationGuardPlugin`` safety plugin. It answers questions and runs scoped ops.
* the **deploy Workflow** — a deterministic ``Workflow`` graph
  (:mod:`adk_release_agent.deploy_workflow`) that previews, pauses on a
  human-in-the-loop ``RequestInput`` confirmation, and applies only on the exact
  ``CONFIRM-xxxxxx`` token. Deploy intent is routed here, never through the LLM.

The external SSE contract is unchanged: ``token`` / ``interrupt`` / ``done`` events,
with a ``confirmation`` interrupt carrying the ``CONFIRM-`` token.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from google.genai import types
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from adk_release_agent import deploy as adk_deploy
from adk_release_agent.agent import app as chat_app
from adk_release_agent.deploy_workflow import build_deploy_app

from .config import settings

logger = logging.getLogger(__name__)

_USER_ID = "fastapi-user"
# name attached to the resume function-response; matches ADK's RequestInput tool.
_REQUEST_INPUT_NAME = "adk_request_input"
# ADK's tool-confirmation long-running function-call name (prod-ops confirmation).
_REQUEST_CONFIRMATION = "adk_request_confirmation"


@dataclass
class PendingAdkCall:
    """A paused chat-agent tool call awaiting the user's confirmation reply."""

    invocation_id: str
    function_call_id: str
    function_name: str
    args: dict[str, Any]


def _content_from_text(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def _confirmation_response(token: str, confirmed: bool) -> types.Content:
    """Function-response message that resumes the paused deploy Workflow."""
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=token,
                    name=_REQUEST_INPUT_NAME,
                    response={"confirmed": confirmed, "token": token},
                )
            )
        ],
    )


def _text_from_event(event: Any) -> str:
    content = getattr(event, "content", None)
    if not content or not getattr(content, "parts", None):
        return ""
    return "".join(part.text for part in content.parts if getattr(part, "text", None))


def _interrupt_token_from_event(event: Any) -> str | None:
    """Return the interrupt id (== CONFIRM token) if this event is a HITL pause."""
    long_running_ids = getattr(event, "long_running_tool_ids", None) or set()
    if not long_running_ids:
        return None
    for function_call in event.get_function_calls() or []:
        if function_call.id in long_running_ids:
            return function_call.id
    return None


def _looks_like_deploy_request(message: str) -> bool:
    """Detect a deploy/add/promote intent WITHOUT minting a preview token.

    The deploy Workflow's gate node is the single place that mints the token; this
    detector only decides whether to route the turn into that Workflow.
    """
    req = adk_deploy._request_from_inputs(message=message.strip())
    return bool(req and req.get("images"))


def _is_positive_response(text: str) -> bool:
    return text.strip().lower() in {"y", "yes", "true", "confirm", "confirmed", "ok", "proceed"}


def _pending_call_from_event(event: Any) -> PendingAdkCall | None:
    """Return a pending tool-confirmation call if this chat event is a HITL pause."""
    long_running_ids = getattr(event, "long_running_tool_ids", None) or set()
    if not long_running_ids:
        return None
    for function_call in event.get_function_calls() or []:
        if function_call.id in long_running_ids:
            return PendingAdkCall(
                invocation_id=getattr(event, "invocation_id", "") or "",
                function_call_id=function_call.id,
                function_name=function_call.name,
                args=dict(function_call.args or {}),
            )
    return None


def _content_from_pending_reply(text: str, pending: PendingAdkCall) -> types.Content:
    """Build the function-response that resumes a paused tool confirmation."""
    if pending.function_name == _REQUEST_CONFIRMATION:
        response: Any = {"confirmed": _is_positive_response(text)}
    else:
        try:
            parsed = json.loads(text)
            response = parsed if isinstance(parsed, dict) else {"result": parsed}
        except (json.JSONDecodeError, ValueError):
            response = {"result": text}
    return types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=pending.function_call_id,
                    name=pending.function_name,
                    response=response,
                )
            )
        ],
    )


def _confirmation_interrupt_payload(pending: PendingAdkCall) -> dict[str, Any]:
    """UI-facing interrupt describing a prod-ops confirmation request."""
    confirmation = pending.args.get("toolConfirmation") or {}
    original = pending.args.get("originalFunctionCall") or {}
    function = original.get("name") or pending.function_name
    if function == "merge_prod_release":
        # Post-click warning: releasing finalizes the day's release.
        hint = (
            "Release today's PRD release now? It promotes the staged charts through "
            "SIT → UAT → PRD. **Once released, no new charts can be added to this "
            "release** — later prod deploys start a new release."
        )
    else:
        hint = confirmation.get("hint") or f"Confirm {function}?"
    return {
        "type": "confirmation",
        "message": hint,
        "action": 'Reply "yes" to approve, anything else to reject.',
        "function": function,
        "args": original.get("args") or {},
    }


class AdkChatService:
    """Stateful adapter around the chat App and the deterministic deploy Workflow."""

    def __init__(self):
        if chat_app is None:
            raise RuntimeError("google-adk is not installed; cannot start ADK chat service")
        self.session_service = InMemorySessionService()
        self.artifact_service = InMemoryArtifactService()
        self.memory_service = InMemoryMemoryService()
        self.chat_runner = Runner(
            app=chat_app,
            artifact_service=self.artifact_service,
            session_service=self.session_service,
            memory_service=self.memory_service,
            auto_create_session=True,
        )
        self.deploy_runner = Runner(
            app=build_deploy_app(),
            artifact_service=self.artifact_service,
            session_service=self.session_service,
            memory_service=self.memory_service,
            auto_create_session=True,
        )
        # thread_id -> pending CONFIRM token awaiting resume of the deploy Workflow.
        self._pending_deploy: dict[str, str] = {}
        # thread_id -> paused chat-agent tool confirmation awaiting a yes/no reply.
        self._pending_adk_calls: dict[str, PendingAdkCall] = {}

    async def stream_chat(self, message: str, thread_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """Yield UI-compatible SSE event payloads."""
        # A paused prod-ops confirmation takes precedence: this reply approves/rejects it.
        pending_call = self._pending_adk_calls.pop(thread_id, None)
        if pending_call is not None:
            async for event in self._run_chat_agent(
                _content_from_pending_reply(message, pending_call),
                thread_id,
                invocation_id=pending_call.invocation_id,
            ):
                yield event
            return

        token = adk_deploy._extract_confirmation_token(message)
        if token:
            pending_token = self._pending_deploy.get(thread_id)
            if pending_token:
                # Resume the paused deploy Workflow: exact match confirms, else cancels.
                async for event in self._stream_deploy_resume(
                    thread_id, pending_token, confirmed=(token == pending_token)
                ):
                    yield event
                return
            if token in adk_deploy._PENDING_PREVIEWS:
                # Stateless fallback (e.g. reconnect with no tracked invocation).
                result = adk_deploy.apply_confirmed_deploy(message)
                yield {"type": "token", "content": self._format_deploy_apply_result(result)}
                yield {"type": "done"}
                return

        if _looks_like_deploy_request(message):
            async for event in self._stream_deploy_preview(message, thread_id):
                yield event
            return

        async for event in self._run_chat_agent(_content_from_text(message), thread_id):
            yield event

    async def _stream_deploy_preview(
        self, message: str, thread_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run the deploy Workflow's preview turn and surface the confirmation interrupt."""
        async for event in self.deploy_runner.run_async(
            user_id=_USER_ID, session_id=thread_id, new_message=_content_from_text(message)
        ):
            text = _text_from_event(event)
            if text:
                yield {"type": "token", "content": text}
            token = _interrupt_token_from_event(event)
            if token:
                pending = adk_deploy._PENDING_PREVIEWS.get(token, {})
                request = pending.get("request", {})
                environment = request.get("environment", "uat")
                self._pending_deploy[thread_id] = token
                yield {
                    "type": "interrupt",
                    "data": {
                        "type": "confirmation",
                        "token": token,
                        "proposed": pending.get("preview", {}),
                        "environment": environment,
                        "message": f"Reply with exactly `{token}` to apply this deploy.",
                    },
                }
                break
        yield {"type": "done"}

    async def _stream_deploy_resume(
        self, thread_id: str, token: str, confirmed: bool
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Resume the paused deploy Workflow with the user's confirmation."""
        self._pending_deploy.pop(thread_id, None)
        result: dict[str, Any] | None = None
        async for event in self.deploy_runner.run_async(
            user_id=_USER_ID,
            session_id=thread_id,
            new_message=_confirmation_response(token, confirmed),
        ):
            output = getattr(event, "output", None)
            if output is not None:
                result = output
        yield {"type": "token", "content": self._format_deploy_apply_result(result or {})}
        yield {"type": "done"}

    async def _run_chat_agent(
        self,
        content: types.Content,
        thread_id: str,
        invocation_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat turn, surfacing prod-ops confirmations and persisting memory.

        If the agent calls a confirmation-gated tool, the run pauses: the pending
        call is stored and surfaced as a ``confirmation`` interrupt. Otherwise the
        turn completes and — when memory is enabled — the session is saved to the
        memory service so future turns can recall it.
        """
        interrupted = False
        async for event in self.chat_runner.run_async(
            user_id=_USER_ID,
            session_id=thread_id,
            invocation_id=invocation_id,
            new_message=content,
        ):
            text = _text_from_event(event)
            if text:
                yield {"type": "token", "content": text}
            pending = _pending_call_from_event(event)
            if pending is not None:
                self._pending_adk_calls[thread_id] = pending
                yield {"type": "interrupt", "data": _confirmation_interrupt_payload(pending)}
                interrupted = True
                break

        if not interrupted and settings.adk_memory_enabled:
            await self._persist_session_to_memory(thread_id)
        yield {"type": "done"}

    async def _persist_session_to_memory(self, thread_id: str) -> None:
        """Best-effort: add the finished chat session to the memory service."""
        try:
            session = await self.session_service.get_session(
                app_name=chat_app.name, user_id=_USER_ID, session_id=thread_id
            )
            if session is not None:
                await self.memory_service.add_session_to_memory(session)
        except Exception:  # memory is best-effort; never break a chat turn
            logger.debug("memory persistence failed for thread %s", thread_id, exc_info=True)

    @staticmethod
    def _format_deploy_apply_result(result: dict[str, Any]) -> str:
        if result.get("ok") is False:
            return f"Not applied: {result.get('error') or result.get('status') or 'confirmation failed'}"
        if result.get("note"):
            return str(result["note"])
        return "Deploy applied:\n\n```json\n" + json.dumps(result, indent=2) + "\n```"


_adk_chat_service: AdkChatService | None = None


def get_adk_chat_service() -> AdkChatService:
    global _adk_chat_service
    if _adk_chat_service is None:
        _adk_chat_service = AdkChatService()
    return _adk_chat_service
