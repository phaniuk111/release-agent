"""BigQuery cost report (design/BQ_COST.md) — no network, a fake BQ client."""
import pytest

from release_agent.tools import bq_cost as C


# --- fakes ---------------------------------------------------------------------

class _Field:
    def __init__(self, name, field_type):
        self.name = name
        self.field_type = field_type


class _FakeJob:
    def __init__(self, rows=None, total_bytes_processed=1000, schema=None,
                 referenced_tables=None, is_dry=False):
        self._rows = rows if rows is not None else []
        self.total_bytes_processed = total_bytes_processed
        self.schema = [_Field(*f) for f in (schema or [])]
        self.referenced_tables = referenced_tables or []
        self._is_dry = is_dry

    def result(self):
        if self._is_dry:
            raise AssertionError("must not call .result() on a dry-run job")
        return list(self._rows)


class _Dataset:
    def __init__(self, dataset_id):
        self.dataset_id = dataset_id


class _FakeClient:
    """.query() answers from ANSWERS, keyed by a SUBSTRING of the SQL — first
    matching key (insertion order) wins, so keys must be specific to the one
    statement they stand for. A value is a list of row-dicts (a real run), a
    dict with rows/total_bytes_processed/schema/referenced_tables (dry run or
    a run whose byte count matters), or an Exception to raise."""

    def __init__(self, answers=None, datasets=None):
        self.answers = answers or {}
        self._datasets = datasets or []
        self.queries = []
        self.inserted = []

    def query(self, sql, job_config=None):
        self.queries.append(sql)
        is_dry = bool(getattr(job_config, "dry_run", False))
        for key, spec in self.answers.items():
            if key in sql:
                if isinstance(spec, Exception):
                    raise spec
                spec = spec if isinstance(spec, dict) else {"rows": spec}
                return _FakeJob(rows=spec.get("rows"),
                                 total_bytes_processed=spec.get("total_bytes_processed", 1000),
                                 schema=spec.get("schema"),
                                 referenced_tables=spec.get("referenced_tables"),
                                 is_dry=is_dry)
        raise AssertionError(f"no fake answer configured for SQL:\n{sql}")

    def list_datasets(self, project=None):
        return [_Dataset(d) for d in self._datasets]

    def insert_rows_json(self, table_id, rows, row_ids=None):
        self.inserted.append((table_id, rows))
        return []


def _shape_row(qhash, runs=10, user="dev@x.com", users=None, distinct_users=1, slot_ms=3_600_000,
               bytes_billed=5 * C._GIB, p50_bytes=1 * C._GIB,
               sql_preview="SELECT 1", job_id="job1", last_run=None):
    return {
        "qhash": qhash, "runs": runs, "any_user_email": user,
        "distinct_users": distinct_users, "users": users if users is not None else [user],
        "total_slot_ms": slot_ms, "total_bytes_billed": bytes_billed,
        "p50_bytes_processed": p50_bytes,
        "sql_preview": sql_preview, "sample_job_id": job_id, "last_run": last_run,
    }


def _dry(bytes_, schema, tables=None):
    return {"ok": True, "bytes": bytes_, "gb": round(bytes_ / C._GIB, 2),
            "schema": [{"name": n, "type": t} for n, t in schema], "referenced_tables": tables or []}


