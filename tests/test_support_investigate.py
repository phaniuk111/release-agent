"""The support-investigate tool wrappers (adk_release_agent/tools.py): the gate,
the business-date window, the PromQL checks, the shared evidence cache."""
from __future__ import annotations

import pytest

from adk_release_agent import tools
from adk_release_agent import support_tools
from release_agent.tools.support import cache
from release_agent import features


@pytest.fixture(autouse=True)
def _preview_everyone(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_users", "*")
    cache.clear()


def test_every_investigate_tool_refuses_a_non_preview_user(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_features", "support-triage")
    monkeypatch.setattr(features.settings, "preview_users", "")
    for call in (lambda: tools.source_metrics("svc-a"), lambda: tools.source_logs("svc-a"),
                 lambda: tools.source_audit(), lambda: tools.dataflow_job("j1"),
                 lambda: tools.run_logs("run-7", "2026-10-03T03:34:16Z"),
                 lambda: tools.release_lookup("svc-a"), lambda: tools.what_changed(["svc-a"], "2026-10-02")):
        assert "preview" in call()["error"]


def test_a_business_date_means_its_overnight_window_clamped_to_now():
    start, end = support_tools._cob_window("2026-09-20")
    assert start == "2026-09-20T00:00:00+00:00" and end == "2026-09-21T12:00:00+00:00"
    start, end = support_tools._cob_window("")          # no date: the last 36 hours
    assert start < end
    from datetime import date
    today = date.today().isoformat()
    _, end = support_tools._cob_window(today)           # today's window has not ended: evaluate now
    assert end <= support_tools._cob_window("")[1]


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
    assert out["ok"] and out["summary"] == "restarts 7 · up 3"
    assert seen[0] == ('sum(x{app="svc-a",namespace="ns-a"}[36h])', "2026-09-21T12:00:00+00:00")
    assert tools.source_metrics("bad name")["ok"] is False


def test_a_failed_or_empty_check_is_said_not_hidden(monkeypatch):
    from release_agent.config import settings
    from release_agent.tools import monitoring

    monkeypatch.setattr(settings, "support_source_promql", "up=up{{app=\"{source}\"}};err=err", raising=False)
    monkeypatch.setattr(monitoring, "run_query",
                        lambda expr, at="": {"ok": True, "series": []} if expr.startswith("up") else {"ok": False, "error": "403 no"})
    assert tools.source_metrics("svc-a")["summary"] == "up no series · err not measured"


def test_evidence_is_shared_for_ten_minutes_but_a_failure_is_retried(monkeypatch):
    calls = []

    def compute():
        calls.append(1)
        return {"ok": len(calls) > 1}
    assert cache.remembered(("k",), compute)["ok"] is False      # first: a failure, not remembered
    assert cache.remembered(("k",), compute)["ok"] is True       # computed again
    assert cache.remembered(("k",), compute)["ok"] is True       # now served from memory
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



def test_ten_callers_of_one_read_cost_one_read():
    """Single-flight: the callers that arrive while the read is running wait for it."""
    import threading
    import time

    calls = []

    def slow():
        calls.append(1)
        time.sleep(0.2)
        return {"ok": True, "n": len(calls)}
    threads = [threading.Thread(target=lambda: cache.remembered(("slow",), slow)) for _ in range(10)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(calls) == 1


def test_a_follow_up_question_reuses_the_reads_of_the_investigation(monkeypatch):
    """The collector and the single-source chat tools key the same read the
    same way: "show the worker log lines" after an investigation reads nothing."""
    from release_agent.config import settings
    from release_agent.tools.support import evidence, source_logs

    reads = []
    monkeypatch.setattr(source_logs, "read", lambda *a, **k: (reads.append(a), {"ok": True, "groups": []})[1])
    monkeypatch.setattr(source_logs, "audit", lambda *a, **k: {"ok": True, "denials": [], "iam_changes": []})
    monkeypatch.setattr(settings, "support_source_promql", "", raising=False)
    from datetime import date

    evidence._gather(date(2026, 9, 20), {"source": "svc-a", "process": "", "job_ids": []}, "")
    assert len(reads) == 1
    out = support_tools.source_logs("svc-a", "2026-09-20")
    assert out["ok"] and len(reads) == 1            # served from the investigation's read


def test_run_logs_reads_the_run_once_and_refuses_a_time_it_cannot_parse(monkeypatch):
    from release_agent.tools.support import source_logs

    calls = []
    monkeypatch.setattr(source_logs, "for_run", lambda run_id, at, **kw: calls.append((run_id, at)) or {"ok": True})
    assert tools.run_logs("run-7", "2026-10-03T03:34:16Z") == {"ok": True}
    assert tools.run_logs("run-7", "2026-10-03T03:34:16+00:00") == {"ok": True}   # same moment, same read
    assert len(calls) == 1 and calls[0][0] == "run-7"
    assert "not a timestamp" in tools.run_logs("run-7", "yesterday")["error"] and len(calls) == 1


def test_the_triage_and_the_evidence_hand_the_model_the_teams_runbook_and_priority_policy(monkeypatch):
    """"Ask why" must not depend on the model choosing to load a skill: the
    policy travels inside the tool results."""
    from release_agent.tools.support import evidence, triage

    monkeypatch.setattr(triage, "triage", lambda day="": {"ok": True, "incidents": [], "errors": []})
    monkeypatch.setattr(triage, "for_model", lambda report: dict(report))
    monkeypatch.setattr(evidence, "collect", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(evidence, "for_model", lambda bundle: dict(bundle))
    policy = support_tools.priority_policy()
    assert "The team's priority policy" in policy
    from release_agent.tools.support import runbook

    got = tools.support_triage("2026-10-03")
    assert got["priority_policy"] == policy and got["runbook"] == runbook.text()
    assert "## Known issues" in got["runbook"]
    got = tools.investigate_evidence("2026-10-03", "e-1")
    assert got["priority_policy"] == policy and got["runbook"] == runbook.text()
    monkeypatch.setattr(triage, "triage", lambda day="": {"ok": False, "error": "x"})
    assert "priority_policy" not in tools.support_triage("2026-10-03")
