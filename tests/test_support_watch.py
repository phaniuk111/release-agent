"""Auto-triage: findings built from investigations, kept append-only in the
portal event log, cleared when the triage no longer shows them, widened when
the incident grew, and one bell item per incident. The routes: gated by the
auto-triage feature, a change recorded against the verified caller."""
from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time

import pytest
from fastapi.testclient import TestClient

from adk_release_agent import support_watch
from release_agent import app_fastapi as APP
from release_agent import features, identity
from release_agent.config import settings
from release_agent.tools import events_store
from release_agent.tools import notifications as N
from release_agent.tools.support import watch

DAY = "2026-10-02"


def _incident(i, priority="high", jobs=("j1", "j2", "j3"), count=3, **kw):
    return {"id": i, "title": f"{i} failed", "category": "process", "priority": priority, "count": count,
            "owner": "data-platform", "action": "rerun", "note": f"note for {i}", "job_ids": list(jobs), **kw}


def _outcome(text="Do now: escalate to the owner\nCause: schema change in orders-api 11.0.0", conclusive=True,
             hint=("escalate", "a release changed this shortly before the first failure")):
    return {"text": text, "model": "gemini-x", "model_calls": 1, "seconds": 4.2, "fallback": False,
            "bundle": {"ok": True, "signals": {"conclusive": conclusive,
                                               "action_hint": {"action": hint[0], "why": hint[1]} if hint else None},
                       "timeline": [{"time": "2026-10-02T01:40:00Z", "source": "release", "text": "orders-api to PRD"}]}}


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(settings, "support_table", "proj.ds.control", raising=False)
    monkeypatch.setattr(settings, "support_notify_recipients", "")
    monkeypatch.setattr(settings, "support_notify_min_priority", "medium")
    monkeypatch.setattr(settings, "support_watch_max_investigations", 2)
    monkeypatch.setattr(settings, "support_watch_investigate_seconds", 240)
    monkeypatch.setattr(settings, "support_watch_minutes", 0)
    watch.reset()


# ----- PURE: findings ------------------------------------------------------------------

def test_a_finding_takes_the_cause_and_action_code_established():
    f = watch.finding_from(_incident("a"), _outcome(), DAY)
    assert f["cause"] == "schema change in orders-api 11.0.0"
    assert f["established"] is True
    assert f["action"] == "escalate" and "release" in f["action_why"]
    assert f["do_now"] == "escalate to the owner"
    assert f["status"] == "open" and f["actor"] == "watcher" and f["event_type"] == "finding"
    assert f["timeline"][0]["text"] == "orders-api to PRD"
    assert f["jobs"] == ["j1", "j2", "j3"] and f["count"] == 3


def test_a_cause_the_evidence_does_not_establish_is_never_shown_as_one():
    f = watch.finding_from(_incident("a"), _outcome(conclusive=False, hint=None), DAY)
    assert f["cause"] == watch.NOT_ESTABLISHED
    assert f["action"] == "rerun"       # the triage's own, untouched


def test_an_investigation_that_timed_out_is_a_finding_that_says_so():
    out = {"text": "", "bundle": {}, "model": "", "model_calls": 0, "seconds": 240.0, "fallback": True,
           "timed_out": True}
    f = watch.finding_from(_incident("a"), out, DAY)
    assert f["cause"] == "no cause established — evidence timed out"
    assert f["established"] is False and f["fallback"] is True


def test_the_job_signature_is_stable_and_falls_back_to_the_runs():
    assert watch.job_signature({"job_ids": ["b", "a", "b"]}) == ["a", "b"]
    assert watch.job_signature({"runs": [{"run_id": "r2"}, {"run_id": "r1"}]}) == ["r1", "r2"]
    assert watch.job_signature({}) == []


def test_an_incident_grew_when_it_has_more_runs_or_a_job_the_finding_never_saw():
    f = watch.finding_from(_incident("a"), _outcome(), DAY)
    assert not watch.grew(f, _incident("a"))
    assert watch.grew(f, _incident("a", count=5))
    assert watch.grew(f, _incident("a", jobs=("j1", "j2", "j9")))
    assert not watch.grew(f, _incident("a", jobs=("j1", "j2")))   # narrower is not news


