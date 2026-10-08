"""The chat SSE stream and the person who stopped reading it.

Starlette cancels a streaming response the instant its socket closes. Before
this, that cancellation landed inside the ADK run: the model call was torn
down mid-flight, tool work already on a thread carried on, and a Workflow
could have been stopped between its gate and apply nodes. Now the turn runs
detached from the response, the free-form chat agent is told to stop through
ADK's ``abort_signal``, and a Workflow run is never handed that signal.
"""
import asyncio

import pytest

from release_agent import adk_service as S
from release_agent import app_fastapi as F
from release_agent.adk_service import AdkChatService


class _Request:
    """A Starlette request as the watcher sees it: connected for a few polls."""

    def __init__(self, polls_before_gone):
        self.polls_before_gone = polls_before_gone
        self.polls = 0

    async def is_disconnected(self):
        self.polls += 1
        return self.polls > self.polls_before_gone


def _fake_stream(monkeypatch, seen):
    """stream_chat as a turn that talks, then waits for its abort, then ends."""

    async def stream_chat(message, thread_id, abort_signal=None):
        seen["signal"] = abort_signal
        yield {"type": "token", "content": "thinking..."}
        await abort_signal.wait()
        seen["stopped"] = True
        yield {"type": "done"}

    monkeypatch.setattr(F.adk_chat_service, "stream_chat", stream_chat)
    monkeypatch.setattr(F, "_DISCONNECT_POLL_SECONDS", 0.01)


async def _settled():
    """Let every task that has nothing left to do finish."""
    for _ in range(5):
        await asyncio.sleep(0)


