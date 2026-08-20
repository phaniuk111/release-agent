"""ADK-backed chat service used by the FastAPI app and CLI.

This is the runtime bridge from the UI/API into the ADK refactor. Two ADK runtimes
back it, both sharing in-memory session/artifact/memory services:

* the **chat App** — a single skills-routed ``Agent`` wrapped in an ``App`` with the
  ``MutationGuardPlugin`` safety plugin. It answers questions and runs scoped ops.
* the **deploy Workflow** — a deterministic ``Workflow`` graph
  (:mod:`adk_release_agent.deploy_workflow`) that previews, pauses on a
  human-in-the-loop ``RequestInput`` confirmation, and applies only on the exact
  ``CONFIRM-xxxxxx`` token. Deploy intent routes here via the deterministic parser,
  with a classify-only LLM fallback for free-form phrasings (routing only — the
  Workflow's token gate is unchanged).

The external SSE contract is unchanged: ``token`` / ``interrupt`` / ``done`` events,
with a ``confirmation`` interrupt carrying the ``CONFIRM-`` token.
"""
from __future__ import annotations

import asyncio
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
from adk_release_agent import intent as adk_intent
from release_agent.agent import parsing as adk_parsing
from adk_release_agent.tracing import log_router_decision
from adk_release_agent.agent import app as chat_app
from adk_release_agent.deploy_workflow import build_deploy_app

from .config import settings

logger = logging.getLogger(__name__)

_USER_ID = "fastapi-user"
# name attached to the resume function-response; matches ADK's RequestInput tool.
_REQUEST_INPUT_NAME = "adk_request_input"
# ADK's tool-confirmation long-running function-call name (prod-ops confirmation).
_REQUEST_CONFIRMATION = "adk_request_confirmation"


def _session_id(thread_id: str, lane: str) -> str:
    """Session id for one thread within one runner lane.

    The chat agent and the deploy Workflow are different agents sharing one
    session service. In memory they stay apart because sessions are keyed by
    app_name too — but VertexAiSessionService resolves everything to the single
    configured Agent Engine and IGNORES app_name, so both lanes would append to
    one session and each agent would replay the other's events. Scoping the id
    keeps them separate under either backend.

    Vertex session ids must match ^[A-Za-z0-9_-]+$, which our
    'fastapi-<random>' thread ids and this suffix both satisfy.
    """
    return f"{thread_id}-{lane}"


def _build_session_service():
    """The configured session store. Defaults to in-memory (per-pod)."""
    backend = (settings.adk_session_backend or "memory").strip().lower()
    if backend in ("", "memory", "inmemory", "in-memory"):
        return InMemorySessionService()
    if backend != "vertex":
        raise RuntimeError(
            f"ADK_SESSION_BACKEND={backend!r} is not supported (use 'memory' or 'vertex')."
        )

    engine_id = (settings.vertex_agent_engine_id or "").strip().rsplit("/", 1)[-1]
    if not engine_id:
        # Failing at startup is the point: silently falling back to in-memory
        # would look like it worked until a pod restart lost every conversation.
        raise RuntimeError(
            "ADK_SESSION_BACKEND=vertex requires VERTEX_AGENT_ENGINE_ID "
            "(the Agent Engine holding the sessions). Create one first — see "
            "the chart README — or set ADK_SESSION_BACKEND=memory."
        )
    try:
        from google.adk.sessions import VertexAiSessionService
    except ImportError as e:      # pragma: no cover - dependency wiring
        raise RuntimeError(
            "ADK_SESSION_BACKEND=vertex needs google-cloud-aiplatform installed."
        ) from e

    logger.info("ADK sessions: Vertex Agent Engine %s", engine_id)
    return VertexAiSessionService(
        project=settings.gcp_project or None,
        location=settings.gcp_location or None,
        agent_engine_id=engine_id,
    )


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