def test_new_grown_gone_and_ranking():
    report = {"incidents": [_incident("a", count=5), _incident("b")]}
    assert [i["id"] for i in watch.new_incidents(report, {"a"})] == ["b"]
    open_a = watch.finding_from(_incident("a"), _outcome(), DAY)
    closed_a = {**open_a, "status": "dismissed"}
    open_c = {"incident_id": "c", "status": "open"}
    assert [(f["incident_id"], i["count"]) for f, i in watch.grown([open_a, closed_a], report)] == [("a", 5)]
    assert watch.gone([open_c, open_a], report) == [open_c]
    ranked = watch.ranked([{"incident_id": "low", "status": "open", "priority": "low"},
                           {"incident_id": "done", "status": "resolved", "priority": "critical"},
                           {"incident_id": "high", "status": "open", "priority": "high"}])
    assert [f["incident_id"] for f in ranked] == ["high", "low", "done"]


def test_a_finding_survives_the_trip_through_a_portal_events_row():
    f = watch.finding_from(_incident("a"), _outcome(), DAY)
    row = watch.to_row(f)
    assert row["domain"] == "watch" and row["subject"] == "incident:a" and row["subject_version"] == DAY
    assert row["severity"] == "high" and row["event_type"] == "finding" and row["status"] == "open"
    assert row["link"] == f"portal:support:a:{DAY}" and row["scope"] == f"watch:{DAY}:a"
    assert set(row["details"]) >= {"answer", "timeline", "jobs", "do_now", "note", "investigated_at"}
    out = events_store.record(row)
    assert out["ok"], out
    back = watch.from_row(events_store.latest("watch", subject_version=DAY)["rows"][0])
    for key in ("business_date", "incident_id", "status", "title", "category", "priority", "count", "jobs", "owner",
                "cause", "established", "do_now", "action", "action_why", "timeline", "note", "answer", "model",
                "model_calls", "seconds", "fallback", "notified", "investigated_at", "actor", "event_type"):
        assert back[key] == f[key], key


# ----- storage: append-only, latest wins --------------------------------------------

def test_a_status_change_is_a_new_row_and_the_latest_wins():
    watch.append([watch.finding_from(_incident("a"), _outcome(), DAY)])
    res = watch.set_status(DAY, "a", "resolved", "carol@example.com")
    assert res["ok"] and res["finding"]["status"] == "resolved" and res["finding"]["event_type"] == "resolved"
    latest = watch.latest(DAY)["findings"]
    assert [(f["incident_id"], f["status"], f["actor"]) for f in latest] == [("a", "resolved", "carol@example.com")]
    assert len(events_store.history("watch")["rows"]) == 2
    assert not watch.set_status(DAY, "a", "cleared", "x")["ok"]     # only the watcher clears
    assert watch.set_status(DAY, "a", "open", "carol@example.com")["finding"]["event_type"] == "reopened"
    assert not watch.set_status(DAY, "zzz", "open", "x")["ok"]


def test_findings_are_kept_per_business_date():
    watch.append([watch.finding_from(_incident("a"), _outcome(), DAY),
                  watch.finding_from(_incident("a"), _outcome(), "2026-10-01")])
    assert [f["business_date"] for f in watch.latest(DAY)["findings"]] == [DAY]
    assert watch.latest("")["findings"] == []


def test_a_store_outage_is_an_error_dict_not_an_exception(monkeypatch):
    monkeypatch.setattr(events_store, "latest", lambda *a, **k: {"ok": False, "error": "down"})
    assert watch.latest(DAY) == {"ok": False, "error": "down", "findings": []}
    assert not watch.set_status(DAY, "a", "resolved", "x")["ok"]


# ----- the bell ---------------------------------------------------------------------

