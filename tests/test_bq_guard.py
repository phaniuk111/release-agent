"""The ONE gate every BQ cost-tool statement passes through: no network."""
import pytest

from release_agent.tools import bq_guard as G


# --- tokens() -----------------------------------------------------------------

def test_tokens_skip_string_literals_so_a_quoted_keyword_cannot_fool_it():
    assert G.tokens("SELECT 'FROM nowhere, SELECT this' AS x") == ["SELECT", "AS", "x"]


def test_tokens_handle_doubled_quote_escaping_inside_a_literal():
    assert G.tokens("SELECT 'it''s fine'") == ["SELECT"]


def test_tokens_keep_backtick_identifiers_whole_with_the_dotted_chain_around_them():
    assert G.tokens("FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT") == [
        "FROM", "`region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT",
    ]
    assert G.tokens("FROM `project.dataset.INFORMATION_SCHEMA.JOBS_BY_PROJECT`") == [
        "FROM", "`project.dataset.INFORMATION_SCHEMA.JOBS_BY_PROJECT`",
    ]


def test_tokens_drop_comments_like_whitespace():
    assert G.tokens("SELECT 1 -- a comment with a fake FROM x\n") == ["SELECT", "1"]
    assert G.tokens("SELECT /* FROM x */ 1") == ["SELECT", "1"]


# --- classify() -----------------------------------------------------------------

def test_classify_true_for_region_qualified_information_schema():
    assert G.classify("SELECT job_id FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT") \
        == "information_schema"


def test_classify_true_for_a_fully_backticked_information_schema_path():
    assert G.classify("SELECT * FROM `project.dataset.INFORMATION_SCHEMA.JOBS_BY_PROJECT`") \
        == "information_schema"


def test_classify_true_for_two_information_schema_joins():
    sql = ("SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT a "
           "JOIN `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT b ON a.job_id = b.job_id")
    assert G.classify(sql) == "information_schema"


def test_classify_true_for_the_legacy_tables_meta_table():
    """__TABLES__ is not INFORMATION_SCHEMA but IS a read-only metadata surface
    the storage-fallback path needs treated the same way (see bq_cost.py)."""
    assert G.classify("SELECT table_id FROM `my-project.mydataset.__TABLES__`") \
        == "information_schema"


def test_classify_from_unnest_is_fine_like_a_subquery():
    """UNNEST's argument is an in-scope array expression, never an external
    table — bq_cost.py excludes its own jobs via
    ``NOT EXISTS (SELECT 1 FROM UNNEST(labels) AS l WHERE l.key = ...)``,
    which must not disqualify the statement's real FROM/JOIN target."""
    sql = ("SELECT job_id FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT "
           "WHERE NOT EXISTS (SELECT 1 FROM UNNEST(labels) AS l WHERE l.key = 'release_copilot')")
    assert G.classify(sql) == "information_schema"
    # UNNEST over a real (non-metadata) table's array column must still not
    # let a genuine FROM elsewhere in the statement off the hook.
    mixed = "SELECT * FROM mydataset.mytable, UNNEST(tags) AS t"
    assert G.classify(mixed) == "select"


def test_classify_a_subquery_after_from_is_fine():
    sql = "SELECT * FROM (SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT)"
    assert G.classify(sql) == "information_schema"


def test_classify_false_when_one_from_is_a_real_table():
    assert G.classify("SELECT * FROM mydataset.mytable") == "select"
    mixed = ("SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT j "
             "JOIN mydataset.mytable t ON j.x = t.y")
    assert G.classify(mixed) == "select"


def test_classify_a_lookalike_table_name_is_not_a_metadata_read():
    """A real table merely NAMED ...INFORMATION_SCHEMA... (substring, not a
    path segment) must be forced to dry run, not run for real."""
    assert G.classify("SELECT * FROM `p.d.my_INFORMATION_SCHEMA_copy`") == "select"
    assert G.classify("SELECT * FROM `p.d.my__TABLES__copy`") == "select"
    assert G.classify("SELECT * FROM `p.d.__TABLES__`") == "information_schema"  # the real meta-table


def test_classify_does_not_resolve_a_cte_as_a_metadata_reference():
    """Documented, deliberate limitation (see classify()'s docstring): a CTE
    name at FROM is just a name, so this is forced to dry run — the safe
    direction — rather than run for real."""
    sql = ("WITH x AS (SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT) "
           "SELECT * FROM x")
    assert G.classify(sql) == "select"


@pytest.mark.parametrize("sql", [
    "INSERT INTO t VALUES (1)",
    "UPDATE t SET x = 1 WHERE TRUE",
    "DELETE FROM t WHERE TRUE",
    "MERGE t USING s ON t.id = s.id WHEN MATCHED THEN UPDATE SET x = 1",
    "CREATE TABLE t (x INT64)",
    "DROP TABLE t",
    "ALTER TABLE t ADD COLUMN y INT64",
    "TRUNCATE TABLE t",
    "CALL my_proc()",
    "EXECUTE IMMEDIATE 'SELECT 1'",
    "GRANT `roles/bigquery.dataViewer` ON TABLE t TO 'user:a@b.com'",
    "REVOKE `roles/bigquery.dataViewer` ON TABLE t FROM 'user:a@b.com'",
    "BEGIN SELECT 1; END",
    "DECLARE x INT64 DEFAULT 1",
    "SET x = 1",
    "EXPORT DATA OPTIONS(uri='gs://x') AS SELECT 1",
    "LOAD DATA INTO t FROM FILES(format='CSV', uris=['gs://x'])",
    "SELECT 1; DROP TABLE t",
    "INSERT INTO t VALUES ('SELECT this')",
])
def test_classify_refuses_everything_that_is_not_a_read(sql):
    assert G.classify(sql) == "refused"


