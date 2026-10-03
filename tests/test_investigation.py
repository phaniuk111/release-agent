"""The investigation collector: incident lookup, the parallel reads, the window,
signals and leads, the timeline, the model's bounded copy, the no-model answer
and the shared single-flight cache. The five evidence functions are replaced by
fakes — nothing here reads Google."""
import json
import threading
import time
from datetime import date, datetime, timezone

import pytest

from release_agent.config import settings
from release_agent.tools import monitoring
from release_agent.tools.support import dataflow_job, release_lookup, source_logs
from release_agent.tools.support import evidence as inv
from release_agent.tools.support import signals as inv_signals
from release_agent.tools.support import triage as support_triage

DAY = "2026-10-03"
INCIDENT_ID = "e-1a2b3c4d"


def incident(**over):
    base = {"id": INCIDENT_ID, "kind": "error", "error_index": 0, "title": "24 failed · upstream · Feed SYS-A",
            "category": "upstream", "count": 24, "priority": "high",
            "facts": ["24 run(s) failed, all Feed SYS-A."], "error_text": "403 Forbidden from bucket",
            "shared": {"source": "sys-a-fetcher", "process": "report-b"}, "job_ids": ["job-1"],
            "runbook": None, "action": "escalate", "steps": ["Open the job.", "Escalate to L2 support."],
            "owner": "L2 support", "note": "NOTE with error text", "note_without_error": "NOTE without error text"}
    base.update(over)
    return base


def report(*incidents, streak=3, last_success="2026-09-30"):
    return {"ok": True, "business_date": DAY, "incidents": list(incidents) or [incident()],
            "errors": [{"max_streak": streak, "last_success": last_success}]}


def metrics_series(restarts=0.0, up=1.0, error_rate=0.0):
    values = {"restarts": restarts, "up": up, "error_rate": error_rate}

    def run_query(expr, session=None, target=None, *, _raw=False, at=""):
        name = "restarts" if "restarts" in expr else "up" if expr.startswith("up=") or "min(up" in expr else "error_rate"
        value = values[name]
        return {"ok": True, "query": expr, "count": 0 if value is None else 1,
                "series": [] if value is None else [{"labels": {}, "value": value}]}

    return run_query


def logs_result(**over):
    out = {"ok": True, "source": "sys-a-fetcher", "lines_read": 20, "security_lines": 20,
           "groups": [{"signature": "# Forbidden from bucket #", "sample": "403 Forbidden from bucket b-1\ntrace…",
                       "count": 20, "first_seen": "2026-10-03T12:30:05.123456789Z",
                       "last_seen": "2026-10-03T12:42:11Z", "severity": "ERROR", "security": True}],
           "summary": "20 error lines in 1 group; 1 security group (403 Forbidden from bucket b-1 × 20)"}
    out.update(over)
    return out


def audit_result(**over):
    out = {"ok": True, "denials_total": 6,
           "denials": [{"method": "storage.objects.get", "service": "storage.googleapis.com",
                        "resource": "projects/_/buckets/b-1", "count": 6, "first_seen": "2026-10-03T02:03:00Z",
                        "last_seen": "2026-10-03T12:40:00Z", "principals": ["svc@example.test"]}],
           "iam_changes": [{"time": "2026-10-03T01:58:00Z", "principal": "admin@example.test",
                            "resource": "projects/p", "method": "SetIamPolicy"}],
           "summary": "6 permission-denied calls in 1 group; 1 IAM policy change"}
    out.update(over)
    return out


def job_result(**over):
    out = {"ok": True, "job_id": "2026-10-03_03_28_27-1234567890123456789", "state": "JOB_STATE_FAILED",
           "kind": "not_found", "created": "2026-10-03T03:28:30+00:00", "ended": "2026-10-03T03:31:00+00:00",
           "errors": [{"text": "FileNotFoundException: no files\n\tat a.b.C(D.java:1)", "time": "2026-10-03T03:30:00Z",
                       "count": 2}],
           "log_groups": [], "summary": "JOB_STATE_FAILED after 3 min — kind: not_found — 'no files' (job message)"}
    out.update(over)
    return out


def changes_result(changes=None, **over):
    out = {"ok": True, "changes": changes if changes is not None else [], "total_changes": len(changes or []),
           "summary": "No changes in the 7 days up to 2026-10-03 across no known artifact"}
    out.update(over)
    return out


def change(when="2026-10-01T09:00:00+00:00", frm="1.4.1", to="1.4.2", env="prd", artifact="report-b"):
    return {"artifact": artifact, "when": when, "environment": env, "from_version": frm, "to_version": to,
            "jira_ticket": "JIRA-1", "release_name": "rel-1"}


class Calls:
    def __init__(self):
        self.lock = threading.Lock()
        self.count: dict[str, int] = {}

    def hit(self, name):
        with self.lock:
            self.count[name] = self.count.get(name, 0) + 1