def test_the_bell_item_is_one_per_incident_and_links_to_it(monkeypatch):
    monkeypatch.setattr(settings, "support_notify_recipients", "Alice@example.com, bob@example.com")
    f = watch.finding_from(_incident("a", owner="owner@example.com"), _outcome(), DAY)
    assert watch.recipients(f) == ["alice@example.com", "bob@example.com", "owner@example.com"]
    assert watch.recipients({**f, "owner": "data-platform"}) == ["alice@example.com", "bob@example.com"]
    n = watch.notification(f)
    assert n["kind"] == "auto_triage" and n["title"] == "Auto-triage: a failed"
    assert n["scope"] == f"watch:{DAY}:a" and n["link"] == f"portal:support:a:{DAY}"
    assert n["severity"] == "critical" and "Cause: schema change" in n["body"]
    assert watch.notify(f) is True
    items = N.list_for("alice@example.com")["items"]
    assert [i["kind"] for i in items] == ["auto_triage"] and items[0]["scope"] == n["scope"]
    assert N.list_for("owner@example.com")["unread"] == 1
    # A status change updates the SAME bell item rather than adding one.
    assert watch.notify(watch.with_status(f, "resolved", "carol@example.com"))
    items = N.list_for("alice@example.com")["items"]
    assert len(items) == 1 and items[0]["title"] == "Auto-triage: Resolved — a failed"


def test_only_findings_at_or_above_the_floor_notify_and_nobody_means_nothing(monkeypatch):
    monkeypatch.setattr(settings, "support_notify_recipients", "alice@example.com")
    monkeypatch.setattr(settings, "support_notify_min_priority", "high")
    assert watch.should_notify({"priority": "high"})
    assert not watch.should_notify({"priority": "medium"})
    monkeypatch.setattr(settings, "support_notify_recipients", "")
    assert not watch.should_notify({"priority": "critical"})
    assert watch.should_notify({"priority": "critical", "owner": "owner@example.com"})
    assert watch.notify({"priority": "low"}) is False


def test_a_bell_that_cannot_be_rung_leaves_the_finding_as_is(monkeypatch):
    monkeypatch.setattr(settings, "support_notify_recipients", "alice@example.com")
    monkeypatch.setattr(N, "notify", lambda *a, **k: {"ok": False, "error": "down"})
    assert watch.notify(watch.finding_from(_incident("a"), _outcome(), DAY)) is False


# ----- a pass ---------------------------------------------------------------------------

def _fake_pass(monkeypatch, incidents, outcome=None):
    monkeypatch.setattr(support_watch.support_triage, "triage",
                        lambda d: {"ok": True, "business_date": DAY, "incidents": incidents})
    calls = []

    async def fake_run_once(day, incident_id, *, thread_id):
        calls.append(incident_id)
        return outcome or _outcome()

    monkeypatch.setattr(support_watch.investigate_workflow, "run_once", fake_run_once)
    return calls


def test_a_pass_investigates_new_incidents_up_to_the_cap_and_the_rest_wait(monkeypatch):
    calls = _fake_pass(monkeypatch, [_incident("a"), _incident("b"), _incident("c")])
    first = asyncio.run(support_watch.run_pass())
    assert first["investigated"] == ["a", "b"] and first["waiting"] == 1 and calls == ["a", "b"]
    assert first["incidents"] == 3 and first["cleared"] == [] and first["updated"] == []

    second = asyncio.run(support_watch.run_pass())
    assert second["investigated"] == ["c"] and second["waiting"] == 0
    assert watch.status()["last_business_date"] == DAY and not watch.status()["running"]
    assert {f["incident_id"] for f in watch.latest(DAY)["findings"]} == {"a", "b", "c"}
    passes = events_store.history("watch", subject="pass")["rows"]
    assert len(passes) == 2 and passes[0]["event_type"] == "checked" and passes[0]["count"] == 3


