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
* the **investigate Workflow** — the Investigate button's pipeline
  (:mod:`adk_release_agent.investigate_workflow`): evidence collected in code,
  then ONE capped model step; read-only, recognised by its message alone.

The external SSE contract is unchanged: ``token`` / ``interrupt`` / ``done`` events,
with a ``confirmation`` interrupt carrying the ``CONFIRM-`` token. An
investigation adds an ``investigation`` event (what the feedback row needs)
just before ``done``.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import aclosing
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
from adk_release_agent.telemetry import traced_stream
from adk_release_agent import agent as chat_agent_module
from adk_release_agent.agent import app as chat_app
from adk_release_agent.commands_workflow import build_commands_app
from adk_release_agent.deploy_workflow import build_deploy_app
from adk_release_agent import investigate_workflow

from . import commands as release_commands
from .config import settings

logger = logging.getLogger(__name__)

_USER_ID = "fastapi-user"

# The investigate lane's findings, shared between people: (business date,
# incident id) → (when, the finished run). Only a finding the MODEL wrote from
# evidence that was read is kept — a fallback summary or a failed collect is
# retried by the next person. In-process, like every other cache here.
_FINDING_TTL = 600.0
_FINDING_MAX = 64
_findings: dict[tuple, tuple[float, Any]] = {}
_finding_locks: dict[tuple, asyncio.Lock] = {}


def _finding_hit(key: tuple) -> tuple[float, Any] | None:
    hit = _findings.get(key)
    if hit and time.time() - hit[0] < _FINDING_TTL:
        return hit
    return None


def _finding_keep(key: tuple, run: Any) -> None:
    bundle = run.bundle if isinstance(run.bundle, dict) else {}
    if not (key[1] and bundle.get("ok") and run.calls and run.text and not run.fallback):
        return
    if len(_findings) >= _FINDING_MAX:
        _findings.clear()
    _findings[key] = (time.time(), run)


def _finding_lock(key: tuple) -> asyncio.Lock:
    """One lock per incident per event loop (an asyncio lock belongs to the loop
    that first waits on it — the app has one, tests start many)."""
    if len(_finding_locks) > 256:
        _finding_locks.clear()
    return _finding_locks.setdefault((id(asyncio.get_running_loop()), *key), asyncio.Lock())


def _user_id() -> str:
    """ADK sessions are keyed per verified caller when identity is on, so one
    person's thread id never resumes another person's conversation or pending
    approval; everyone shares the fixed id when it is off, as before."""
    from . import identity

    return identity.current_email() or _USER_ID
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


def _resolve_agent_engine_id() -> str:
    """The Agent Engine holding the sessions: pinned, found by name, or created.

    An explicit VERTEX_AGENT_ENGINE_ID always wins. Otherwise the engine is
    looked up by display name in the configured location and created if absent,
    so the same values file works in every project and nobody has to paste an
    opaque numeric id.

    On the race: two pods starting together can both create one. That is
    tolerable because SELECTION is deterministic — every pod sorts the matches
    and takes the same one — so they converge on a single session store even
    when a duplicate exists. The duplicate is inert, and logged loudly so it can
    be removed.
    """
    pinned = (settings.vertex_agent_engine_id or "").strip()
    if pinned:
        return pinned.rsplit("/", 1)[-1]

    display_name = (settings.vertex_agent_engine_name or "").strip()
    if not display_name:
        raise RuntimeError(
            "ADK_SESSION_BACKEND=vertex needs VERTEX_AGENT_ENGINE_NAME "
            "(or an explicit VERTEX_AGENT_ENGINE_ID)."
        )
    project, location = settings.gcp_project, settings.gcp_location
    if not project:
        raise RuntimeError("ADK_SESSION_BACKEND=vertex needs GOOGLE_CLOUD_PROJECT.")

    try:
        import vertexai

        client = vertexai.Client(project=project, location=location)
    except Exception as e:                      # pragma: no cover - dependency wiring
        raise RuntimeError(f"Could not reach Vertex AI in {project}/{location}: {e}") from e

    def _matching() -> list[str]:
        found = []
        for engine in client.agent_engines.list():
            resource = getattr(engine, "api_resource", engine)
            if (getattr(resource, "display_name", None) or "") == display_name:
                name = getattr(resource, "name", "") or ""
                if name:
                    found.append(name.rsplit("/", 1)[-1])
        # deterministic: same order in every pod, so all converge on one engine
        return sorted(found)

    existing = _matching()
    if existing:
        if len(existing) > 1:
            logger.warning(
                "%d Agent Engines are named %r in %s/%s — using %s. Delete the "
                "extras; they hold sessions nothing reads.",
                len(existing), display_name, project, location, existing[0],
            )
        return existing[0]

    logger.info("No Agent Engine named %r in %s/%s — creating it.",
                display_name, project, location)
    try:
        client.agent_engines.create(config={"display_name": display_name})
    except Exception as e:
        raise RuntimeError(
            f"Could not create the Agent Engine {display_name!r} in "
            f"{project}/{location}: {e}. The service account needs "
            "roles/aiplatform.user, or pin an existing one with "
            "VERTEX_AGENT_ENGINE_ID."
        ) from e

    # Re-list rather than trusting the create response: if another pod created
    # one at the same moment, this is where both pods agree which to use.
    created = _matching()
    if not created:
        raise RuntimeError(
            f"Created an Agent Engine named {display_name!r} but it did not appear "
            "in the listing — retry, or pin VERTEX_AGENT_ENGINE_ID."
        )
    return created[0]