@pytest.fixture(autouse=True)
def fakes(monkeypatch):
    inv.clear_cache()
    monkeypatch.setattr(settings, "support_namespace", "ns", raising=False)
    monkeypatch.setattr(settings, "support_cluster", "cl", raising=False)
    monkeypatch.setattr(settings, "support_source_promql", "", raising=False)
    monkeypatch.setattr(settings, "support_service_account", "svc@example.test", raising=False)
    monkeypatch.setattr(settings, "support_errors_to_model", False, raising=False)
    calls = Calls()
    seen: dict[str, object] = {}

    def triage(day=""):
        calls.hit("triage")
        return report()

    def read(source, start, end, *, min_severity="ERROR", session=None):
        calls.hit("logs")
        seen["logs"] = (source, start, end)
        return logs_result()

    def audit(start, end, *, principal="", session=None):
        calls.hit("audit")
        seen["principal"] = principal
        return audit_result()

    def inspect(job_id, *, region=""):
        calls.hit("dataflow")
        return job_result(job_id=job_id)

    def what_changed(names, before, days=7):
        calls.hit("changes")
        seen["changes"] = (list(names), before, days)
        return changes_result([change()])

    query = metrics_series(0.0, 1.0, 0.0)

    def run_query(expr, session=None, target=None, *, _raw=False, at=""):
        calls.hit("metric")
        seen["at"] = at
        return query(expr, at=at)

    monkeypatch.setattr(support_triage, "triage", triage)
    monkeypatch.setattr(source_logs, "read", read)
    monkeypatch.setattr(source_logs, "audit", audit)
    monkeypatch.setattr(dataflow_job, "inspect", inspect)
    monkeypatch.setattr(release_lookup, "what_changed", what_changed)
    monkeypatch.setattr(monitoring, "run_query", run_query)
    return calls, seen


# ----- window -------------------------------------------------------------------------

def test_window_is_36_hours_from_midnight_utc():
    start, end = inv.cob_window(date(2026, 10, 1), now=datetime(2026, 10, 9, 8, 0, 0, 500, tzinfo=timezone.utc))
    assert start == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def test_window_is_clamped_to_now_for_a_date_still_running():
    now = datetime(2026, 10, 3, 14, 5, 7, 999, tzinfo=timezone.utc)
    start, end = inv.cob_window(date(2026, 10, 3), now=now)
    assert start == datetime(2026, 10, 3, tzinfo=timezone.utc)
    assert end == datetime(2026, 10, 3, 14, 5, 7, tzinfo=timezone.utc)


def test_window_of_yesterday_is_clamped_while_the_36_hours_are_still_running():
    now = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
    assert inv.cob_window(date(2026, 10, 2), now=now)[1] == now


def test_collect_uses_the_window_for_every_read(fakes):
    calls, seen = fakes
    b = inv.collect("2026-09-20", source="sys-a-fetcher")
    assert b["window"] == {"start": "2026-09-20T00:00:00+00:00", "end": "2026-09-21T12:00:00+00:00"}
    assert seen["at"] == "2026-09-21T12:00:00+00:00"
    assert seen["logs"][1:] == (datetime(2026, 9, 20, tzinfo=timezone.utc), datetime(2026, 9, 21, 12, tzinfo=timezone.utc))


def test_a_future_date_is_refused():
    b = inv.collect("2999-01-01", "", source="sys-a-fetcher")
    assert b["ok"] is False and "has not started" in b["error"]


# ----- incident lookup ------------------------------------------------------------------

def test_incident_found_and_read_by_its_shared_source_process_and_jobs(fakes):
    calls, seen = fakes
    b = inv.collect(DAY, INCIDENT_ID, source="ignored", process="ignored")
    assert b["ok"] and b["incident"]["id"] == INCIDENT_ID
    assert b["target"] == {"source": "sys-a-fetcher", "process": "report-b", "job_ids": ["job-1"]}
    assert seen["logs"][0] == "sys-a-fetcher"
    assert seen["changes"] == (["sys-a-fetcher", "report-b"], date(2026, 10, 3), 7)
    assert seen["principal"] == "svc@example.test"


def test_incident_not_found_says_so(monkeypatch):
    b = inv.collect(DAY, "e-ffffffff")
    assert b["ok"] is False
    assert b["error"] == "no incident e-ffffffff on 2026-10-03 — the list may have changed; reopen Support triage"


def test_triage_failure_is_passed_on_with_its_hint(monkeypatch):
    monkeypatch.setattr(support_triage, "triage", lambda day="": {"ok": False, "error": "BQ down", "hint": "grant it"})
    b = inv.collect(DAY, INCIDENT_ID)
    assert (b["ok"], b["error"], b["hint"]) == (False, "BQ down", "grant it")


def test_free_form_question_by_source_process_and_jobs(fakes):
    calls, seen = fakes
    b = inv.collect(DAY, source="sys-b-fetcher", process="report-b", job_ids=["j1", "j2", "j2", "j3", "j4"])
    assert b["ok"] and b["incident"] is None and "triage" not in calls.count
    assert b["target"]["job_ids"] == ["j1", "j2", "j3"]
    assert len(b["evidence"]["dataflow"]) == 3
    assert "What L1 should do" not in inv.render_markdown(b)


def test_free_form_with_nothing_to_look_at_is_refused():
    b = inv.collect(DAY)
    assert b["ok"] is False and "incident id" in b["error"]


def test_a_bad_date_is_refused():
    b = inv.collect("not-a-date", source="sys-a-fetcher")
    assert b["ok"] is False and "not a date" in b["error"]


def test_an_incident_without_a_source_leaves_the_source_steps_unavailable(monkeypatch, fakes):
    calls, _ = fakes
    monkeypatch.setattr(support_triage, "triage", lambda day="": report(incident(shared={}, job_ids=[])))
    b = inv.collect(DAY, INCIDENT_ID)
    assert b["ok"]
    steps = {u["step"]: u["error"] for u in b["unavailable"]}
    assert steps == {"metrics": "this incident's runs do not share one source",
                     "logs": "this incident's runs do not share one source"}
    assert "logs" not in calls.count and "metric" not in calls.count
    assert b["evidence"]["audit"] is not None   # the audit log needs no source


