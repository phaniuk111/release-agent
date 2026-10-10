"""The portal event log: the memory and BigQuery seams, latest-wins, and the
schema file staying in step with the module."""
import datetime as dt
import json
import pathlib

import pytest

from release_agent.config import settings
from release_agent.tools import events_store as es

SCHEMA_FILE = pathlib.Path(es.__file__).parents[3] / "bigquery" / "portal_events.schema.json"


def _row(**kw):
    base = {"domain": "notification", "event_type": "created", "subject": "notification:a"}
    return {**base, **kw}


def _ago(**delta):
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).isoformat()


# ----- schema ------------------------------------------------------------------------

def test_the_module_schema_mirrors_the_json_file():
    cols = json.loads(SCHEMA_FILE.read_text())
    assert [(c["name"], c["type"]) for c in cols] == es._SCHEMA


def test_every_column_is_nullable_and_described():
    for col in json.loads(SCHEMA_FILE.read_text()):
        assert col["mode"] == "NULLABLE", col["name"]
        assert col["description"].strip(), col["name"]


def test_business_date_is_a_date_and_details_is_text():
    types = dict(es._SCHEMA)
    assert types["business_date"] == "DATE"
    assert types["details"] == "STRING"


def test_terraform_wires_the_table_the_spec_way():
    tf = (SCHEMA_FILE.parent / "terraform" / "main.tf").read_text()
    assert 'variable "portal_events_table"' in tf and 'default     = "portal_events"' in tf
    assert 'clustering = ["domain", "subject", "recipient"]' in tf
    assert "portal_events.schema.json" in tf and 'output "portal_events_table_id"' in tf


# ----- memory store ------------------------------------------------------------------

def test_memory_is_the_store_when_no_dataset_is_set():
    assert not es.enabled() and es.store_name() == "memory"


def test_record_fills_the_id_and_time_and_reads_back():
    out = es.record(_row(title="Hi", pr_number="42"))
    assert out["ok"] and out["event_id"]
    (row,) = es.latest("notification")["rows"]
    assert row["event_id"] == out["event_id"] and row["title"] == "Hi"
    assert row["pr_number"] == 42, "typed as the column says"
    assert dt.datetime.fromisoformat(row["event_ts"]).tzinfo is not None


@pytest.mark.parametrize("bad, why", [
    ({"domain": "n", "event_type": "c"}, "subject"),
    ({"domain": "n", "subject": "s"}, "event_type"),
    ({"event_type": "c", "subject": "s"}, "domain"),
    (_row(nope=1), "unknown column"),
    (_row(pr_number="abc"), "pr_number"),
    (_row(established="yes"), "established"),
    (_row(business_date="not-a-date"), "business_date"),
    (_row(seconds=float("nan")), "seconds"),
])
def test_a_bad_row_is_an_error_dict_never_an_exception(bad, why):
    out = es.record(bad)
    assert out["ok"] is False and why in out["error"]
    assert es.history("notification")["rows"] == []


def test_record_many_is_all_or_nothing():
    out = es.record_many([_row(), _row(pr_number="x")])
    assert not out["ok"] and "row 1" in out["error"]
    assert es.history("notification")["rows"] == []
    ok = es.record_many([_row(subject="notification:1"), _row(subject="notification:2")])
    assert ok["ok"] and ok["recorded"] == 2 and len(ok["event_ids"]) == 2


def test_the_same_event_id_twice_is_one_row():
    es.record(_row(event_id="e1"))
    es.record(_row(event_id="e1"))
    assert len(es.history("notification")["rows"]) == 1


def test_latest_wins_per_subject_and_history_keeps_every_row():
    es.record(_row(event_ts=_ago(hours=3), title="first", status="unread"))
    es.record(_row(event_ts=_ago(hours=1), event_type="read", title="second", status="read"))
    es.record(_row(subject="notification:b", event_ts=_ago(hours=2), title="other"))
    rows = es.latest("notification")["rows"]
    assert [r["subject"] for r in rows] == ["notification:a", "notification:b"], "newest first"
    assert rows[0]["title"] == "second" and rows[0]["status"] == "read"
    assert [r["title"] for r in es.history("notification")["rows"]] == ["second", "other", "first"]


def test_a_later_append_wins_a_tie():
    stamp = _ago(hours=1)
    es.record(_row(event_ts=stamp, title="one"))
    es.record(_row(event_ts=stamp, title="two"))
    assert es.latest("notification")["rows"][0]["title"] == "two"


def test_subject_version_makes_a_separate_row_per_version():
    es.record(_row(subject="chart:svc-a", subject_version="1.0.0", title="old"))
    es.record(_row(subject="chart:svc-a", subject_version="1.1.0", title="new"))
    es.record(_row(subject="chart:svc-a", subject_version="1.1.0", title="newer"))
    titles = sorted(r["title"] for r in es.latest("notification")["rows"])
    assert titles == ["newer", "old"]
    only = es.latest("notification", subject="chart:svc-a", subject_version="1.0.0")["rows"]
    assert [r["title"] for r in only] == ["old"]


def test_since_days_leaves_out_older_rows():
    es.record(_row(subject="notification:old", event_ts=_ago(days=20)))
    es.record(_row(subject="notification:new", event_ts=_ago(days=1)))
    assert [r["subject"] for r in es.latest("notification", since_days=14)["rows"]] == ["notification:new"]
    assert len(es.latest("notification", since_days=30)["rows"]) == 2


def test_recipient_and_domain_filters():
    es.record(_row(recipient="a@example.com"))
    es.record(_row(subject="notification:b", recipient="b@example.com"))
    es.record(_row(domain="watch", event_type="finding", subject="incident:1"))
    assert [r["subject"] for r in es.latest("notification", recipient="a@example.com")["rows"]] == ["notification:a"]
    assert len(es.latest("notification")["rows"]) == 2
    assert [r["subject"] for r in es.latest("watch")["rows"]] == ["incident:1"]