def test_the_watcher_aborts_the_turn_when_the_request_reports_disconnected(monkeypatch):
    seen = {}
    _fake_stream(monkeypatch, seen)
    request = _Request(polls_before_gone=2)

    async def run():
        chunks = [c async for c in F._chat_events(request, "hi", "t-gone", None)]
        await _settled()
        return chunks, [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    chunks, leftover = asyncio.run(asyncio.wait_for(run(), 5))

    assert request.polls >= 3, "the watcher kept asking until the request said gone"
    assert seen["signal"].is_set() and seen["stopped"], "the chat lane was told to stop"
    assert chunks[0].startswith('data: {"type": "token"') and chunks[-1].startswith('data: {"type": "done"')
    assert leftover == [] and F._detached_turns == set(), "nothing left running or unreferenced"


def test_closing_the_stream_early_aborts_the_chat_but_lets_the_turn_finish(monkeypatch):
    """Starlette's cancel arrives in the relaying generator; the turn is not
    cancelled with it — it is aborted and runs to its own end."""
    seen = {}
    _fake_stream(monkeypatch, seen)
    request = _Request(polls_before_gone=10_000)

    async def run():
        events = F._chat_events(request, "hi", "t-closed", None)
        first = await events.__anext__()

        async def read_the_rest():
            return [c async for c in events]

        reader = asyncio.create_task(read_the_rest())
        await asyncio.sleep(0.02)          # parked on the relay, the turn still running
        reader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await reader
        turn = next(iter(F._detached_turns), None)
        if turn is not None:
            await asyncio.wait_for(turn, 2)
        await _settled()
        return first, [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]

    first, leftover = asyncio.run(asyncio.wait_for(run(), 5))

    assert first.startswith('data: {"type": "token"')
    assert seen["signal"].is_set() and seen["stopped"], "the turn was aborted, and finished on its own"
    assert leftover == [] and F._detached_turns == set()


class _Recorder:
    """A runner that records what it was asked and ends the lane at once."""

    app_name = "adk_release_agent"

    def __init__(self, event):
        self.calls = []
        self.event = event

    async def run_async(self, **kwargs):
        self.calls.append(kwargs)
        yield self.event


class _Done:
    """An event every lane reads as 'the run ended with nothing to show'."""

    content = None
    actions = None
    output = None
    author = "x"
    invocation_id = "i"
    long_running_tool_ids = set()

    def get_function_calls(self):
        return []

    def get_function_responses(self):
        return []


def _service(monkeypatch):
    service = AdkChatService.__new__(AdkChatService)
    service._last_seen, service._running = {}, {}
    service._pending_adk_calls = {}
    service._pending_deploy = {}
    service._pending_commands = {}

    async def none(*a, **k):
        return None

    for name in ("_pending_call_from_session", "_pending_token_from_session",
                 "_persist_pending_call", "_command_state_value", "_command_state",
                 "_persist_session_to_memory"):
        monkeypatch.setattr(service, name, none, raising=False)
    monkeypatch.setattr(S.adk_parsing, "is_queue_intent", lambda m: False)
    monkeypatch.setattr(S.adk_intent, "deploy_payload_from_freeform", lambda m: None)
    service.deploy_runner = _Recorder(_Done())
    service.commands_runner = _Recorder(_Done())
    service.chat_runner = _Recorder(_Done())
    return service


def _drain(service, message, abort):
    async def run():
        return [e async for e in service.stream_chat(message, "t-scope", abort_signal=abort)]

    return asyncio.run(run())


def test_a_deploy_workflow_run_is_never_given_the_abort_signal(monkeypatch):
    service = _service(monkeypatch)
    abort = asyncio.Event()

    _drain(service, "deploy abc-client-api-svc:1.1.1230 to uat", abort)

    assert len(service.deploy_runner.calls) == 1, "the deterministic lane previewed it"
    assert "abort_signal" not in service.deploy_runner.calls[0]
    assert service.chat_runner.calls == []


def test_a_commands_workflow_run_is_never_given_the_abort_signal(monkeypatch):
    service = _service(monkeypatch)
    monkeypatch.setattr(S.settings, "llm_enabled", False)
    abort = asyncio.Event()

    _drain(service, "release queue", abort)

    assert len(service.commands_runner.calls) == 1
    assert "abort_signal" not in service.commands_runner.calls[0]
    assert service.deploy_runner.calls == [] and service.chat_runner.calls == []


def test_the_free_form_chat_lane_is_the_one_that_gets_it(monkeypatch):
    service = _service(monkeypatch)
    monkeypatch.setattr(S.settings, "llm_enabled", True)
    abort = asyncio.Event()

    _drain(service, "what is deployed in uat?", abort)

    assert len(service.chat_runner.calls) == 1
    # ADK gets a PRIVATE stop signal, never the endpoint's own: handing it the
    # endpoint's would cancel a tool's await while its worker thread carries on.
    given = service.chat_runner.calls[0]["abort_signal"]
    assert isinstance(given, asyncio.Event) and given is not abort
    assert service.deploy_runner.calls == [] and service.commands_runner.calls == []


def test_an_aborted_turn_drops_the_pause_the_abort_sealed(monkeypatch):
    """The person left while only the model was busy, so ADK was told to stop
    and sealed the invocation; a confirmation that still came through was sealed
    with it: no reminder may invite a 'yes' to it."""
    from types import SimpleNamespace

    from google.genai import types as T

    service = _service(monkeypatch)
    persisted = []

    async def persist(thread_id, pending):
        persisted.append(pending)

    monkeypatch.setattr(service, "_persist_pending_call", persist, raising=False)
    abort = asyncio.Event()

    class _Pause(_Done):
        long_running_tool_ids = {"c1"}

        def get_function_calls(self):
            return [SimpleNamespace(id="c1", name="adk_request_confirmation", args={
                "originalFunctionCall": {"id": "o1", "name": "promote_release",
                                         "args": {"target": "prd"}}})]

    class _PausingRunner(_Recorder):
        async def run_async(self, **kwargs):
            abort.set()                 # the reader left while the model was answering
            for _ in range(3):
                await asyncio.sleep(0)  # …and the stop reached ADK before the pause came out
            assert kwargs["abort_signal"].is_set()
            yield _Pause()

    service.chat_runner = _PausingRunner(None)

    async def run():
        return [e async for e in service._run_chat_agent(
            T.Content(role="user", parts=[T.Part(text="promote it")]), "t-pause", abort_signal=abort)]

    events = asyncio.run(run())

    assert any(e["type"] == "interrupt" for e in events)
    assert service._pending_adk_calls == {}, "the sealed pause is not left waiting for a yes"
    assert persisted[-1] is None, "and not in session state either"
    assert events[-1] == {"type": "done", "mutated": False}


def test_a_pause_reached_before_the_reader_left_stays_a_valid_approval(monkeypatch):
    """Nothing was stopped, so nothing was sealed: the approval waits for an
    explicit yes or no, exactly as when the tab is simply closed at a pause."""
    from types import SimpleNamespace

    from google.genai import types as T

    service = _service(monkeypatch)
    abort = asyncio.Event()

    class _Pause(_Done):
        long_running_tool_ids = {"c1"}

        def get_function_calls(self):
            return [SimpleNamespace(id="c1", name="adk_request_confirmation", args={
                "originalFunctionCall": {"id": "o1", "name": "promote_release",
                                         "args": {"target": "prd"}}})]

    class _PausingRunner(_Recorder):
        async def run_async(self, **kwargs):
            yield _Pause()

    service.chat_runner = _PausingRunner(None)

    async def run():
        seen = []
        async for e in service._run_chat_agent(
                T.Content(role="user", parts=[T.Part(text="promote it")]), "t-pause", abort_signal=abort):
            seen.append(e)
            if e["type"] == "interrupt":
                abort.set()             # the reader leaves as the pause appears
        return seen

    events = asyncio.run(run())

    assert [e["type"] for e in events] == ["interrupt", "done"]
    assert list(service._pending_adk_calls) == [(S._user_id(), "t-pause")]
