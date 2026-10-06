"""The chat turn's abort-on-disconnect on google-adk 2.11, end to end.

Real ADK runtime (LlmAgent, tool confirmation, the deploy Workflow, sessions)
and, for the endpoint, a real uvicorn socket that is closed mid-turn — the one
thing a TestClient cannot do. Only the model is scripted, and GitHub is never
reached. What must hold (AGENTS.md invariant 1):

- only the free-form chat lane stops when the reader leaves; a deploy preview
  runs to its end and its CONFIRM token is still usable afterwards;
- an aborted chat turn leaves no approval behind that a later "yes" could answer;
- a state-changing tool that was already running when the reader left still
  lands its real result in the session — never the synthetic abort error.
"""
import asyncio
import contextlib
import json
import logging
import socket
import threading
import time

import httpx
import pytest
import uvicorn
from google.adk import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.tools import FunctionTool
from google.genai import types

from adk_release_agent import approvals, deploy
from release_agent import adk_service, app_fastapi
from release_agent.adk_service import AdkChatService
from release_agent.config import settings

ABORTED = "INVOCATION_ABORTED"


class _Scripted(BaseLlm):
    """A model that follows a fixed script: ask for the tool until it has been
    answered ``tool_rounds`` times, then say ``final``. Counts its calls."""

    model: str = "scripted"
    tool: str = "promote_release"
    args: dict = {}
    tool_rounds: int = 1
    final: str = "All done."
    delay: float = 0.0
    calls: list = []

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.calls.append(time.monotonic())
        if self.delay:
            await asyncio.sleep(self.delay)
        last_user_text = max((i for i, c in enumerate(llm_request.contents)
                              if c.role == "user" and any(p.text for p in c.parts or [])), default=-1)
        answered = sum(1 for c in llm_request.contents[last_user_text + 1:] for p in (c.parts or [])
                       if p.function_response and p.function_response.name == self.tool)
        if answered < self.tool_rounds:
            part = types.Part(function_call=types.FunctionCall(name=self.tool, args=dict(self.args)))
        else:
            part = types.Part(text=self.final)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def _chat_runner(service: AdkChatService, llm: _Scripted, tools: list) -> Runner:
    app = App(name="adk211_chat", root_agent=Agent(name="release_copilot_adk", model=llm, instruction="test",
                                                   tools=tools),
              resumability_config=ResumabilityConfig(is_resumable=True))
    return Runner(app=app, session_service=service.session_service, artifact_service=service.artifact_service,
                  memory_service=service.memory_service, auto_create_session=True)


@pytest.fixture
def no_classifier(monkeypatch):
    """The free-form deploy classifier is a model call; these turns are not deploys."""
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(adk_service.adk_intent, "deploy_payload_from_freeform", lambda message: None)


def _promotion(ran: list, seconds: float = 0.0):
    async def promote_release(target: str) -> dict:
        """Promote the current release to ``target``."""
        if seconds:
            await asyncio.sleep(seconds)
        ran.append(target)
        return {"ok": True, "note": f"Promoted to {target.upper()}"}
    return promote_release


async def _collect(stream) -> list[dict]:
    return [event async for event in stream]


async def _session_events(service: AdkChatService, thread: str):
    session = await service.session_service.get_session(
        app_name=service.chat_runner.app_name, user_id=adk_service._user_id(),
        session_id=adk_service._session_id(thread, "chat"))
    return session, list(session.events)


def _text(events) -> str:
    return "".join(e.get("content") or "" for e in events if e.get("type") == "token")


# --- the yes/no approval on 2.11 (what a live "promote to PRD" turn does) --------

