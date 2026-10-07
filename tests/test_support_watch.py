"""The support watcher: findings built from investigations, kept append-only,
cleared when the triage no longer shows them, and one Backstage notification
per incident."""
from __future__ import annotations

import asyncio

import pytest

from adk_release_agent import support_watch
from release_agent.config import settings
from release_agent.tools.support import watch

DAY = "2026-10-02"


def _incident(i, priority="high", **kw):
    return {"id": i, "title": f"{i} failed", "category": "process", "priority": priority, "count": 3,
            "owner": "data-platform", "action": "rerun", "note": f"note for {i}", **kw}


def _outcome(text="Do now: escalate to the owner\nCause: schema change in orders-api 11.0.0", conclusive=True,
             hint=("escalate", "a release changed this shortly before the first failure")):
    return {"text": text, "model": "gemini-x", "model_calls": 1, "seconds": 4.2, "fallback": False,
            "bundle": {"ok": True, "signals": {"conclusive": conclusive,
                                               "action_hint": {"action": hint[0], "why": hint[1]} if hint else None},
                       "timeline": [{"time": "2026-10-02T01:40:00Z", "source": "release", "text": "orders-api to PRD"}]}}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(watch, "_memory", watch._MemoryStore())
    monkeypatch.setattr(settings, "support_watch_dataset", "")
    monkeypatch.setattr(settings, "support_notify_url", "")
    monkeypatch.setattr(settings, "support_watch_max_investigations", 2)
    watch.finish()


def test_a_finding_takes_the_cause_and_action_code_established():
    f = watch.finding_from(_incident("a"), _outcome(), DAY)
    assert f["cause"] == "schema change in orders-api 11.0.0"
    assert f["established"] is True
    assert f["action"] == "escalate" and "release" in f["action_why"]
    assert f["do_now"] == "escalate to the owner"
    assert f["status"] == "open" and f["actor"] == "watcher"
    assert f["timeline"][0]["text"] == "orders-api to PRD"


def test_a_cause_the_evidence_does_not_establish_is_never_shown_as_one():
    f = watch.finding_from(_incident("a"), _outcome(conclusive=False, hint=None), DAY)
    assert f["cause"] == "Not established from the evidence"
    assert f["action"] == "rerun"       # the triage's own, untouched


def test_new_gone_and_ranking():
    report = {"incidents": [_incident("a"), _incident("b")]}
    assert [i["id"] for i in watch.new_incidents(report, {"a"})] == ["b"]
    open_c = {"incident_id": "c", "status": "open"}
    assert watch.gone([open_c, {"incident_id": "a", "status": "open"}], report) == [open_c]
    ranked = watch.ranked([{"incident_id": "low", "status": "open", "priority": "low"},
                           {"incident_id": "done", "status": "resolved", "priority": "critical"},
                           {"incident_id": "high", "status": "open", "priority": "high"}])
    assert [f["incident_id"] for f in ranked] == ["high", "low", "done"]


def test_a_status_change_is_a_new_row_and_the_latest_wins():
    watch.append([watch.finding_from(_incident("a"), _outcome(), DAY)])
    res = watch.set_status(DAY, "a", "resolved", "carol@example.com")
    assert res["ok"] and res["finding"]["status"] == "resolved"
    latest = watch.latest(DAY)["findings"]
    assert [(f["incident_id"], f["status"], f["actor"]) for f in latest] == [("a", "resolved", "carol@example.com")]
    assert len(watch._memory._rows) == 2
    assert not watch.set_status(DAY, "a", "cleared", "x")["ok"]     # only the watcher clears


def test_the_notification_is_one_per_incident_and_links_to_it(monkeypatch):
    monkeypatch.setattr(settings, "support_notify_recipients", "group:default/data-platform, user:default/alice")
    n = watch.notification(watch.finding_from(_incident("a"), _outcome(), DAY))
    assert n["recipients"] == {"type": "entity", "entityRef": ["group:default/data-platform", "user:default/alice"]}
    assert n["payload"]["scope"] == f"support-watch:{DAY}:a"
    assert n["payload"]["severity"] == "high"
    assert n["payload"]["link"] == f"/operations?view=support&incident=a&date={DAY}"
    assert "Cause: schema change" in n["payload"]["description"]


def test_only_findings_at_or_above_the_floor_notify(monkeypatch):
    monkeypatch.setattr(settings, "support_notify_url", "http://backstage/api/notifications/notifications")
    monkeypatch.setattr(settings, "support_notify_min_priority", "high")
    assert watch.should_notify({"priority": "high"})
    assert not watch.should_notify({"priority": "medium"})


def _fake_pass(monkeypatch, incidents):
    monkeypatch.setattr(support_watch.support_triage, "triage",
                        lambda d: {"ok": True, "business_date": DAY, "incidents": incidents})
    calls = []

    async def fake_run_once(day, incident_id, *, thread_id):
        calls.append(incident_id)
        return _outcome()

    monkeypatch.setattr(support_watch.investigate_workflow, "run_once", fake_run_once)
    return calls


def test_a_pass_investigates_new_incidents_up_to_the_cap_and_the_rest_wait(monkeypatch):
    calls = _fake_pass(monkeypatch, [_incident("a"), _incident("b"), _incident("c")])
    first = asyncio.run(support_watch.run_pass())
    assert first["investigated"] == ["a", "b"] and first["waiting"] == 1 and calls == ["a", "b"]

    second = asyncio.run(support_watch.run_pass())
    assert second["investigated"] == ["c"] and second["waiting"] == 0
    assert watch.status()["last_business_date"] == DAY and not watch.status()["running"]


def test_a_finding_whose_incident_left_the_triage_is_cleared(monkeypatch):
    _fake_pass(monkeypatch, [_incident("a")])
    asyncio.run(support_watch.run_pass())
    _fake_pass(monkeypatch, [])
    res = asyncio.run(support_watch.run_pass())
    assert res["cleared"] == ["a"]
    assert watch.latest(DAY)["findings"][0]["status"] == "cleared"


def test_one_pass_at_a_time(monkeypatch):
    _fake_pass(monkeypatch, [_incident("a")])
    assert watch.begin()
    res = asyncio.run(support_watch.run_pass())
    assert not res["ok"] and "already running" in res["error"]
    watch.finish()


def test_a_triage_that_cannot_be_read_is_reported_not_raised(monkeypatch):
    monkeypatch.setattr(support_watch.support_triage, "triage", lambda d: {"ok": False, "error": "no table"})
    res = asyncio.run(support_watch.run_pass())
    assert res == {"ok": False, "error": "no table"}
    assert watch.status()["last_error"] == "no table"
