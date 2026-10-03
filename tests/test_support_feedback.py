"""Support feedback memory (tools/support_feedback.py) and its three routes.

No BigQuery: a fake client records what would be sent. The "latest verdict per
person wins" rule is decided by the SQL, so here the SQL's shape is pinned and
the aggregation of what BigQuery returns is checked; the behaviour itself was
run against a real table (see the change's live check)."""
import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from release_agent import app_fastapi as APP
from release_agent import features, identity
from release_agent.config import settings
from release_agent.tools import support_feedback as fb

TESTER = identity.Caller(email="tester@example.com")
OTHER = identity.Caller(email="someone@example.com")
ROOT = pathlib.Path(__file__).resolve().parents[1]


class FakeJob:
    def __init__(self, rows):
        self._rows = rows

    def result(self):
        return self._rows


class FakeClient:
    def __init__(self, rows=None, insert_errors=None, raises=None):
        self.rows = rows or []
        self.insert_errors = insert_errors or []
        self.raises = raises
        self.inserts = []
        self.queries = []

    def insert_rows_json(self, table, rows, row_ids=None):
        if self.raises:
            raise self.raises
        self.inserts.append((table, rows, row_ids))
        return self.insert_errors

    def query(self, sql, job_config=None, location=None):
        if self.raises:
            raise self.raises
        self.queries.append((sql, job_config, location))
        return FakeJob(self.rows)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "support_feedback_dataset", "support_demo")
    monkeypatch.setattr(settings, "support_feedback_table", "support_findings")
    monkeypatch.setattr(settings, "support_project", "team-proj")
    monkeypatch.setattr(settings, "bq_location", "US")
    client = FakeClient()
    monkeypatch.setattr(fb, "_get_client", lambda: client)
    return client


def _record(**over):
    args = dict(business_date="2026-10-02", incident_id="e-0000aaaa", title="Feed A late", verdict="right")
    args.update(over)
    return fb.record(**args)


def _params(config):
    return {p.name: p.value for p in config.query_parameters}


# --- schema file ------------------------------------------------------------------

def test_schema_file_has_the_columns_the_row_carries(on):
    cols = {c["name"]: c for c in json.loads((ROOT / "bigquery/support_findings.schema.json").read_text())}
    assert cols["created_at"]["type"] == "TIMESTAMP" and cols["business_date"]["type"] == "DATE"
    assert cols["model_calls"]["type"] == "INT64" and cols["seconds"]["type"] == "FLOAT64"
    assert all(c["mode"] == "NULLABLE" for c in cols.values())   # additive-only evolution
    _record(actual_cause="x", model_calls=2, seconds=1.5, actor="a@example.com")
    row = on.inserts[0][1][0]
    assert set(row) == set(cols)


def test_terraform_module_consumes_the_schema_file():
    tf = (ROOT / "bigquery/terraform/main.tf").read_text()
    assert "support_findings.schema.json" in tf and "support_findings_table" in tf


# --- disabled ---------------------------------------------------------------------

def test_disabled_when_the_dataset_is_empty(monkeypatch):
    monkeypatch.setattr(settings, "support_feedback_dataset", "")
    monkeypatch.setattr(fb, "_get_client", lambda: pytest.fail("no client when disabled"))
    for res in (_record(), fb.stats(), fb.for_incident("2026-10-02", "e-1")):
        assert res["ok"] is False and res["disabled"] is True and res["error"]


def test_disabled_without_a_project(monkeypatch):
    monkeypatch.setattr(settings, "support_feedback_dataset", "support_demo")
    monkeypatch.setattr(settings, "support_project", "")
    monkeypatch.setattr(settings, "bq_project", "")
    monkeypatch.setattr(settings, "gcp_project", "")
    assert _record()["disabled"] is True


# --- record: validation -----------------------------------------------------------

@pytest.mark.parametrize("verdict", ["", "RIGHTISH", "maybe", "wrong; DROP"])
def test_unknown_verdicts_are_refused(on, verdict):
    res = _record(verdict=verdict)
    assert res["ok"] is False and "verdict" in res["error"]
    assert on.inserts == []


def test_verdict_is_case_and_space_tolerant(on):
    assert _record(verdict=" Direction ")["ok"] is True
    assert on.inserts[0][1][0]["verdict"] == "direction"