def test_history_filters_and_refuses_an_unknown_one():
    es.record(_row(scope="s1", event_type="created"))
    es.record(_row(scope="s1", event_type="updated"))
    es.record(_row(subject="notification:b", scope="s2"))
    assert len(es.history("notification", scope="s1")["rows"]) == 2
    assert len(es.history("notification", scope="s1", event_type="updated")["rows"]) == 1
    out = es.history("notification", title="x")
    assert out["ok"] is False and "title" in out["error"]
    assert es.latest("")["ok"] is False


def test_details_dict_is_stored_as_json_text_and_dates_as_dates():
    es.record(_row(details={"timeline": [1, 2]}, business_date=dt.date(2026, 10, 9), count=3))
    (row,) = es.latest("notification")["rows"]
    assert json.loads(row["details"]) == {"timeline": [1, 2]}
    assert row["business_date"] == "2026-10-09" and row["count"] == 3


def test_memory_is_bounded(monkeypatch):
    monkeypatch.setattr(es, "_MEMORY_MAX_ROWS", 3)
    for i in range(5):
        es.record(_row(subject=f"notification:{i}"))
    assert len(es.history("notification")["rows"]) == 3


# ----- BigQuery seam -----------------------------------------------------------------

class FakeClient:
    def __init__(self, insert_errors=None, rows=None, raises=None):
        self.insert_errors, self.rows, self.raises = insert_errors or [], rows or [], raises
        self.inserted, self.queries = [], []

    def insert_rows_json(self, table, rows, row_ids=None):
        if self.raises:
            raise self.raises
        self.inserted.append((table, rows, row_ids))
        return self.insert_errors

    def query(self, sql, job_config=None, location=None):
        if self.raises:
            raise self.raises
        self.queries.append((sql, job_config))
        client = self

        class Job:
            def result(self):
                return client.rows

        return Job()


@pytest.fixture
def bq(monkeypatch):
    monkeypatch.setattr(settings, "portal_events_dataset", "ds", raising=False)
    monkeypatch.setattr(settings, "portal_events_project", "proj", raising=False)
    fake = FakeClient()
    monkeypatch.setattr(es, "_get_client", lambda: fake)
    return fake


def test_bigquery_is_the_store_once_a_dataset_is_set(bq):
    assert es.enabled() and es.store_name() == "bigquery"
    assert es._table_id() == "proj.ds.portal_events"


def test_the_project_defaults_to_the_release_logs(monkeypatch):
    monkeypatch.setattr(settings, "portal_events_dataset", "ds", raising=False)
    monkeypatch.setattr(settings, "portal_events_project", "", raising=False)
    monkeypatch.setattr(settings, "bq_project", "release-proj", raising=False)
    assert es._table_id() == "release-proj.ds.portal_events"


def test_insert_goes_to_the_table_with_the_event_id_as_insert_id(bq):
    out = es.record_many([_row(subject="notification:1"), _row(subject="notification:2")])
    (table, rows, ids), = bq.inserted
    assert out["ok"] and table == "proj.ds.portal_events"
    assert ids == [r["event_id"] for r in rows] == out["event_ids"]
    assert es.history("notification")["ok"] and not es._memory, "nothing leaks into memory"


def test_insert_errors_come_back_as_an_error_dict(bq):
    bq.insert_errors = [{"index": 0, "errors": [{"reason": "invalid"}]}]
    out = es.record(_row())
    assert out["ok"] is False and "insert failed" in out["error"]


def test_a_down_bigquery_never_raises(bq):
    bq.raises = RuntimeError("503 backend error")
    assert es.record(_row())["ok"] is False
    assert es.latest("notification") == {"ok": False, "error": "RuntimeError: 503 backend error"}
    assert es.history("notification")["ok"] is False


def test_a_missing_table_says_how_to_create_it(bq):
    bq.raises = RuntimeError("404 Not found: Table proj:ds.portal_events")
    assert "portal_events.schema.json" in es.record(_row())["hint"]


def test_the_query_binds_every_value_and_interpolates_only_the_table(bq):
    es.latest("notification", since_days=7, recipient="a@example.com'; DROP TABLE x; --")
    (sql, cfg), = bq.queries
    assert "`proj.ds.portal_events`" in sql and "DROP" not in sql and "a@example.com" not in sql
    values = {p.name: p.value for p in cfg.query_parameters}
    assert values["domain"] == "notification" and values["days"] == 7
    assert values["recipient"].startswith("a@example.com")
    assert "QUALIFY ROW_NUMBER()" in sql and "PARTITION BY subject, IFNULL(subject_version, '')" in sql


def test_history_has_no_window_function_and_binds_its_filters(bq):
    es.history("notification", scope="s1")
    (sql, cfg), = bq.queries
    assert "QUALIFY" not in sql
    assert {p.name: p.value for p in cfg.query_parameters}["scope"] == "s1"


def test_bigquery_rows_come_back_without_nulls_and_with_iso_times(bq):
    bq.rows = [{"event_id": "e", "event_ts": dt.datetime(2026, 10, 9, 8, 0, tzinfo=dt.timezone.utc),
                "domain": "notification", "subject": "notification:a", "title": None,
                "business_date": dt.date(2026, 10, 9)}]
    (row,) = es.latest("notification")["rows"]
    assert row == {"event_id": "e", "event_ts": "2026-10-09T08:00:00+00:00", "domain": "notification",
                   "subject": "notification:a", "business_date": "2026-10-09"}
