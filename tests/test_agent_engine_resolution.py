"""Resolving (and creating) the Agent Engine that holds ADK sessions.

Nobody can be asked to paste a 19-digit id into a values file per environment,
so the engine is found by display name and created when absent. The property
that matters is CONVERGENCE: two pods starting together may both create one,
and they must still end up using the same engine.
"""
import pytest

from release_agent import adk_service as S
from release_agent.config import settings


class _Engine:
    def __init__(self, engine_id, display_name):
        self.api_resource = type("R", (), {
            "name": f"projects/p/locations/us-central1/reasoningEngines/{engine_id}",
            "display_name": display_name,
        })()


class _Engines:
    def __init__(self, existing):
        self.items = list(existing)
        self.created = []

    def list(self):
        return iter(self.items)

    def create(self, config=None):
        name = (config or {}).get("display_name")
        self.created.append(name)
        self.items.append(_Engine(f"new-{len(self.items)}", name))


@pytest.fixture
def vertex(monkeypatch):
    def _set(existing=(), name="release-copilot-sessions", pinned=""):
        monkeypatch.setattr(settings, "vertex_agent_engine_id", pinned)
        monkeypatch.setattr(settings, "vertex_agent_engine_name", name)
        monkeypatch.setattr(settings, "gcp_project", "p")
        monkeypatch.setattr(settings, "gcp_location", "us-central1")
        engines = _Engines(existing)
        import vertexai
        monkeypatch.setattr(
            vertexai, "Client",
            lambda **kw: type("C", (), {"agent_engines": engines})(),
        )
        return engines

    return _set


def test_a_pinned_id_wins_and_nothing_is_created(vertex):
    engines = vertex(pinned="projects/p/locations/l/reasoningEngines/12345")
    assert S._resolve_agent_engine_id() == "12345"
    assert engines.created == []


def test_an_existing_engine_is_reused_not_duplicated(vertex):
    engines = vertex(existing=[_Engine("999", "release-copilot-sessions")])
    assert S._resolve_agent_engine_id() == "999"
    assert engines.created == []


def test_it_is_created_when_absent(vertex):
    engines = vertex()
    engine_id = S._resolve_agent_engine_id()
    assert engines.created == ["release-copilot-sessions"]
    assert engine_id.startswith("new-")


def test_engines_with_other_names_are_ignored(vertex):
    """Someone else's Agent Engine in the same project must not be hijacked."""
    engines = vertex(existing=[_Engine("777", "someone-elses-agent")])
    S._resolve_agent_engine_id()
    assert engines.created == ["release-copilot-sessions"]


def test_duplicates_converge_on_the_same_engine(vertex):
    """The race this design tolerates: two pods each created one. Every pod must
    still pick the SAME engine, or sessions split between two stores."""
    vertex(existing=[_Engine("b-second", "release-copilot-sessions"),
                     _Engine("a-first", "release-copilot-sessions")])
    first = S._resolve_agent_engine_id()
    # a second pod, listing in a different order, must agree
    vertex(existing=[_Engine("a-first", "release-copilot-sessions"),
                     _Engine("b-second", "release-copilot-sessions")])
    assert S._resolve_agent_engine_id() == first


def test_a_creation_failure_says_what_permission_is_missing(vertex, monkeypatch):
    engines = vertex()
    def _boom(config=None):
        raise PermissionError("caller lacks aiplatform.reasoningEngines.create")
    monkeypatch.setattr(engines, "create", _boom)
    with pytest.raises(RuntimeError, match="roles/aiplatform.user"):
        S._resolve_agent_engine_id()


def test_no_project_is_refused_before_calling_vertex(vertex):
    vertex()
    import release_agent.adk_service as mod
    mod.settings.gcp_project = ""
    with pytest.raises(RuntimeError, match="GOOGLE_CLOUD_PROJECT"):
        S._resolve_agent_engine_id()