def test_classify_a_trailing_semicolon_alone_is_not_a_second_statement():
    assert G.classify("SELECT 1;") == "select"


def test_classify_empty_sql_is_refused():
    assert G.classify("") == "refused"
    assert G.classify("   ") == "refused"


# --- run() ----------------------------------------------------------------------

class _FakeJob:
    def __init__(self, *, total_bytes_processed=1234, rows=None, is_dry=False):
        self.total_bytes_processed = total_bytes_processed
        self._rows = rows if rows is not None else []
        self._is_dry = is_dry
        self.result_called = False

    def result(self):
        if self._is_dry:
            raise AssertionError("bq_guard.run must never call .result() on a dry-run job")
        self.result_called = True
        return list(self._rows)


class _FakeClient:
    def __init__(self):
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        is_dry = bool(getattr(job_config, "dry_run", False))
        return _FakeJob(is_dry=is_dry)


class _FailingClient:
    def __init__(self, error):
        self.error = error
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        raise self.error


_IS_SQL = "SELECT job_id FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT"


def test_run_forces_dry_run_for_a_real_table_select_and_never_calls_result():
    client = _FakeClient()
    job = G.run(client, "SELECT * FROM mydataset.mytable")
    sql, cfg = client.queries[-1]
    assert cfg.dry_run is True
    assert job.result_called is False
    assert job.total_bytes_processed == 1234  # read straight off the job, no .result()


def test_run_real_runs_information_schema_and_waits_for_it():
    client = _FakeClient()
    job = G.run(client, _IS_SQL)
    sql, cfg = client.queries[-1]
    assert not bool(getattr(cfg, "dry_run", False))
    assert job.result_called is True


def test_run_raises_before_bigquery_on_refused():
    client = _FakeClient()
    with pytest.raises(G.GuardRefused):
        G.run(client, "DROP TABLE x")
    assert client.queries == []  # never touched BigQuery


def test_run_honours_a_caller_supplied_dry_run_even_for_information_schema():
    """bq_cost.dry_run() passes its own dry_run=True config; run() must respect
    it (and never call .result()) even though classify() says 'information_schema'."""
    from google.cloud import bigquery

    client = _FakeClient()
    cfg = bigquery.QueryJobConfig(dry_run=True)
    job = G.run(client, _IS_SQL, job_config=cfg)
    assert job.result_called is False


def test_run_sets_a_maximum_bytes_billed_backstop_on_every_real_run():
    client = _FakeClient()
    G.run(client, _IS_SQL)
    _, cfg = client.queries[-1]
    assert cfg.maximum_bytes_billed == G._MAX_BYTES_BILLED


def test_run_labels_every_statement_it_issues_real_or_dry():
    """So bq_cost.py's own cost-ranking queries can exclude their OWN jobs
    from the ranking (NOT EXISTS ... labels ... 'release_copilot')."""
    client = _FakeClient()
    G.run(client, _IS_SQL)
    _, is_cfg = client.queries[-1]
    assert is_cfg.labels == {"release_copilot": "bq_cost"}

    G.run(client, "SELECT * FROM mydataset.mytable")
    _, select_cfg = client.queries[-1]
    assert select_cfg.labels == {"release_copilot": "bq_cost"}


def test_run_preserves_a_caller_supplied_label_alongside_its_own():
    from google.cloud import bigquery

    client = _FakeClient()
    cfg = bigquery.QueryJobConfig(labels={"team": "release-copilot"})
    G.run(client, _IS_SQL, job_config=cfg)
    assert cfg.labels == {"team": "release-copilot", "release_copilot": "bq_cost"}


def test_run_does_not_need_a_bytes_cap_on_a_dry_run():
    client = _FakeClient()
    G.run(client, "SELECT * FROM mydataset.mytable")
    _, cfg = client.queries[-1]
    assert cfg.dry_run is True
    assert cfg.maximum_bytes_billed is None


def test_run_calls_budget_spend_with_bytes_processed():
    client = _FakeClient()
    budget = G.Budget(10)
    G.run(client, _IS_SQL, budget=budget)
    assert budget.queries == 1
    assert budget.bytes == 1234


def test_budget_refuses_the_41st_statement():
    budget = G.Budget(40)
    for _ in range(40):
        budget.spend(1000)
    assert budget.queries == 40 and budget.bytes == 40000
    with pytest.raises(G.GuardRefused):
        budget.spend(1000)
    assert budget.queries == 40  # the refused attempt is not counted again


def test_run_refuses_past_a_tiny_budget_mid_scan():
    client = _FakeClient()
    budget = G.Budget(1)
    G.run(client, _IS_SQL, budget=budget)
    with pytest.raises(G.GuardRefused):
        G.run(client, _IS_SQL, budget=budget)


def test_a_failed_attempt_still_counts_against_budget_at_zero_bytes():
    """A denied read must not be retryable past the scan's own cap."""
    client = _FailingClient(RuntimeError("Access Denied"))
    budget = G.Budget(5)
    with pytest.raises(RuntimeError):
        G.run(client, _IS_SQL, budget=budget)
    assert budget.queries == 1
    assert budget.bytes == 0


def test_a_refused_statement_never_touches_the_budget():
    budget = G.Budget(5)
    with pytest.raises(G.GuardRefused):
        G.run(_FakeClient(), "DROP TABLE x", budget=budget)
    assert budget.queries == 0
