"""Only SUPPORT_TABLE required (tools/support/discover.py): the column map comes
from the table's schema, the status words from the table itself — and anything
configured wins. A made-up table only."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from release_agent.tools.support import config as s_config
from release_agent.tools.support import discover, queries

FIELDS = [
    ("insertedAt", "TIMESTAMP", "Timestamp when row was added."),
    ("eventTs", "TIMESTAMP", "When the triggering message was received."),
    ("triggerId", "STRING", "The business event that triggered this workflow."),
    ("runId", "STRING", "One execution of the workflow."),
    ("accountId", "STRING", "The account this workflow runs for."),
    ("runDate", "DATE", "The Close of Business date. This is the primary partitioning key."),
    ("feedName", "STRING", "The upstream system name, e.g. SYS-A."),
    ("processKind", "STRING", "The type of process being run."),
    ("state", "STRING", "The current status."),
    ("jobId", "STRING", "The Dataflow job."),
    ("errMsg", "STRING", "A human-readable error message."),
]


@pytest.fixture
def table(monkeypatch):
    s = s_config.settings
    monkeypatch.setattr(s, "support_table", "p.ops.control_runs")
    monkeypatch.setattr(s, "support_columns", "")
    schema = [SimpleNamespace(name=n, field_type=t, description=d) for n, t, d in FIELDS]
    calls = []
    monkeypatch.setattr(queries, "_get_client", lambda: SimpleNamespace(
        get_table=lambda ref: calls.append(ref) or SimpleNamespace(schema=schema, time_partitioning=None)))
    return calls


def test_only_the_table_is_needed_the_columns_come_from_its_schema(table):
    columns, labels, how = discover.mapping()
    assert how == "discovered"
    assert {k: columns[k] for k in ("date", "run_id", "status", "updated_at", "event_at", "event_id", "job_id",
                                    "error", "system", "process", "unit")} == {
        "date": "runDate", "run_id": "runId", "status": "state", "updated_at": "insertedAt", "event_at": "eventTs",
        "event_id": "triggerId", "job_id": "jobId", "error": "errMsg", "system": "feedName",
        "process": "processKind", "unit": "accountId"}
    assert labels["unit"] == "Account" and labels["process"] == "Process kind" and labels["system"] == "Feed"
    assert discover.label_for_date() == "COB"
    discover.mapping()
    assert table == ["p.ops.control_runs"]          # the schema is read once, then remembered


def test_configured_columns_and_label_win(table, monkeypatch):
    monkeypatch.setattr(s_config.settings, "support_columns", "date=runDate:Run date,run_id=runId,status=state")
    columns, labels, how = discover.mapping()
    assert how == "configured" and columns == {"date": "runDate", "run_id": "runId", "status": "state"}
    assert table == [] and discover.label_for_date() == s_config.settings.support_date_label


def test_a_table_without_the_required_columns_says_which(monkeypatch):
    monkeypatch.setattr(s_config.settings, "support_table", "p.ops.other")
    monkeypatch.setattr(s_config.settings, "support_columns", "")
    schema = [SimpleNamespace(name="note", field_type="STRING", description="")]
    monkeypatch.setattr(queries, "_get_client", lambda: SimpleNamespace(get_table=lambda ref: SimpleNamespace(schema=schema)))
    with pytest.raises(s_config.ConfigError, match="date, run_id, status"):
        discover.mapping()


def test_an_unreadable_schema_is_a_config_error_naming_the_cause(monkeypatch):
    monkeypatch.setattr(s_config.settings, "support_table", "p.ops.locked")
    monkeypatch.setattr(s_config.settings, "support_columns", "")

    def denied(ref):
        raise RuntimeError("403 Access Denied")
    monkeypatch.setattr(queries, "_get_client", lambda: SimpleNamespace(get_table=denied))
    with pytest.raises(s_config.ConfigError, match="403 Access Denied"):
        discover.mapping()


@pytest.mark.parametrize("word, kind", [
    ("FAILED", "failed"), ("Failure", "failed"), ("ERRORED", "failed"), ("ABORTED", "failed"),
    ("TIMED_OUT", None), ("TIMEOUT", "failed"), ("CANCELLED", "failed"), ("REJECTED", "failed"),
    ("SUCCEEDED", "done"), ("success", "done"), ("COMPLETED", "done"), ("DONE", "done"), ("OK", "done"),
    ("RUNNING", None), ("STARTED", None), ("QUEUED", None), ("", None),
])
def test_status_words_are_sorted_by_code(word, kind):
    assert discover.classify_status(word) == kind


def test_the_table_s_own_status_words_join_the_defaults_unless_configured(monkeypatch):
    params = {"failed": ["FAILED", "ERROR"], "done": ["SUCCEEDED"]}
    monkeypatch.setattr(queries, "_run", lambda *a: [{"s": "FAILED"}, {"s": "ABORTED"}, {"s": "COMPLETED"},
                                                     {"s": "RUNNING"}, {"s": "STARTED"}])
    got = queries._statuses("t", "sql", params, None, {})
    assert got == {"failed": ["FAILED", "ERROR", "ABORTED"], "done": ["SUCCEEDED", "COMPLETED"],
                   "running": ["RUNNING", "STARTED"], "how": "discovered"}

    monkeypatch.setattr(discover, "configured", lambda *names: "support_failed_statuses" in names)
    assert queries._statuses("t", "sql", params, None, {})["how"] == "configured"


def test_a_status_read_that_fails_keeps_the_defaults_and_says_so(monkeypatch):
    def boom(*a):
        raise RuntimeError("quota")
    monkeypatch.setattr(queries, "_run", boom)
    errors: dict = {}
    got = queries._statuses("t", "sql", {"failed": ["FAILED"], "done": ["DONE"]}, None, errors)
    assert got["how"] == "defaults" and got["failed"] == ["FAILED"] and "quota" in errors["statuses"]


def test_the_config_check_says_the_columns_were_discovered(table):
    out = queries.check_config()
    assert out["ok"] is True and out["mapping"] == "discovered" and out["roles"]["run_id"] == "runId"
    assert any("matched from the table's schema" in n for n in out["notes"])
    assert any(n.startswith("check these groupings") for n in out["notes"])


def test_a_failed_schema_read_is_not_remembered(monkeypatch):
    """Found live: the demo table expired, was reloaded, and the triage kept
    saying "not found" — the failure had been cached for an hour."""
    monkeypatch.setattr(s_config.settings, "support_table", "p.ops.flaky")
    monkeypatch.setattr(s_config.settings, "support_columns", "")
    schema = [SimpleNamespace(name=n, field_type=t, description=d) for n, t, d in FIELDS]
    answers = [RuntimeError("404 Not found: Table p:ops.flaky"), SimpleNamespace(schema=schema)]

    def get_table(ref):
        got = answers.pop(0)
        if isinstance(got, Exception):
            raise got
        return got
    monkeypatch.setattr(queries, "_get_client", lambda: SimpleNamespace(get_table=get_table))
    with pytest.raises(s_config.ConfigError, match="404"):
        discover.mapping()
    assert discover.mapping()[2] == "discovered"     # read again, now it is there
