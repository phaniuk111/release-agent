"""Support triage (tools/support/): a made-up control table only —
the real one's names live in private config, never here."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from release_agent.tools import bq_guard
from release_agent.tools.support import config as s_config, incidents as s_incidents, queries as s_queries, report as s_report, triage as s_triage

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
D, P, P2 = date(2026, 10, 2), date(2026, 10, 1), date(2026, 9, 30)
ROLES = ["date", "run_id", "status", "updated_at", "event_at", "event_id", "job_id", "error", "details",
         "members", "system", "process", "unit"]
COLS = ",".join(f"{r}=c_{r}" for r in ROLES)


def fail(run, unit, *, system="SYS-A", process="REPORT-1", err="boom", job=None, key=None, attempts=1):
    """A row of the failures statement (role-keyed, as _run returns it)."""
    return {"run_id": run, "status": "FAILED", "unit": unit, "system": system, "process": process,
            "error": err, "job_id": job, "key": key or f"{system}|{process}|{unit}", "attempts": attempts}


def report(*, failures=(), counts=None, values=None, history=(), dates=(P, D), cob=D, previous=P, roles=ROLES, **rows):
    """build_report over hand-written statement results."""
    failures = list(failures)
    counts = counts if counts is not None else {"failed": len(failures)}
    count_rows = [{"state": st_, "n": n, "retried_after_failure": rows.pop(f"recovered_{st_}", 0)}
                  for st_, n in counts.items()]
    value_rows = [{"role": r, "value": v, "n": n} for (r, v), n in (values or {}).items()]
    return s_report.build_report(roles=roles, cob=cob, previous=previous, dates=list(dates), now=NOW,
                           rows={"counts": count_rows, "failures": failures, "values": value_rows,
                                 "history": list(history), **rows})


# ----- configuration --------------------------------------------------------------

def test_columns_map_roles_with_labels_and_refuse_anything_else():
    cols, labels = s_config.parse_columns("date=biz_date:COB, run_id=run, status=state, unit=acct:Account")
    assert cols == {"date": "biz_date", "run_id": "run", "status": "state", "unit": "acct"}
    assert labels["date"] == "COB" and labels["unit"] == "Account" and labels["run_id"] == "run"
    for bad in ("date=d,run_id=r", "date=d,run_id=r,status=s,colour=c",
                "date=d,run_id=r,status=s;DROP", "date=d,run_id=r,status=`x`", "date=d,date=e,run_id=r,status=s"):
        with pytest.raises(s_config.ConfigError):
            s_config.parse_columns(bad)


def test_the_table_reference_is_checked_character_by_character():
    assert s_config.parse_table("my-proj.ops.control_runs") == "`my-proj.ops.control_runs`"
    assert s_config.parse_table("`my-proj`.`ops`.`control_runs`") == "`my-proj.ops.control_runs`"
    for bad in ("ops.control_runs", "p.d.t;DROP TABLE x", "p.d.t` OR 1", "p.d-s.t", ""):
        with pytest.raises(s_config.ConfigError):
            s_config.parse_table(bad)


# ----- the statements ---------------------------------------------------------------

REF = "`p.ops.control_runs`"


def test_every_statement_is_a_real_read_of_the_one_table_and_nothing_else():
    cols, _ = s_config.parse_columns(COLS + ",source=c_source,scope=c_scope")
    for name, sql in s_queries.Query(REF, cols).statements().items():
        assert bq_guard.classify(sql, (REF,)) == "allowed", name
        assert bq_guard.classify(sql) == "select", name           # no permission → a dry run
        assert not sql.upper().startswith("WITH"), name           # the guard would dry-run a CTE
        assert "@" not in sql.replace("@start", "").replace("@end", "").replace("@cob", "").replace(
            "@prev", "").replace("@failed", "").replace("@done", "").replace("@stuck_before", ""), name
    other = s_queries.Query(REF, cols).failures().replace(" WHERE TRUE", " JOIN `p.ops.other` o ON TRUE WHERE TRUE", 1)
    assert bq_guard.classify(other, (REF,)) == "select"


def test_only_the_business_date_reads_error_text():
    cols, _ = s_config.parse_columns(COLS)
    for name, sql in s_queries.Query(REF, cols).statements().items():
        if "c_error" in sql or "c_details" in sql:
            assert name == "failures" and "BETWEEN" not in sql, name


def test_a_map_without_grouping_columns_leaves_out_what_needs_them():
    cols, _ = s_config.parse_columns("date=d,run_id=r,status=s")
    names = set(s_queries.Query(REF, cols).statements())
    assert names == {"dates", "counts", "failures", "stuck", "retries"}
    sql = s_queries.Query(REF, cols).items("d = @cob")
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY r_date, r_run_id ORDER BY r_run_id DESC) = 1" in sql


def test_a_run_is_its_latest_row_and_retries_of_one_event_are_one_item():
    cols, _ = s_config.parse_columns(COLS)
    sql = s_queries.Query(REF, cols).items("c_date = @cob")
    assert "PARTITION BY c_date, c_run_id ORDER BY c_updated_at DESC" in sql
    assert "COALESCE(r_event_id, r_run_id) AS r_item" in sql
    assert "COUNTIF(UPPER(TRIM(r_status)) IN UNNEST(@failed)) OVER" in sql


# ----- the report ---------------------------------------------------------------------

def test_many_units_one_system_is_an_upstream_pattern_and_one_error_group():
    fails = [fail(f"d{u}", f"U{u}", err=f"Source file /in/feed_2026100{u}.csv not found", job=f"job-{u}")
             for u in range(1, 6)]
    rep = report(failures=fails, counts={"failed": 5, "done": 1},
                 values={("system", "SYS-A"): 5, ("system", "SYS-B"): 1, ("process", "REPORT-1"): 5})
    assert rep["business_date"] == "2026-10-02" and rep["previous_date"] == "2026-10-01"
    assert rep["counts"]["failed"] == 5 and rep["counts"]["items"] == 6
    assert rep["patterns"][0] == {"role": "system", "value": "SYS-A", "failed": 5, "of_failed": 5,
                                  "runs_with_value": 5}
    (err,) = rep["errors"]
    assert err["signature"] == "Source file # not found"     # the dated file names group together
    assert err["category"] == "upstream" and err["count"] == 5 and err["job_ids"] == ["job-1", "job-2", "job-3"]


def test_a_failure_fixed_by_a_retry_counts_as_recovered_not_failed():
    rep = report(counts={"done": 1}, recovered_done=1,
                 retries=[{"item": "E1", "attempts": 2, "status": "SUCCEEDED", "state": "done",
                           "unit": "U1", "system": "SYS-A", "process": "REPORT-1"}])
    assert rep["counts"]["failed"] == 0 and rep["counts"]["recovered"] == 1
    assert rep["retries"][0]["attempts"] == 2 and rep["retries"][0]["final_state"] == "done"


def test_recurring_failures_count_dates_in_a_row_and_name_the_last_success():
    k = "SYS-A|REPORT-1|U1"
    history = [{"key": k, "date": P2, "state": "done"}, {"key": k, "date": P, "state": "failed"},
               {"key": k, "date": D, "state": "failed"}]
    rep = report(failures=[fail("x2", "U1", err="schema mismatch", key=k)], history=history, dates=(P2, P, D))
    (f,) = rep["failures"]
    assert f["streak"] == 2 and f["last_success"] == "2026-09-30"
    assert rep["errors"][0]["category"] == "unit" and rep["errors"][0]["recurring"] == 1


def test_weekends_without_runs_do_not_break_a_streak():
    k = "SYS-A|REPORT-1|U1"
    friday, monday = date(2026, 9, 25), date(2026, 9, 28)
    history = [{"key": k, "date": date(2026, 9, 24), "state": "done"},
               {"key": k, "date": friday, "state": "failed"}, {"key": k, "date": monday, "state": "failed"}]
    rep = report(failures=[fail("m", "U1", key=k)], history=history, cob=monday, previous=friday,
                 dates=(friday, monday))
    assert rep["failures"][0]["streak"] == 2 and rep["failures"][0]["last_success"] == "2026-09-24"


def test_stuck_missing_and_collapsed_output_come_through_with_their_totals():
    rep = report(counts={"done": 1, "open": 2},
                 stuck=[{"run_id": "s1", "status": "RUNNING", "unit": "U7", "updated_at": NOW - timedelta(hours=5),
                         "job_id": None, "total": 3}],
                 missing=[{"system": "SYS-A", "process": "REPORT-1", "unit": "U8", "status": "SUCCEEDED", "total": 1}],
                 volume=[{"run_id": "v2", "unit": "U9", "members": "0", "previous_members": "1200"}],
                 slowest=[{"run_id": "r1", "minutes": 95}])
    assert rep["stuck"][0]["minutes_since_update"] == 300 and rep["counts"]["stuck"] == 3
    assert rep["missing"] == [{"dims": {"system": "SYS-A", "process": "REPORT-1", "unit": "U8"},
                               "previous_status": "SUCCEEDED"}] and rep["counts"]["missing"] == 1
    assert rep["volume"][0]["members"] == 0 and rep["volume"][0]["previous_members"] == 1200
    assert rep["slowest"][0]["minutes"] == 95


def test_a_date_with_no_rows_is_empty_not_an_error():
    rep = report(dates=(P,), cob=D)
    assert rep["empty"] is True and rep["counts"]["failed"] == 0


def test_more_failures_than_read_in_full_keeps_the_exact_count_and_says_so():
    rep = report(failures=[fail("a", "U1")], counts={"failed": 7000})
    assert rep["counts"]["failed"] == 7000 and any("first 1 were read" in n for n in rep["notes"])


def test_a_check_that_could_not_run_is_a_note_not_a_failure():
    rep = s_report.build_report(roles=ROLES, cob=D, previous=P, dates=[P, D], now=NOW,
                          rows={"counts": [{"state": "done", "n": 3}]}, errors={"volume": "Forbidden: 403"})
    assert rep["counts"]["done"] == 3 and any("volume check could not run" in n for n in rep["notes"])


def test_without_grouping_columns_it_says_what_it_cannot_work_out():
    rep = report(failures=[{"run_id": "r1", "status": "FAILED", "key": ""}], roles=["date", "run_id", "status"])
    assert rep["counts"]["failed"] == 1
    assert any("recurrence" in n for n in rep["notes"]) and any("retry" in n for n in rep["notes"])


# ----- the L1 layer ------------------------------------------------------------------

LABELS = {"system": "System", "process": "Report", "unit": "Book"}


def _with_label(rep):
    rep["date_label"] = "COB"
    return rep


def test_each_category_gets_one_l1_action_and_an_owner():
    up = report(failures=[fail(f"d{u}", f"U{u}", err="file not found") for u in range(1, 4)],
                values={("system", "SYS-A"): 3})
    incs = s_incidents.incidents(_with_label(up), labels=LABELS, runbook=[],
                        owners={"system:SYS-A": "Feed team", "default": "Platform L2"})
    (inc,) = incs
    assert inc["action"] == "wait" and inc["owner"] == "Feed team" and inc["priority"] == "high"
    assert "Do not re-run" in " ".join(inc["steps"])
    assert inc["note"].startswith("[Support triage] COB 2026-10-02:")
    assert "Suggested owner: Feed team" in inc["note"] and "Error: file not found" in inc["note"]
    assert "Error:" not in inc["note_without_error"]

    proc = report(failures=[fail(f"d{u}", f"U{u}", system=f"SYS-{u}", err="null pointer") for u in range(1, 4)])
    assert s_incidents.incidents(_with_label(proc), labels=LABELS, runbook=[], owners={})[0]["action"] == "escalate"

    single = report(failures=[fail("d1", "U1", err="timeout")])
    (one,) = s_incidents.incidents(_with_label(single), labels=LABELS, runbook=[], owners={})
    assert one["action"] == "check" and one["owner"] == "L2 support"   # one unit: check its data first


def test_a_runbook_match_wins_and_names_the_known_issue():
    runbook = [{"match": ["quota", "exceeded"], "title": "Cloud quota exceeded", "action": "retrigger",
                "steps": ["Re-trigger after 15 minutes."], "escalate_to": "Platform L2",
                "category": None, "when": {}}]
    rep_ = report(failures=[fail("d1", "U1", err="Quota exceeded for workers in region")])
    (inc,) = s_incidents.incidents(_with_label(rep_), labels=LABELS, runbook=runbook, owners={})
    assert inc["runbook"] == "Cloud quota exceeded" and inc["action"] == "retrigger"
    assert inc["owner"] == "Platform L2" and "Known issue: Cloud quota exceeded" in inc["note"]


def test_incidents_come_most_urgent_first():
    rep_ = report(failures=[fail("d1", "U1", err="timeout")], counts={"failed": 1, "done": 1, "open": 1},
                  stuck=[{"run_id": "s1", "status": "RUNNING", "updated_at": NOW - timedelta(hours=5), "total": 1}],
                  volume=[{"run_id": "v2", "unit": "U9", "members": "0", "previous_members": "500"}])
    kinds = [i["kind"] for i in s_incidents.incidents(_with_label(rep_), labels=LABELS, runbook=[], owners={})]
    assert kinds[0] == "volume" and kinds.index("stuck") < kinds.index("error")


def test_the_runbook_file_is_read_and_bad_entries_skipped(tmp_path):
    good = tmp_path / "rb.json"
    good.write_text(json.dumps({"entries": [
        {"match": "not found", "title": "Feed late", "action": "wait", "steps": ["x"]},
        {"match": "", "title": "no match text"}, {"title": "no match"}, "not an object",
        {"match": "y", "title": "Unknown action", "action": "reboot"}]}))
    entries, problem = s_incidents.load_runbook(str(good))
    assert problem is None and [e["title"] for e in entries] == ["Feed late", "Unknown action"]
    assert entries[1]["action"] == "check"
    assert s_incidents.load_runbook(str(tmp_path / "missing.json"))[1]
    assert s_incidents.load_runbook("") == ([], None)


def test_the_runbook_shipped_in_the_chart_loads():
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "helm" / "release-copilot" / "files" / "support_runbook.json"
    entries, problem = s_incidents.load_runbook(str(path))
    assert problem is None and len(entries) >= 3


def test_owners_parse_role_value_pairs_and_a_default():
    assert s_incidents.parse_owners("system:SYS-A=Feed team, process:R=Reports L2,default=Platform L2,junk") == {
        "system:SYS-A": "Feed team", "process:R": "Reports L2", "default": "Platform L2"}


# ----- what the model may see ----------------------------------------------------------

def test_error_text_stays_out_of_the_model_unless_allowed(monkeypatch):
    rep = {"ok": True, **_with_label(report(failures=[fail("d1", "U1", err="account 12345 balance mismatch")]))}
    rep["incidents"] = s_incidents.incidents(rep, labels=LABELS, runbook=[], owners={})
    monkeypatch.setattr(s_config.settings, "support_errors_to_model", False)
    seen = json.dumps(s_triage.for_model(rep))
    assert "balance mismatch" not in seen and "error #1" in seen
    monkeypatch.setattr(s_config.settings, "support_errors_to_model", True)
    assert "balance mismatch" in json.dumps(s_triage.for_model(rep))


# ----- the entry point ---------------------------------------------------------------

@pytest.fixture
def configured(monkeypatch):
    s = s_config.settings
    monkeypatch.setattr(s, "support_table", "p.ops.control_runs")
    monkeypatch.setattr(s, "support_columns",
                        "date=biz_date:COB,run_id=run,status=state,updated_at=written,unit=acct,system=feed,error=msg")
    monkeypatch.setattr(s, "support_runbook_file", "")
    monkeypatch.setattr(s, "support_owners", "default=Platform L2")
    return s


def _fake_statements(monkeypatch, answers, seen):
    """Stand in for BigQuery: answer each statement by its NAME (the Query
    method that built it), and record the parameters it was sent."""
    def fake_run(table_ref, sql, params, budget):
        cols, _ = s_config.parse_columns(s_config.settings.support_columns)
        name = next(n for n, q in s_queries.Query(table_ref, cols).statements().items() if q == sql)
        seen[name] = {k for k in params if f"@{k}" in sql}
        budget.reserve()
        return answers.get(name, [])
    monkeypatch.setattr(s_queries, "_run", fake_run)


def test_triage_runs_every_statement_and_returns_l1_incidents(monkeypatch, configured):
    answers = {
        "dates": [{"d": P}, {"d": D}],
        "counts": [{"state": "failed", "n": 3, "retried_after_failure": 0},
                   {"state": "done", "n": 9, "retried_after_failure": 1}],
        "failures": [{"run_id": f"r{u}", "status": "FAILED", "unit": f"U{u}", "system": "SYS-A",
                      "error": "file not found", "key": f"SYS-A|U{u}"} for u in range(3)],
        "values": [{"role": "system", "value": "SYS-A", "n": 3}],
    }
    seen: dict = {}
    _fake_statements(monkeypatch, answers, seen)
    out = s_triage.triage("")
    assert out["ok"] is True and out["business_date"] == "2026-10-02" and out["previous_date"] == "2026-10-01"
    assert out["counts"] == {"items": 12, "failed": 3, "done": 9, "open": 0, "stuck": 0, "missing": 0, "recovered": 1}
    assert set(seen) == {"dates", "counts", "failures", "values", "stuck", "retries", "missing", "history"}
    assert seen["counts"] == {"cob", "failed", "done"} and seen["missing"] >= {"prev", "cob"}
    assert out["incidents"][0]["action"] == "wait" and out["labels"] == {"system": "feed", "unit": "acct"}
    assert out["queries"] == len(seen)


def test_an_optional_check_failing_degrades_to_a_note(monkeypatch, configured):
    def fake_run(table_ref, sql, params, budget):
        if "NOT IN (SELECT DISTINCT" in sql:
            raise RuntimeError("403 Forbidden")
        return [{"d": P}, {"d": D}] if " AS d FROM " in sql else []
    monkeypatch.setattr(s_queries, "_run", fake_run)
    out = s_triage.triage("2026-10-02")
    assert out["ok"] is True and any("could not run" in n for n in out["notes"])


def test_triage_disabled_misconfigured_and_denied_answer_instead_of_raising(monkeypatch, configured):
    monkeypatch.setattr(s_config.settings, "support_table", "")
    assert s_triage.triage()["disabled"] is True
    monkeypatch.setattr(s_config.settings, "support_table", "p.ops.control_runs")
    monkeypatch.setattr(s_config.settings, "support_columns", "date=d")
    assert "required" in s_triage.triage()["error"]
    monkeypatch.setattr(s_config.settings, "support_columns", "date=d,run_id=r,status=s")
    assert "not a date" in s_triage.triage("yesterday")["error"]

    class Denied:
        def query(self, *a, **k):
            raise RuntimeError("403 Access Denied: User does not have permission bigquery.tables.getData")
    monkeypatch.setattr(s_queries, "_get_client", lambda: Denied())   # the real _run → the guard → this client
    out = s_triage.triage()
    assert out["ok"] is False and "Data Viewer" in out["hint"]


def test_the_api_refuses_non_preview_users_and_the_chat_tool_too(monkeypatch):
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi, features

    monkeypatch.setattr(features.settings, "preview_features", "support-triage")
    monkeypatch.setattr(features.settings, "preview_users", "")
    res = TestClient(app_fastapi.app).get("/api/support/triage")
    assert res.status_code == 403 and "preview" in res.json()["error"]

    from adk_release_agent import tools

    assert "preview" in tools.support_triage()["error"]


def test_the_api_answers_disabled_for_a_preview_user(monkeypatch):
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi, features

    monkeypatch.setattr(features.settings, "preview_users", "*")
    monkeypatch.setattr(s_config.settings, "support_table", "")
    from release_agent import support_api

    support_api._support_caches.clear()
    res = TestClient(app_fastapi.app).get("/api/support/triage?fresh=1")
    assert res.status_code == 200 and res.json()["disabled"] is True


def test_a_few_failures_in_a_healthy_feed_are_not_an_upstream_outage():
    """Found live: 4 accounts of one feed failing one report with a schema error,
    while that feed's other 20 runs succeeded — a process problem, not the feed."""
    fails = [fail(f"bad{u}", f"V{u}", system="SYS-B", process="REPORT-B", err="Schema mismatch: column x")
             for u in range(4)]
    rep = report(failures=fails, counts={"failed": 4, "done": 20},
                 values={("system", "SYS-B"): 24, ("process", "REPORT-B"): 4, ("process", "REPORT-A"): 20})
    (err,) = rep["errors"]
    assert err["category"] == "process" and err["cause"] == {"process": "REPORT-B"}
    (inc,) = s_incidents.incidents(_with_label(rep), labels=LABELS, runbook=[],
                          owners={"system:SYS-B": "Feed team", "process:REPORT-B": "Reports L2"})
    assert inc["owner"] == "Reports L2" and inc["title"] == "4 failed · process · Report REPORT-B"
    assert inc["steps"][0].startswith("4 Book values of Report REPORT-B fail")