def test_an_approval_pauses_reminds_and_is_consumed_only_by_yes_or_no(no_classifier):
    ran: list = []
    service = AdkChatService()
    llm = _Scripted(args={"target": "prd"}, final="Reported.", calls=[])
    service.chat_runner = _chat_runner(service, llm, [
        FunctionTool(_promotion(ran), require_confirmation=approvals.RULES["promote_release"])])

    async def scenario():
        asked = await _collect(service.stream_chat("promote the release to prd", "t-no"))
        again = await _collect(service.stream_chat("what does that do?", "t-no"))
        refused = await _collect(service.stream_chat("no", "t-no"))
        after = await _collect(service.stream_chat("no", "t-no"))        # nothing is waiting any more
        ran_after_no = list(ran)
        asked2 = await _collect(service.stream_chat("promote the release to prd", "t-yes"))
        approved = await _collect(service.stream_chat("yes", "t-yes"))
        return asked, again, refused, after, ran_after_no, asked2, approved

    asked, again, refused, after, ran_after_no, asked2, approved = asyncio.run(scenario())

    interrupt = next(e["data"] for e in asked if e["type"] == "interrupt")
    assert interrupt["function"] == "promote_release" and interrupt["args"] == {"target": "prd"}
    assert asked[-1] == {"type": "done", "mutated": False}
    assert "still waiting for your answer" in _text(again) and again[-1] == {"type": "done"}
    assert refused[-1] == {"type": "done", "mutated": False}
    assert [e["type"] for e in after][-1] == "done" and not any(e["type"] == "error" for e in after)
    assert ran_after_no == []                               # neither "no" ran anything
    assert any(e["type"] == "interrupt" for e in asked2)
    assert approved[-1] == {"type": "done", "mutated": True}
    assert ran == ["prd"]                                   # once, and only for the "yes"
    assert (adk_service._user_id(), "t-yes") not in service._pending_adk_calls


# --- abort: the chat lane -------------------------------------------------------

def test_a_pause_that_arrives_as_the_reader_leaves_stays_answerable(no_classifier, caplog):
    """The pause was reached before anything was stopped, so ADK sealed nothing:
    the approval waits for an explicit answer, and a later 'yes' runs it ONCE."""
    ran: list = []
    service = AdkChatService()
    llm = _Scripted(args={"target": "prd"}, final="Reported.", calls=[])
    service.chat_runner = _chat_runner(service, llm, [
        FunctionTool(_promotion(ran), require_confirmation=approvals.RULES["promote_release"])])
    abort = asyncio.Event()
    key = (adk_service._user_id(), "t-gone")

    async def scenario():
        seen = []
        async for event in service.stream_chat("promote the release to prd", "t-gone", abort_signal=abort):
            seen.append(event)
            if event["type"] == "interrupt":
                abort.set()                                  # the reader leaves as the pause appears
        _, events = await _session_events(service, "t-gone")
        left_waiting = key in service._pending_adk_calls
        later = await _collect(service.stream_chat("yes", "t-gone"))
        return seen, events, left_waiting, later

    with caplog.at_level(logging.INFO):
        seen, events, left_waiting, later = asyncio.run(scenario())

    assert [e["type"] for e in seen][-2:] == ["interrupt", "done"]
    assert left_waiting is True
    assert not [e for e in events if e.error_code == ABORTED]          # nothing was sealed
    assert "Chat turn aborted" not in caplog.text
    assert not any(e["type"] == "error" for e in later)
    assert ran == ["prd"] and later[-1] == {"type": "done", "mutated": True}


async def _abort_mid_tool(service: AdkChatService, thread: str, abort: asyncio.Event, settle: float = 0.0):
    """One chat turn whose reader leaves 0.1 s into the tool call."""
    seen = []
    async for event in service.stream_chat("promote the release to uat", thread, abort_signal=abort):
        seen.append(event)
        if event["type"] == "progress":
            asyncio.get_running_loop().call_later(0.1, abort.set)
    if settle:
        await asyncio.sleep(settle)
    _, events = await _session_events(service, thread)
    answers = [r.response for e in events for r in e.get_function_responses() if r.name == "promote_release"]
    return seen, answers


