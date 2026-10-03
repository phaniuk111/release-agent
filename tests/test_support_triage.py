"""Support triage (tools/support_triage.py): a made-up control table only —
the real one's names live in private config, never here."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from release_agent.tools import bq_guard
from release_agent.tools import support_triage as st

NOW = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
D, P, P2 = date(2026, 10, 2), date(2026, 10, 1), date(2026, 9, 30)
ROLES = ["date", "run_id", "status", "updated_at", "event_at", "event_id", "job_id", "error",
         "members", "system", "process", "unit"]


def row(d, run, status, unit, *, system="SYS-A", process="REPORT-1", err="", members=100,
        event=None, at=None, job=None):
    at = at or datetime(d.year, d.month, d.day, 22, tzinfo=timezone.utc)
    return {"date": d, "run_id": run, "status": status, "unit": unit, "system": system,
            "process": process, "error": err, "members": str(members),
            "event_id": event or f"{d}-{unit}-{process}", "updated_at": at,
            "event_at": at - timedelta(minutes=30), "job_id": job}


def analyse(rows, **kw):
    return st.analyse(rows, roles=kw.pop("roles", ROLES), business_date=kw.pop("business_date", None),
                      now=NOW, failed_statuses=["FAILED", "ERROR"], done_statuses=["SUCCEEDED"], **kw)


# ----- configuration --------------------------------------------------------------

def test_columns_map_roles_with_labels_and_refuse_anything_else():
    cols, labels = st.parse_columns("date=biz_date:COB, run_id=run, status=state, unit=acct:Account")
    assert cols == {"date": "biz_date", "run_id": "run", "status": "state", "unit": "acct"}
    assert labels["date"] == "COB" and labels["unit"] == "Account" and labels["run_id"] == "run"
    for bad in ("date=d,run_id=r", "date=d,run_id=r,status=s,colour=c",
                "date=d,run_id=r,status=s;DROP", "date=d,run_id=r,status=`x`", "date=d,date=e,run_id=r,status=s"):
        with pytest.raises(st.ConfigError):
            st.parse_columns(bad)


def test_the_table_reference_is_checked_character_by_character():
    assert st.parse_table("my-proj.ops.control_runs") == "`my-proj.ops.control_runs`"
    assert st.parse_table("`my-proj`.`ops`.`control_runs`") == "`my-proj.ops.control_runs`"
    for bad in ("ops.control_runs", "p.d.t;DROP TABLE x", "p.d.t` OR 1", "p.d-s.t", ""):
        with pytest.raises(st.ConfigError):
            st.parse_table(bad)


def test_the_query_is_one_read_of_the_one_table_and_the_guard_runs_it_for_real():
    cols, _ = st.parse_columns("date=biz_date,run_id=run,status=state,updated_at=written")
    ref = st.parse_table("p.ops.control_runs")
    sql = st.build_sql(ref, cols)
    assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY run ORDER BY written DESC) = 1" in sql
    assert "@start" in sql and "@end" in sql and "@max_rows" in sql
    assert bq_guard.classify(sql, (ref,)) == "allowed"
    # no permission given → a dry run, as for any other table
    assert bq_guard.classify(sql) == "select"
    # a second table in the statement is not covered by the permission
    other = sql.replace(" WHERE", " JOIN `p.ops.other` o ON TRUE WHERE")
    assert bq_guard.classify(other, (ref,)) == "select"
    assert bq_guard.classify("DELETE FROM `p.ops.control_runs` WHERE TRUE", (ref,)) == "refused"


# ----- the analysis ----------------------------------------------------------------

def test_many_units_one_system_is_an_upstream_pattern_and_one_error_group():
    rows = [row(P, f"p{u}", "SUCCEEDED", f"U{u}") for u in range(1, 6)]
    rows += [row(D, f"d{u}", "FAILED", f"U{u}", err=f"Source file /in/feed_2026100{u}.csv not found",
                 job=f"job-{u}") for u in range(1, 6)]
    rows.append(row(D, "ok1", "SUCCEEDED", "U9", system="SYS-B", process="REPORT-2"))
    rep = analyse(rows)
    assert rep["business_date"] == "2026-10-02" and rep["previous_date"] == "2026-10-01"
    assert rep["counts"]["failed"] == 5 and rep["counts"]["items"] == 6
    assert rep["patterns"][0] == {"role": "system", "value": "SYS-A", "failed": 5, "of_failed": 5,
                                  "runs_with_value": 5}
    (err,) = rep["errors"]
    assert err["signature"] == "Source file # not found"     # the dated file names group together
    assert err["category"] == "upstream" and err["count"] == 5
    assert err["new"] == 5 and err["recurring"] == 0


def test_a_failure_fixed_by_a_retry_is_not_a_failure_now():
    rows = [row(D, "a1", "FAILED", "U1", event="E1", err="boom",
                at=datetime(2026, 10, 2, 20, tzinfo=timezone.utc)),
            row(D, "a2", "SUCCEEDED", "U1", event="E1", at=datetime(2026, 10, 2, 21, tzinfo=timezone.utc))]
    rep = analyse(rows)
    assert rep["counts"]["failed"] == 0 and rep["counts"]["recovered"] == 1
    assert rep["retries"][0]["attempts"] == 2 and rep["retries"][0]["final_state"] == "done"


def test_recurring_failures_count_dates_in_a_row_and_name_the_last_success():
    rows = [row(P2, "x0", "SUCCEEDED", "U1"), row(P, "x1", "FAILED", "U1", err="schema mismatch"),
            row(D, "x2", "FAILED", "U1", err="schema mismatch")]
    rep = analyse(rows)
    (f,) = rep["failures"]
    assert f["streak"] == 2 and f["last_success"] == "2026-09-30"
    assert rep["errors"][0]["category"] == "unit" and rep["errors"][0]["recurring"] == 1


def test_stuck_missing_and_collapsed_output_are_found():
    rows = [row(P, "m1", "SUCCEEDED", "U8"),                       # ran yesterday, nothing today
            row(P, "v1", "SUCCEEDED", "U9", members=1200),
            row(D, "v2", "SUCCEEDED", "U9", members=0),            # succeeded with nothing
            row(D, "s1", "RUNNING", "U7", at=NOW - timedelta(hours=5)),
            row(D, "s2", "RUNNING", "U6", at=NOW - timedelta(minutes=10))]   # still fresh
    rep = analyse(rows)
    assert [s["run_id"] for s in rep["stuck"]] == ["s1"] and rep["stuck"][0]["minutes_since_update"] == 300
    assert rep["missing"] == [{"dims": {"system": "SYS-A", "process": "REPORT-1", "unit": "U8"},
                               "previous_status": "SUCCEEDED"}]
    assert rep["volume"][0]["members"] == 0 and rep["volume"][0]["previous_members"] == 1200


def test_a_date_with_no_rows_is_empty_not_an_error():
    rep = analyse([row(P, "p1", "SUCCEEDED", "U1")], business_date=D)
    assert rep["empty"] is True and rep["counts"]["failed"] == 0


def test_without_grouping_columns_it_says_what_it_cannot_work_out():
    rows = [{"date": D, "run_id": "r1", "status": "FAILED"}]
    rep = analyse(rows, roles=["date", "run_id", "status"])
    assert rep["counts"]["failed"] == 1
    assert any("recurrence" in n for n in rep["notes"]) and any("retry" in n for n in rep["notes"])


# ----- the L1 layer ------------------------------------------------------------------

LABELS = {"system": "System", "process": "Report", "unit": "Book"}


def _report(rows):
    rep = analyse(rows)
    rep["date_label"] = "COB"
    return rep


def test_each_category_gets_one_l1_action_and_an_owner():
    up = [row(D, f"d{u}", "FAILED", f"U{u}", err="file not found") for u in range(1, 4)]
    incs = st.incidents(_report(up), labels=LABELS, runbook=[],
                        owners={"system:SYS-A": "Feed team", "default": "Platform L2"})
    (inc,) = incs
    assert inc["action"] == "wait" and inc["owner"] == "Feed team" and inc["priority"] == "high"
    assert "Do not re-run" in " ".join(inc["steps"])
    assert inc["note"].startswith("[Support triage] COB 2026-10-02:")
    assert "Suggested owner: Feed team" in inc["note"] and "Error: file not found" in inc["note"]
    assert "Error:" not in inc["note_without_error"]

    proc = [row(D, f"d{u}", "FAILED", f"U{u}", system=f"SYS-{u}", err="null pointer") for u in range(1, 4)]
    assert st.incidents(_report(proc), labels=LABELS, runbook=[], owners={})[0]["action"] == "escalate"

    single = [row(D, "d1", "FAILED", "U1", err="timeout")]
    (one,) = st.incidents(_report(single), labels=LABELS, runbook=[], owners={})
    assert one["action"] == "check" and one["owner"] == "L2 support"   # one unit: check its data first


def test_a_runbook_match_wins_and_names_the_known_issue():
    runbook = [{"match": ["quota", "exceeded"], "title": "Cloud quota exceeded", "action": "retrigger",
                "steps": ["Re-trigger after 15 minutes."], "escalate_to": "Platform L2",
                "category": None, "when": {}}]
    rows = [row(D, "d1", "FAILED", "U1", err="Quota exceeded for workers in region")]
    (inc,) = st.incidents(_report(rows), labels=LABELS, runbook=runbook, owners={})
    assert inc["runbook"] == "Cloud quota exceeded" and inc["action"] == "retrigger"
    assert inc["owner"] == "Platform L2" and "Known issue: Cloud quota exceeded" in inc["note"]


def test_incidents_come_most_urgent_first():
    rows = [row(D, "s1", "RUNNING", "U7", at=NOW - timedelta(hours=5)),
            row(P, "v1", "SUCCEEDED", "U9", members=500), row(D, "v2", "SUCCEEDED", "U9", members=0),
            row(D, "d1", "FAILED", "U1", err="timeout")]
    kinds = [i["kind"] for i in st.incidents(_report(rows), labels=LABELS, runbook=[], owners={})]
    assert kinds[0] == "volume" and kinds.index("stuck") < kinds.index("error")


def test_the_runbook_file_is_read_and_bad_entries_skipped(tmp_path):
    good = tmp_path / "rb.json"
    good.write_text(json.dumps({"entries": [
        {"match": "not found", "title": "Feed late", "action": "wait", "steps": ["x"]},
        {"match": "", "title": "no match text"}, {"title": "no match"}, "not an object",
        {"match": "y", "title": "Unknown action", "action": "reboot"}]}))
    entries, problem = st.load_runbook(str(good))
    assert problem is None and [e["title"] for e in entries] == ["Feed late", "Unknown action"]
    assert entries[1]["action"] == "check"
    assert st.load_runbook(str(tmp_path / "missing.json"))[1]
    assert st.load_runbook("") == ([], None)


def test_the_runbook_shipped_in_the_chart_loads():
    import pathlib

    path = pathlib.Path(__file__).resolve().parent.parent / "helm" / "release-copilot" / "files" / "support_runbook.json"
    entries, problem = st.load_runbook(str(path))
    assert problem is None and len(entries) >= 3


def test_owners_parse_role_value_pairs_and_a_default():
    assert st.parse_owners("system:SYS-A=Feed team, process:R=Reports L2,default=Platform L2,junk") == {
        "system:SYS-A": "Feed team", "process:R": "Reports L2", "default": "Platform L2"}


# ----- what the model may see ----------------------------------------------------------

def test_error_text_stays_out_of_the_model_unless_allowed(monkeypatch):
    rows = [row(D, "d1", "FAILED", "U1", err="account 12345 balance mismatch")]
    rep = {"ok": True, **_report(rows)}
    rep["incidents"] = st.incidents(rep, labels=LABELS, runbook=[], owners={})
    monkeypatch.setattr(st.settings, "support_errors_to_model", False)
    seen = json.dumps(st.for_model(rep))
    assert "balance mismatch" not in seen and "error #1" in seen
    monkeypatch.setattr(st.settings, "support_errors_to_model", True)
    assert "balance mismatch" in json.dumps(st.for_model(rep))


# ----- the entry point ---------------------------------------------------------------

class _Job:
    def __init__(self, rows):
        self._rows, self.total_bytes_processed = rows, 2048

    def result(self):
        return iter(self._rows)


class _Client:
    def __init__(self, rows):
        self.rows, self.calls = rows, []

    def query(self, sql, job_config=None):
        self.calls.append((sql, job_config))
        return _Job(self.rows)


@pytest.fixture
def configured(monkeypatch):
    s = st.settings
    monkeypatch.setattr(s, "support_table", "p.ops.control_runs")
    monkeypatch.setattr(s, "support_columns",
                        "date=biz_date:COB,run_id=run,status=state,updated_at=written,unit=acct,system=feed,error=msg")
    monkeypatch.setattr(s, "support_runbook_file", "")
    monkeypatch.setattr(s, "support_owners", "default=Platform L2")
    return s


def test_triage_reads_once_for_real_and_returns_l1_incidents(monkeypatch, configured):
    raw = [{"r_date": D, "r_run_id": f"r{u}", "r_status": "FAILED",
            "r_updated_at": NOW - timedelta(hours=1), "r_unit": f"U{u}", "r_system": "SYS-A",
            "r_error": "file not found"} for u in range(1, 4)]
    client = _Client(raw)
    monkeypatch.setattr(st, "_get_client", lambda: client)
    out = st.triage("2026-10-02")
    assert out["ok"] is True and out["counts"]["failed"] == 3 and out["bytes_processed"] == 2048
    (sql, cfg), = client.calls
    assert cfg.dry_run is not True                       # a real read of the one table
    assert cfg.labels == {"release_copilot": "support_triage"}
    assert {p.name for p in cfg.query_parameters} == {"start", "end", "max_rows"}
    assert out["incidents"][0]["action"] == "wait" and out["labels"] == {"system": "feed", "unit": "acct"}


def test_triage_disabled_misconfigured_and_denied_answer_instead_of_raising(monkeypatch, configured):
    monkeypatch.setattr(st.settings, "support_table", "")
    assert st.triage()["disabled"] is True
    monkeypatch.setattr(st.settings, "support_table", "p.ops.control_runs")
    monkeypatch.setattr(st.settings, "support_columns", "date=d")
    assert "required" in st.triage()["error"]
    monkeypatch.setattr(st.settings, "support_columns", "date=d,run_id=r,status=s")
    assert "not a date" in st.triage("yesterday")["error"]

    class Denied:
        def query(self, *a, **k):
            raise RuntimeError("403 Access Denied: User does not have permission bigquery.tables.getData")
    monkeypatch.setattr(st, "_get_client", lambda: Denied())
    out = st.triage()
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
    monkeypatch.setattr(st.settings, "support_table", "")
    app_fastapi._support_caches.clear()
    res = TestClient(app_fastapi.app).get("/api/support/triage?fresh=1")
    assert res.status_code == 200 and res.json()["disabled"] is True


def test_a_few_failures_in_a_healthy_feed_are_not_an_upstream_outage():
    """Found live: 4 accounts of one feed failing one report with a schema error,
    while that feed's other 20 runs succeeded — a process problem, not the feed."""
    rows = [row(D, f"ok{u}", "SUCCEEDED", f"U{u}", system="SYS-B", process="REPORT-A") for u in range(20)]
    rows += [row(D, f"bad{u}", "FAILED", f"V{u}", system="SYS-B", process="REPORT-B",
                 err="Schema mismatch: column x") for u in range(4)]
    (err,) = analyse(rows)["errors"]
    assert err["category"] == "process" and err["cause"] == {"process": "REPORT-B"}
    rep = _report(rows)
    (inc,) = st.incidents(rep, labels=LABELS, runbook=[],
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
    # the cluster's preview defaults gate it too (values.yaml overrides config.py)
    assert 'PREVIEW_FEATURES: "monitoring,bq-cost,support-triage"' in off
    assert 'PREVIEW_GROUPS: "Check,Monitoring,Support"' in off