def test_the_chart_mounts_the_runbook_only_when_enabled():
    import pathlib
    import shutil
    import subprocess

    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    chart = pathlib.Path(__file__).resolve().parent.parent / "helm" / "release-copilot"
    on = subprocess.run(["helm", "template", "t", str(chart), "--set", "supportRunbook.enabled=true"],
                        capture_output=True, text=True, check=True).stdout
    assert "name: t-release-copilot-support-runbook" in on
    assert 'SUPPORT_RUNBOOK_FILE: "/etc/release-copilot/support/runbook.json"' in on
    assert "mountPath: /etc/release-copilot/support" in on and "checksum/support-runbook" in on
    off = subprocess.run(["helm", "template", "t", str(chart)], capture_output=True, text=True, check=True).stdout
    assert "support-runbook" not in off and "SUPPORT_RUNBOOK_FILE" not in off
    # the cluster's preview defaults gate it too (values.yaml overrides s_config.py)
    assert 'PREVIEW_FEATURES: "monitoring,bq-cost,support-triage"' in off
    assert 'PREVIEW_GROUPS: "Check,Monitoring,Support"' in off


# ----- priority ---------------------------------------------------------------------

def _prio(rep, **rules):
    return [(i["priority"], i["priority_reason"]) for i in
            s_incidents.incidents(_with_label(rep), labels=LABELS, runbook=[], owners={}, rules=s_incidents.PriorityRules(**rules))]