def test_a_finding_is_kept_and_notified_as_each_investigation_completes(monkeypatch):
    monkeypatch.setattr(settings, "support_notify_recipients", "alice@example.com")
    seen = []

    async def run_once(day, incident_id, *, thread_id):
        seen.append(("investigate", incident_id, {f["incident_id"] for f in watch.latest(DAY)["findings"]},
                     N.unread_count("alice@example.com")))
        return _outcome()

    monkeypatch.setattr(support_watch.support_triage, "triage",
                        lambda d: {"ok": True, "business_date": DAY, "incidents": [_incident("a"), _incident("b")]})
    monkeypatch.setattr(support_watch.investigate_workflow, "run_once", run_once)
    res = asyncio.run(support_watch.run_pass())
    assert res["notified"] == 2
    # When b is investigated, a's finding is already kept and its bell rung.
    assert seen == [("investigate", "a", set(), 0), ("investigate", "b", {"a"}, 1)]
    assert all(f["notified"] for f in watch.latest(DAY)["findings"])


def test_an_investigation_past_the_deadline_is_a_timed_out_finding_and_the_pass_moves_on(monkeypatch):
    monkeypatch.setattr(settings, "support_watch_investigate_seconds", 1)
    monkeypatch.setattr(support_watch.support_triage, "triage",
                        lambda d: {"ok": True, "business_date": DAY, "incidents": [_incident("a"), _incident("b")]})

    async def slow_then_fast(day, incident_id, *, thread_id):
        if incident_id == "a":
            await asyncio.sleep(30)
        return _outcome()

    monkeypatch.setattr(support_watch.investigate_workflow, "run_once", slow_then_fast)
    res = asyncio.run(support_watch.run_pass())
    assert res["investigated"] == ["a", "b"]
    by_id = {f["incident_id"]: f for f in watch.latest(DAY)["findings"]}
    assert by_id["a"]["cause"] == watch.TIMED_OUT_CAUSE and by_id["a"]["fallback"] and not by_id["a"]["established"]
    assert by_id["b"]["established"]


def test_an_investigation_that_raises_is_a_fallback_finding(monkeypatch):
    async def boom(day, incident_id, *, thread_id):
        raise RuntimeError("no model")

    monkeypatch.setattr(support_watch.investigate_workflow, "run_once", boom)
    out = asyncio.run(support_watch.investigate(DAY, "a"))
    assert out["fallback"] and "no model" in out["text"]


def test_an_incident_that_grew_is_updated_and_told_again(monkeypatch):
    monkeypatch.setattr(settings, "support_notify_recipients", "alice@example.com")
    _fake_pass(monkeypatch, [_incident("a")])
    asyncio.run(support_watch.run_pass())
    calls = _fake_pass(monkeypatch, [_incident("a", count=5, jobs=("j1", "j2", "j3", "j4", "j5"),
                                               title="5 failed · process")])
    res = asyncio.run(support_watch.run_pass())
    assert res["updated"] == ["a"] and res["investigated"] == [] and calls == []   # no second investigation
    f = watch.latest(DAY)["findings"][0]
    assert f["count"] == 5 and f["jobs"] == ["j1", "j2", "j3", "j4", "j5"] and f["event_type"] == "updated"
    assert f["previous_count"] == 3 and f["cause"] == "schema change in orders-api 11.0.0"
    items = N.list_for("alice@example.com")["items"]
    assert len(items) == 1 and items[0]["event_type"] == "updated" and items[0]["body"].startswith("Now 5 jobs (was 3)")
    # The same width again is not news.
    assert asyncio.run(support_watch.run_pass())["updated"] == []


def test_a_finding_whose_incident_left_the_triage_is_cleared(monkeypatch):
    monkeypatch.setattr(settings, "support_notify_recipients", "alice@example.com")
    _fake_pass(monkeypatch, [_incident("a")])
    asyncio.run(support_watch.run_pass())
    _fake_pass(monkeypatch, [])
    res = asyncio.run(support_watch.run_pass())
    assert res["cleared"] == ["a"]
    f = watch.latest(DAY)["findings"][0]
    assert f["status"] == "cleared" and f["event_type"] == "cleared" and f["actor"] == "watcher"
    assert N.list_for("alice@example.com")["items"][0]["title"] == "Auto-triage: Cleared — a failed"


