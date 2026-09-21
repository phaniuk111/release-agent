"""BigQuery cost report: the two HTTP routes (JSON and the .xlsx download) and
the seven ADK chat-tool wrappers, all gated by the "bq-cost" preview feature —
mirrors test_features.py / test_monitoring.py for the Monitoring pill. Every
test mocks release_agent.tools.bq_cost at the function level: no BigQuery."""
import io
import pathlib
import time
import zipfile

import pytest
from fastapi.testclient import TestClient

from adk_release_agent import tools as T
from release_agent import app_fastapi as APP
from release_agent import features, identity
from release_agent.tools import bq_cost

TESTER = identity.Caller(email="tester@example.com")
OTHER = identity.Caller(email="someone@example.com")

FAKE_SCAN = {"ok": True, "shapes": [], "storage": [], "writes": [], "report_cost_bytes": 12345}
FAKE_FULL_SCAN = {
    "ok": True, "project": "team-bq", "scanned_at": "2026-09-19T20:40:58+00:00", "report_cost_bytes": 12345,
    "shapes": [{"qhash": "abc123", "runs": 3, "gb_billed": 1.5, "sql_preview": "SELECT 1"}],
    "storage": [], "writes": [], "history": None,
}
FAKE_DETAIL = {"ok": True, "qhash": "abc123", "sql": "SELECT 1"}
FAKE_LAYOUT = {"ok": True, "table": "p.d.t", "partitioning": None}
FAKE_PRUNE = {"ok": True, "measured": False, "estimate": {}}
FAKE_DRY_RUN = {"ok": True, "bytes": 0, "schema": [], "referenced_tables": []}
FAKE_VERIFY = {"ok": True, "verdict": "accept"}
FAKE_FINDINGS = {"ok": True, "runs": [], "adoption": []}

BQ_TOOL_NAMES = [
    "bq_cost_scan", "bq_query_detail", "bq_table_layout", "bq_prune_estimate",
    "bq_dry_run", "bq_verify_rewrite", "bq_findings",
]


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_users", "tester@example.com", raising=False)
    monkeypatch.setattr(features.settings, "preview_features", "bq-cost", raising=False)


@pytest.fixture
def client():
    return TestClient(APP.app)


def _caller_is(monkeypatch, caller):
    """Every request in the test resolves to this signed-in caller."""
    monkeypatch.setattr(APP, "_caller", lambda request: caller)


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(APP, "_bq_cost_cache", {"at": 0.0, "value": None})


# --- HTTP: 403 for a non-preview caller, on both routes -----------------------

def test_report_refuses_a_non_preview_caller(monkeypatch, client):
    _caller_is(monkeypatch, OTHER)
    res = client.get("/api/bq-cost/report")
    assert res.status_code == 403
    assert res.json() == {"ok": False, "error": features.refusal("bq-cost")}


def test_xlsx_refuses_a_non_preview_caller(monkeypatch, client):
    _caller_is(monkeypatch, OTHER)
    res = client.get("/api/bq-cost/report.xlsx")
    assert res.status_code == 403
    assert res.json() == {"ok": False, "error": features.refusal("bq-cost")}


# --- HTTP: the report for a preview caller, as JSON and as a workbook ---------

def test_report_returns_the_scan_for_a_preview_caller(monkeypatch, client):
    _caller_is(monkeypatch, TESTER)
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(bq_cost, "scan", lambda *a, **k: FAKE_SCAN)
    res = client.get("/api/bq-cost/report")
    assert res.status_code == 200
    assert res.json() == FAKE_SCAN