def test_every_priority_says_which_rule_set_it():
    one = report(failures=[fail("d1", "U1", err="timeout")])
    assert _prio(one) == [("low", "one run, first time")]
    many = report(failures=[fail(f"d{u}", f"U{u}", system=f"S{u}", process=f"P{u}", err="x") for u in range(12)])
    assert _prio(many) == [("high", "12 runs (≥ 10)")]
    assert _prio(many, high_count=20) == [("high", "12 runs, the same error everywhere (platform)")]
    few = report(failures=[fail("d1", "U1", err="a"), fail("d2", "U2", err="b"), fail("d3", "U3", err="c")])
    assert _prio(few, medium_count=1) == [("medium", "1 run (≥ 1)")] * 3


def test_a_critical_value_is_always_high_even_for_one_run():
    one = report(failures=[fail("d1", "U1", process="REG-1", err="timeout")])
    assert _prio(one, critical={("process", "REG-1")}) == [("high", "critical: process REG-1")]
    assert s_incidents.parse_critical("process:REG-1, scope:E1, colour:red, junk") == {("process", "REG-1"), ("scope", "E1")}


def test_an_error_incident_keeps_its_id_when_counts_and_order_change():
    a = report(failures=[fail("d1", "U1", err="timeout on 2026-10-02")])
    b = report(failures=[fail(f"x{u}", f"U{u}", err="quota exceeded") for u in range(5)]
               + [fail("d9", "U9", err="timeout on 2026-10-03")])
    ids_a = {i["title"].split(" · ")[1]: i["id"] for i in s_incidents.incidents(_with_label(a), labels=LABELS, runbook=[], owners={})}
    by_text = {i["error_text"][:7]: i["id"] for i in s_incidents.incidents(_with_label(b), labels=LABELS, runbook=[], owners={})}
    assert list(ids_a.values())[0] == by_text["timeout"] and by_text["timeout"].startswith("e-")
    assert by_text["quota e"] != by_text["timeout"]


