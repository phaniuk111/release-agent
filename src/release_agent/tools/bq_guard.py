"""The ONE gate every BigQuery statement the cost tool issues passes through.

The cost tool's service account holds roles/bigquery.jobUser, which can run real
queries. This module is what keeps it to reads: a statement is executed for
real ONLY when every table it reads is an INFORMATION_SCHEMA view — or the
legacy ``__TABLES__`` meta-table, the one other read-only, non-INFORMATION_SCHEMA
surface this tool needs (tools/bq_cost.py's per-dataset storage fallback, used
when a region-wide INFORMATION_SCHEMA view is denied because one dataset in the
region — often one of BigQuery's own hidden anonymous cached-result datasets —
excludes this account). Anything else is forced to a dry run; DDL/DML/scripting
is refused outright. The check is on TOKENS (no regex — house rule), and string
literals are skipped so a quoted 'FROM' cannot fool it.

    tokens(sql) -> the sql's words/punctuation, literals removed, backtick-quoted
                   identifiers (and the dotted chain around them) kept whole so
                   `region-us`.INFORMATION_SCHEMA.JOBS reads as one token.
    classify(sql) -> "information_schema" | "select" | "refused"
    run(client, sql, *, budget=None, job_config=None) -> the QueryJob (real for
                   an INFORMATION_SCHEMA/__TABLES__-only read, dry for anything
                   else — decided from the EFFECTIVE job_config.dry_run, so a
                   caller-supplied dry_run=True is honoured even for a
                   classify()=="information_schema" statement, e.g. bq_cost.dry_run).
                   Every REAL run also gets maximum_bytes_billed capped at
                   _MAX_BYTES_BILLED (1 GiB) as a backstop, whatever the caller
                   passed in.
    Budget(max_queries) -> refuses the (max+1)th statement in one scan; counts
                           bytes processed so a report can state its own cost.
                           A failed attempt still counts against max_queries (at
                           0 bytes) — a denied read must not be retryable past
                           the cap.
"""
from __future__ import annotations

from typing import Any


class GuardRefused(ValueError):
    """The statement is not a read, or the scan budget is spent."""


# Backstop on every REAL run (never a dry run — those never bill): scan reads
# are a 10 MB minimum each, so 1 GiB is a huge margin over the intended cost,
# there only to cap a mistaken statement, not to be hit in normal operation.
_MAX_BYTES_BILLED = 2**30


class Budget:
    """Per-scan cap: INFORMATION_SCHEMA reads bill a 10 MB minimum each."""

    def __init__(self, max_queries: int) -> None:
        self.max_queries = int(max_queries)
        self.queries = 0
        self.bytes = 0

    def spend(self, bytes_processed: int) -> None:
        if self.queries >= self.max_queries:
            raise GuardRefused(
                f"BQ cost scan budget exhausted ({self.max_queries} statements) — "
                "refusing to issue another query this run."
            )
        self.queries += 1
        self.bytes += int(bytes_processed or 0)


# Word chars only — no regex (house rule): tokens() is a hand-rolled scan.
_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _is_word(ch: str) -> bool:
    return ch in _WORD_CHARS