def _build_session_service():
    """The configured session store. Defaults to in-memory (per-pod)."""
    backend = (settings.adk_session_backend or "memory").strip().lower()
    if backend in ("", "memory", "inmemory", "in-memory"):
        return InMemorySessionService()
    if backend != "vertex":
        raise RuntimeError(
            f"ADK_SESSION_BACKEND={backend!r} is not supported (use 'memory' or 'vertex')."
        )

    engine_id = _resolve_agent_engine_id()
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


_NEGATIVE_REPLIES = {"n", "no", "nope", "reject", "rejected", "cancel", "cancelled", "canceled",
                     "abort", "stop", "decline", "declined"}


def _is_negative_response(text: str) -> bool:
    return text.strip().rstrip(".!").strip().lower() in _NEGATIVE_REPLIES


def _reply_kind(message: str) -> str:
    """What a message means to something already waiting on the thread:
    "yes", "no", a new deploy/release "request" (which replaces it), or "other"
    (which is answered with a reminder and leaves it waiting). Only the
    deterministic detector counts as a request — a free-form sentence that
    merely sounds deploy-ish is not enough to throw a pending preview away."""
    if _is_positive_response(message):
        return "yes"
    if _is_negative_response(message):
        return "no"
    if _looks_like_deploy_request(message):
        return "request"
    return "other"


def _operation_label(pending: PendingAdkCall) -> str:
    original = (pending.args or {}).get("originalFunctionCall") or {}
    name = original.get("name") or pending.function_name or "the paused operation"
    return str(name).replace("_", " ")


def _approval_reminder(pending: PendingAdkCall) -> str:
    return (f"**{_operation_label(pending)}** is still waiting for your answer — nothing has "
            "happened yet. Reply `yes` to approve it or `no` to reject it. To do something "
            "else first, send the new deploy or release request and this one is dropped.")


def _token_reminder(pending_token: str, given: str = "") -> str:
    head = f"`{given}` is not the token waiting on this thread. " if given else ""
    return (head + f"`{pending_token}` is still waiting — nothing has been applied. Paste it "
            "exactly to apply, reply `no` to cancel, or send a new deploy or release "
            "request to replace it.")


def _original_call(pending: PendingAdkCall) -> tuple[str, str] | None:
    """(tool name, canonical args) of the operation a confirmation is FOR."""
    original = (pending.args or {}).get("originalFunctionCall") or {}
    name = original.get("name") or ""
    if not name:
        return None
    return name, json.dumps(original.get("args") or {}, sort_keys=True)


def _result_note(event: Any, tool_name: str) -> str:
    """The human sentence a tool's result carries (``note``), if it returned one."""
    for response in event.get_function_responses() or []:
        if (getattr(response, "name", "") or "") != tool_name:
            continue
        body = getattr(response, "response", None) or {}
        if isinstance(body, dict) and isinstance(body.get("result"), dict):
            body = body["result"]
        if isinstance(body, dict) and body.get("note"):
            return str(body["note"])
    return ""


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
    "get_build_report": "Diagnosing the build run",
    "promote_release": "Promoting the release file-set",
    "promote_df_release": "Promoting the DF release file-set",
    "bq_cost_scan": "Scanning BigQuery costs",
    "bq_query_detail": "Reading the query's stages",
    "bq_table_layout": "Reading the table layout",
    "bq_prune_estimate": "Estimating partition pruning",
    "bq_dry_run": "Dry-running the query",
    "bq_verify_rewrite": "Testing the rewrite",
    "bq_findings": "Reading earlier findings",
    "support_triage": "Reading the control table",
    "investigate_evidence": "Collecting the evidence",
    "source_metrics": "Checking the source's metrics",
    "source_logs": "Reading the source's logs",
    "source_audit": "Reading the audit logs",
    "dataflow_job": "Reading the Dataflow job",
    "release_lookup": "Looking up the release log",
    "what_changed": "Checking what was released",
    "consult_advisor": "Consulting the advisor model",
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
    "promote_df_release",
})