def test_an_error_group_and_its_incident_carry_each_runs_id_and_written_time():
    """The run id and when its row was written are what find the run's own log
    lines; a run with no written time has nothing to anchor that search on."""
    at = NOW - timedelta(minutes=30)
    rows = [{**fail("r1", "U1", err="no files matched spec"), "updated_at": at},
            {**fail("r2", "U2", err="no files matched spec"), "updated_at": at.isoformat()},
            {**fail("r3", "U3", err="no files matched spec"), "updated_at": None}]
    rep = report(failures=rows)
    assert rep["errors"][0]["runs"] == [{"run_id": "r1", "at": at.isoformat()},
                                        {"run_id": "r2", "at": at.isoformat()}]
    inc = s_incidents.incidents(rep, labels={}, runbook=[], owners={})[0]
    assert inc["runs"] == rep["errors"][0]["runs"]


def test_a_stuck_incident_carries_its_runs_last_written_time():
    at = NOW - timedelta(hours=5)
    rep = report(counts={"open": 1}, stuck=[{"run_id": "s1", "status": "RUNNING", "updated_at": at, "total": 1}])
    inc = next(i for i in s_incidents.incidents(rep, labels={}, runbook=[], owners={}) if i["kind"] == "stuck")
    assert inc["runs"] == [{"run_id": "s1", "at": at.isoformat()}]