def test_xlsx_downloads_the_same_cached_scan_as_a_workbook(monkeypatch, client):
    _caller_is(monkeypatch, TESTER)
    _fresh_cache(monkeypatch)
    calls = []
    monkeypatch.setattr(bq_cost, "scan", lambda *a, **k: calls.append(1) or FAKE_FULL_SCAN)

    client.get("/api/bq-cost/report")
    res = client.get("/api/bq-cost/report.xlsx")

    assert res.status_code == 200
    assert res.headers["content-type"].startswith("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    assert res.headers["content-disposition"] == 'attachment; filename="bq-cost-report-team-bq-2026-09-19.xlsx"'
    assert len(calls) == 1, "the workbook is built from the cached JSON scan, not a second scan"
    with zipfile.ZipFile(io.BytesIO(res.content)) as zf:
        assert "abc123" in zf.read("xl/worksheets/sheet2.xml").decode()


def test_xlsx_says_why_when_there_is_nothing_to_download(monkeypatch, client):
    _caller_is(monkeypatch, TESTER)
    _fresh_cache(monkeypatch)
    failed = {"ok": False, "error": "403 missing bigquery.jobs.listAll", "hint": "grant resourceViewer"}
    monkeypatch.setattr(bq_cost, "scan", lambda *a, **k: failed)
    res = client.get("/api/bq-cost/report.xlsx")
    assert res.status_code == 503
    assert res.json() == failed


# --- the cache returns the same object twice; fresh=1 recomputes -------------

def test_the_report_endpoint_caches_but_fresh_bypasses(monkeypatch, client):
    _caller_is(monkeypatch, TESTER)
    _fresh_cache(monkeypatch)
    calls = []
    monkeypatch.setattr(bq_cost, "scan", lambda *a, **k: calls.append(1) or dict(FAKE_SCAN))

    first = client.get("/api/bq-cost/report").json()
    second = client.get("/api/bq-cost/report").json()
    client.get("/api/bq-cost/report", params={"fresh": 1})

    assert first == second == FAKE_SCAN
    assert len(calls) == 2, "the second call must be served from cache, the fresh=1 call must not"


# --- the seven ADK chat-tool wrappers: same refusal when gated ---------------

def test_the_seven_wrappers_refuse_with_the_same_refusal_when_gated():
    refusal = features.refusal("bq-cost")
    with identity.activate(OTHER):
        assert T.bq_cost_scan()["error"] == refusal
        assert T.bq_query_detail("abc123")["error"] == refusal
        assert T.bq_table_layout("p.d.t")["error"] == refusal
        assert T.bq_prune_estimate("p.d.t", "col")["error"] == refusal
        assert T.bq_dry_run("select 1")["error"] == refusal
        assert T.bq_verify_rewrite("select *", "select a")["error"] == refusal
        assert T.bq_findings()["error"] == refusal


def test_the_seven_wrappers_pass_through_when_allowed(monkeypatch):
    monkeypatch.setattr(bq_cost, "scan", lambda *a, **k: FAKE_SCAN)
    monkeypatch.setattr(bq_cost, "query_detail", lambda qhash: FAKE_DETAIL)
    monkeypatch.setattr(bq_cost, "table_layout", lambda table: FAKE_LAYOUT)
    monkeypatch.setattr(bq_cost, "prune_estimate", lambda table, column: FAKE_PRUNE)
    monkeypatch.setattr(bq_cost, "dry_run", lambda sql: FAKE_DRY_RUN)
    monkeypatch.setattr(bq_cost, "verify_rewrite", lambda *a, **k: FAKE_VERIFY)
    monkeypatch.setattr(bq_cost, "findings", lambda qhash=None, runs=5: FAKE_FINDINGS)

    with identity.activate(TESTER):
        assert T.bq_cost_scan() == FAKE_SCAN
        assert T.bq_query_detail("abc123") == FAKE_DETAIL
        assert T.bq_table_layout("p.d.t") == FAKE_LAYOUT
        assert T.bq_prune_estimate("p.d.t", "col") == FAKE_PRUNE
        assert T.bq_dry_run("select 1") == FAKE_DRY_RUN
        assert T.bq_verify_rewrite("select *", "select a") == FAKE_VERIFY
        assert T.bq_findings() == FAKE_FINDINGS


def test_bq_cost_scan_wrapper_maps_zero_to_the_configured_default(monkeypatch):
    seen = []
    monkeypatch.setattr(bq_cost, "scan", lambda days, top: seen.append((days, top)) or FAKE_SCAN)
    with identity.activate(TESTER):
        T.bq_cost_scan()
        T.bq_cost_scan(days=7, top=3)
    assert seen == [(None, None), (7, 3)]


# --- every wrapper is registered and unlocked by its skill --------------------

def test_the_seven_wrappers_are_registered_and_unlocked_by_the_skill():
    from google.adk.skills import load_skill_from_dir

    chat_names = {tool.__name__ for tool in T.ADK_CHAT_TOOLS}
    assert set(BQ_TOOL_NAMES) <= chat_names

    skill_dir = pathlib.Path(T.__file__).parent / "skills" / "bq-cost"
    skill = load_skill_from_dir(skill_dir)
    declared = set(skill.frontmatter.metadata.get("adk_additional_tools") or [])
    assert declared == set(BQ_TOOL_NAMES)


# --- the cache is single-flight: a cold-cache crowd must not each scan --------

def test_a_cold_cache_crowd_runs_the_scan_once(monkeypatch):
    """Ten people opening the pill together used to run ten BigQuery scans —
    ~42 MB of INFORMATION_SCHEMA billed each, which is precisely what the
    5-minute cache exists to prevent. _cached is single-flight: one caller
    computes, the rest take what it stored."""
    import threading

    cache: dict = {"at": 0.0, "value": None}
    runs, started = [], threading.Barrier(10)

    def slow():
        runs.append(1)
        time.sleep(0.2)          # long enough for the others to pile up
        return {"ok": True, "n": len(runs)}

    def call():
        started.wait(timeout=5)  # everyone arrives while the cache is cold
        return APP._cached(cache, 300.0, slow)

    threads = [threading.Thread(target=call) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(runs) == 1, f"the scan ran {len(runs)} times for 10 callers"
    assert cache["value"] == {"ok": True, "n": 1}


def test_a_fresh_crowd_coalesces_too_but_never_serves_a_stale_value(monkeypatch):
    """fresh=1 means 'not from before I asked' — a value computed WHILE the
    caller queued counts, so a refresh storm is one scan, not N. A value from
    BEFORE it asked does not count."""
    import threading

    cache: dict = {"at": time.time(), "value": {"ok": True, "old": True}}
    runs = []

    def slow():
        runs.append(1)
        time.sleep(0.2)
        return {"ok": True, "old": False}

    results: list = []
    threads = [threading.Thread(target=lambda: results.append(APP._cached(cache, 300.0, slow, fresh=True)))
               for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(runs) == 1, f"a 5-way refresh ran the scan {len(runs)} times"
    assert all(r == {"ok": True, "old": False} for r in results), "nobody may be served the pre-refresh value"


def test_a_failing_scan_does_not_poison_the_cache(monkeypatch):
    """fn() raising must release the lock and leave the cache untouched, so the
    next caller retries instead of inheriting a wedged cache."""
    cache: dict = {"at": 0.0, "value": None}

    def boom():
        raise RuntimeError("BigQuery said no")

    with pytest.raises(RuntimeError):
        APP._cached(cache, 300.0, boom)
    assert cache["value"] is None
    assert APP._cached(cache, 300.0, lambda: {"ok": True}) == {"ok": True}