def test_an_unsafe_source_name_is_never_queried(fakes):
    calls, _ = fakes
    b = inv.collect(DAY, source='x"} or vector(1) #')
    assert {u["step"] for u in b["unavailable"]} == {"metrics", "logs"}
    assert "metric" not in calls.count


# ----- parallel collection -----------------------------------------------------------------

def test_the_bundle_is_assembled_from_every_step(fakes):
    b = inv.collect(DAY, INCIDENT_ID)
    assert b["ok"] and b["unavailable"] == []
    assert set(b["evidence"]) == {"metrics", "logs", "audit", "dataflow", "changes"}
    assert b["evidence"]["metrics"]["summary"] == "restarts 0 · up 1 · error_rate 0"
    assert b["evidence"]["dataflow"][0]["job_id"] == "job-1"
    assert "Source metrics (PromQL): restarts 0 · up 1 · error_rate 0" in b["checked"]
    assert any(line.startswith("Audit logs") for line in b["checked"])
    assert isinstance(b["elapsed_ms"], int)
    json.dumps(b)   # a bundle is plain data


def test_the_reads_run_at_the_same_time(monkeypatch):
    # seven reads (3 metrics, logs, audit, one job, release log) must all be
    # inside their fake at once, or the barrier breaks and the test fails.
    barrier = threading.Barrier(7, timeout=5)

    def meet(result):
        def fake(*a, **k):
            barrier.wait()
            return result
        return fake

    ok_query = metrics_series()
    monkeypatch.setattr(monitoring, "run_query", lambda e, session=None, target=None, _raw=False, at="": (barrier.wait(), ok_query(e))[1])
    monkeypatch.setattr(source_logs, "read", meet(logs_result()))
    monkeypatch.setattr(source_logs, "audit", meet(audit_result()))
    monkeypatch.setattr(dataflow_job, "inspect", meet(job_result()))
    monkeypatch.setattr(release_lookup, "what_changed", meet(changes_result()))
    b = inv.collect(DAY, INCIDENT_ID)
    assert b["ok"] and b["unavailable"] == []


def test_a_failing_step_is_unavailable_with_its_hint_and_the_rest_answer(monkeypatch):
    monkeypatch.setattr(source_logs, "read", lambda *a, **k: {"ok": False, "error": "Cloud Logging said 403",
                                                              "hint": "needs roles/logging.viewer"})
    b = inv.collect(DAY, INCIDENT_ID)
    assert b["ok"] and b["evidence"]["logs"] is None and b["evidence"]["audit"] is not None
    assert b["unavailable"] == [{"step": "logs", "error": "Cloud Logging said 403", "hint": "needs roles/logging.viewer"}]
    assert "Could not read logs: Cloud Logging said 403 (needs roles/logging.viewer)" in inv.render_markdown(b)


