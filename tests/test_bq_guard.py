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


def test_tokens_a_raw_string_ends_at_the_first_quote_whatever_precedes_it():
    """B2: a backslash inside r'...'/rb'...'/br'...' etc. is ordinary content,
    not an escape — the old scanner treated it as one unconditionally and
    read straight past the closing quote into the rest of the statement."""
    assert G.tokens("SELECT r'\\' AS x") == ["SELECT", "AS", "x"]
    assert G.tokens('SELECT R"\\" AS x') == ["SELECT", "AS", "x"]
    assert G.tokens("SELECT rb'\\' AS x") == ["SELECT", "AS", "x"]
    assert G.tokens("SELECT Br'\\' AS x") == ["SELECT", "AS", "x"]
    # a bare identifier that merely starts with r/b is untouched
    assert G.tokens("SELECT region, budget FROM t") == ["SELECT", "region", ",", "budget", "FROM", "t"]


def test_tokens_handle_triple_quoted_strings_explicitly():
    """B2: only three consecutive matching quote characters close a triple-
    quoted literal — a lone or doubled one inside is just content. The old
    scanner had no explicit triple-quote handling and fell through to the
    doubled-quote-escape rule, which mistakes a LONE embedded quote for the
    literal's end and leaks whatever follows as fake tokens."""
    assert G.tokens("SELECT '''it's a test''' AS x") == ["SELECT", "AS", "x"]
    assert G.tokens('SELECT """she said "hi""" AS x') == ["SELECT", "AS", "x"]


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


def test_classify_checks_every_table_in_a_comma_separated_from_not_just_the_first():
    """B1: an implicit cross join lists more than one table after a single
    FROM/JOIN — the old loop only ever inspected the token right after
    FROM/JOIN, so every table after the first was invisible to it and this
    wrongly classified as information_schema (safe to run for real)."""
    assert G.classify(
        "SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS, proj.ds.secret_table"
    ) == "select"
    assert G.classify(
        "SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS a, "
        "`region-us`.INFORMATION_SCHEMA.TABLES b, p.d.secret c"
    ) == "select"


def test_classify_an_alias_between_a_table_and_a_comma_is_not_mistaken_for_another_table():
    """The comma form is legitimate — bq_cost.py's own
    ``FROM ... AS j, UNNEST(j.referenced_tables) AS rt`` — so a real alias
    right before the comma must not itself be tested as a table, and the
    genuine next table must still be found and tested."""
    assert G.classify(
        "SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS_BY_PROJECT AS j, "
        "UNNEST(j.referenced_tables) AS rt"
    ) == "information_schema"
    assert G.classify(
        "SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS a, "
        "`region-us`.INFORMATION_SCHEMA.TABLES b"
    ) == "information_schema"  # bare (no AS) aliases on both sides of the comma


def test_classify_a_raw_string_does_not_hide_a_trailing_real_table():
    """B2: BigQuery raw strings (r'...') do not treat backslash as an escape.
    The old scanner did, so it read past the literal's true end, hid the
    trailing UNION ALL over a real table, and wrongly classified this as
    information_schema (safe to run for real)."""
    sql = ("SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS WHERE a = r'\\' AND b = 'x' "
           "UNION ALL SELECT * FROM proj.ds.secret_table")
    assert G.classify(sql) != "information_schema"


def test_classify_a_triple_quoted_string_does_not_hide_a_trailing_real_table():
    """B2: a lone quote embedded in a triple-quoted string is just content —
    only three consecutive matching quotes close it. The old scanner relied
    on the doubled-quote-escape rule alone, which mistook this lone quote for
    the literal's end and, past it, hid the trailing UNION ALL over a real
    table."""
    sql = ("SELECT * FROM `region-us`.INFORMATION_SCHEMA.JOBS WHERE a = '''it's fine''' "
           "UNION ALL SELECT * FROM proj.ds.secret_table")
    assert G.classify(sql) != "information_schema"


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


def test_run_reserves_and_records_bytes_processed():
    client = _FakeClient()
    budget = G.Budget(10)
    G.run(client, _IS_SQL, budget=budget)
    assert budget.queries == 1
    assert budget.bytes == 1234


def test_budget_refuses_the_41st_statement():
    budget = G.Budget(40)
    for _ in range(40):
        budget.reserve()
        budget.record(1000)
    assert budget.queries == 40 and budget.bytes == 40000
    with pytest.raises(G.GuardRefused):
        budget.reserve()
    assert budget.queries == 40  # the refused reservation is not counted again


def test_budget_refuses_the_third_statement_before_it_ever_reaches_the_client():
    """B5: the old code only checked the cap INSIDE spend(), called AFTER
    client.query()/job.result() — so Budget(2) let a 3rd statement reach (and
    complete against) the client, and never counted its bytes at all. Proven
    directly against the fake client: only 2 of 3 attempts may ever reach it."""
    client = _FakeClient()
    budget = G.Budget(2)
    G.run(client, _IS_SQL, budget=budget)
    G.run(client, _IS_SQL, budget=budget)
    with pytest.raises(G.GuardRefused):
        G.run(client, _IS_SQL, budget=budget)
    assert len(client.queries) == 2  # the 3rd statement never reached the client
    assert budget.queries == 2
    assert budget.bytes == 2468  # 2 * 1234 — not silently missing the 3rd attempt's bytes


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


def test_run_propagates_the_clients_own_error_rather_than_guardrefused():
    """B6: run()'s except-block used to call budget.spend(0) on a failed
    attempt, and spend() itself raises GuardRefused once the budget is at
    its cap — replacing whatever BigQuery actually raised (e.g. a 403 on
    TABLE_STORAGE) with a misleading 'budget exhausted', denying
    bq_cost.hint_for() the text it needs to explain the real problem."""
    client = _FailingClient(RuntimeError("Access Denied: 403 on TABLE_STORAGE"))
    budget = G.Budget(5)
    with pytest.raises(RuntimeError, match="403 on TABLE_STORAGE"):
        G.run(client, _IS_SQL, budget=budget)


def test_run_never_lets_an_over_budget_statement_reach_a_failing_client():
    """B5+B6 together: an over-cap statement must be refused BEFORE it ever
    reaches the client, so there is never a real BigQuery error left for a
    budget check to mask. The old code had no upfront check, so it called
    client.query() regardless of the budget and only then, in its
    except-block, replaced the client's own error with GuardRefused."""
    client = _FailingClient(RuntimeError("Access Denied: 403 on TABLE_STORAGE"))
    budget = G.Budget(0)
    with pytest.raises(G.GuardRefused):
        G.run(client, _IS_SQL, budget=budget)
    assert client.queries == []  # never issued — nothing left for a budget check to mask


def test_a_refused_statement_never_touches_the_budget():
    budget = G.Budget(5)
    with pytest.raises(G.GuardRefused):
        G.run(_FakeClient(), "DROP TABLE x", budget=budget)
    assert budget.queries == 0