def test_a_cancelled_pass_releases_the_lock(monkeypatch):
    """Shutdown cancels the loop's task mid-pass; a lock left held would refuse
    every later pass and Run now."""
    _fake_pass(monkeypatch, [_incident("a")])
    async def scenario():
        started = asyncio.Event()

        async def hang(day, incident_id, *, thread_id):
            started.set()
            await asyncio.sleep(60)

        monkeypatch.setattr(support_watch.investigate_workflow, "run_once", hang)
        task = asyncio.create_task(support_watch.run_pass())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    st = watch.status()
    assert st["running"] is False and st["error"] == "cancelled" and st["last_business_date"] == DAY
    assert watch.begin(), "the next pass is not refused"
    watch.finish()


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
    assert watch.status()["error"] == "no table" and watch.status()["last_run_at"]


def test_a_pass_that_raises_is_reported_not_raised(monkeypatch):
    def boom(d):
        raise RuntimeError("bq exploded")

    monkeypatch.setattr(support_watch.support_triage, "triage", boom)
    res = asyncio.run(support_watch.run_pass())
    assert not res["ok"] and "bq exploded" in res["error"] and not watch.status()["running"]


# ----- the schedule --------------------------------------------------------------------

def test_the_next_pass_is_measured_from_the_start_but_never_within_a_minute_of_the_end():
    t0 = dt.datetime(2026, 10, 2, 9, 0, tzinfo=dt.timezone.utc)
    quick = t0 + dt.timedelta(minutes=2)
    assert support_watch.next_run_at(t0, quick, 10) == t0 + dt.timedelta(minutes=10)
    slow = t0 + dt.timedelta(minutes=12)
    assert support_watch.next_run_at(t0, slow, 10) == slow + dt.timedelta(minutes=1)
    assert support_watch.next_run_at(t0, quick, 0) == quick + dt.timedelta(minutes=1)


def test_the_loop_runs_only_when_asked_and_there_is_a_table(monkeypatch):
    assert not support_watch.scheduled()
    monkeypatch.setattr(settings, "support_watch_minutes", 10)
    assert support_watch.scheduled()
    monkeypatch.setattr(settings, "support_table", "")
    assert not support_watch.scheduled()
    assert not watch.status()["enabled"] and not watch.status()["configured"]


def test_the_status_says_where_findings_live_and_when_it_looks_again(monkeypatch):
    monkeypatch.setattr(settings, "support_watch_minutes", 10)
    monkeypatch.setattr(settings, "support_notify_recipients", "alice@example.com")
    s = watch.status()
    assert s["store"] == "memory" and s["notifications"] is True and s["next_at"] is None and s["enabled"]
    at = dt.datetime(2026, 10, 2, 9, 10, tzinfo=dt.timezone.utc)
    watch.schedule(at)
    assert watch.status()["next_at"] == at.isoformat()
    assert "auto-triage" in type(settings).model_fields["preview_features"].default
    assert settings.support_watch_minutes == 10 and type(settings).model_fields["support_watch_minutes"].default == 0


# ----- routes ---------------------------------------------------------------------------

TESTER = identity.Caller(email="tester@example.com")
OTHER = identity.Caller(email="other@example.com")


@pytest.fixture
def client():
    return TestClient(APP.app)


@pytest.fixture
def signed_in(monkeypatch):
    def _as(caller):
        monkeypatch.setattr(APP, "_caller", lambda request: caller)
    monkeypatch.setattr(features.settings, "preview_users", "", raising=False)
    monkeypatch.setattr(features.settings, "preview_features", "", raising=False)
    return _as