def test_a_step_that_raises_is_unavailable_not_an_exception(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("bug in a reader")

    monkeypatch.setattr(dataflow_job, "inspect", boom)
    b = inv.collect(DAY, INCIDENT_ID)
    assert b["ok"] and b["unavailable"][0]["step"] == "dataflow job-1"
    assert "RuntimeError" in b["unavailable"][0]["error"]


def test_a_step_that_times_out_is_unavailable(monkeypatch):
    monkeypatch.setattr(inv, "_STEP_TIMEOUT", 0.3)
    release = threading.Event()

    def slow(*a, **k):
        release.wait(5)
        return audit_result()

    monkeypatch.setattr(source_logs, "audit", slow)
    started = time.monotonic()
    b = inv.collect(DAY, INCIDENT_ID)
    release.set()
    assert time.monotonic() - started < 3
    assert b["ok"] and b["evidence"]["audit"] is None
    assert b["unavailable"][0]["step"] == "audit" and "timed out after 0.3s" in b["unavailable"][0]["error"]


def test_metrics_all_failing_is_one_unavailable_step(monkeypatch):
    monkeypatch.setattr(monitoring, "run_query", lambda *a, **k: {"ok": False, "error": "no creds", "hint": "login"})
    b = inv.collect(DAY, INCIDENT_ID)
    assert [u for u in b["unavailable"] if u["step"] == "metrics"] == [
        {"step": "metrics", "error": "no creds", "hint": "login"}]
    assert b["signals"]["source_healthy"] is None


def test_a_check_with_no_series_is_said_so(monkeypatch):
    monkeypatch.setattr(monitoring, "run_query", metrics_series(0.0, None, 0.0))
    b = inv.collect(DAY, INCIDENT_ID)
    assert b["evidence"]["metrics"]["summary"] == "restarts 0 · up no series · error_rate 0"
    assert b["signals"]["source_healthy"] is None


def test_a_placeholder_the_configuration_cannot_fill_is_unavailable(monkeypatch):
    monkeypatch.setattr(settings, "support_source_promql", "x=up{{job=\"{nonsense}\"}}", raising=False)
    b = inv.collect(DAY, INCIDENT_ID)
    assert b["unavailable"][0]["step"] == "metrics" and "cannot be filled" in b["unavailable"][0]["error"]


# ----- signals ----------------------------------------------------------------------------------

@pytest.mark.parametrize("values,expected", [
    ({"restarts": 0, "up": 1, "error_rate": 0}, True),
    ({"restarts": 0, "up": 2, "error_rate": 0.049}, True),
    ({"restarts": 7, "up": 0, "error_rate": 0.42}, False),
    ({"restarts": 1, "up": 1, "error_rate": 0}, False),
    ({"restarts": 0, "up": 0, "error_rate": 0}, False),
    ({"restarts": 0, "up": 1, "error_rate": 0.05}, False),
    ({"restarts": 3, "up": None, "error_rate": None}, False),     # a measured fault is not hidden by a gap
    ({"restarts": 0, "up": 1, "error_rate": None}, None),         # a gap never makes it look healthy
    ({}, None),
])
def test_source_health_rule(values, expected):
    assert inv_signals.source_is_healthy(values) is expected


def test_signals_from_the_live_shaped_bundle(fakes):
    b = inv.collect(DAY, INCIDENT_ID)
    s = b["signals"]
    assert s["source_healthy"] is True and s["security_in_logs"] == 20 and s["denials"] == 6
    assert s["iam_changes"] == 1 and s["job_kinds"] == ["not_found"]
    assert s["streak"] == 3 and s["category"] == "upstream" and s["last_success"] == "2026-09-30"
    assert s["recent_change"]["artifact"] == "report-b" and s["recent_change"]["to_version"] == "1.4.2"


def evidence(**over):
    ev = {"metrics": None, "logs": None, "audit": None, "dataflow": [], "changes": None}
    ev.update(over)
    return ev


def metrics_of(restarts, up, error_rate):
    values = {"restarts": restarts, "up": up, "error_rate": error_rate}
    return {"ok": True, "values": values, "summary": f"restarts {restarts} · up {up} · error_rate {error_rate}"}


def sig(ev, streak=None, business=date(2026, 10, 3), principal="svc@example.test"):
    return inv.compute_signals(business=business, evidence=ev, category="upstream", streak=streak,
                               last_success=None, audit_principal=principal)


def test_lead_security_in_logs():
    leads = sig(evidence(logs=logs_result()))["leads"]
    assert leads == ["The source's own logs show 20 security lines (403 Forbidden from bucket b-1) "
                     "— look at what it was denied."]


def test_lead_one_security_line_is_singular():
    leads = sig(evidence(logs=logs_result(security_lines=1)))["leads"]
    assert "1 security line (" in leads[0]


def test_lead_iam_change_before_the_first_denial():
    leads = sig(evidence(audit=audit_result()))["leads"]
    assert leads[0] == "An IAM policy changed at 01:58 UTC, 5 minutes before the first denial."


def test_lead_iam_change_after_the_denial_or_a_day_earlier_is_not_a_lead():
    late = audit_result(iam_changes=[{"time": "2026-10-03T05:00:00Z"}])
    early = audit_result(denials=[dict(audit_result()["denials"][0], first_seen="2026-10-05T03:00:00Z")])
    assert not any("IAM policy" in x for x in sig(evidence(audit=late))["leads"])
    assert not any("IAM policy" in x for x in sig(evidence(audit=early))["leads"])


def test_audit_leads_say_when_the_read_was_project_wide():
    ev = evidence(audit=audit_result())
    wide = sig(ev, principal="")
    assert wide["audit_scope"] == "project-wide"
    assert all(x.endswith("(project-wide: SUPPORT_SERVICE_ACCOUNT is not set)") for x in wide["leads"]) and wide["leads"]
    assert sig(ev)["audit_scope"] == "service account"


def test_lead_denials_without_an_iam_change():
    leads = sig(evidence(audit=audit_result(iam_changes=[])))["leads"]
    assert leads == ["6 permission-denied calls in the audit log (most: storage.objects.get on "
                     "projects/_/buckets/b-1) — look at which access was removed or never granted."]


def test_lead_recent_release_change_with_days_before():
    ev = evidence(changes=changes_result([change("2026-10-01T09:00:00+00:00")]))
    s = sig(ev, streak=1)
    assert s["recent_change"] == {"artifact": "report-b", "to_version": "1.4.2", "from_version": "1.4.1",
                                  "environment": "prd", "when": "2026-10-01T09:00:00+00:00", "days_before": 2}
    assert s["leads"] == ["report-b went 1.4.1 → 1.4.2 on prd 2 days before the first failure."]


def test_lead_change_is_measured_from_the_first_failing_date_of_a_streak():
    ev = evidence(changes=changes_result([change("2026-10-01T09:00:00+00:00")]))
    s = sig(ev, streak=3)   # failing since 2026-10-01: the change was the same day
    assert s["recent_change"]["days_before"] == 0
    assert s["leads"] == ["report-b went 1.4.1 → 1.4.2 on prd on the same day as the first failure."]


def test_a_change_after_the_first_failure_is_not_a_lead():
    ev = evidence(changes=changes_result([change("2026-10-03T20:00:00+00:00")]))
    assert sig(ev, streak=3)["recent_change"] is None


def test_change_wording_for_a_new_deploy_and_a_removal():
    new = inv_signals._lead_change({"recent_change": {"artifact": "a", "to_version": "2", "from_version": None,
                                              "environment": None, "days_before": 1}}, {})
    gone = inv_signals._lead_change({"recent_change": {"artifact": "a", "to_version": None, "from_version": "1",
                                               "environment": "uat", "days_before": 2}}, {})
    assert new == "a was deployed at 2 1 day before the first failure."
    assert gone == "a was removed on uat 2 days before the first failure."


def test_the_newest_change_wins():
    ev = evidence(changes=changes_result([change("2026-09-30T09:00:00+00:00", to="1.4.1"),
                                          change("2026-10-02T09:00:00+00:00", to="1.4.3")]))
    assert sig(ev)["recent_change"]["to_version"] == "1.4.3"


def test_lead_healthy_source_points_downstream():
    leads = sig(evidence(metrics=metrics_of(0, 1, 0)))["leads"]
    assert leads == ["The source is healthy by its metrics — the problem is downstream of it."]


def test_a_healthy_source_that_logs_security_errors_is_not_called_downstream():
    leads = sig(evidence(metrics=metrics_of(0, 1, 0), logs=logs_result()))["leads"]
    assert not any("downstream" in x for x in leads)


def test_lead_unhealthy_source_comes_first():
    leads = sig(evidence(metrics=metrics_of(7, 0, 0.42), logs=logs_result()))["leads"]
    assert leads[0] == "The source is unhealthy by its metrics (restarts 7 · up 0 · error_rate 0.42) — start with the source."
    assert "security line" in leads[1]


def test_lead_dataflow_job_failure():
    leads = sig(evidence(dataflow=[job_result()]))["leads"]
    assert leads == ["Dataflow job …23456789 failed, kind not_found: no files"]


def test_a_healthy_job_is_no_lead_and_no_kind():
    s = sig(evidence(dataflow=[job_result(kind="none", state="JOB_STATE_DONE")]))
    assert s["leads"] == [] and s["job_kinds"] == []


def test_leads_are_capped_at_four_in_reading_order():
    ev = evidence(metrics=metrics_of(7, 0, 0.42), logs=logs_result(), audit=audit_result(),
                  changes=changes_result([change()]), dataflow=[job_result()])
    leads = sig(ev, streak=1)["leads"]
    assert len(leads) == 4
    assert [("unhealthy" in x, "security" in x, "IAM" in x) for x in leads[:3]] == [
        (True, False, False), (False, True, False), (False, False, True)]


def test_no_evidence_no_leads_and_unknown_health():
    s = sig(evidence())
    assert s["leads"] == [] and s["source_healthy"] is None and s["recent_change"] is None


# ----- timeline --------------------------------------------------------------------------------------

def test_timeline_merges_every_source_in_time_order():
    ev = evidence(logs=logs_result(), audit=audit_result(), dataflow=[job_result()],
                  changes=changes_result([change("2026-10-01T09:00:00+00:00")]))
    tl = inv.build_timeline(ev, last_success="2026-09-30")
    times = [t["time"] for t in tl]
    assert times == sorted(times)
    assert [(t["kind"], t["source"]) for t in tl] == [
        ("run", "control_table"), ("release", "release_log"), ("iam", "audit"), ("denied", "audit"),
        ("job", "dataflow"), ("job", "dataflow"), ("job", "dataflow"), ("security", "logs"),
        ("denied", "audit"), ("security", "logs")]
    assert tl[0]["text"] == "Last successful run of this was on 2026-09-30"
    assert any(t["text"].startswith("Dataflow job …23456789 first error: FileNotFoundException: no files") for t in tl)
    assert any("ended failed (not_found)" in t["text"] for t in tl)
    kinds = {t["kind"] for t in tl}
    assert kinds <= {"run", "release", "iam", "denied", "log", "security", "job", "metric"}


def test_timeline_text_is_one_short_line_never_a_stack_trace():
    trace = "boom " + "x" * 400 + "\n\tat a.b.C(D.java:1)\n\tat a.b.D(E.java:2)"
    ev = evidence(logs=logs_result(groups=[{"signature": "s", "sample": trace, "count": 2, "security": False,
                                            "first_seen": "2026-10-03T01:00:00Z", "last_seen": "2026-10-03T02:00:00Z"}]))
    for t in inv.build_timeline(ev):
        assert len(t["text"]) <= 160 and "\n" not in t["text"] and "D.java" not in t["text"]


def test_timeline_first_and_last_of_a_group_and_a_single_sighting_once():
    one = {"signature": "s", "sample": "once", "count": 1, "security": False,
           "first_seen": "2026-10-03T01:00:00Z", "last_seen": "2026-10-03T01:00:00Z"}
    tl = inv.build_timeline(evidence(logs=logs_result(groups=[one])))
    assert [t["text"] for t in tl] == ["Source logged: once (×1)"]


def test_timeline_removes_duplicates():
    dup = change("2026-10-01T09:00:00+00:00")
    tl = inv.build_timeline(evidence(changes=changes_result([dup, dict(dup)])))
    assert len(tl) == 1


def test_timeline_is_capped_keeping_the_earliest_of_each_kind_and_the_latest():
    changes = [change(f"2026-09-{d:02d}T10:00:00+00:00", to=f"1.{d}") for d in range(1, 29)]
    denials = [{"method": f"m{i}", "resource": f"r{i}", "count": 1, "first_seen": f"2026-10-03T{i:02d}:00:00Z",
                "last_seen": f"2026-10-03T{i:02d}:00:00Z"} for i in range(8)]
    groups = [{"signature": f"s{i}", "sample": f"log {i}", "count": 1, "security": i % 2 == 0,
               "first_seen": f"2026-10-03T{10 + i:02d}:00:00Z", "last_seen": f"2026-10-03T{10 + i:02d}:30:00Z"}
              for i in range(8)]
    ev = evidence(changes=changes_result(changes), audit=audit_result(denials=denials, iam_changes=[]),
                  logs=logs_result(groups=groups))
    tl = inv.build_timeline(ev)
    assert len(tl) == 40
    assert [t["time"] for t in tl] == sorted(t["time"] for t in tl)
    texts = [t["text"] for t in tl]
    assert "report-b 1.0 → 1.1 on prd" not in texts and any("→ 1.1 on" in x for x in texts)   # the earliest release kept
    assert any(t["text"] == "Source last logged: log 7" for t in tl)                           # the latest overall kept
    assert any(t["kind"] == "denied" for t in tl) and any(t["kind"] == "log" for t in tl)


def test_timeline_unparseable_times_are_dropped():
    ev = evidence(audit=audit_result(iam_changes=[{"time": "garbage", "principal": "p", "resource": "r"}], denials=[]))
    assert inv.build_timeline(ev) == []


# ----- for_model ----------------------------------------------------------------------------------------

def big_bundle(words=20):
    long = "word " * words   # 20 words is a 100-character log line, which is what real ones look like
    logs = logs_result(groups=[{"signature": f"sig {i} " + long, "sample": f"{i} " + long, "count": 50 - i,
                                "first_seen": f"2026-10-03T{i:02d}:00:00Z", "last_seen": f"2026-10-03T{i:02d}:30:00Z",
                                "security": i < 3, "severity": "ERROR"} for i in range(20)])
    audit = audit_result(denials=[{"method": f"m.{i}", "resource": f"projects/p/buckets/b-{i}", "count": 9 - i % 9,
                                   "first_seen": f"2026-10-03T{i:02d}:10:00Z", "last_seen": f"2026-10-03T{i:02d}:40:00Z",
                                   "principals": ["a@example.test", "b@example.test", "c@example.test"],
                                   "message": long} for i in range(15)],
                         iam_changes=[{"time": f"2026-10-03T{i:02d}:00:00Z", "principal": f"admin{i}@example.test",
                                       "resource": f"projects/p-{i}", "method": "SetIamPolicy"} for i in range(12)])
    jobs = [job_result(job_id=f"job-{i}", summary=long,
                       errors=[{"text": f"{k} " + long, "time": f"2026-10-03T03:0{k}:00Z", "count": 1} for k in range(5)],
                       log_groups=[{"sample": long, "count": 3, "first_seen": "2026-10-03T03:00:00Z",
                                    "last_seen": "2026-10-03T03:30:00Z"} for _ in range(6)]) for i in range(2)]
    changes = changes_result([change(f"2026-10-01T{i:02d}:00:00+00:00", to=f"1.{i}") for i in range(15)])
    ev = {"metrics": metrics_of(0, 1, 0) | {"source": "sys-a-fetcher", "checks": {"x": long * 20}},
          "logs": logs, "audit": audit, "dataflow": jobs, "changes": changes}
    return {"ok": True, "business_date": DAY, "window": {"start": "a", "end": "b"},
            "incident": incident(), "evidence": ev, "unavailable": [{"step": "x", "error": long, "hint": long}],
            "timeline": inv.build_timeline(ev, "2026-09-30"), "checked": ["checked " + long] * 6,
            "signals": sig(ev, streak=3)}


def test_for_model_caps_each_list_before_it_fits_anything(monkeypatch):
    # With the size fit switched off, only the per-list caps hold: this is the
    # widest copy the model could be given.
    monkeypatch.setattr(inv, "_M_BYTES", 10**9)
    b = big_bundle(words=6)
    m = inv.for_model(b)
    ev = m["evidence"]
    assert len(ev["logs"]["groups"]) == 8 and ev["logs"]["groups_total"] == 20
    assert len(ev["audit"]["denials"]) == 8 and len(ev["audit"]["iam_changes"]) == 8
    assert len(ev["dataflow"]) == 2
    assert all(len(j["errors"]) == 3 and len(j["log_groups"]) == 4 for j in ev["dataflow"])
    assert len(ev["changes"]["changes"]) == 8 and len(m["timeline"]) == 40
    assert "checks" not in ev["metrics"]                       # raw PromQL answers stay out


def test_for_model_of_a_big_bundle_is_bounded_and_under_12_kb():
    b = big_bundle()
    m = inv.for_model(b)
    ev = m["evidence"]
    assert len(ev["logs"]["groups"]) <= 8 and ev["logs"]["groups_total"] == 20
    assert len(ev["audit"]["denials"]) <= 8 and len(ev["audit"]["iam_changes"]) <= 8
    assert all(len(j["errors"]) <= 3 and len(j["log_groups"]) <= 4 for j in ev["dataflow"])
    assert len(ev["changes"]["changes"]) <= 8 and len(m["timeline"]) <= 40
    assert all(len(g["sample"]) <= 200 for g in ev["logs"]["groups"])
    assert all(len(e["text"]) <= 200 for j in ev["dataflow"] for e in j["errors"])
    assert inv.model_size(b) < 12 * 1024
    assert b["evidence"]["logs"]["groups"][0]["sample"].startswith("0 word")   # the bundle itself is untouched
    assert len(b["evidence"]["logs"]["groups"]) == 20


def test_for_model_worst_case_still_has_a_ceiling():
    b = big_bundle()
    b["evidence"]["dataflow"] = b["evidence"]["dataflow"] * 2    # the most jobs an incident carries is 3
    b["evidence"]["dataflow"].append(dict(b["evidence"]["dataflow"][0], job_id="job-9"))
    b["evidence"]["dataflow"] = b["evidence"]["dataflow"][:3]
    big = "x" * 400
    for g in b["evidence"]["logs"]["groups"]:
        g["sample"], g["signature"] = big, big
    b["timeline"] = [{"time": f"2026-10-03T00:{i:02d}:00+00:00", "kind": "log", "source": "logs", "text": big[:160]}
                     for i in range(60)]
    assert inv.model_size(b) < 16 * 1024


def test_for_model_keeps_signals_leads_and_unavailable():
    m = inv.for_model(big_bundle())
    assert m["signals"]["leads"] and m["unavailable"][0]["step"] == "x"
    assert set(m["incident"]) >= {"facts", "action", "steps", "owner", "runbook", "note"}


def test_for_model_withholds_the_error_text_unless_allowed(monkeypatch):
    b = big_bundle()
    assert inv.for_model(b)["incident"]["note"] == "NOTE without error text"
    monkeypatch.setattr(settings, "support_errors_to_model", True, raising=False)
    assert inv.for_model(b)["incident"]["note"] == "NOTE with error text"


def test_for_model_of_a_failure_is_just_the_failure():
    assert inv.for_model({"ok": False, "error": "e", "hint": "h", "elapsed_ms": 5}) == {
        "ok": False, "error": "e", "hint": "h"}


# ----- render_markdown ------------------------------------------------------------------------------------

def test_markdown_has_every_section_and_no_conclusions(fakes):
    md = inv.render_markdown(inv.collect(DAY, INCIDENT_ID))
    heads = [line for line in md.splitlines() if line.startswith("### ")]
    assert heads == ["### What I checked", "### Timeline", "### Leads", "### What L1 should do"]
    assert "- Source metrics (PromQL): restarts 0 · up 1 · error_rate 0" in md
    assert "- 2026-10-03 01:58 UTC — IAM policy changed on projects/p by admin@example.test" in md
    assert "not conclusions" in md
    assert "Do now: escalate · Owner: L2 support" in md and "1. Open the job." in md and "2. Escalate to L2 support." in md
    assert md.endswith("```\nNOTE with error text\n```")      # the person's own page may show the error text
    assert "root cause" not in md.lower()


def test_markdown_names_what_could_not_be_read(monkeypatch):
    monkeypatch.setattr(source_logs, "audit", lambda *a, **k: {"ok": False, "error": "denied", "hint": "grant viewer"})
    md = inv.render_markdown(inv.collect(DAY, INCIDENT_ID))
    assert "- Could not read audit: denied (grant viewer)" in md


def test_markdown_of_a_failure():
    md = inv.render_markdown({"ok": False, "error": "no incident", "hint": "reopen"})
    assert md.startswith("I could not gather the evidence: no incident") and "reopen" in md


def test_markdown_with_nothing_found_says_so():
    md = inv.render_markdown({"ok": True, "incident": None, "checked": [], "unavailable": [], "timeline": [],
                              "signals": {"leads": []}})
    assert "Nothing could be read" in md and "No dated evidence" in md and "gives no lead" in md


# ----- cache ----------------------------------------------------------------------------------------------------

def test_ten_people_cost_one_collect(fakes, monkeypatch):
    calls, _ = fakes
    gate = threading.Event()
    real = source_logs.read

    def slow_read(*a, **k):
        gate.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(source_logs, "read", slow_read)
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(inv.collect(DAY, INCIDENT_ID))) for _ in range(10)]
    for t in threads:
        t.start()
    time.sleep(0.3)      # all ten are inside collect(), nine of them waiting on the first
    gate.set()
    for t in threads:
        t.join(10)
    assert len(results) == 10 and all(r["ok"] for r in results)
    assert calls.count == {"triage": 1, "metric": 3, "logs": 1, "audit": 1, "dataflow": 1, "changes": 1}