def test_a_tool_in_flight_finishes_and_the_turn_stops_right_after_it(no_classifier, caplog):
    """Handing ADK the signal directly cancels its root task mid-await (2.11) —
    an async tool is cut off, a thread-backed one lands unrecorded. So the stop
    is held until no tool is running: the tool answers, is recorded, and only
    then does the turn end, with no further model call."""
    ran: list = []
    service = AdkChatService()
    llm = _Scripted(args={"target": "uat"}, final="Promoted.", calls=[])
    service.chat_runner = _chat_runner(service, llm, [_promotion(ran, seconds=0.3)])   # UAT: no approval

    with caplog.at_level(logging.INFO):
        seen, answers = asyncio.run(_abort_mid_tool(service, "t-mid", asyncio.Event(), settle=0.5))

    assert ran == ["uat"]                                                # it finished
    assert answers == [{"ok": True, "note": "Promoted to UAT"}]          # and its own answer is what is recorded
    assert seen[-1] == {"type": "done", "mutated": True}
    assert len(llm.calls) == 1                                           # no model call after the stop
    assert "ran=promote_release" in caplog.text


def _thread_backed_promotion(ran: list):
    from adk_release_agent import tools as release_tools

    def promote_release(target: str) -> dict:
        """Promote the current release to ``target``."""
        time.sleep(0.3)                    # the GitHub work, on a worker thread
        ran.append(target)
        return {"ok": True, "note": f"Promoted to {target.upper()}"}
    return release_tools.off_event_loop(promote_release)


def test_a_thread_backed_change_is_not_stopped_by_the_abort(no_classifier):
    """The portal's tools run on worker threads (off_event_loop), and a thread
    cannot be cancelled: the change LANDS even though the turn was aborted."""
    ran: list = []
    service = AdkChatService()
    llm = _Scripted(args={"target": "uat"}, calls=[])
    service.chat_runner = _chat_runner(service, llm, [_thread_backed_promotion(ran)])

    asyncio.run(_abort_mid_tool(service, "t-thread", asyncio.Event(), settle=0.6))

    assert ran == ["uat"]


# Was a strict xfail when found (ADK 2.11 abort + off_event_loop): the abort cancelled
# the await, not the worker thread, so a landed promotion was recorded as aborted.
def test_a_change_that_landed_while_the_reader_left_is_recorded_as_what_ran(no_classifier, caplog):
    ran: list = []
    service = AdkChatService()
    llm = _Scripted(args={"target": "uat"}, calls=[])
    service.chat_runner = _chat_runner(service, llm, [_thread_backed_promotion(ran)])

    with caplog.at_level(logging.INFO):
        seen, answers = asyncio.run(_abort_mid_tool(service, "t-landed", asyncio.Event(), settle=0.6))

    assert answers == [{"ok": True, "note": "Promoted to UAT"}]          # the tool's own answer, not the abort error
    assert seen[-1] == {"type": "done", "mutated": True}
    assert "ran=promote_release" in caplog.text


def test_a_turn_whose_reader_was_never_there_runs_nothing(no_classifier):
    ran: list = []
    service = AdkChatService()
    llm = _Scripted(args={"target": "uat"}, calls=[])
    service.chat_runner = _chat_runner(service, llm, [_promotion(ran)])
    abort = asyncio.Event()
    abort.set()

    seen = asyncio.run(_collect(service.stream_chat("promote the release to uat", "t-early", abort_signal=abort)))

    assert seen == [{"type": "done", "mutated": False}]
    assert ran == [] and llm.calls == []


# --- abort: through a real socket -----------------------------------------------