def test_every_route_refuses_when_the_feature_is_off_for_the_caller(monkeypatch, client):
    monkeypatch.setattr(features.settings, "preview_features", "auto-triage", raising=False)
    monkeypatch.setattr(features.settings, "preview_users", "someone-else@example.com", raising=False)
    monkeypatch.setattr(APP, "_caller", lambda request: OTHER)
    for res in (client.get("/api/support/watch"),
                client.post("/api/support/watch/run", json={}),
                client.post("/api/support/watch/status", json={"business_date": DAY, "incident_id": "a",
                                                               "status": "resolved"})):
        assert res.status_code == 403
        assert res.json() == {"ok": False, "error": "Auto-triage is not available yet — it is a preview feature."}
    assert "auto-triage" not in features.ui_config(OTHER)["features"], "the page hides the pill and the strip"
    assert "auto-triage" in features.ui_config(identity.Caller(email="someone-else@example.com"))["features"]


def test_the_feed_answers_the_findings_and_the_status(signed_in, client, monkeypatch):
    signed_in(TESTER)
    watch.append([watch.finding_from(_incident("a"), _outcome(), DAY)])
    body = client.get(f"/api/support/watch?date={DAY}").json()
    assert body["ok"] and body["business_date"] == DAY and [f["incident_id"] for f in body["findings"]] == ["a"]
    assert set(body["status"]) >= {"running", "last_run_at", "next_at", "last_summary", "error", "store",
                                   "notifications", "interval_minutes", "enabled"}
    empty = client.get("/api/support/watch").json()
    assert empty["ok"] and empty["business_date"] == "" and empty["findings"] == []
    monkeypatch.setattr(events_store, "latest", lambda *a, **k: {"ok": False, "error": "down"})
    assert client.get(f"/api/support/watch?date={DAY}").json()["error"] == "down"


def test_run_now_answers_at_once_while_the_pass_runs_on_and_says_when_one_is_already_running(
        signed_in, monkeypatch):
    signed_in(TESTER)
    _fake_pass(monkeypatch, [_incident("a")])
    release, ran = threading.Event(), []

    async def slow_pass(day=""):
        ran.append(day)
        while not release.is_set():          # the pass outlives the request that started it
            await asyncio.sleep(0.01)
        return {"ok": True}

    monkeypatch.setattr(support_watch, "run_pass", slow_pass)
    with TestClient(APP.app) as client:      # one event loop for the whole session, as in the app
        body = client.post("/api/support/watch/run", json={"business_date": DAY}).json()
        assert body == {"ok": True, "started": True}
        deadline = time.monotonic() + 5
        while not ran and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ran == [DAY], "the pass was started, not awaited"
        release.set()
        assert watch.begin()
        refused = client.post("/api/support/watch/run", json={}).json()
        assert refused["ok"] is False and "already running" in refused["error"]
        watch.finish()


def test_a_status_change_is_recorded_against_the_verified_caller(signed_in, client):
    signed_in(TESTER)
    watch.append([watch.finding_from(_incident("a"), _outcome(), DAY)])
    body = client.post("/api/support/watch/status",
                       json={"business_date": DAY, "incident_id": "a", "status": "dismissed",
                             "actor": "forged@example.com"}).json()
    assert body["ok"] and body["finding"]["status"] == "dismissed" and body["finding"]["actor"] == TESTER.email
    assert not client.post("/api/support/watch/status",
                           json={"business_date": DAY, "incident_id": "a", "status": "cleared"}).json()["ok"]


def test_with_identity_off_a_change_is_anonymous_and_with_it_required_refused(signed_in, client, monkeypatch):
    signed_in(None)
    watch.append([watch.finding_from(_incident("a"), _outcome(), DAY)])
    body = client.post("/api/support/watch/status",
                       json={"business_date": DAY, "incident_id": "a", "status": "resolved"}).json()
    assert body["ok"] and body["finding"]["actor"] == ""
    monkeypatch.setattr(identity.settings, "identity_header", "x-rctoken", raising=False)
    monkeypatch.setattr(identity.settings, "identity_required", True, raising=False)
    body = client.post("/api/support/watch/status",
                       json={"business_date": DAY, "incident_id": "a", "status": "open"}).json()
    assert body["ok"] is False and "Sign-in" in body["error"]
    assert "Sign-in" in client.post("/api/support/watch/run", json={}).json()["error"]
    assert client.get(f"/api/support/watch?date={DAY}").json()["ok"], "reading is open to a signed-out tester"