@pytest.mark.parametrize("bad", ["", "yesterday", "2026-13-40"])
def test_a_bad_date_is_refused(on, bad):
    res = _record(business_date=bad)
    assert res["ok"] is False and "date" in res["error"] and on.inserts == []


def test_an_incident_id_is_required(on):
    assert _record(incident_id="  ")["ok"] is False and on.inserts == []


# --- record: the INSERT -----------------------------------------------------------

def test_record_appends_one_row_to_the_configured_table(on):
    res = _record(verdict="wrong", actual_cause="  The feed's schema changed  ", category="upstream",
                  action="escalate", model="m-1", model_calls=3, seconds=12.5, actor="tester@example.com")
    assert res == {"ok": True, "recorded": 1}
    table, rows, row_ids = on.inserts[0]
    assert table == "team-proj.support_demo.support_findings"
    row = rows[0]
    assert row["actual_cause"] == "The feed's schema changed"          # the person's words, trimmed only
    assert (row["verdict"], row["category"], row["action"], row["model"]) == ("wrong", "upstream", "escalate", "m-1")
    assert (row["model_calls"], row["seconds"], row["actor"]) == (3, 12.5, "tester@example.com")
    assert row["business_date"] == "2026-10-02" and row["created_at"].endswith("+00:00")
    assert len(row_ids) == 1 and row_ids[0]
    assert on.queries == []                                             # never a DML statement


def test_free_text_is_capped(on):
    _record(actual_cause="c" * 900, title="t" * 900)
    row = on.inserts[0][1][0]
    assert len(row["actual_cause"]) == fb.MAX_CAUSE and len(row["title"]) == fb.MAX_TITLE


def test_bad_timings_are_dropped_not_fatal(on):
    _record(model_calls="many", seconds=float("nan"))
    row = on.inserts[0][1][0]
    assert row["model_calls"] is None and row["seconds"] is None
    _record(model_calls=-1, seconds=-3)
    assert on.inserts[1][1][0]["model_calls"] is None and on.inserts[1][1][0]["seconds"] is None


def test_actor_defaults_to_empty(on):
    _record()
    assert on.inserts[0][1][0]["actor"] == ""


def test_row_errors_degrade_to_an_error_dict(on):
    on.insert_errors = [{"index": 0, "errors": [{"reason": "invalid"}]}]
    res = _record()
    assert res["ok"] is False and "insert failed" in res["error"]


def test_a_403_names_the_role_on_that_dataset_and_job_user(monkeypatch, on):
    on.raises = RuntimeError("403 Access Denied: Table team-proj:support_demo.support_findings: Permission")
    res = _record()
    assert res["ok"] is False
    assert "roles/bigquery.dataEditor" in res["hint"] and "team-proj.support_demo" in res["hint"]
    assert "roles/bigquery.jobUser" in res["hint"]


def test_a_missing_table_points_at_the_schema_file(on):
    on.raises = RuntimeError("404 Not found: Table team-proj:support_demo.support_findings")
    assert "support_findings.schema.json" in _record()["hint"]


def test_other_failures_never_raise(on):
    on.raises = ConnectionError("down")
    res = _record()
    assert res["ok"] is False and "ConnectionError" in res["error"] and "hint" not in res


# --- stats ------------------------------------------------------------------------

STATS_ROW = {
    "total": 4, "right_n": 2, "direction_n": 1, "wrong_n": 1,
    "median_seconds": 9.5, "median_model_calls": 3,
    "by_category": [{"name": "upstream", "total": 3, "right_n": 2, "direction_n": 1, "wrong_n": 0},
                    {"name": "", "total": 1, "right_n": 0, "direction_n": 0, "wrong_n": 1}],
    "by_model": [{"name": "m-1", "total": 4, "right_n": 2, "direction_n": 1, "wrong_n": 1}],
    "recent_wrong": [{"business_date": "2026-10-01", "incident_id": "e-1", "title": "Feed B",
                      "actual_cause": "the vendor re-sent an old file", "category": "", "created_at": None}],
}