# Human labels for the tool calls a turn makes, streamed as `progress` events so
# the UI shows what the agent is doing instead of dots. A multi-tool turn (load a
# skill -> query -> answer) can take a minute against Gemini; silence reads as a
# hang. Unmapped tools fall back to their humanized name, so new tools need no
# entry here.
_TOOL_LABELS = {
    "release_stats": "Reading the release history",
    "list_release_queue": "Reading the next-release queue",
    "queue_release_intent": "Checking the build and queueing",
    "withdraw_release_intent": "Withdrawing from the queue",
    "check_release_window": "Checking the release window",
    "list_allowed_images": "Reading the image catalog",
    "get_recent_runs": "Listing recent workflow runs",
    "get_workflow_status": "Checking the workflow run",
    "find_prs": "Searching pull requests",
    "get_pr_details": "Reading the pull request",
    "get_pr_comments": "Reading PR comments",
    "summarize_pr_controls": "Summarizing PR controls",
    "verify_image_tag_build": "Verifying the build for this tag",
    "get_build_controls": "Reading the build controls",
    "get_build_report": "Diagnosing the build run",
    "promote_release": "Promoting the release file-set",
    "remove_from_release": "Removing from the release",
    "merge_prod_release": "Releasing the staged PRD batch",
    "retrigger_deployment_workflow": "Re-running the deployment workflow",
}


def _progress_label(name: str, args: dict[str, Any]) -> str:
    """One short present-tense line describing a tool call."""
    if "skill" in name:
        skill = str(args.get("skill_name") or args.get("name") or "").strip()
        return f"Loading {skill} guidance" if skill else "Loading skill guidance"
    label = _TOOL_LABELS.get(name)
    if label:
        return label
    return (name or "working").replace("_", " ").strip().capitalize()


# Tools whose success changes release/deploy state — i.e. the banner is now
# stale. Only these trigger an automatic banner refresh; every other turn leaves
# the cached snapshot alone (the ⟳ button forces a live read on demand).
_STATE_CHANGING_TOOLS = frozenset({
    "promote_release",
    "merge_prod_release",
    "remove_from_release",
    "retrigger_deployment_workflow",
})


def _changes_release_state(event: Any) -> bool:
    for call in event.get_function_calls() or []:
        if (getattr(call, "name", "") or "") in _STATE_CHANGING_TOOLS:
            return True
    return False