def tokens(sql: str) -> list[str]:
    """Words and punctuation of ``sql`` with string literals removed. Pure.

    Single/double-quoted spans are string literals and are dropped entirely —
    contributing NO token — so a quoted 'FROM' or 'SELECT' cannot masquerade as
    SQL. Backtick-quoted spans are BigQuery IDENTIFIERS, not literals, and are
    kept — chained together with any surrounding ``.name`` parts (plain or
    backtick-quoted) into a single token, so both
    `` `region-us`.INFORMATION_SCHEMA.JOBS `` and a fully backtick-quoted
    `` `project.dataset.INFORMATION_SCHEMA.JOBS` `` read as ONE identifier token
    that ``classify`` can test whole for "contains INFORMATION_SCHEMA". Line
    (``--``) and block (``/* */``) comments are dropped like whitespace.
    """
    out: list[str] = []
    i, n = 0, len(sql or "")
    while i < n:
        ch = sql[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "-" and i + 1 < n and sql[i + 1] == "-":
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if ch == "/" and i + 1 < n and sql[i + 1] == "*":
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch in ("'", '"'):
            quote = ch
            i += 1
            while i < n:
                if sql[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if sql[i] == quote:
                    if i + 1 < n and sql[i + 1] == quote:  # doubled-quote escape
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue  # a literal contributes NO token
        if ch == "`" or _is_word(ch):
            start = i
            while i < n:
                c = sql[i]
                if c == "`":
                    i += 1
                    while i < n and sql[i] != "`":
                        i += 1
                    if i < n:
                        i += 1  # consume the closing backtick
                    continue
                if _is_word(c):
                    i += 1
                    continue
                if c == "." and i + 1 < n and (sql[i + 1] == "`" or _is_word(sql[i + 1])):
                    i += 1  # chain a dotted identifier part onto this token
                    continue
                break
            out.append(sql[start:i])
            continue
        out.append(ch)  # single-char punctuation token: ( ) , ; = * etc.
        i += 1
    return out


# First-token keywords that mean "not a read" — anything NOT SELECT/WITH is
# refused regardless, so this set only documents the intent (see classify()).
_NOT_A_READ = {
    "INSERT", "UPDATE", "DELETE", "MERGE", "CREATE", "DROP", "ALTER", "TRUNCATE",
    "CALL", "EXECUTE", "GRANT", "REVOKE", "BEGIN", "DECLARE", "SET", "EXPORT", "LOAD",
}


def _is_metadata_ref(token: str) -> bool:
    """True for an INFORMATION_SCHEMA view or the legacy ``__TABLES__`` meta-table
    — the two read-only surfaces this tool ever executes for real.

    Checked by PATH SEGMENT, not substring: a real table merely NAMED
    ``...INFORMATION_SCHEMA...`` (e.g. ``p.d.my_INFORMATION_SCHEMA_copy``) must
    not pass just because the substring appears somewhere in it.
    """
    segments = [seg for seg in token.replace("`", "").split(".") if seg]
    if not segments:
        return False
    return any(seg.upper() == "INFORMATION_SCHEMA" for seg in segments) or segments[-1].upper() == "__TABLES__"


def classify(sql: str) -> str:
    """'information_schema' when every FROM/JOIN target is an INFORMATION_SCHEMA
    view (or ``__TABLES__``), 'select' for any other single SELECT/WITH, 'refused'
    for anything that is not a read (CREATE/INSERT/UPDATE/DELETE/MERGE/DROP/ALTER/
    TRUNCATE/CALL/EXECUTE/GRANT/REVOKE/BEGIN/DECLARE/SET/EXPORT/LOAD, or several
    statements).

    KNOWN, DELIBERATE LIMITATION: a CTE (``WITH x AS (SELECT ... FROM <IS view>)
    SELECT ... FROM x``) is NOT recognised — ``x`` is just a name at that FROM,
    not a metadata reference — so a statement shaped that way is classified
    'select' and dry-runs instead of running for real. That is the SAFE
    direction (never mistakenly a real run), so it is left alone rather than
    taught to resolve CTE names. Every real read in this codebase (bq_cost.py)
    is written as a plain SELECT or an inline subquery (``FROM (SELECT ... FROM
    <IS view>)``, which IS recognised — see the "subquery" branch below);
    keep it that way rather than introducing a CTE that would silently stop
    running for real.
    """
    toks = tokens(sql)
    if not toks:
        return "refused"
    first = toks[0].upper()
    if first not in ("SELECT", "WITH"):
        return "refused"  # covers _NOT_A_READ, and anything else that isn't a read
    semi = next((i for i, t in enumerate(toks) if t == ";"), None)
    if semi is not None and semi < len(toks) - 1:
        return "refused"  # a second statement follows the first's terminator
    saw_from_or_join = False
    all_metadata = True
    for i, t in enumerate(toks):
        if t.upper() in ("FROM", "JOIN"):
            saw_from_or_join = True
            nxt = toks[i + 1] if i + 1 < len(toks) else ""
            if nxt == "(" or nxt.upper() == "UNNEST":
                # A subquery, or UNNEST of an in-scope array expression (e.g.
                # ``FROM UNNEST(labels) AS l`` over a column already read from
                # a real FROM elsewhere in the statement) — UNNEST's argument
                # is an expression, never an external table name, so this can
                # never smuggle in a real-table read. Its own FROM/JOIN (if
                # the expression were itself a subquery) is checked in turn.
                continue
            if not _is_metadata_ref(nxt):
                all_metadata = False
    return "information_schema" if saw_from_or_join and all_metadata else "select"


def run(client: Any, sql: str, *, budget: Budget | None = None, job_config: Any = None) -> Any:
    """Execute through the gate. Real run only for 'information_schema'; a
    'select' is ALWAYS dry-run (job_config.dry_run forced True); 'refused' raises
    GuardRefused before anything reaches BigQuery. Waits for the job and returns it.

    Whether ``.result()`` is called is decided from the job_config's EFFECTIVE
    ``dry_run`` flag, not from ``kind`` alone: a caller (bq_cost.dry_run) may
    pass its own ``dry_run=True`` config for a statement that classifies as
    'information_schema' — that must still never call ``.result()`` on it.

    A failed attempt (BigQuery itself raised — e.g. a permission error) still
    counts against ``budget`` at 0 bytes: it was a statement issued, not one
    refused up front, and a caller retrying a denied read must not be able to
    run past the scan's own cap.
    """
    kind = classify(sql)
    if kind == "refused":
        raise GuardRefused(f"not a read, or more than one statement: {sql[:160]!r}")
    from google.cloud import bigquery

    cfg = job_config
    if kind == "select":
        cfg = cfg if cfg is not None else bigquery.QueryJobConfig()
        cfg.dry_run = True
    is_dry = bool(getattr(cfg, "dry_run", False))
    if not is_dry:
        # Belt-and-braces: even a correctly-classified real run should never
        # be able to bill more than a scan read ever legitimately would.
        cfg = cfg if cfg is not None else bigquery.QueryJobConfig()
        cfg.maximum_bytes_billed = _MAX_BYTES_BILLED
    # Every statement this gate issues — real or dry — carries this label, so
    # the scan's own cost-ranking queries can exclude their OWN jobs from the
    # ranking (see bq_cost.py's _shapes_sql/_totals_sql/_table_reads_sql) and
    # so the tool's own footprint is identifiable in JOBS like anyone else's.
    cfg = cfg if cfg is not None else bigquery.QueryJobConfig()
    cfg.labels = {**(cfg.labels or {}), "release_copilot": "bq_cost"}
    try:
        job = client.query(sql, job_config=cfg)
        if not is_dry:  # dry-run jobs have no rows to wait for
            job.result()
    except GuardRefused:
        raise
    except Exception:
        if budget is not None:
            budget.spend(0)
        raise
    if budget is not None:
        budget.spend(getattr(job, "total_bytes_processed", None) or 0)
    return job