def test_stats_aggregates_what_bigquery_returns(on):
    on.rows = [STATS_ROW]
    res = fb.stats(30)
    assert res["ok"] and res["days"] == 30 and res["total"] == 4
    assert (res["right"], res["direction"], res["wrong"]) == (2, 1, 1)
    assert res["accuracy"] == 0.5 and res["right_or_direction"] == 0.75
    assert res["median_seconds"] == 9.5 and res["median_model_calls"] == 3
    assert res["by_category"][0] == {"name": "upstream", "total": 3, "right": 2, "direction": 1, "wrong": 0,
                                     "accuracy": 0.6667, "right_or_direction": 1.0}
    assert res["by_model"][0]["name"] == "m-1"
    assert res["recent_wrong"][0]["actual_cause"] == "the vendor re-sent an old file"


def test_stats_with_nothing_rated_has_no_ratio(on):
    on.rows = [{"total": 0, "right_n": 0, "direction_n": 0, "wrong_n": 0, "median_seconds": None,
                "median_model_calls": None, "by_category": [], "by_model": [], "recent_wrong": []}]
    res = fb.stats()
    assert res["ok"] and res["total"] == 0 and res["accuracy"] is None and res["right_or_direction"] is None


def test_stats_sql_is_one_parameterised_select_where_each_persons_latest_wins(on):
    on.rows = [STATS_ROW]
    fb.stats(7)
    assert len(on.queries) == 1
    sql, cfg, location = on.queries[0]
    flat = " ".join(sql.split())
    assert flat.lstrip().startswith("WITH latest AS (SELECT * FROM `team-proj.support_demo.support_findings`")
    assert ("QUALIFY ROW_NUMBER() OVER (PARTITION BY business_date, incident_id, actor "
            "ORDER BY created_at DESC) = 1") in flat
    assert "INSERT" not in flat.upper() and "UPDATE" not in flat.upper() and "DELETE" not in flat.upper()
    assert _params(cfg) == {"days": 7}
    assert location == "US" and cfg.labels == {"release_copilot": "support_feedback"}
    assert cfg.maximum_bytes_billed


@pytest.mark.parametrize("asked,used", [(0, 1), (-5, 1), (9999, 365), ("x", 30)])
def test_stats_days_are_clamped(on, asked, used):
    on.rows = [STATS_ROW]
    assert fb.stats(asked)["days"] == used
    assert _params(on.queries[0][1])["days"] == used


def test_stats_failure_is_an_error_dict(on):
    on.raises = RuntimeError("403 Access Denied")
    res = fb.stats()
    assert res["ok"] is False and "dataEditor" in res["hint"]


# --- for_incident -----------------------------------------------------------------

def test_for_incident_counts_each_persons_answer(on):
    on.rows = [{"verdict": "right", "actual_cause": "", "created_at": None},
               {"verdict": "right", "actual_cause": "", "created_at": None},
               {"verdict": "wrong", "actual_cause": "a re-sent file", "created_at": None}]
    res = fb.for_incident("2026-10-02", "e-0000aaaa")
    assert (res["total"], res["right"], res["direction"], res["wrong"]) == (3, 2, 0, 1)
    assert res["answers"][2]["actual_cause"] == "a re-sent file"
    assert all("actor" not in a for a in res["answers"])               # counts, not names
    sql, cfg, _ = on.queries[0]
    assert "business_date = @business_date AND incident_id = @incident_id" in " ".join(sql.split())
    assert "PARTITION BY business_date, incident_id, actor" in sql
    params = _params(cfg)
    assert str(params["business_date"]) == "2026-10-02" and params["incident_id"] == "e-0000aaaa"


def test_for_incident_validates(on):
    assert fb.for_incident("nope", "e-1")["ok"] is False
    assert fb.for_incident("2026-10-02", "")["ok"] is False
    assert on.queries == []


# --- routes -----------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_users", "tester@example.com", raising=False)
    monkeypatch.setattr(features.settings, "preview_features", "support-triage", raising=False)
    return TestClient(APP.app)


def _caller_is(monkeypatch, caller):
    monkeypatch.setattr(APP, "_caller", lambda request: caller)


