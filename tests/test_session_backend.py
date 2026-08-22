"""Where ADK chat sessions live, and how they are keyed.

In-memory sessions are per-pod: a restart loses the conversation and a second
replica sees a different history. ADK_SESSION_BACKEND=vertex moves them into a
Vertex Agent Engine — for the CONVERSATION only; the pending-CONFIRM maps and
the per-thread PAT stay process-local, so this alone does not make the app
safe at replicaCount > 1.
"""
import pytest

from google.adk.sessions import InMemorySessionService

from release_agent import adk_service as S
from release_agent.config import settings


@pytest.fixture
def backend(monkeypatch):
    def _set(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setattr(settings, key, value)
        return S._build_session_service()

    return _set


def test_defaults_to_in_memory(backend):
    assert isinstance(backend(adk_session_backend="memory"), InMemorySessionService)


@pytest.mark.parametrize("spelling", ["", "  ", "in-memory", "InMemory", "MEMORY"])
def test_memory_spellings_are_accepted(backend, spelling):
    assert isinstance(backend(adk_session_backend=spelling), InMemorySessionService)


def test_vertex_backend_is_built_from_the_engine_id(backend):
    from google.adk.sessions import VertexAiSessionService

    svc = backend(adk_session_backend="vertex",
                  vertex_agent_engine_id="1234567890123456789")
    assert isinstance(svc, VertexAiSessionService)


def test_full_resource_name_is_accepted_for_the_engine_id(backend):
    """Operators paste what the Agent Engine API returns, not just the digits."""
    svc = backend(
        adk_session_backend="vertex",
        vertex_agent_engine_id="projects/p/locations/us-central1/reasoningEngines/987654321",
    )
    assert svc._agent_engine_id == "987654321"


def test_vertex_with_neither_id_nor_name_fails_at_startup(backend):
    """An id is no longer required — the engine is resolved by display name and
    created if absent. But with NEITHER, there is nothing to resolve, and falling
    back to in-memory would look like it worked right up until a restart silently
    dropped every conversation."""
    with pytest.raises(RuntimeError, match="VERTEX_AGENT_ENGINE_NAME"):
        backend(adk_session_backend="vertex", vertex_agent_engine_id="",
                vertex_agent_engine_name="")


def test_an_unknown_backend_is_refused_rather_than_guessed(backend):
    with pytest.raises(RuntimeError, match="not supported"):
        backend(adk_session_backend="postgres")


def test_chat_and_deploy_lanes_get_separate_sessions():
    """VertexAiSessionService resolves every app_name to the one configured
    Agent Engine, so without this the chat agent and the deploy Workflow would
    append to a single session and replay each other's events."""
    chat = S._session_id("fastapi-ab12cd34", "chat")
    deploy = S._session_id("fastapi-ab12cd34", "deploy")
    assert chat != deploy


@pytest.mark.parametrize("thread_id", ["fastapi-ab12cd34", "abc123", "a-b_c"])
def test_session_ids_satisfy_the_vertex_id_pattern(thread_id):
    """Vertex rejects anything outside ^[A-Za-z0-9_-]+$ — including the suffix."""
    from google.adk.sessions import vertex_ai_session_service as V

    for lane in ("chat", "deploy"):
        assert V._SESSION_ID_PATTERN.fullmatch(S._session_id(thread_id, lane))