def test_a_later_call_is_served_from_the_cache(fakes):
    calls, _ = fakes
    first = inv.collect(DAY, INCIDENT_ID)
    second = inv.collect(DAY, INCIDENT_ID)
    assert second["cached"] is True and "cached" not in first
    assert second["elapsed_ms"] == first["elapsed_ms"]
    assert calls.count["triage"] == 1 and calls.count["logs"] == 1
    second["evidence"]["logs"] = None                      # a caller cannot spoil the shared copy
    assert inv.collect(DAY, INCIDENT_ID)["evidence"]["logs"] is not None


def test_different_incidents_and_free_form_questions_have_their_own_entries(fakes):
    calls, _ = fakes
    inv.collect(DAY, INCIDENT_ID)
    inv.collect(DAY, source="sys-b-fetcher")
    inv.collect(DAY, source="sys-b-fetcher")
    inv.collect("2026-10-02", INCIDENT_ID)
    # three questions, three bundles (the repeat is the cached one) …
    assert len(inv._cache) == 3
    # … but the same source on the same day is READ once, whichever question
    # asked (the shared read cache, tools/support/cache.py); the other day is new
    assert calls.count["logs"] == 2


def test_a_failure_is_not_cached(monkeypatch, fakes):
    calls, _ = fakes
    answers = [{"ok": False, "error": "BQ down"}, report()]
    monkeypatch.setattr(support_triage, "triage", lambda day="": (calls.hit("triage"), answers.pop(0))[1])
    assert inv.collect(DAY, INCIDENT_ID)["ok"] is False
    assert inv.collect(DAY, INCIDENT_ID)["ok"] is True
    assert calls.count["triage"] == 2


