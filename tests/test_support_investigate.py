"""The support-investigate tool wrappers (adk_release_agent/tools.py): the gate,
the business-date window, the PromQL checks, the shared evidence cache."""
from __future__ import annotations

import pytest

from adk_release_agent import tools
from release_agent import features


@pytest.fixture(autouse=True)
def _preview_everyone(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_users", "*")
    tools._evidence.clear()


def test_every_investigate_tool_refuses_a_non_preview_user(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_features", "support-triage")
    monkeypatch.setattr(features.settings, "preview_users", "")
    for call in (lambda: tools.source_metrics("svc-a"), lambda: tools.source_logs("svc-a"),
                 lambda: tools.source_audit(), lambda: tools.dataflow_job("j1"),
                 lambda: tools.release_lookup("svc-a"), lambda: tools.what_changed(["svc-a"], "2026-10-02")):
        assert "preview" in call()["error"]


def test_a_business_date_means_its_overnight_window_clamped_to_now():
    start, end = tools._cob_window("2026-09-20")
    assert start == "2026-09-20T00:00:00+00:00" and end == "2026-09-21T12:00:00+00:00"
    start, end = tools._cob_window("")          # no date: the last 36 hours
    assert start < end
    from datetime import date
    today = date.today().isoformat()
    _, end = tools._cob_window(today)           # today's window has not ended: evaluate now
    assert end <= tools._cob_window("")[1]


def test_source_metrics_fills_the_configured_expressions_and_reads_at_the_window_end(monkeypatch):
    from release_agent.config import settings
    from release_agent.tools import monitoring

    monkeypatch.setattr(settings, "support_namespace", "ns-a", raising=False)
    monkeypatch.setattr(settings, "support_cluster", "cluster-a", raising=False)
    monkeypatch.setattr(settings, "support_source_promql",
                        'restarts=sum(x{{app="{source}",namespace="{namespace}"}}[{window}]);up=min(up{{app="{source}"}})',
                        raising=False)
    seen = []

    def fake(expr, at=""):
        seen.append((expr, at))
        return {"ok": True, "series": [{"value": 7.0}]} if expr.startswith("sum(x") else \
            {"ok": True, "series": [{"value": 3.0}, {"value": 1.0}]}
    monkeypatch.setattr(monitoring, "run_query", fake)
    out = tools.source_metrics("svc-a", "2026-09-20")
    assert out["ok"] and out["summary"] == "restarts: 7 · up: 3"
    assert seen[0] == ('sum(x{app="svc-a",namespace="ns-a"}[36h])', "2026-09-21T12:00:00+00:00")
    assert tools.source_metrics("bad name")["ok"] is False


def test_a_failed_or_empty_check_is_said_not_hidden(monkeypatch):
    from release_agent.config import settings
    from release_agent.tools import monitoring

    monkeypatch.setattr(settings, "support_source_promql", "up=up{{app=\"{source}\"}};err=err", raising=False)
    monkeypatch.setattr(monitoring, "run_query",
                        lambda expr, at="": {"ok": True, "series": []} if expr.startswith("up") else {"ok": False, "error": "403 no"})
    assert tools.source_metrics("svc-a")["summary"] == "up: no series · err: not measured (403 no)"


def test_evidence_is_shared_for_ten_minutes_but_a_failure_is_retried(monkeypatch):
    calls = []

    def compute():
        calls.append(1)
        return {"ok": len(calls) > 1}
    assert tools._remembered(("k",), compute)["ok"] is False      # first: a failure, not remembered
    assert tools._remembered(("k",), compute)["ok"] is True       # computed again
    assert tools._remembered(("k",), compute)["ok"] is True       # now served from memory
    assert len(calls) == 2


def test_the_advisor_is_off_without_a_model_and_capped_with_one(monkeypatch):
    from adk_release_agent import agent as agent_module
    from release_agent.config import settings

    monkeypatch.setattr(settings, "advisor_model", "")
    assert agent_module.advisor_tool() is None
    monkeypatch.setattr(settings, "advisor_model", "gemini-2.5-pro")
    monkeypatch.setattr(settings, "advisor_max_uses", 0)          # a cap below 1 would mean "never": floored
    tool = agent_module.advisor_tool()
    assert tool.name == "consult_advisor" and tool.max_uses == 1 and tool.session_max_uses == 5


def test_the_advisor_survives_the_prod_ops_confirmation_wrapping(monkeypatch):
    """Found live: the wrapping looks tools up by __name__, which a BaseTool lacks."""
    from adk_release_agent import agent as agent_module
    from release_agent.config import settings

    monkeypatch.setattr(settings, "advisor_model", "gemini-2.5-pro")
    monkeypatch.setattr(settings, "adk_confirm_prod_ops", True)
    names = [getattr(t, "name", None) or getattr(t, "__name__", None) for t in agent_module._chat_additional_tools()]
    assert names[-1] == "consult_advisor" and "promote_release" in names


def test_a_transport_drop_is_retried_only_before_anything_was_yielded(monkeypatch):
    """Seen live: Vertex closed the connection mid-investigation with no status."""
    import asyncio

    import httpx

    from adk_release_agent import agent as agent_module

    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: _done())

    async def collect(agen):
        return [r async for r in agen]

    calls = []

    def flaky_then_ok():
        calls.append(1)
        async def gen():
            if len(calls) == 1:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response")
            yield "ok"
        return gen()
    out = asyncio.run(collect(agent_module._retry_transport_drops(flaky_then_ok)))
    assert out == ["ok"] and len(calls) == 2

    def drops_mid_stream():
        async def gen():
            yield "first"
            raise httpx.ReadError("gone")
        return gen()
    with pytest.raises(httpx.ReadError):
        asyncio.run(collect(agent_module._retry_transport_drops(drops_mid_stream)))

    def always_down():
        async def gen():
            raise httpx.ConnectError("down")
            yield
        return gen()
    with pytest.raises(httpx.ConnectError):
        asyncio.run(collect(agent_module._retry_transport_drops(always_down, attempts=2)))


async def _done():
    return None