BODY = {"business_date": "2026-10-02", "incident_id": "e-0000aaaa", "title": "Feed A late",
        "verdict": "wrong", "actual_cause": "vendor re-sent", "category": "upstream", "action": "wait",
        "model": "m-1", "model_calls": 2, "seconds": 7.5}


def test_every_route_refuses_a_non_preview_caller(monkeypatch, client):
    _caller_is(monkeypatch, OTHER)
    monkeypatch.setattr(fb, "record", lambda **kw: pytest.fail("refused callers write nothing"))
    monkeypatch.setattr(fb, "stats", lambda *a, **k: pytest.fail("refused"))
    monkeypatch.setattr(fb, "for_incident", lambda *a, **k: pytest.fail("refused"))
    want = {"ok": False, "error": features.refusal("support-triage")}
    for res in (client.post("/api/support/feedback", json=BODY),
                client.get("/api/support/feedback/stats"),
                client.get("/api/support/feedback?date=2026-10-02&incident_id=e-1")):
        assert res.status_code == 403 and res.json() == want


def test_post_records_against_the_verified_caller(monkeypatch, client):
    _caller_is(monkeypatch, TESTER)
    seen = {}
    monkeypatch.setattr(fb, "record", lambda **kw: seen.update(kw) or {"ok": True, "recorded": 1})
    # A typed email is a claim: an extra field naming someone else changes nothing.
    res = client.post("/api/support/feedback", json={**BODY, "actor": "boss@example.com", "requested_by": "x@y.z"})
    assert res.status_code == 200 and res.json() == {"ok": True, "recorded": 1}
    assert seen["actor"] == "tester@example.com"
    assert seen["verdict"] == "wrong" and seen["actual_cause"] == "vendor re-sent"
    assert seen["model_calls"] == 2 and seen["seconds"] == 7.5 and seen["incident_id"] == "e-0000aaaa"


def test_post_with_identity_off_is_anonymous(monkeypatch, client):
    monkeypatch.setattr(features.settings, "preview_features", "", raising=False)   # feature not gated
    _caller_is(monkeypatch, None)
    seen = {}
    monkeypatch.setattr(fb, "record", lambda **kw: seen.update(kw) or {"ok": True, "recorded": 1})
    assert client.post("/api/support/feedback", json={**BODY, "actor": "boss@example.com"}).json()["ok"]
    assert seen["actor"] == ""


def test_post_is_refused_when_identity_is_required_and_unverified(monkeypatch, client):
    monkeypatch.setattr(features.settings, "preview_features", "", raising=False)
    monkeypatch.setattr(identity.settings, "identity_header", "X-Token", raising=False)
    monkeypatch.setattr(identity.settings, "identity_required", True, raising=False)
    _caller_is(monkeypatch, None)
    monkeypatch.setattr(fb, "record", lambda **kw: pytest.fail("an unverified caller writes nothing"))
    res = client.post("/api/support/feedback", json=BODY).json()
    assert res["ok"] is False and "Sign-in" in res["error"]


def test_post_passes_the_403_hint_through(monkeypatch, client, on):
    _caller_is(monkeypatch, TESTER)
    on.raises = RuntimeError("403 Access Denied")
    res = client.post("/api/support/feedback", json=BODY).json()
    assert res["ok"] is False and "roles/bigquery.dataEditor" in res["hint"]


def test_get_routes_for_a_preview_caller(monkeypatch, client):
    _caller_is(monkeypatch, TESTER)
    monkeypatch.setattr(fb, "stats", lambda days=30: {"ok": True, "days": days})
    monkeypatch.setattr(fb, "for_incident", lambda d, i: {"ok": True, "date": d, "id": i})
    assert client.get("/api/support/feedback/stats?days=7").json() == {"ok": True, "days": 7}
    assert client.get("/api/support/feedback/stats").json() == {"ok": True, "days": 30}
    assert client.get("/api/support/feedback?date=2026-10-02&incident_id=e-9").json() == \
        {"ok": True, "date": "2026-10-02", "id": "e-9"}


def test_post_with_the_memory_off_says_disabled(monkeypatch, client):
    _caller_is(monkeypatch, TESTER)
    monkeypatch.setattr(settings, "support_feedback_dataset", "")
    res = client.post("/api/support/feedback", json=BODY).json()
    assert res["ok"] is False and res["disabled"] is True