def test_waiters_share_a_failure_instead_of_retrying_it(monkeypatch, fakes):
    calls, _ = fakes
    gate = threading.Event()

    def slow_fail(day=""):
        calls.hit("triage")
        gate.wait(5)
        return {"ok": False, "error": "BQ down"}

    monkeypatch.setattr(support_triage, "triage", slow_fail)
    results: list[dict] = []
    threads = [threading.Thread(target=lambda: results.append(inv.collect(DAY, INCIDENT_ID))) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.3)
    gate.set()
    for t in threads:
        t.join(10)
    assert len(results) == 5 and not any(r["ok"] for r in results) and calls.count["triage"] == 1


def test_a_bundle_with_an_unread_step_is_remembered_only_briefly(monkeypatch, fakes):
    calls, _ = fakes
    monkeypatch.setattr(source_logs, "audit", lambda *a, **k: {"ok": False, "error": "slow"})
    inv.collect(DAY, INCIDENT_ID)
    (_, ttl, _), = inv._cache.values()
    assert ttl == inv._CACHE_TTL_PARTIAL < inv._CACHE_TTL
    inv.clear_cache()
    monkeypatch.setattr(source_logs, "audit", lambda *a, **k: audit_result())
    inv.collect(DAY, INCIDENT_ID)
    (_, ttl, _), = inv._cache.values()
    assert ttl == inv._CACHE_TTL


