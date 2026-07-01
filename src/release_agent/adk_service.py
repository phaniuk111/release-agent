"""ADK-backed chat service used by the FastAPI app.

This is the runtime bridge from the existing UI/API into the ADK refactor. It
keeps deterministic deploy confirmation local and streams free-form requests
through the ADK Runner/root_agent.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, AsyncGenerator

from google.genai import types
from google.adk.artifacts import InMemoryArtifactService
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from adk_release_agent import deploy as adk_deploy
from adk_release_agent.agent import root_agent


_REQUEST_CONFIRMATION = "adk_request_confirmation"


@dataclass
class PendingAdkCall:
    invocation_id: str
    function_call_id: str
    function_name: str
    args: dict[str, Any]


def _is_positive_response(text: str) -> bool:
    return text.strip().lower() in {"y", "yes", "true", "confirm", "confirmed", "ok", "proceed"}


def _content_from_text(text: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=text)])


def _content_from_pending_reply(text: str, pending: PendingAdkCall) -> types.Content:
    if pending.function_name == _REQUEST_CONFIRMATION:
        response = {"confirmed": _is_positive_response(text)}
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


def _text_from_event(event: Any) -> str:
    content = getattr(event, "content", None)
    if not content or not getattr(content, "parts", None):
        return ""
    parts = []
    for part in content.parts:
        text = getattr(part, "text", None)
        if text:
            parts.append(text)
    return "".join(parts)


def _pending_call_from_event(event: Any) -> PendingAdkCall | None:
    long_running_ids = getattr(event, "long_running_tool_ids", None)
    content = getattr(event, "content", None)
    if not long_running_ids or not content or not getattr(content, "parts", None):
        return None
    for part in content.parts:
        function_call = getattr(part, "function_call", None)
        if function_call and function_call.id in long_running_ids:
            return PendingAdkCall(
                invocation_id=getattr(event, "invocation_id", "") or "",
                function_call_id=function_call.id,
                function_name=function_call.name,
                args=dict(function_call.args or {}),
            )
    return None


def _interrupt_payload(pending: PendingAdkCall) -> dict[str, Any]:
    if pending.function_name == _REQUEST_CONFIRMATION:
        confirmation = pending.args.get("toolConfirmation", {})
        original = pending.args.get("originalFunctionCall", {})
        hint = confirmation.get("hint") or f"Confirm {original.get('name', 'tool call')}?"
        return {
            "type": "adk_confirmation",
            "message": hint,
            "action": 'Type "yes" to confirm, anything else to reject.',
            "function": original.get("name") or pending.function_name,
            "args": original.get("args") or {},
        }
    return {
        "type": "adk_input",
        "message": f"ADK is waiting for input for {pending.function_name}.",
        "action": "Provide the requested value.",
        "function": pending.function_name,
        "args": pending.args,
    }


def _looks_like_deploy_request(message: str) -> bool:
    stripped = message.strip()
    preview = adk_deploy.prepare_deploy_preview(message=stripped)
    if preview.get("ok"):
        # Leave the pending preview in place; the caller will stream it.
        return True
    return False


class AdkChatService:
    """Small stateful adapter around an ADK Runner."""

    def __init__(self):
        if root_agent is None:
            raise RuntimeError("google-adk is not installed; cannot start ADK chat service")
        self.session_service = InMemorySessionService()
        self.artifact_service = InMemoryArtifactService()
        self.memory_service = InMemoryMemoryService()
        self.runner = Runner(
            app_name="release_copilot_adk",
            agent=root_agent,
            artifact_service=self.artifact_service,
            session_service=self.session_service,
            memory_service=self.memory_service,
            auto_create_session=True,
        )
        self._pending_adk_calls: dict[str, PendingAdkCall] = {}

    async def stream_chat(self, message: str, thread_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """Yield UI-compatible SSE event payloads."""
        pending = self._pending_adk_calls.pop(thread_id, None)
        if pending:
            async for event in self._stream_adk(
                _content_from_pending_reply(message, pending),
                thread_id,
                invocation_id=pending.invocation_id,
            ):
                yield event
            return

        token = adk_deploy._extract_confirmation_token(message)
        if token:
            result = adk_deploy.apply_confirmed_deploy(message)
            yield {"type": "token", "content": self._format_deploy_apply_result(result)}
            yield {"type": "done"}
            return

        if _looks_like_deploy_request(message):
            # _looks_like_deploy_request already created the pending preview.
            latest_token = next(reversed(adk_deploy._PENDING_PREVIEWS))
            preview = adk_deploy._PENDING_PREVIEWS[latest_token]["preview"]
            pending_preview = adk_deploy._PENDING_PREVIEWS[latest_token]["request"]
            env = pending_preview.get("environment", "uat")
            image_tags = adk_deploy._image_tags(pending_preview)
            content = (
                f"**Deploy {image_tags} to {str(env).upper()}**\n\n"
                "```json\n"
                + json.dumps(preview, indent=2)
                + "\n```\n\n"
                f"Reply `{latest_token}` to confirm."
            )
            yield {"type": "token", "content": content}
            yield {
                "type": "interrupt",
                "data": {
                    "type": "confirmation",
                    "token": latest_token,
                    "proposed": preview,
                    "environment": env,
                    "message": f"Reply with exactly `{latest_token}` to apply this deploy.",
                },
            }
            yield {"type": "done"}
            return

        async for event in self._stream_adk(_content_from_text(message), thread_id):
            yield event

    async def _stream_adk(
        self,
        content: types.Content,
        thread_id: str,
        invocation_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        user_id = "fastapi-user"
        async for event in self.runner.run_async(
            user_id=user_id,
            session_id=thread_id,
            invocation_id=invocation_id,
            new_message=content,
        ):
            text = _text_from_event(event)
            if text:
                yield {"type": "token", "content": text}
            pending = _pending_call_from_event(event)
            if pending:
                self._pending_adk_calls[thread_id] = pending
                yield {"type": "interrupt", "data": _interrupt_payload(pending)}
                break
        yield {"type": "done"}

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