@pytest.fixture(autouse=True)
def _cost_settings(monkeypatch):
    monkeypatch.setattr(C.settings, "bq_cost_project", "test-project", raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_region", "region-test", raising=False)
    monkeypatch.setattr(C.settings, "bq_project", "", raising=False)
    monkeypatch.setattr(C.settings, "gcp_project", "", raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_billing", "on-demand", raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_days", 14, raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_top", 10, raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_max_queries", 40, raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_max_datasets", 25, raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_usd_per_tib", 6.25, raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_dataset", "", raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_findings_table", "bq_cost_findings", raising=False)
    yield


# --- enabled() -------------------------------------------------------------------

def test_enabled_rules(monkeypatch):
    assert C.enabled() is True
    monkeypatch.setattr(C.settings, "bq_cost_region", "", raising=False)
    assert C.enabled() is False
    monkeypatch.setattr(C.settings, "bq_cost_region", "region-test", raising=False)
    monkeypatch.setattr(C.settings, "bq_cost_project", "", raising=False)
    assert C.enabled() is False  # no bq_project/gcp_project fallback set either
    monkeypatch.setattr(C.settings, "bq_project", "fallback-project", raising=False)
    assert C.enabled() is True


def test_scan_returns_the_disabled_shape_when_region_is_empty(monkeypatch):
    monkeypatch.setattr(C.settings, "bq_cost_region", "", raising=False)
    assert C.scan() == {
        "ok": False, "disabled": True,
        "error": "BigQuery cost report is disabled (BQ_COST_REGION unset, or no project).",
    }


# --- rank_shapes / approx_usd ------------------------------------------------------

def test_rank_shapes_orders_by_slot_hours_for_reservations():
    rows = [_shape_row("a", slot_ms=1_000 * 3600 * 1000, bytes_billed=1 * C._GIB),
            _shape_row("b", slot_ms=5_000 * 3600 * 1000, bytes_billed=100 * C._GIB)]
    assert [s["qhash"] for s in C.rank_shapes(rows, "reservations", 10)] == ["b", "a"]


def test_rank_shapes_orders_by_gb_billed_for_on_demand():
    rows = [_shape_row("a", slot_ms=5_000 * 3600 * 1000, bytes_billed=1 * C._GIB),
            _shape_row("b", slot_ms=1_000 * 3600 * 1000, bytes_billed=100 * C._GIB)]
    assert [s["qhash"] for s in C.rank_shapes(rows, "on-demand", 10)] == ["b", "a"]


def test_rank_shapes_caps_to_top_n_and_shapes_the_documented_fields():
    rows = [_shape_row(str(i), bytes_billed=i * C._GIB) for i in range(5)]
    shapes = C.rank_shapes(rows, "on-demand", 2)
    assert len(shapes) == 2
    assert set(shapes[0]) == {"qhash", "runs", "who", "users", "users_count", "slot_hours",
                               "gb_billed", "approx_usd", "p50_gb", "p50_bytes", "p50_partitions",
                               "sql_preview", "sample_job", "referenced_tables", "insights", "last_run"}
    assert shapes[0]["users"] == ["dev@x.com"] and shapes[0]["users_count"] == 1
    assert shapes[0]["p50_partitions"] is None


def test_rank_shapes_filters_out_a_blank_user_email():
    """Live finding: an empty user_email must not be counted as a distinct
    user or listed alongside real emails — belt-and-braces on top of the SQL
    now using NULLIF(user_email, '')."""
    row = _shape_row("a", users=["", "dev@x.com"])
    shape = C.rank_shapes([row], "on-demand", 10)[0]
    assert shape["users"] == ["dev@x.com"]


def test_rank_shapes_emits_p50_bytes_alongside_p50_gb_for_small_queries():
    """A 21 KB scan rounds to 0.0 GB — the UI prefers the exact byte count."""
    row = _shape_row("a", p50_bytes=21_000)
    shape = C.rank_shapes([row], "on-demand", 10)[0]
    assert shape["p50_gb"] == 0.0 and shape["p50_bytes"] == 21_000


def test_approx_usd():
    assert C.approx_usd(2**40, 6.25) == 6.25
    assert C.approx_usd(0, 6.25) == 0.0
    assert C.approx_usd(None, 6.25) == 0.0


# --- hint_for ----------------------------------------------------------------------

def test_hint_for_dataset_level_permission_error_names_the_hidden_datasets():
    msg = ("User does not have the required permissions ('bigquery.routines.list', "
           "'bigquery.tables.list' permission(s) at the dataset level).")
    assert C.hint_for(msg) == C._HINT_DATASET_LEVEL
    assert "hidden anonymous" in C.hint_for(msg)


def test_hint_for_project_level_permission_error_names_metadataviewer():
    msg = ("User does not have the required permissions ('bigquery.tables.list' "
           "permission(s) at the project level) to query system entity.")
    assert C.hint_for(msg) == C._HINT_PROJECT_LEVEL


def test_hint_for_a_403_on_jobs_names_resourceviewer():
    assert "resourceViewer" in C.hint_for("403 Forbidden: cannot list jobs for other users")


def test_hint_for_a_generic_403_names_jobuser():
    assert "jobUser" in C.hint_for("403 access denied running this query")


def test_hint_for_wrong_region_when_no_error_and_zero_rows():
    assert "region-test" in C.hint_for("", rows_seen=0, users_seen=0)


def test_hint_for_only_your_own_jobs_when_a_single_user_seen():
    assert "resourceViewer" in C.hint_for("", rows_seen=50, users_seen=1)


def test_hint_for_none_when_healthy():
    assert C.hint_for("", rows_seen=50, users_seen=5) is None


def test_hint_for_region_wide_overrides_project_and_dataset_level_wording():
    """Confirmed live: BigQuery's own 'project level'/'dataset level' wording
    does not reliably say WHICH kind of denial this is — the caller (a
    region-wide-only view, e.g. the write timelines) says so instead."""
    project_level_msg = ("User does not have the required permissions ('bigquery.tables.list' "
                          "permission(s) at the project level) to query system entity.")
    assert C.hint_for(project_level_msg) == C._HINT_PROJECT_LEVEL  # unchanged for a non-region-wide caller
    assert C.hint_for(project_level_msg, region_wide=True) == C._HINT_REGION_WIDE
    assert "no per-dataset form" in C._HINT_REGION_WIDE


# --- verdict(): every row of the §5 table -------------------------------------------

def test_verdict_invalid_when_either_dry_run_failed():
    result = C.verdict({"ok": False, "error": "parse error"}, _dry(100, [("a", "INT64")]))
    assert result["verdict"] == "reject" and result["reason"] == "invalid"


def test_verdict_not_cheaper_when_bytes_after_is_not_less():
    result = C.verdict(_dry(100, [("a", "INT64")]), _dry(200, [("a", "INT64")]))
    assert result["verdict"] == "reject" and result["reason"] == "not_cheaper"
    result_equal = C.verdict(_dry(100, [("a", "INT64")]), _dry(100, [("a", "INT64")]))
    assert result_equal["reason"] == "not_cheaper"


def test_verdict_schema_changed_undeclared_is_rejected_with_columns_listed():
    before = _dry(200, [("a", "INT64"), ("b", "STRING")])
    after = _dry(100, [("a", "INT64"), ("c", "STRING")])
    result = C.verdict(before, after)
    assert result["verdict"] == "reject" and result["reason"] == "schema_changed"
    assert result["dropped_columns"] == ["b"] and result["added_columns"] == ["c"]


def test_verdict_schema_changed_declared_is_accepted_with_the_flag_set():
    before = _dry(200, [("a", "INT64"), ("b", "STRING")])
    after = _dry(100, [("a", "INT64")])
    result = C.verdict(before, after, declared_schema_change=True)
    assert result["verdict"] == "accept" and result["schema_changed"] is True
    assert result["dropped_columns"] == ["b"]


def test_verdict_new_tables_is_rejected():
    before = _dry(200, [("a", "INT64")], tables=["p.d.t1"])
    after = _dry(100, [("a", "INT64")], tables=["p.d.t1", "p.d.t2"])
    result = C.verdict(before, after)
    assert result["verdict"] == "reject" and result["reason"] == "new_tables"
    assert result["new_tables"] == ["p.d.t2"]


def test_verdict_accept_reports_the_saving_and_always_carries_the_note():
    before = _dry(200 * C._GIB, [("a", "INT64")], tables=["p.d.t1"])
    after = _dry(100 * C._GIB, [("a", "INT64")], tables=["p.d.t1"])
    result = C.verdict(before, after)
    assert result["verdict"] == "accept" and result["reason"] == "ok"
    assert result["saving_gb"] == 100.0 and result["saving_pct"] == 50.0
    assert result["schema_changed"] is False
    assert result["note"]


def test_verdict_schema_change_is_reported_even_when_bytes_are_no_better():
    """Live finding: a schema diff must be visible even when some OTHER check
    (here, not_cheaper) would otherwise have fired first — the report leads
    with the schema change, and the model revises the rewrite from these
    fields, whatever the final verdict/reason."""
    before = _dry(100, [("one", "INT64"), ("two", "INT64")])
    after = _dry(100, [("one", "INT64")])
    result = C.verdict(before, after)
    assert result["reason"] == "schema_changed"
    assert result["schema_changed"] is True and result["dropped_columns"] == ["two"]


def test_verdict_declared_schema_change_with_equal_bytes_is_not_cheaper_but_still_flags_the_diff():
    before = _dry(100, [("one", "INT64"), ("two", "INT64")])
    after = _dry(100, [("one", "INT64")])
    result = C.verdict(before, after, declared_schema_change=True)
    assert result["reason"] == "not_cheaper"
    assert result["schema_changed"] is True and result["dropped_columns"] == ["two"]


def test_verdict_rejects_a_parameterised_dry_run_even_though_it_looks_cheaper():
    before = {**_dry(1000, [("a", "INT64")]), "parameters": ["days"]}
    after = _dry(10, [("a", "INT64")])
    result = C.verdict(before, after)
    assert result["reason"] == "parameterised"
    assert "days" in result["note"] and result["parameters"] == ["days"]
    # schema/new_tables are still computed even though the verdict is about parameters
    assert result["schema_changed"] is False and result["new_tables"] == []


def test_verdict_parameters_on_the_after_side_alone_also_rejects():
    before = _dry(1000, [("a", "INT64")])
    after = {**_dry(10, [("a", "INT64")]), "parameters": ["cutoff"]}
    assert C.verdict(before, after)["reason"] == "parameterised"


def test_verify_rewrite_runs_two_dry_runs_and_verdicts_them(monkeypatch):
    client = _FakeClient(answers={
        "FROM t1": {"rows": [], "total_bytes_processed": 200 * C._GIB,
                    "schema": [("a", "INT64")], "referenced_tables": []},
        "FROM t2": {"rows": [], "total_bytes_processed": 100 * C._GIB,
                    "schema": [("a", "INT64")], "referenced_tables": []},
    })
    monkeypatch.setattr(C, "_get_client", lambda: client)
    result = C.verify_rewrite("SELECT a FROM t1", "SELECT a FROM t2")
    assert result["verdict"] == "accept" and result["saving_gb"] == 100.0
    assert result["before"]["bytes"] == 200 * C._GIB and result["after"]["bytes"] == 100 * C._GIB


def test_dry_run_never_executes_even_an_information_schema_read(monkeypatch):
    client = _FakeClient(answers={
        "JOBS_BY_PROJECT": {"rows": [{"should": "never appear"}], "total_bytes_processed": 42,
                             "schema": [("job_id", "STRING")], "referenced_tables": []},
    })
    monkeypatch.setattr(C, "_get_client", lambda: client)
    result = C.dry_run("SELECT job_id FROM `region-test`.INFORMATION_SCHEMA.JOBS_BY_PROJECT")
    assert result == {"ok": True, "bytes": 42, "gb": round(42 / C._GIB, 2),
                       "schema": [{"name": "job_id", "type": "STRING"}], "referenced_tables": [],
                       "parameters": []}


def test_dry_run_reports_a_refused_statement_as_not_ok():
    result = C.dry_run("DROP TABLE x")
    assert result["ok"] is False and "error" in result


def test_dry_run_detects_undeclared_parameters(monkeypatch):
    """VERIFIER HOLE: BigQuery prices an undeclared @parameter as if its
    predicate did not exist (0 extra bytes) instead of refusing."""
    client = _FakeClient(answers={
        "FROM t": {"rows": [], "total_bytes_processed": 0, "schema": [("a", "INT64")]},
    })
    monkeypatch.setattr(C, "_get_client", lambda: client)
    result = C.dry_run("SELECT a FROM t WHERE ts > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)")
    assert result["parameters"] == ["days"]


def test_sql_parameters_is_empty_for_a_plain_statement():
    assert C._sql_parameters("SELECT 1 FROM t WHERE x = 1") == []


# --- adoption() ----------------------------------------------------------------------

def test_adoption_new_when_no_prior_report():
    result = C.adoption([], [{"qhash": "a", "gb_billed": 10.0}])
    assert result == [{"qhash": "a", "status": "new", "times_reported": 0,
                        "cost_before_gb": None, "cost_now_gb": 10.0, "change_pct": None}]


def test_adoption_gone_when_no_longer_reported_this_run():
    prior = [{"qhash": "a", "run_ts": "t1", "cost_before_bytes": 10 * C._GIB}]
    result = C.adoption(prior, [])
    assert result[0]["status"] == "gone" and result[0]["times_reported"] == 1
    assert result[0]["cost_now_gb"] is None


def test_adoption_adopted_when_cost_fell_30_percent_or_more():
    prior = [{"qhash": "a", "run_ts": "t1", "cost_before_bytes": 100 * C._GIB}]
    result = C.adoption(prior, [{"qhash": "a", "gb_billed": 50.0}])
    assert result[0]["status"] == "adopted" and result[0]["change_pct"] == -50.0


def test_adoption_still_open_when_cost_is_unchanged_after_several_reports():
    prior = [{"qhash": "a", "run_ts": f"t{i}", "cost_before_bytes": 100 * C._GIB} for i in range(3)]
    result = C.adoption(prior, [{"qhash": "a", "gb_billed": 98.0}])
    assert result[0]["status"] == "still_open" and result[0]["times_reported"] == 3


# --- record_findings / findings ---------------------------------------------------

def test_record_findings_is_a_noop_when_dataset_is_empty():
    result = C.record_findings({"ok": True, "shapes": [{"qhash": "a", "gb_billed": 1.0}]})
    assert result["ok"] is True and "note" in result


def test_record_findings_warns_on_a_missing_table(monkeypatch):
    monkeypatch.setattr(C.settings, "bq_cost_dataset", "costs", raising=False)
    client = _FakeClient()

    def _boom(table_id, rows, row_ids=None):
        raise RuntimeError("Not found: Table costs.bq_cost_findings")

    client.insert_rows_json = _boom
    monkeypatch.setattr(C, "_get_client", lambda: client)
    report = {"ok": True, "shapes": [{"qhash": "a", "gb_billed": 1.0, "slot_hours": 0.1}],
              "storage": [], "writes": []}
    result = C.record_findings(report)
    assert result["ok"] is False and "warning" in result


def test_record_findings_records_one_row_per_shape_storage_and_write(monkeypatch):
    monkeypatch.setattr(C.settings, "bq_cost_dataset", "costs", raising=False)
    client = _FakeClient()
    monkeypatch.setattr(C, "_get_client", lambda: client)
    report = {
        "ok": True,
        "shapes": [{"qhash": "a", "gb_billed": 10.0, "slot_hours": 1.0}],
        "storage": [{"table": "p.d.t", "gb": 5.0, "kind": "no_expiration"}],
        "writes": [{"table": None, "input_gb": 1.0, "kind": "errors"}],
    }
    result = C.record_findings(report)
    assert result == {"ok": True, "recorded": 3}
    table_id, rows = client.inserted[0]
    assert table_id.endswith("costs.bq_cost_findings")
    assert {r["kind"] for r in rows} == {"query", "storage", "write"}


def test_findings_reads_only_its_own_table_and_reports_disabled_without_a_dataset():
    assert C.findings()["ok"] is False and C.findings()["disabled"] is True


# --- the write views' DIFFERENT success codes ----------------------------------------

def test_write_findings_success_code_differs_between_the_two_views():
    streaming_rows = [{"error_code": None, "total_requests": 100, "total_rows": 1000, "total_input_bytes": 1000},
                       {"error_code": "INTERNAL", "total_requests": 5, "total_rows": 5, "total_input_bytes": 5}]
    write_api_rows = [{"error_code": "OK", "total_requests": 100, "total_rows": 1000, "total_input_bytes": 1000},
                       {"error_code": "INTERNAL", "total_requests": 5, "total_rows": 5, "total_input_bytes": 5}]
    streaming = C._write_findings(streaming_rows, source="streaming", ok_code=None)
    write_api = C._write_findings(write_api_rows, source="write_api", ok_code="OK")
    assert streaming[0]["errors"] == 5 and write_api[0]["errors"] == 5

    # error_code='OK' is NOT success under streaming's rule (NULL is) — the two
    # views must never share one "ok_code".
    mislabelled_if_confused = C._write_findings(
        [{"error_code": "OK", "total_requests": 10, "total_rows": 10, "total_input_bytes": 1}],
        source="streaming", ok_code=None,
    )
    assert mislabelled_if_confused[0]["kind"] == "errors"


def test_write_findings_unbatched_needs_both_low_rows_per_request_and_high_volume():
    small = C._write_findings([{"error_code": "OK", "total_requests": 100, "total_rows": 100,
                                 "total_input_bytes": 1}], source="write_api", ok_code="OK")
    assert small == []  # below the 10000-request threshold
    unbatched = C._write_findings([{"error_code": "OK", "total_requests": 20000, "total_rows": 20000,
                                     "total_input_bytes": 1}], source="write_api", ok_code="OK")
    assert unbatched[0]["kind"] == "unbatched" and unbatched[0]["rows_per_request"] == 1.0


def test_scan_writes_denial_uses_the_region_wide_hint_not_the_generic_one(monkeypatch):
    denied = RuntimeError(
        "User does not have the required permissions ('bigquery.tables.list' "
        "permission(s) at the project level) to query system entity."
    )
    answers = _happy_answers(**{
        "STREAMING_TIMELINE_BY_PROJECT": denied,
        "WRITE_API_TIMELINE_BY_PROJECT": denied,
    })
    client = _FakeClient(answers=answers)
    monkeypatch.setattr(C, "_get_client", lambda: client)

    out = C.scan()
    assert out["writes"] == []
    assert out["writes_hint"] == C._HINT_REGION_WIDE


# --- the scan excludes its own INFORMATION_SCHEMA jobs from the ranking ---------------

def test_the_ranking_queries_exclude_the_scans_own_labelled_jobs():
    for sql in (C._shapes_sql(), C._totals_sql(), C._table_reads_sql()):
        assert "NOT EXISTS" in sql
        assert "l.key = 'release_copilot'" in sql
        assert "FROM UNNEST(labels)" in sql or "FROM UNNEST(j.labels)" in sql


# --- scan(): end to end on the fake client --------------------------------------------

def _happy_answers(**overrides):
    answers = {
        "p50_bytes_processed": [_shape_row(
            "abc123", runs=42, user="dev@x.com", users=["dev@x.com", "other@x.com", "third@x.com"],
            distinct_users=3, slot_ms=7_200_000, bytes_billed=50 * C._GIB, p50_bytes=2 * C._GIB,
            sql_preview="SELECT * FROM orders", job_id="job-abc",
        )],
        "cache_hits": [{"queries": 100, "cache_hits": 20, "total_slot_ms": 36_000_000,
                         "total_bytes_billed": 500 * C._GIB, "distinct_users": 5}],
        "job_id IN UNNEST": [{
            "job_id": "job-abc",
            "referenced_tables": [{"project_id": "test-project", "dataset_id": "d", "table_id": "orders"}],
            "stage_insights": [{"slot_contention": True, "insufficient_shuffle_quota": False}],
        }],
        "rt.project_id, rt.dataset_id, rt.table_id": [
            {"project_id": "test-project", "dataset_id": "d", "table_id": "orders",
             "reads": 7, "last_read": "2026-09-01T00:00:00+00:00"},
        ],
        "s.total_logical_bytes >= @min_bytes": [{
            "table_schema": "d", "table_name": "orders",
            "total_logical_bytes": 20 * C._GIB, "active_logical_bytes": 20 * C._GIB,
            "total_physical_bytes": 15 * C._GIB, "storage_last_modified_time": None,
            "expiration_ts": None, "ddl": "CREATE TABLE d.orders (x INT64) PARTITION BY DATE(ts)\n",
        }],
        "STREAMING_TIMELINE_BY_PROJECT": [
            {"error_code": None, "total_requests": 50, "total_rows": 5000, "total_input_bytes": 1 * C._GIB},
        ],
        "WRITE_API_TIMELINE_BY_PROJECT": [
            {"error_code": "OK", "total_requests": 20000, "total_rows": 40000, "total_input_bytes": 2 * C._GIB},
            {"error_code": "INVALID_ARGUMENT", "total_requests": 100, "total_rows": 100, "total_input_bytes": 0},
        ],
    }
    answers.update(overrides)
    return answers


def test_scan_end_to_end_produces_the_documented_shape(monkeypatch):
    client = _FakeClient(answers=_happy_answers())
    monkeypatch.setattr(C, "_get_client", lambda: client)
    out = C.scan(days=14, top=10)

    assert out["ok"] is True
    assert out["project"] == "test-project" and out["region"] == "region-test"
    assert out["queries_run"] == 7  # shapes, totals, shape_detail, table_reads, storage, streaming, write_api
    assert out["report_cost_bytes"] == 7 * 1000
    assert out["hint"] is None  # 5 distinct users, plenty of rows — healthy

    assert out["totals"] == {"queries": 100, "slot_hours": 10.0, "gb_billed": 500.0, "cache_hit_pct": 20.0}

    assert len(out["shapes"]) == 1
    shape = out["shapes"][0]
    assert shape["qhash"] == "abc123" and shape["who"] == "dev@x.com"
    assert shape["users"] == ["dev@x.com", "other@x.com", "third@x.com"] and shape["users_count"] == 3
    assert shape["p50_partitions"] is None
    assert shape["referenced_tables"] == ["test-project.d.orders"]
    assert shape["insights"] == ["slot_contention"]

    assert out["storage_source"] == "table_storage"
    assert "storage_error" not in out
    assert out["storage"] == [{
        "table": "test-project.d.orders", "gb": 20.0, "physical_gb": 15.0,
        "expiration": None, "last_modified": None, "last_read": "2026-09-01T00:00:00+00:00",
        "reads": 7, "approx_usd_month": 0.4, "kind": "no_expiration",
    }]

    assert "writes_error" not in out
    kinds = {w["kind"] for w in out["writes"]}
    assert kinds == {"unbatched", "errors"}
    errors_entry = next(w for w in out["writes"] if w["kind"] == "errors")
    assert errors_entry["errors"] == 100 and errors_entry["table"] is None


def test_scan_error_and_hint_on_a_403(monkeypatch):
    client = _FakeClient(answers={
        "p50_bytes_processed": RuntimeError(
            "User does not have the required permissions ('bigquery.tables.list' "
            "permission(s) at the project level) to query system entity."
        ),
    })
    monkeypatch.setattr(C, "_get_client", lambda: client)
    result = C.scan()
    assert result["ok"] is False
    assert result["hint"] == C._HINT_PROJECT_LEVEL


def test_scan_includes_history_from_adoption_when_memory_is_on(monkeypatch):
    monkeypatch.setattr(C.settings, "bq_cost_dataset", "costs", raising=False)
    answers = _happy_answers(**{
        "bq_cost_findings": [
            {"qhash": "abc123", "run_ts": "2026-08-01T00:00:00+00:00", "cost_before_bytes": 100 * C._GIB},
        ],
    })
    client = _FakeClient(answers=answers)
    monkeypatch.setattr(C, "_get_client", lambda: client)
    out = C.scan()
    assert out["history"]["adoption"][0] == {
        "qhash": "abc123", "status": "adopted", "times_reported": 1,
        "cost_before_gb": 100.0, "cost_now_gb": 50.0, "change_pct": -50.0,
    }
    assert out["history"]["adopted"] == 1


def test_scan_history_is_none_when_memory_is_off(monkeypatch):
    client = _FakeClient(answers=_happy_answers())
    monkeypatch.setattr(C, "_get_client", lambda: client)
    assert C.scan()["history"] is None


# --- storage: the per-dataset fallback (the common, well-tested path) -----------------

def test_storage_falls_back_per_dataset_when_the_region_view_is_denied(monkeypatch):
    denied = RuntimeError(
        "User does not have the required permissions ('bigquery.routines.list', "
        "'bigquery.tables.list' permission(s) at the dataset level) to query system entity."
    )
    answers = _happy_answers(**{"s.total_logical_bytes >= @min_bytes": denied})
    answers["__TABLES__"] = [{
        "table_id": "orders", "size_bytes": 20 * C._GIB, "row_count": 100,
        "last_modified_time": None, "expiration_ts": None, "partition_col": None,
    }]
    client = _FakeClient(answers=answers, datasets=["d", "_hidden_cache_1", "_hidden_cache_2"])
    monkeypatch.setattr(C, "_get_client", lambda: client)

    out = C.scan()

    assert out["ok"] is True
    assert out["storage_source"] == "per_dataset"
    assert "storage_error" not in out  # the fallback succeeding is the normal case
    assert not any("_hidden_cache" in q for q in client.queries), "hidden datasets must never be queried"
    kinds = {e["kind"] for e in out["storage"]}
    # unpartitioned (partition_col is None) + no expiration + read >= 5 times
    assert kinds == {"no_expiration", "large_unpartitioned"}
    assert all(e["table"] == "test-project.d.orders" for e in out["storage"])


def test_storage_fallback_skips_one_denied_dataset_and_keeps_the_rest(monkeypatch):
    denied = RuntimeError("Access Denied")

    class _SelectiveClient(_FakeClient):
        def query(self, sql, job_config=None):
            if "bad_dataset.__TABLES__" in sql:
                self.queries.append(sql)
                raise denied
            return super().query(sql, job_config=job_config)

    answers = _happy_answers(**{"s.total_logical_bytes >= @min_bytes": RuntimeError("denied")})
    answers["__TABLES__"] = [{
        "table_id": "orders", "size_bytes": 20 * C._GIB, "row_count": 100,
        "last_modified_time": None, "expiration_ts": None, "partition_col": "ts",
    }]
    client = _SelectiveClient(answers=answers, datasets=["bad_dataset", "d"])
    monkeypatch.setattr(C, "_get_client", lambda: client)

    out = C.scan()
    assert out["storage_source"] == "per_dataset"
    assert out["storage"]  # the good dataset's tables still made it through


def test_storage_reports_partial_when_the_budget_runs_out_mid_fallback(monkeypatch):
    answers = _happy_answers(**{
        "s.total_logical_bytes >= @min_bytes": RuntimeError(
            "permission(s) at the dataset level"),
        "cache_hits": [],
        "job_id IN UNNEST": [],
        "rt.project_id, rt.dataset_id, rt.table_id": [],
        "STREAMING_TIMELINE_BY_PROJECT": [],
        "WRITE_API_TIMELINE_BY_PROJECT": [],
    })
    answers["__TABLES__"] = []
    client = _FakeClient(answers=answers, datasets=["ds1", "ds2", "ds3"])
    monkeypatch.setattr(C, "_get_client", lambda: client)
    monkeypatch.setattr(C.settings, "bq_cost_max_queries", 5, raising=False)

    out = C.scan()
    assert out["storage_source"] == "per_dataset"
    assert "partial" in out.get("storage_error", "")
    assert out["queries_run"] == 5


def test_storage_list_datasets_failure_is_reported_with_a_hint(monkeypatch):
    class _NoListClient(_FakeClient):
        def list_datasets(self, project=None):
            raise RuntimeError("403 Forbidden: no datasets.list")

    answers = _happy_answers(**{"s.total_logical_bytes >= @min_bytes": RuntimeError("denied")})
    client = _NoListClient(answers=answers)
    monkeypatch.setattr(C, "_get_client", lambda: client)

    out = C.scan()
    assert out["storage_source"] is None
    assert "storage_error" in out and out["storage_hint"]


def test_scan_never_raises_even_when_a_section_has_an_unexpected_bug(monkeypatch):
    """Live finding: an AttributeError (or any other bug) inside the storage or
    writes section must degrade that section, never escape scan() as a 500."""
    client = _FakeClient(answers=_happy_answers())
    monkeypatch.setattr(C, "_get_client", lambda: client)
    monkeypatch.setattr(C, "_scan_storage", lambda *a, **k: (_ for _ in ()).throw(AttributeError("boom")))
    monkeypatch.setattr(C, "_scan_writes", lambda *a, **k: (_ for _ in ()).throw(TypeError("boom")))

    out = C.scan()

    assert out["ok"] is True
    assert out["storage"] == [] and out["storage_source"] is None
    assert "boom" in out["storage_error"]
    assert out["writes"] == [] and "boom" in out["writes_error"]


# --- C2: an unknown read count must never be reported as a verified zero -------------

def test_scan_never_reports_unread_when_the_table_reads_query_itself_fails(monkeypatch):
    """C2: scan() used to swallow a failure of the table-reads query
    (`except Exception: read_rows = []`), so _storage_entries saw reads == 0
    for every table and called EVERY one of them "unread" — turning a
    permissions gap (exactly the partial grant hint_for() itself describes)
    into "delete me" advice on every large table. Two tables here, to prove
    it is not just the one this test happens to look at."""
    reads_denied = RuntimeError(
        "User does not have the required permissions ('bigquery.jobs.listAll' "
        "permission(s) at the project level) to query system entity."
    )
    answers = _happy_answers(**{
        "rt.project_id, rt.dataset_id, rt.table_id": reads_denied,
        "s.total_logical_bytes >= @min_bytes": [
            {"table_schema": "d", "table_name": "orders", "total_logical_bytes": 20 * C._GIB,
             "active_logical_bytes": 20 * C._GIB, "total_physical_bytes": 15 * C._GIB,
             "storage_last_modified_time": None, "expiration_ts": None,
             "ddl": "CREATE TABLE d.orders (x INT64)\n"},
            {"table_schema": "d", "table_name": "events", "total_logical_bytes": 50 * C._GIB,
             "active_logical_bytes": 50 * C._GIB, "total_physical_bytes": 40 * C._GIB,
             "storage_last_modified_time": None, "expiration_ts": None,
             "ddl": "CREATE TABLE d.events (x INT64)\n"},
        ],
    })
    client = _FakeClient(answers=answers)
    monkeypatch.setattr(C, "_get_client", lambda: client)

    out = C.scan()

    assert out["ok"] is True
    assert len(out["storage"]) == 2
    assert {e["kind"] for e in out["storage"]} == {"no_expiration"}  # never "unread"
    assert all(e["reads"] is None for e in out["storage"])           # unknown, not a fabricated 0
    assert out["storage_reads_error"] == str(reads_denied)
    assert out["storage_reads_hint"] == C._HINT_PROJECT_LEVEL


def test_storage_fallback_never_reports_unread_when_the_reads_query_itself_fails(monkeypatch):
    """C2, the per-dataset fallback path: the same bug, in the other code
    path that assembles storage findings."""
    storage_denied = RuntimeError(
        "User does not have the required permissions ('bigquery.routines.list', "
        "'bigquery.tables.list' permission(s) at the dataset level) to query system entity."
    )
    reads_denied = RuntimeError("Resources exceeded during query execution.")
    answers = _happy_answers(**{
        "s.total_logical_bytes >= @min_bytes": storage_denied,
        "rt.project_id, rt.dataset_id, rt.table_id": reads_denied,
    })
    answers["__TABLES__"] = [{
        "table_id": "orders", "size_bytes": 20 * C._GIB, "row_count": 100,
        "last_modified_time": None, "expiration_ts": None, "partition_col": None,
    }]
    client = _FakeClient(answers=answers, datasets=["d"])
    monkeypatch.setattr(C, "_get_client", lambda: client)

    out = C.scan()

    assert out["storage_source"] == "per_dataset"
    assert [e["kind"] for e in out["storage"]] == ["no_expiration"]
    assert out["storage"][0]["reads"] is None
    assert out["storage_reads_error"] == str(reads_denied)


def test_reads_info_is_unknown_when_reads_are_not_known_even_for_a_table_seen_before():
    """PURE unit check on the helper itself: a table that DOES appear in
    reads_by_table must still come back unknown once reads_known is False —
    reads_known governs the whole answer, not a per-table lookup."""
    by_table = {"p.d.t": {"reads": 7, "last_read": "2026-09-01T00:00:00+00:00"}}
    assert C._reads_info("p.d.t", by_table, True) == by_table["p.d.t"]
    assert C._reads_info("p.d.t", by_table, False) == {"reads": None, "last_read": None}


# --- C3: the findings read must be labelled, bounded, windowed, and budgeted ---------

class _ConfigCapturingClient(_FakeClient):
    """Records the job_config each .query() call received — _FakeClient
    itself only looks at .dry_run, so a test needing to check labels or
    maximum_bytes_billed needs this instead."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.job_configs = []

    def query(self, sql, job_config=None):
        self.job_configs.append(job_config)
        return super().query(sql, job_config=job_config)


def test_read_own_findings_table_labels_bounds_and_windows_its_own_query(monkeypatch):
    """C3: _read_own_findings_table deliberately bypasses bq_guard.run (a
    plain SELECT on an ordinary table would otherwise be forced to a dry
    run, per its own docstring — kept as is) — so bq_guard.run's protections
    must be reapplied by hand: the release_copilot label (so this read
    excludes ITSELF from next run's own "top query shapes", exactly like
    every statement bq_guard.run issues), a maximum_bytes_billed backstop,
    and a run_ts predicate (the table is day-partitioned; LIMIT reduces rows
    returned, never bytes scanned)."""
    monkeypatch.setattr(C.settings, "bq_cost_dataset", "costs", raising=False)
    client = _ConfigCapturingClient(answers={
        "bq_cost_findings": [
            {"qhash": "abc123", "run_ts": "2026-08-01T00:00:00+00:00", "cost_before_bytes": 1 * C._GIB},
        ],
    })
    monkeypatch.setattr(C, "_get_client", lambda: client)

    rows = C._read_own_findings_table(None, runs=5)

    assert rows and rows[0]["qhash"] == "abc123"
    sql = client.queries[0]
    assert "run_ts > TIMESTAMP_SUB" in sql
    cfg = client.job_configs[0]
    assert cfg.labels == {"release_copilot": "bq_cost"}
    assert cfg.maximum_bytes_billed == C._FINDINGS_MAX_BYTES_BILLED


def test_read_own_findings_table_keeps_the_run_ts_window_when_a_qhash_is_given(monkeypatch):
    monkeypatch.setattr(C.settings, "bq_cost_dataset", "costs", raising=False)
    client = _FakeClient(answers={"bq_cost_findings": []})
    monkeypatch.setattr(C, "_get_client", lambda: client)

    C._read_own_findings_table("abc123", runs=3)

    sql = client.queries[0]
    assert "run_ts > TIMESTAMP_SUB" in sql and "qhash = @qhash" in sql


def test_scan_accounts_the_findings_read_in_its_own_query_budget(monkeypatch):
    """C3: the findings read sat entirely outside the scan's Budget
    (reserve()/record()) accounting, so report_cost_bytes/queries_run
    understated what the scan actually read whenever memory-across-runs
    (BQ_COST_DATASET) is on."""
    monkeypatch.setattr(C.settings, "bq_cost_dataset", "costs", raising=False)
    answers = _happy_answers(**{
        "bq_cost_findings": {"rows": [
            {"qhash": "abc123", "run_ts": "2026-08-01T00:00:00+00:00", "cost_before_bytes": 1 * C._GIB},
        ], "total_bytes_processed": 12_345},
    })
    client = _FakeClient(answers=answers)
    monkeypatch.setattr(C, "_get_client", lambda: client)

    out = C.scan()

    assert out["ok"] is True
    assert out["queries_run"] == 8  # the usual 7 scan statements + this findings read
    assert out["report_cost_bytes"] == 7 * 1000 + 12_345


# --- C4: _split_table must not mangle a backtick-quoted table reference --------------

def test_split_table_handles_a_console_style_single_backtick_wrapped_reference():
    assert C._split_table("`myproj.analytics.events`") == ("myproj", "analytics", "events")


def test_split_table_handles_each_part_individually_backtick_quoted():
    """C4: '`proj`.`ds`.`tbl`' used to become ('proj`', '`ds`', '`tbl') —
    .strip('`') only strips the two ends of the WHOLE string, leaving the
    backtick right after 'proj' (and the one right before the final 'tbl')
    in place — and that mangled tuple went straight into an f-string
    building a real-run statement."""
    assert C._split_table("`proj`.`ds`.`tbl`") == ("proj", "ds", "tbl")


def test_split_table_still_handles_the_plain_unquoted_forms():
    assert C._split_table("dataset.table") == (C._project(), "dataset", "table")
    assert C._split_table("project.dataset.table") == ("project", "dataset", "table")


def test_table_layout_accepts_a_per_part_backtick_quoted_reference(monkeypatch):
    client = _FakeClient(answers={
        "o_exp.option_value": [{
            "table_name": "tbl", "ddl": "CREATE TABLE proj.ds.tbl (a INT64)\n",
            "creation_time": None, "expiration_ts": None, "partition_expiration_days": None,
            "partition_field": None, "clustering_columns": [], "columns": [{"name": "a", "type": "INT64"}],
        }],
        "total_rows, total_logical_bytes": [],
        "partition_count": [],
    })
    monkeypatch.setattr(C, "_get_client", lambda: client)

    result = C.table_layout("`proj`.`ds`.`tbl`")

    assert result["ok"] is True and result["table"] == "proj.ds.tbl"


# --- C5: project/dataset must be validated before reaching a REAL-run f-string -------

def test_valid_bq_identifier_allows_bigquerys_own_characters():
    assert C._valid_bq_identifier("my-project_1") is True
    assert C._valid_bq_identifier("") is False


def test_valid_bq_identifier_allows_at_most_one_colon_and_only_for_a_project():
    assert C._valid_bq_identifier("myorg:my-project", allow_colon=True) is True
    assert C._valid_bq_identifier("myorg:my-project", allow_colon=False) is False
    assert C._valid_bq_identifier("a:b:c", allow_colon=True) is False


def test_valid_bq_identifier_refuses_punctuation_a_backtick_above_all():
    assert C._valid_bq_identifier("d`ataset") is False
    assert C._valid_bq_identifier("d ataset") is False
    assert C._valid_bq_identifier("d;DROP TABLE x") is False


def test_split_table_refuses_a_backtick_in_the_dataset_before_it_reaches_sql():
    """C5: _dataset_ref/_tables_legacy_ref f-string project/dataset UNESCAPED
    into a statement that then executes for real — the table NAME is
    parameterised, they are not. Refusing here on character set is the
    designed defence; _split_table merely requiring 2-3 dot-separated parts
    is not (a backtick contains no dot, so it survives that check today)."""
    with pytest.raises(ValueError, match="invalid dataset"):
        C._split_table("proj.d`ataset.tbl")


def test_split_table_refuses_bad_characters_in_the_project():
    with pytest.raises(ValueError, match="invalid project"):
        C._split_table("pr`oj.ds.tbl")


def test_table_layout_reports_an_invalid_identifier_as_a_clear_error_dict():
    result = C.table_layout("proj.d`ataset.tbl")
    assert result["ok"] is False and "invalid dataset" in result["error"]


def test_prune_estimate_reports_an_invalid_identifier_as_a_clear_error_dict():
    result = C.prune_estimate("pr`oj.ds.tbl", "col")
    assert result["ok"] is False and "invalid project" in result["error"]


# --- C1 (nit): scan()'s days/top coercion must never raise (model-driven input) ------

def test_coerce_int_falls_back_to_the_default_on_bad_input():
    assert C._coerce_int("not-a-number", 14) == 14
    assert C._coerce_int(None, 14) == 14
    assert C._coerce_int(0, 14) == 14
    assert C._coerce_int("7", 14) == 7
    assert C._coerce_int(7.9, 14) == 7


def test_scan_coerces_non_integer_days_and_top_instead_of_raising(monkeypatch):
    """C1: bq_cost_scan(days, top) is model-driven — a non-coercible value
    from Gemini must degrade to the configured default, never raise out of a
    function (scan()) the whole design relies on never raising."""
    client = _FakeClient(answers=_happy_answers())
    monkeypatch.setattr(C, "_get_client", lambda: client)

    out = C.scan(days="not-a-number", top="also-not-a-number")

    assert out["ok"] is True
    assert out["days"] == C.settings.bq_cost_days
    assert len(out["shapes"]) == 1


# --- C6 (nit): _run_rows must fail loudly if a statement stops classifying as metadata

def test_run_rows_refuses_a_non_metadata_statement_before_ever_issuing_it():
    """C6: bq_guard.run would silently force a non-INFORMATION_SCHEMA
    statement to a dry run, and .result() on a dry-run job returns an EMPTY
    row iterator on the real client (not an error) — so a future bug in one
    of this module's SQL builders would just make a section report "no
    findings", with nothing to say why. _run_rows must catch that itself,
    before ever reaching the client."""
    client = _FakeClient(answers={"FROM orders": {"rows": [{"a": 1}]}})

    with pytest.raises(AssertionError, match="INFORMATION_SCHEMA"):
        C._run_rows(client, "SELECT * FROM orders", None)
    assert client.queries == []  # refused up front — never even reached BigQuery