def test_the_cache_expires_and_is_bounded(monkeypatch, fakes):
    calls, _ = fakes
    monkeypatch.setattr(inv, "_CACHE_MAX", 3)
    for i in range(6):
        inv.collect(DAY, source=f"svc-{i}")
    assert len(inv._cache) <= 3
    inv.clear_cache()
    inv.collect(DAY, INCIDENT_ID)
    key = next(iter(inv._cache))
    at, ttl, bundle = inv._cache[key]
    inv._cache[key] = (at - ttl - 1, ttl, bundle)
    inv.collect(DAY, INCIDENT_ID)
    assert calls.count["triage"] == 2


def test_collect_never_raises(monkeypatch):
    def boom(day=""):
        raise RuntimeError("triage bug")

    monkeypatch.setattr(support_triage, "triage", boom)
    b = inv.collect(DAY, INCIDENT_ID)
    assert b["ok"] is False and "RuntimeError" in b["error"]


def test_signals_record_whether_the_audit_read_was_narrowed(monkeypatch):
    assert inv.collect(DAY, source="sys-a-fetcher")["signals"]["audit_scope"] == "service account"
    inv.clear_cache()
    monkeypatch.setattr(settings, "support_service_account", "", raising=False)
    b = inv.collect(DAY, source="sys-a-fetcher")
    assert b["signals"]["audit_scope"] == "project-wide"