def _landed_changes(event: Any) -> list[tuple[str, str]]:
    """(tool, note) for each state-changing tool that RAN in this event, whatever
    the model does next. A response is not a run when it is only a refusal —
    ADK's answer to a "no" ({"error": "This tool call is rejected."}) or the
    mutation guard's block — neither carries the tool's own ok/note/result.
    Found under load: a 429 after a "no" was reported as the promotion running."""
    landed = []
    for response in event.get_function_responses() or []:
        name = getattr(response, "name", "") or ""
        body = getattr(response, "response", None) or {}
        if name not in _STATE_CHANGING_TOOLS or not isinstance(body, dict):
            continue
        if not ({"ok", "note", "result"} & set(body)):
            continue
        landed.append((name, _result_note(event, name)))
    return landed


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
    if function in ("promote_release", "promote_df_release"):
        target = str((original.get("args") or {}).get("target", "")).upper() or "the target environment"
        which = "DF release" if function == "promote_df_release" else "release"
        hint = (
            f"Promote the current {which}'s file-set to **{target}**? This copies the "
            "release files onto that environment branch and merges the promotion PR."
        )
    else:
        # ADK supplies its own hint describing the FunctionResponse protocol —
        # true, and meaningless to the person clicking. Prefer our own wording.
        supplied = str(confirmation.get("hint") or "").strip()
        hint = supplied if supplied and "FunctionResponse" not in supplied else f"Confirm {function}?"
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
        # Keyed by (owner, thread) like the paused approvals and the PAT store:
        # a thread id is printed in the page header, so on its own it is not a
        # secret, and a reminder that quotes the token must never be shown to
        # someone else who sends a message against that thread id.
        self._pending_deploy: dict[tuple[str, str], str] = {}
        # thread_id -> paused chat-agent tool confirmation awaiting a yes/no reply.
        # Keyed by (owner, thread) like the PAT store and the CONFIRM preview: a
        # thread id is not a secret (it is printed in the header), so keying on
        # it alone would let one person's "yes" answer another's paused prod-ops
        # approval. Identity off = one shared owner = the behaviour as before.
        self._pending_adk_calls: dict[tuple[str, str], PendingAdkCall] = {}
        # LLM_ENABLED=false: the chat's fixed commands, a Workflow in the same
        # ADK runtime (same session service, same pause/resume) — so a paused
        # approval there is keyed and answered exactly like the ones above.
        self.commands_runner = Runner(
            app=build_commands_app(),
            artifact_service=self.artifact_service,
            session_service=self.session_service,
            memory_service=self.memory_service,
            auto_create_session=True,
        )
        self._pending_commands: dict[tuple[str, str], str] = {}
        # The Investigate button: collect in code, one capped model step. Read-only,
        # so it has no pending state to key and never touches the ones above.
        self.investigate_runner = Runner(
            app=investigate_workflow.build_investigate_app(),
            artifact_service=self.artifact_service,
            session_service=self.session_service,
            memory_service=self.memory_service,
            auto_create_session=True,
        )

    async def stream_chat(
        self, message: str, thread_id: str, abort_signal: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Yield UI-compatible SSE event payloads.

        ``abort_signal`` is tripped by the endpoint when the person's browser
        has gone. It reaches ONLY the free-form chat agent (the last lane
        below; the investigate lane, like the Workflows, runs to its end): a turn nobody will read stops at its next event boundary
        instead of running the model and its tools to the end. It is NEVER
        handed to the deploy or commands Workflow runners — their nodes do
        blocking GitHub work, and a graph stopped between gate and apply
        would re-run that node on resume, repeating its side effects
        (AGENTS.md invariant 1). A preview or an approved operation whose
        reader has left runs to its end; its token or outcome waits in session
        state for the thread's next message, exactly as it would after a
        restart.
        """
        # Something already waiting on this thread — a paused yes/no approval
        # (prod ops) or a pending CONFIRM token — is consumed ONLY by an explicit
        # answer: yes/no for an approval, the exact token or "no" for a preview.
        # A NEW deploy/release request replaces it: the old one is cancelled
        # (never applied) and the new one gets its own preview and token.
        # Anything else gets a reminder and the pending item stays put.
        # Found live: a release form submitted while an approval was paused was
        # read as the reply to it — the person was told "you rejected it" and
        # never saw a preview of what they had actually asked for.
        owner = _user_id()
        # The Investigate button's message goes FIRST, ahead of everything that
        # is waiting: it is a read-only request, neither a yes/no nor a token,
        # so it must run while an approval or a CONFIRM token is pending —
        # without consuming, cancelling or even reminding about them. Both
        # stay exactly as they were for the person's real answer.
        investigate = adk_parsing.investigate_request(message)
        if investigate is not None:
            async with aclosing(traced_stream(
                    self._stream_investigation(investigate, thread_id),
                    "investigate", thread_id=thread_id, detail=investigate.get("incident_id", ""),
                    user_id=owner, session_id=_session_id(thread_id, "investigate"))) as events:
                async for event in events:
                    yield event
            return

        pending_call = self._pending_adk_calls.get((owner, thread_id))
        if pending_call is None:
            # The pause may have been served by another replica, or by this one
            # before a restart.
            pending_call = await self._pending_call_from_session(thread_id)
        if pending_call is not None:
            kind = _reply_kind(message)
            if kind in ("yes", "no"):
                # Consumed — clear it in both places, or a later message would be
                # read as answering an approval that has already been decided.
                self._pending_adk_calls.pop((owner, thread_id), None)
                await self._persist_pending_call(thread_id, None)
                approved = _original_call(pending_call) if kind == "yes" else None
                async with aclosing(traced_stream(
                        self._run_chat_agent(
                            _content_from_pending_reply(message, pending_call),
                            thread_id,
                            invocation_id=pending_call.invocation_id,
                            approved=approved,
                        ),
                        "chat:approval", thread_id=thread_id,
                        user_id=owner, session_id=_session_id(thread_id, "chat"))) as events:
                    async for event in events:
                        yield event
                return
            if kind != "request":
                yield {"type": "token", "content": _approval_reminder(pending_call)}
                yield {"type": "done"}
                return
            self._pending_adk_calls.pop((owner, thread_id), None)
            await self._persist_pending_call(thread_id, None)
            await self._dismiss_pending_call(thread_id, pending_call)
            yield {"type": "token", "content": (
                f"Dropped the pending approval for **{_operation_label(pending_call)}** — "
                "nothing was applied. Previewing the new request instead.")}
            # ...and fall through: the new request is previewed below.

        if not settings.llm_enabled:
            approval_id = self._pending_commands.get((owner, thread_id)) or \
                await self._command_state_value(thread_id, "command_approval_id")
            if approval_id:
                kind = _reply_kind(message)
                if kind in ("yes", "no"):
                    async with aclosing(traced_stream(
                            self._stream_command_resume(thread_id, approval_id, confirmed=kind == "yes"),
                            "command:approval", thread_id=thread_id,
                            user_id=owner, session_id=_session_id(thread_id, "command"))) as events:
                        async for event in events:
                            yield event
                    return
                pending = await self._command_state(thread_id)
                label = ((pending or {}).get("command_pending") or {}).get("label") or "The operation"
                if kind != "request":
                    yield {"type": "token", "content": (
                        f"**{label}** is still waiting for your answer — nothing has happened yet. "
                        "Reply `yes` to approve it or `no` to reject it. To do something else "
                        "first, send the new deploy or release request and this one is dropped.")}
                    yield {"type": "done"}
                    return
                await self._resume_command(thread_id, approval_id, confirmed=False)
                yield {"type": "token", "content": (
                    f"Dropped the pending approval for **{label}** — nothing was applied. "
                    "Previewing the new request instead.")}
                # ...and fall through: the new request is previewed below.

        token = adk_deploy._extract_confirmation_token(message)
        pending_token = self._pending_deploy.get((owner, thread_id))
        if pending_token is None:
            # Not in THIS process — the preview may have been served by another
            # replica, or by this one before a restart. The Workflow persisted
            # the token in session state; ask the session service.
            pending_token = await self._pending_token_from_session(thread_id)
        if pending_token:
            if token == pending_token:
                async with aclosing(traced_stream(
                        self._stream_deploy_resume(thread_id, pending_token, confirmed=True),
                        "deploy_workflow:resume", thread_id=thread_id,
                        user_id=owner, session_id=_session_id(thread_id, "deploy"))) as events:
                    async for event in events:
                        yield event
                return
            # A different token is a typo or an old one pasted from higher up
            # the thread — neither may throw the current preview away.
            kind = "other" if token else _reply_kind(message)
            if kind == "no":
                async with aclosing(traced_stream(
                        self._stream_deploy_resume(thread_id, pending_token, confirmed=False),
                        "deploy_workflow:resume", thread_id=thread_id,
                        user_id=owner, session_id=_session_id(thread_id, "deploy"))) as events:
                    async for event in events:
                        yield event
                return
            if kind != "request":
                yield {"type": "token", "content": _token_reminder(pending_token, token)}
                yield {"type": "done"}
                return
            await self._resume_deploy(thread_id, pending_token, confirmed=False)
            yield {"type": "token", "content": (
                f"Replaced the pending `{pending_token}` — nothing was applied. "
                "Here is the new preview:")}
            # ...and fall through: the new request is previewed below.
        elif token:
            preview = adk_deploy._PENDING_PREVIEWS.get(token)
            if preview is not None and preview.get("owner") == owner:
                # Stateless fallback (e.g. reconnect with no tracked invocation)
                # — but only for the person the preview was minted for.
                # _PENDING_PREVIEWS is process-wide and keyed by token alone,
                # and the token is printed in the chat, so without this check
                # anyone who saw someone else's token could apply THEIR pending
                # deploy from their own thread (and spend the token doing it).
                result = await asyncio.to_thread(adk_deploy.apply_confirmed_deploy, message)
                yield {"type": "token", "content": self._format_deploy_apply_result(result)}
                yield {"type": "done", "mutated": True}
                return
            # A token that matches nothing: already used (tokens are single-use),
            # cancelled, or expired. Say so plainly — handing it to the chat model
            # produced a guess about what the user "intended to confirm".
            yield {"type": "token", "content": (
                f"`{token}` isn't waiting on this thread — it was already used, "
                "cancelled, or has expired. Nothing was applied. Preview the deploy "
                "again to get a new token."
            )}
            yield {"type": "done"}
            return

        if _looks_like_deploy_request(message):
            async with aclosing(traced_stream(
                    self._stream_deploy_preview(message, thread_id),
                    "deploy_workflow:deterministic", thread_id=thread_id,
                    user_id=_user_id(), session_id=_session_id(thread_id, "deploy"))) as events:
                async for event in events:
                    yield event
            return

        # No model: the chat is the fixed commands (the pills and the same
        # phrases typed), run by the commands Workflow. Nothing is classified —
        # a message that is not a command is told what is.
        if not settings.llm_enabled:
            async with aclosing(traced_stream(
                    self._stream_command(message, thread_id),
                    "command", thread_id=thread_id,
                    user_id=owner, session_id=_session_id(thread_id, "command"))) as events:
                async for event in events:
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
            async with aclosing(traced_stream(
                    self._stream_deploy_preview(payload, thread_id),
                    "deploy_workflow:classifier", thread_id=thread_id, detail=payload,
                    user_id=_user_id(), session_id=_session_id(thread_id, "deploy"))) as events:
                async for event in events:
                    yield event
            return

        # The one lane that may be aborted: an LlmAgent turn with read-only
        # tools, where stopping early loses nothing but a reply nobody reads.
        async with aclosing(traced_stream(
                self._run_chat_agent(_content_from_text(message), thread_id, abort_signal=abort_signal),
                "chat", thread_id=thread_id,
                user_id=_user_id(), session_id=_session_id(thread_id, "chat"))) as events:
            async for event in events:
                yield event

    async def _stream_investigation(
        self, request: dict[str, str], thread_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        """One investigation: evidence collected in code, then one model step.

        Like the deploy and commands lanes it never receives the endpoint's
        abort signal: the Workflow is iterated to its natural end and, if the
        reader has left, the turn simply finishes detached. Nothing in it
        changes anything, so a reader who left costs a model call, not safety.
        """
        from . import features, identity

        if not features.allowed("support-triage", identity.current()):
            yield {"type": "token", "content": features.refusal("support-triage")}
            yield {"type": "done", "mutated": False}
            return
        run = investigate_workflow.InvestigateRun(
            business_date=request.get("business_date", ""), incident_id=request.get("incident_id", ""))
        # The mode is part of the key: an answer written by a model is not the
        # answer for a deployment (or a test) running without one, or on another.
        key = (run.business_date, run.incident_id, f"{settings.llm_enabled}:{run.model}")
        shared = _finding_hit(key)
        if shared is None:
            lock = _finding_lock(key)
            if lock.locked():
                yield {"type": "progress",
                       "content": "Someone is already investigating this incident — waiting for that finding"}
            async with lock:
                shared = _finding_hit(key)
                if shared is None:
                    async with aclosing(investigate_workflow.stream_run(
                            self.investigate_runner, run, user_id=_user_id(),
                            session_id=_session_id(thread_id, "investigate"))) as events:
                        async for event in events:
                            yield event
                    _finding_keep(key, run)
        if shared is not None:
            # ONE finding per incident, shared for ten minutes. Load test: ten
            # people on one incident made ten model calls, the sandbox throttled
            # them (429, 8–16 s backoffs) and the slowest waited 59 s for an
            # answer the first already had. It is also the same answer for
            # everyone, which a model asked ten times would not give.
            age = max(0, int(time.time() - shared[0]))
            run = shared[1]
            yield {"type": "progress", "content": f"Using the finding written {age} s ago for this incident"}
            yield {"type": "token", "content": run.text}
        bundle = run.bundle if isinstance(run.bundle, dict) else {}
        if bundle.get("ok"):
            await self._remember_investigation(thread_id, run)
            incident = bundle.get("incident") if isinstance(bundle.get("incident"), dict) else {}
            data = {
                "business_date": str(bundle.get("business_date") or run.business_date),
                "incident_id": str(incident.get("id") or run.incident_id),
                "title": str(incident.get("title") or ""),
                "model": run.model if run.calls else "",
                "model_calls": run.calls,
                "seconds": run.seconds,
            }
            if shared is not None:
                data["shared"] = True
            yield {"type": "investigation", "data": data}
        yield {"type": "done", "mutated": False}

    # What the chat lane learns of an investigation: kept small — a follow-up
    # needs the finding, not the evidence (the tools can read that again).
    _INVESTIGATION_KEY = chat_agent_module.LAST_INVESTIGATION_KEY
    _INVESTIGATION_MAX_CHARS = 4000

    async def _remember_investigation(self, thread_id: str, run: Any) -> None:
        """Write the finding to the CHAT session's state, for follow-up questions.

        State only, never a message event: the chat session may hold a paused
        approval, and a text turn between a function call and its response would
        break the order the model requires. The chat agent's instruction reads
        it back (agent._root_instruction). Best-effort: the answer is already
        on the person's screen.
        """
        try:
            from google.adk.events.event import Event
            from google.adk.events.event_actions import EventActions

            session = await self._chat_session(thread_id)
            if session is None:
                session = await self.session_service.create_session(
                    app_name=self.chat_runner.app_name, user_id=_user_id(),
                    session_id=_session_id(thread_id, "chat"))
            incident = (run.bundle or {}).get("incident")
            await self.session_service.append_event(
                session=session,
                event=Event(author="release_copilot", actions=EventActions(state_delta={
                    self._INVESTIGATION_KEY: {
                        "incident_id": run.incident_id,
                        "business_date": str((run.bundle or {}).get("business_date") or run.business_date),
                        "title": str((incident or {}).get("title") or "") if isinstance(incident, dict) else "",
                        "finding": run.text[:self._INVESTIGATION_MAX_CHARS],
                    }})),
            )
        except Exception:
            logger.debug("could not remember the investigation for %s", thread_id, exc_info=True)

    async def _stream_deploy_preview(
        self, message: str, thread_id: str
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Run the deploy Workflow's preview turn and surface the confirmation interrupt."""
        said_something = False
        reported = ""
        paused = False
        # Run to the END even past the pause — do not break at the token. A run
        # left half-consumed is finalised later by the garbage collector, in
        # another context: ADK then cancels the Workflow's leftover tasks there
        # and (2.11) logs "Failed to detach context" at ERROR for each — seen on
        # GKE on every paused preview. After the pause the run ends on its own.
        async for event in self.deploy_runner.run_async(
            user_id=_user_id(),
            session_id=_session_id(thread_id, "deploy"),
            new_message=_content_from_text(message),
        ):
            if paused:
                continue
            for label in _progress_events(event):
                yield {"type": "progress", "content": label}
            text = _text_from_event(event)
            if text:
                said_something = True
                yield {"type": "token", "content": text}
            # A preview that could NOT be built routes straight to cancel, which
            # carries its reason in the node's output rather than as text — so
            # without this the form is submitted and the user is told nothing at
            # all (found live: a malformed date answered with silence). Both the
            # gate's rejecting Event and the cancel node carry the same reason,
            # so it is said ONCE.
            failure = getattr(event, "output", None)
            if isinstance(failure, dict) and failure.get("error") and not failure.get("ok"):
                reason = str(failure["error"])
                if reason != reported:
                    reported = reason
                    said_something = True
                    yield {"type": "token", "content": reason}
            token = _interrupt_token_from_event(event)
            if token:
                pending = adk_deploy._PENDING_PREVIEWS.get(token, {})
                request = pending.get("request", {})
                environment = request.get("environment", "uat")
                self._pending_deploy[(_user_id(), thread_id)] = token
                yield {
                    "type": "interrupt",
                    "data": {
                        "type": "confirmation",
                        "token": token,
                        "proposed": pending.get("preview", {}),
                        "environment": environment,
                        "message": (f"Reply with exactly `{token}` to create this release."
                                    if request.get("deployment_type") == "release"
                                    else f"Reply with exactly `{token}` to apply this deploy."),
                    },
                }
                said_something = True
                paused = True
        if not said_something:
            # Belt and braces: the lane must never answer a submitted form with
            # an empty turn — silence reads as "it worked" and it did not.
            yield {"type": "token", "content": (
                "That could not be previewed and nothing was changed — the deploy "
                "graph ended without saying why. Check the server log for this thread.")}
        yield {"type": "done"}

    async def _resume_deploy(self, thread_id: str, token: str, confirmed: bool) -> dict[str, Any]:
        """Resume the paused deploy Workflow and return the terminal node's output."""
        self._pending_deploy.pop((_user_id(), thread_id), None)
        result: dict[str, Any] | None = None
        async for event in self.deploy_runner.run_async(
            user_id=_user_id(),
            session_id=_session_id(thread_id, "deploy"),
            new_message=_confirmation_response(token, confirmed),
        ):
            output = getattr(event, "output", None)
            if output is not None:
                result = output
        return result or {}

    async def _stream_deploy_resume(
        self, thread_id: str, token: str, confirmed: bool
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Resume the paused deploy Workflow with the user's confirmation."""
        result = await self._resume_deploy(thread_id, token, confirmed)
        if confirmed:
            yield {"type": "token", "content": self._format_deploy_apply_result(result)}
        else:
            # An explicit "no": say what it did in the person's own terms, not
            # the node's ("Not applied: cancelled").
            yield {"type": "token", "content": (
                f"Cancelled `{token}` — nothing was applied. Ask again when you're ready.")}
        # A cancelled resume applied nothing: saying otherwise makes every
        # wrong-token reply re-read the release banner for no reason.
        yield {"type": "done", "mutated": bool(confirmed)}

    async def _stream_command(self, message: str, thread_id: str) -> AsyncGenerator[dict[str, Any], None]:
        """One command through the commands Workflow: its answer, or its pause."""
        result: dict[str, Any] | None = None
        approval_id = None
        # Run to the end even past the pause: leaving the runner's generator
        # early cancels the Workflow's leftover tasks mid-flight.
        async for event in self.commands_runner.run_async(
            user_id=_user_id(),
            session_id=_session_id(thread_id, "command"),
            new_message=_content_from_text(message),
        ):
            output = getattr(event, "output", None)
            if isinstance(output, dict) and "text" in output:
                result = output
            approval_id = _interrupt_token_from_event(event) or approval_id
        if approval_id:
            self._pending_commands[(_user_id(), thread_id)] = approval_id
            pending = ((await self._command_state(thread_id)) or {}).get("command_pending") or {}
            yield {"type": "interrupt", "data": release_commands.approval_payload(
                pending.get("name") or "", pending.get("label") or "This operation")}
            yield {"type": "done"}
            return
        result = result or {"text": "That command ended without an answer — nothing was changed."}
        yield {"type": "token", "content": str(result.get("text") or "")}
        yield {"type": "done", "mutated": bool(result.get("mutated"))}

    async def _resume_command(self, thread_id: str, approval_id: str, confirmed: bool) -> dict[str, Any]:
        """Answer the commands Workflow's pause; the terminal node's output."""
        self._pending_commands.pop((_user_id(), thread_id), None)
        result: dict[str, Any] = {}
        async for event in self.commands_runner.run_async(
            user_id=_user_id(),
            session_id=_session_id(thread_id, "command"),
            new_message=_confirmation_response(approval_id, confirmed),
        ):
            output = getattr(event, "output", None)
            if isinstance(output, dict) and "text" in output:
                result = output
        return result

    async def _stream_command_resume(
        self, thread_id: str, approval_id: str, confirmed: bool
    ) -> AsyncGenerator[dict[str, Any], None]:
        result = await self._resume_command(thread_id, approval_id, confirmed)
        yield {"type": "token", "content": str(result.get("text") or "Nothing was changed.")}
        yield {"type": "done", "mutated": bool(result.get("mutated"))}

    async def _command_state(self, thread_id: str) -> dict[str, Any] | None:
        return await self._session_state(self.commands_runner, thread_id, "command")

    async def _command_state_value(self, thread_id: str, key: str) -> str | None:
        value = ((await self._command_state(thread_id)) or {}).get(key)
        return str(value) if value else None

    async def _run_chat_agent(
        self,
        content: types.Content,
        thread_id: str,
        invocation_id: str | None = None,
        approved: tuple[str, str] | None = None,
        abort_signal: asyncio.Event | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a chat turn, surfacing prod-ops confirmations and persisting memory.

        If the agent calls a confirmation-gated tool, the run pauses: the pending
        call is stored and surfaced as a ``confirmation`` interrupt. Otherwise the
        turn completes and — when memory is enabled — the session is saved to the
        memory service so future turns can recall it.

        ``abort_signal`` (set when the person has left) ends the turn — but NOT
        by handing it to ADK directly. ADK 2.11 cancels its root task the moment
        its signal is set and seals every unanswered function call with an
        ``INVOCATION_ABORTED`` error; our tools do their GitHub/BigQuery work on
        a worker thread, which a cancelled await does not stop. A promotion in
        flight would land AND be recorded as aborted — the next turn's model
        would believe nothing happened. So ADK gets a private ``stop`` signal,
        set only while no tool is running: a tool in flight finishes, its real
        answer is recorded, and the turn stops right after it.
        """
        interrupted = False
        mutated = False          # did this turn change release/deploy state?
        said_anything = False
        approved_note = ""       # the approved operation's own result sentence
        repeat = None            # the model asking to run the approved operation AGAIN
        landed: list[tuple[str, str]] = []   # state-changing tools that returned this turn
        stop = asyncio.Event() if abort_signal is not None else None
        tools_running = 0        # function calls the model made that have not answered yet

        async def _stop_when_the_reader_left() -> None:
            await abort_signal.wait()
            if not tools_running:    # only the model is busy: nothing to wait for
                stop.set()

        watcher = asyncio.create_task(_stop_when_the_reader_left()) if abort_signal is not None else None
        try:
            # aclosing: the two breaks below leave the run early, and only a
            # closed generator seals an aborted invocation NOW rather than
            # whenever the garbage collector finalises it — the abort check
            # after this block relies on the sealing having happened.
            async with aclosing(self.chat_runner.run_async(
                user_id=_user_id(),
                session_id=_session_id(thread_id, "chat"),
                invocation_id=invocation_id,
                new_message=content,
                abort_signal=stop,
            )) as run:
                async for event in run:
                    tools_running = max(0, tools_running + len(event.get_function_calls() or [])
                                        - len(event.get_function_responses() or []))
                    for label in _progress_events(event):
                        yield {"type": "progress", "content": label}
                    # A change is a tool that RAN, not one the model called: a call
                    # that pauses for approval changed nothing (load test: a paused
                    # promotion marked a "deploy status" turn as a change).
                    ran = _landed_changes(event)
                    mutated = mutated or bool(ran)
                    landed += ran
                    text = _text_from_event(event)
                    if text:
                        said_anything = True
                        yield {"type": "token", "content": text}
                    if approved:
                        approved_note = _result_note(event, approved[0]) or approved_note
                    pending = _pending_call_from_event(event)
                    if pending is not None and approved and approved_note and _original_call(pending) == approved:
                        # Seen live: after an approved PRD promotion ran, the model asked to
                        # run the SAME promotion again — a second approval for something
                        # already done. Decline it here instead of asking the person.
                        repeat = pending
                        break
                    if pending is not None:
                        self._pending_adk_calls[(_user_id(), thread_id)] = pending
                        await self._persist_pending_call(thread_id, pending)
                        yield {"type": "interrupt", "data": _confirmation_interrupt_payload(pending)}
                        interrupted = True
                        break
                    if abort_signal is not None and abort_signal.is_set() and not tools_running:
                        # The reader left while a tool was running; it has now
                        # answered and been recorded above. Stop here.
                        stop.set()
                        break
        except Exception:
            if watcher is not None:
                watcher.cancel()
            if not landed:
                raise           # nothing ran: the endpoint's "nothing was changed" is true
            # Found under load: Vertex answered 429 AFTER an approved promotion
            # had run, and the person was told "Nothing was changed". The tool's
            # own sentence says what happened; the model's summary is what failed.
            logger.warning("Chat turn failed after %s ran | thread=%s",
                           ", ".join(n for n, _ in landed), thread_id, exc_info=True)
            done = "\n".join(f"- **{n.replace('_', ' ')}** ran: {note or 'see the release banner'}"
                             for n, note in landed)
            yield {"type": "token", "content": (
                f"{done}\n\nThe AI model then failed before it could finish its reply, so "
                "check the release banner before trying anything again — do NOT repeat the "
                "operation above.")}
            yield {"type": "done", "mutated": True}
            return

        if repeat is not None:
            # Answer the repeat with "no" so the session never holds an open
            # approval, and say what actually happened: the one approved run.
            # The model's reply to that "no" is not shown — it would describe a
            # rejection the person never made.
            logger.info("Declined a repeat of an already-approved %s | thread=%s", approved[0], thread_id)
            async for _ in self.chat_runner.run_async(
                user_id=_user_id(),
                session_id=_session_id(thread_id, "chat"),
                invocation_id=repeat.invocation_id,
                new_message=_content_from_pending_reply("no", repeat),
            ):
                pass
            if not said_anything:
                yield {"type": "token", "content": approved_note}
        if watcher is not None:
            watcher.cancel()
        if stop is not None and stop.is_set():
            # The person left mid-turn and ADK sealed the invocation (``stop``,
            # not the endpoint's signal: only a stop we actually gave ADK seals
            # anything — a pause reached before it stays a valid approval). A pause
            # that arrived in the same breath was sealed with it, so the "yes"
            # a reminder would invite could answer nothing: drop it from both
            # stores rather than leave an approval that cannot be acted on. The
            # tools that ran before the stop are still recorded in the session
            # and the event log; an aborted turn is not saved to memory because
            # it is not a turn the model finished.
            if interrupted:
                self._pending_adk_calls.pop((_user_id(), thread_id), None)
                await self._persist_pending_call(thread_id, None)
            logger.info("Chat turn aborted: the client went away | thread=%s | ran=%s | paused=%s",
                        thread_id, ", ".join(n for n, _ in landed) or "-", interrupted)
            yield {"type": "done", "mutated": mutated}
            return
        if not interrupted and settings.adk_memory_enabled:
            await self._persist_session_to_memory(thread_id)
        yield {"type": "done", "mutated": mutated}

    # A paused prod-op approval is four strings, so it travels as plain data —
    # the same reasoning that let the deploy preview move into session state.
    # It is written on the CHAT session because that is the lane that paused.
    _PENDING_CALL_KEY = "pending_tool_confirmation"

    async def _chat_session(self, thread_id: str):
        return await self.session_service.get_session(
            app_name=self.chat_runner.app_name,
            user_id=_user_id(),
            session_id=_session_id(thread_id, "chat"),
        )

    async def _dismiss_pending_call(self, thread_id: str, pending: PendingAdkCall) -> None:
        """Answer a paused approval with "no" without showing the model's reply.

        The person did not reject anything — they asked for something else — so
        the model's "you rejected it" would describe a decision never made. The
        pause still has to be answered, or the chat session keeps an open
        approval that the next plain message would be read against.
        """
        try:
            async with aclosing(self._run_chat_agent(
                    _content_from_pending_reply("no", pending), thread_id,
                    invocation_id=pending.invocation_id, approved=None)) as events:
                async for _ in events:
                    pass
        except Exception:  # noqa: BLE001 — already cleared from both stores; the new request must go on
            logger.warning("could not dismiss the paused approval on %s", thread_id, exc_info=True)

    async def _persist_pending_call(
        self, thread_id: str, pending: PendingAdkCall | None
    ) -> None:
        """Record the paused approval in session state.

        The chat agent is an LlmAgent, not a Workflow, so there is no node to
        yield state from — the supported way to write state outside a node is an
        event carrying a state_delta, which is what the Workflow does internally.
        Best-effort: failing to persist must not break the turn that paused.
        """
        try:
            from google.adk.events.event import Event
            from google.adk.events.event_actions import EventActions

            session = await self._chat_session(thread_id)
            if session is None:
                return
            await self.session_service.append_event(
                session=session,
                event=Event(
                    author="release_copilot",
                    actions=EventActions(state_delta={
                        self._PENDING_CALL_KEY: {
                            "invocation_id": pending.invocation_id,
                            "function_call_id": pending.function_call_id,
                            "function_name": pending.function_name,
                            "args": pending.args,
                        } if pending is not None else None,
                    }),
                ),
            )
        except Exception:
            logger.debug("could not persist paused approval for %s", thread_id, exc_info=True)

    async def _pending_call_from_session(self, thread_id: str) -> PendingAdkCall | None:
        """The paused approval this thread is waiting on, from session state."""
        try:
            session = await self._chat_session(thread_id)
        except Exception:
            logger.debug("paused-approval lookup failed for %s", thread_id, exc_info=True)
            return None
        raw = ((session.state if session else None) or {}).get(self._PENDING_CALL_KEY)
        if not isinstance(raw, dict) or not raw.get("function_call_id"):
            return None
        return PendingAdkCall(
            invocation_id=str(raw.get("invocation_id") or ""),
            function_call_id=str(raw["function_call_id"]),
            function_name=str(raw.get("function_name") or ""),
            args=dict(raw.get("args") or {}),
        )

    async def _pending_token_from_session(self, thread_id: str) -> str | None:
        """The CONFIRM token this thread is waiting on, read from session state.

        The deploy Workflow writes it there on every preview, so this answers
        even when the preview was served by a different process. Returns None on
        any failure: a routing hint must never break a chat turn.
        """
        return await self._deploy_state_value(thread_id, "deploy_confirm_token")

    async def _deploy_state_value(self, thread_id: str, key: str) -> str | None:
        value = ((await self._session_state(self.deploy_runner, thread_id, "deploy")) or {}).get(key)
        return str(value) if value else None

    async def _session_state(self, runner: Runner, thread_id: str, lane: str) -> dict[str, Any] | None:
        """A Workflow's session state for this person's thread; None on any
        failure — a routing hint must never break a chat turn."""
        try:
            session = await self.session_service.get_session(
                app_name=runner.app_name,
                user_id=_user_id(),
                session_id=_session_id(thread_id, lane),
            )
        except Exception:
            logger.debug("%s-state lookup failed for %s", lane, thread_id, exc_info=True)
            return None
        return dict(session.state) if session and session.state else None

    async def _persist_session_to_memory(self, thread_id: str) -> None:
        """Best-effort: add the finished chat session to the memory service."""
        try:
            session = await self.session_service.get_session(
                app_name=chat_app.name,
                user_id=_user_id(),
                session_id=_session_id(thread_id, "chat"),
            )
            if session is not None:
                await self.memory_service.add_session_to_memory(session)
        except Exception:  # memory is best-effort; never break a chat turn
            logger.debug("memory persistence failed for thread %s", thread_id, exc_info=True)

    @staticmethod
    def _format_deploy_apply_result(result: dict[str, Any]) -> str:
        # An apply that failed part-way may have landed some of it, so "Not
        # applied" would be false: the note — what may and may not have
        # happened, and that the token is spent — is the whole answer.
        if result.get("status") == "apply_error":
            parts = [str(result.get("note") or "").strip(), str(result.get("error") or "").strip()]
            return "\n\n".join(p for p in parts if p) or "The deploy did not complete."
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
                run = result.get("run") or {}
                run_link = (f"[Run #{run.get('id')}]({run['url']})" if run.get("url")
                            else (f"[the DF runs]({result['runs_page']})"
                                  if result.get("runs_page") else "the DF run"))
                # Raised at dispatch, before the build finishes, on purpose:
                # whether the run is good enough to merge on is the person's
                # call, so hand them the run right beside the PR.
                note += (f"\n\nComposer DAGs: [PR #{bump.get('pr_number')}]({bump['pr_url']}) "
                         f"raised. Check {run_link} and merge the PR only once it is "
                         "green — that call is yours; nothing merges automatically.")
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