def _progress_events(event: Any) -> list[str]:
    """Progress labels for an ADK event's tool calls (HITL pauses excluded —
    those surface as their own interrupt)."""
    labels: list[str] = []
    for call in event.get_function_calls() or []:
        name = getattr(call, "name", "") or ""
        if not name or name == _REQUEST_CONFIRMATION:
            continue
        labels.append(_progress_label(name, dict(getattr(call, "args", None) or {})))
    return labels


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
    elif function == "promote_release":
        target = str((original.get("args") or {}).get("target", "")).upper() or "the target environment"
        hint = (
            f"Promote the current release's file-set to **{target}**? This copies the "
            "release files onto that environment branch and merges the promotion PR."
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
        self.session_service = _build_session_service()
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
            if pending_token is None:
                # Not in THIS process — the preview may have been served by
                # another replica, or by this one before a restart. The Workflow
                # persisted the token in session state; ask the session service.
                pending_token = await self._pending_token_from_session(thread_id)
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
                yield {"type": "done", "mutated": True}
                return

        if _looks_like_deploy_request(message):
            log_router_decision(thread_id, message, "deploy_workflow:deterministic")
            async for event in self._stream_deploy_preview(message, thread_id):
                yield event
            return

        # Free-form English fallback: the deterministic parser missed, but the
        # message sounds deploy-ish ("can you get payments-api:1.2.3 to prod").
        # One classifier call; on a hit we route the SAME deterministic Workflow
        # (preview + CONFIRM token + mutation guard) — routing only, never a
        # new mutation path. On a miss/error the turn falls to the chat lane.
        # Queue phrasings ("add X to the NEXT release") skip the classifier too —
        # they belong to the release-queue skill in the chat lane.
        payload = None
        if not adk_parsing.is_queue_intent(message):
            payload = await asyncio.to_thread(adk_intent.deploy_payload_from_freeform, message)
        if payload:
            log_router_decision(thread_id, message, "deploy_workflow:classifier", detail=payload)
            async for event in self._stream_deploy_preview(payload, thread_id):
                yield event
            return

        log_router_decision(thread_id, message, "chat")
        async for event in self._run_chat_agent(_content_from_text(message), thread_id):
            yield event

    async def _stream_deploy_preview(
        self, message: str, thread_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run the deploy Workflow's preview turn and surface the confirmation interrupt."""
        async for event in self.deploy_runner.run_async(
            user_id=_USER_ID,
            session_id=_session_id(thread_id, "deploy"),
            new_message=_content_from_text(message),
        ):
            for label in _progress_events(event):
                yield {"type": "progress", "content": label}
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
            session_id=_session_id(thread_id, "deploy"),
            new_message=_confirmation_response(token, confirmed),
        ):
            output = getattr(event, "output", None)
            if output is not None:
                result = output
        yield {"type": "token", "content": self._format_deploy_apply_result(result or {})}
        yield {"type": "done", "mutated": True}

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
        mutated = False          # did this turn change release/deploy state?
        async for event in self.chat_runner.run_async(
            user_id=_USER_ID,
            session_id=_session_id(thread_id, "chat"),
            invocation_id=invocation_id,
            new_message=content,
        ):
            for label in _progress_events(event):
                yield {"type": "progress", "content": label}
            if _changes_release_state(event):
                mutated = True
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
        yield {"type": "done", "mutated": mutated}

    async def _pending_token_from_session(self, thread_id: str) -> str | None:
        """The CONFIRM token this thread is waiting on, read from session state.

        The deploy Workflow writes it there on every preview, so this answers
        even when the preview was served by a different process. Returns None on
        any failure: a routing hint must never break a chat turn.
        """
        try:
            session = await self.session_service.get_session(
                app_name=self.deploy_runner.app_name,
                user_id=_USER_ID,
                session_id=_session_id(thread_id, "deploy"),
            )
        except Exception:
            logger.debug("pending-token lookup failed for %s", thread_id, exc_info=True)
            return None
        token = ((session.state if session else None) or {}).get("deploy_confirm_token")
        return str(token) if token else None

    async def _persist_session_to_memory(self, thread_id: str) -> None:
        """Best-effort: add the finished chat session to the memory service."""
        try:
            session = await self.session_service.get_session(
                app_name=chat_app.name,
                user_id=_USER_ID,
                session_id=_session_id(thread_id, "chat"),
            )
            if session is not None:
                await self.memory_service.add_session_to_memory(session)
        except Exception:  # memory is best-effort; never break a chat turn
            logger.debug("memory persistence failed for thread %s", thread_id, exc_info=True)

    @staticmethod
    def _format_deploy_apply_result(result: dict[str, Any]) -> str:
        if result.get("ok") is False:
            # Prefer the tool's own explanation (e.g. the one-release-at-a-time
            # guard's note naming the blocking PR) over a generic failure line.
            detail = result.get("note") or result.get("error") or result.get("status") or "confirmation failed"
            return f"Not applied: {detail}"
        note = str(result.get("note") or "")
        # A DF deploy can carry a SECOND mutation (the Composer DAG PR). The
        # dispatch note alone would report half the action, and the half it hid
        # is the one still needing a human to merge it.
        bump = result.get("dag_bump")
        if isinstance(bump, dict):
            if bump.get("ok") and bump.get("pr_url"):
                note += (f"\n\nComposer DAGs: PR #{bump.get('pr_number')} raised "
                         f"({bump['pr_url']}) — merge it once the run above is green.")
            elif bump.get("ok"):
                note += f"\n\nComposer DAGs: {bump.get('note') or 'no change needed'}."
            else:
                note += (f"\n\nComposer DAGs NOT updated: {bump.get('error')} "
                         "— the template was still dispatched; bump the DAGs manually.")
            for problem in bump.get("problems") or []:
                note += f"\n  - skipped {problem.get('file')}: {problem.get('error')}"
        if note:
            return note
        return "Deploy applied:\n\n```json\n" + json.dumps(result, indent=2) + "\n```"


_adk_chat_service: AdkChatService | None = None


def get_adk_chat_service() -> AdkChatService:
    global _adk_chat_service
    if _adk_chat_service is None:
        _adk_chat_service = AdkChatService()
    return _adk_chat_service
