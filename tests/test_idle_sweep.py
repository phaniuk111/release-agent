"""A conversation nobody writes to for SESSION_IDLE_MINUTES is forgotten: its
ADK sessions, what was pending on it, and the GitHub token connected to it."""
from __future__ import annotations

import asyncio
import time

from adk_release_agent import deploy
from release_agent.adk_service import AdkChatService, _session_id, _user_id
from release_agent.session_creds import SessionCredentials, SessionCredentialStore

HOUR = 3600.0


def _preview(service: AdkChatService, thread_id: str) -> None:
    async def _run():
        return [e async for e in service.stream_chat("deploy abc-client-api-svc:1.1.1230 to uat", thread_id)]

    events = asyncio.run(_run())
    assert any(e.get("type") == "interrupt" for e in events)


def _deploy_session(service: AdkChatService, thread_id: str):
    return asyncio.run(service.session_service.get_session(
        app_name=service.deploy_runner.app_name, user_id=_user_id(),
        session_id=_session_id(thread_id, "deploy")))


def test_an_idle_conversation_is_forgotten_with_what_was_pending_on_it():
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    _preview(service, "idle-old")
    key = (_user_id(), "idle-old")
    assert _deploy_session(service, "idle-old") is not None
    assert key in service._pending_deploy

    dropped = asyncio.run(service.sweep_idle(HOUR, now=time.monotonic() + HOUR + 1))

    assert dropped == [key]
    assert _deploy_session(service, "idle-old") is None
    assert key not in service._pending_deploy
    assert key not in service._last_seen


def test_a_recent_conversation_is_kept():
    service = AdkChatService()
    _preview(service, "idle-recent")

    assert asyncio.run(service.sweep_idle(HOUR)) == []
    assert _deploy_session(service, "idle-recent") is not None


def test_a_thread_with_a_turn_still_running_is_never_swept():
    service = AdkChatService()
    _preview(service, "idle-busy")
    key = (_user_id(), "idle-busy")
    service._running[key] = 1

    assert asyncio.run(service.sweep_idle(HOUR, now=time.monotonic() + 10 * HOUR)) == []
    assert _deploy_session(service, "idle-busy") is not None


def test_the_thread_is_busy_while_its_turn_streams_and_idle_counts_from_the_end():
    service = AdkChatService()
    key = (_user_id(), "idle-stream")
    seen_running = []

    async def fake_turn(message, thread_id, abort_signal=None):
        seen_running.append(service._running.get(key))
        yield {"type": "done"}

    service._stream_chat = fake_turn
    before = time.monotonic()

    async def _run():
        return [e async for e in service.stream_chat("hi", "idle-stream")]

    asyncio.run(_run())
    assert seen_running == [1]
    assert key not in service._running
    assert service._last_seen[key] >= before


def test_an_unused_github_token_is_dropped_and_a_used_one_kept():
    store = SessionCredentialStore()
    store.set("old", SessionCredentials(pat_token="ghp_old"))
    store.set("used", SessionCredentials(pat_token="ghp_used"))
    later = time.monotonic() + HOUR + 1
    store._last_used["used"] = later - 60      # its thread ran a turn a minute ago

    assert store.sweep_idle(HOUR, now=later) == ["old"]
    assert store.get("old") is None
    assert store.get("used").pat_token == "ghp_used"


def test_running_a_turn_counts_as_using_the_token():
    store = SessionCredentialStore()
    store.set("t", SessionCredentials(pat_token="ghp_t"))
    store._last_used["t"] = 0.0
    with store.activate("t"):
        pass
    assert store.sweep_idle(HOUR) == []