def test_an_incident_with_no_shared_key_still_collects(monkeypatch, fakes):
    # stuck / missing / volume incidents carry no `shared` and no error_index
    stuck = {"id": "stuck", "kind": "stuck", "title": "1 stuck", "category": "stuck", "count": 1, "facts": ["f"],
             "job_ids": ["job-7"], "runbook": None, "action": "check", "steps": ["Open the job."],
             "owner": "L2 support", "note": "n", "note_without_error": "n"}
    monkeypatch.setattr(support_triage, "triage", lambda day="": report(stuck))
    b = inv.collect(DAY, "stuck")
    assert b["ok"] and b["target"]["job_ids"] == ["job-7"] and b["signals"]["streak"] is None
    assert {u["step"] for u in b["unavailable"]} == {"metrics", "logs"}


# ----- the judgement code makes for the model ------------------------------------------

def test_a_failure_kind_decides_the_action_and_whether_a_cause_is_established():
    from release_agent.tools.support import evidence as inv

    def judged(**sig):
        j = inv.with_judgement({"source_healthy": True, "security_in_logs": 0, "denials": 0, "job_kinds": [],
                                "recent_change": None, "category": "unit", **sig})
        return j["conclusive"], (j["action_hint"] or {}).get("action")

    assert judged(job_kinds=["out_of_memory"]) == (True, "escalate")     # a retry fails the same way
    assert judged(job_kinds=["quota"]) == (True, "retrigger")
    assert judged(job_kinds=["not_found"]) == (True, "wait")
    assert judged(job_kinds=["schema"], recent_change={"artifact": "report-b"}, category="process") == (True, "escalate")
    assert judged(security_in_logs=20, job_kinds=["not_found"]) == (True, "escalate")   # a denial outranks the symptom
    assert judged(source_healthy=False, job_kinds=["not_found"]) == (True, "wait")      # start with the source
    # nothing points anywhere: no cause, no hint — the triage's own action stands
    assert judged(job_kinds=["unknown"]) == (False, None)
    assert judged() == (False, None)
    # project-wide audit denials are cluster noise, not this pipeline's evidence
    assert judged(denials=200, audit_scope="project-wide") == (False, None)
    assert judged(denials=3, audit_scope="service account") == (True, "escalate")


def test_a_bundle_built_without_the_judgement_gets_it_when_the_model_sees_it():
    from release_agent.tools.support import evidence as inv

    bundle = {"ok": True, "business_date": "2026-10-02", "incident": None, "evidence": {}, "unavailable": [],
              "timeline": [], "checked": [], "signals": {"job_kinds": ["out_of_memory"], "source_healthy": True}}
    seen = inv.for_model(bundle)["signals"]
    assert seen["conclusive"] is True and seen["action_hint"]["action"] == "escalate"
    assert "The evidence suggests: escalate" in inv.render_markdown(bundle)
    bundle["signals"] = {"job_kinds": ["unknown"], "source_healthy": True}
    assert "does not establish a cause" in inv.render_markdown(bundle)