@contextlib.contextmanager
def _served():
    """The portal on a real localhost socket, in a thread, without startup tasks."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app_fastapi.app, lifespan="off", log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def _post_and_hang_up(port: int, thread_id: str, message: str, after: float) -> None:
    body = json.dumps({"thread_id": thread_id, "message": message}).encode()
    head = (f"POST /api/chat HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode()
    with socket.create_connection(("127.0.0.1", port)) as s:
        s.sendall(head + body)
        time.sleep(after)


def _chat(port: int, thread_id: str, message: str) -> list[dict]:
    r = httpx.post(f"http://127.0.0.1:{port}/api/chat", json={"thread_id": thread_id, "message": message}, timeout=30)
    return [json.loads(line[6:]) for line in r.text.splitlines() if line.startswith("data: ")]


def _wait(condition, seconds: float = 8.0) -> bool:
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


def test_a_deploy_preview_whose_reader_hangs_up_still_mints_a_usable_token(monkeypatch, caplog):
    """The deploy Workflow is never aborted: the preview finishes with nobody
    reading, and the token waits for the thread's next message."""
    deploy._PENDING_PREVIEWS.clear()
    real = deploy.prepare_deploy_preview

    def slow(**kwargs):
        time.sleep(0.8)                 # the hang-up lands while the preview is being built
        return real(**kwargs)

    monkeypatch.setattr(deploy, "prepare_deploy_preview", slow)
    service = app_fastapi.adk_chat_service
    thread_id = "adk211-socket-deploy"
    key = (adk_service._user_id(), thread_id)

    with caplog.at_level(logging.INFO), _served() as port:
        _post_and_hang_up(port, thread_id, "deploy abc-client-api-svc:1.1.1230 to uat", after=0.2)
        assert _wait(lambda: key in service._pending_deploy), "the preview did not finish after the hang-up"
        token = service._pending_deploy[key]
        assert _wait(lambda: not app_fastapi._detached_turns), "the detached turn is still held"
        cancelled = _chat(port, thread_id, "no")

    assert token.startswith("CONFIRM-")
    assert "Chat client gone" in caplog.text and thread_id in caplog.text      # the signal WAS tripped...
    assert "Chat turn aborted" not in caplog.text                              # ...and the Workflow ignored it
    assert f"Cancelled `{token}`" in _text(cancelled) and cancelled[-1] == {"type": "done", "mutated": False}
    assert key not in service._pending_deploy


def test_a_chat_turn_whose_reader_hangs_up_stops_early_and_is_sealed(monkeypatch, no_classifier, caplog):
    service = app_fastapi.adk_chat_service
    looked: list = []

    async def look(what: str) -> dict:
        """Read something (read-only)."""
        await asyncio.sleep(0.2)
        looked.append(what)
        return {"ok": True, "seen": what}

    llm = _Scripted(tool="look", args={"what": "uat"}, tool_rounds=4, delay=0.3, calls=[])
    monkeypatch.setattr(service, "chat_runner", _chat_runner(service, llm, [look]))
    thread_id = "adk211-socket-chat"

    with caplog.at_level(logging.INFO), _served() as port:
        _post_and_hang_up(port, thread_id, "what is deployed in uat right now?", after=0.4)
        assert _wait(lambda: "Chat turn aborted: the client went away | thread=" + thread_id in caplog.text), \
            "the chat lane never reported the abort"
        assert _wait(lambda: not app_fastapi._detached_turns), "the detached turn is still held"
        calls_at_stop = len(llm.calls)
        time.sleep(1.0)
        full = _chat(port, "adk211-socket-chat-full", "what is deployed in uat right now?")

    # A full turn is 5 model calls (4 tool rounds + the answer); the abandoned one stopped well short.
    assert 1 <= calls_at_stop < 5
    assert len(llm.calls) - calls_at_stop == 5 and _text(full) == "All done."
    session, events = asyncio.run(_session_events(service, thread_id))
    assert any(e.error_code == ABORTED for e in events)
    assert (adk_service._user_id(), thread_id) not in service._pending_adk_calls
    assert session.state.get(AdkChatService._PENDING_CALL_KEY) is None
