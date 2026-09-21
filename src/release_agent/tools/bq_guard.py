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
    Budget(max_queries) -> refuses the (max+1)th statement in one scan BEFORE
                           it ever reaches the client; counts bytes processed
                           afterwards so a report can state its own cost. A
                           failed attempt still counts against max_queries (at
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
    """Per-scan cap on issued statements (INFORMATION_SCHEMA reads bill a
    10 MB minimum each), plus a running total of bytes processed.

    Accounting is split across a statement's own attempt (see run()):
    reserve() claims one of ``max_queries`` slots and refuses the (max+1)th
    BEFORE the statement is issued — BigQuery is never even asked for a
    query the budget was always going to refuse. record() adds the bytes of
    a statement that then actually completed. A slot claimed by reserve() is
    never given back: even when the statement then FAILS (e.g. a permission
    error), it stays spent at 0 bytes — record() is simply never reached —
    so a denied read cannot be retried past the cap.
    """

    def __init__(self, max_queries: int) -> None:
        self.max_queries = int(max_queries)
        self.queries = 0
        self.bytes = 0

    def reserve(self) -> None:
        """Claim a slot BEFORE the statement reaches the client."""
        if self.queries >= self.max_queries:
            raise GuardRefused(
                f"BQ cost scan budget exhausted ({self.max_queries} statements) — "
                "refusing to issue another query this run."
            )
        self.queries += 1

    def record(self, bytes_processed: int) -> None:
        """Add a completed statement's bytes — only ever called after the
        reserve() that claimed its slot."""
        self.bytes += int(bytes_processed or 0)


# Word chars only — no regex (house rule): tokens() is a hand-rolled scan.
_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)


def _is_word(ch: str) -> bool:
    return ch in _WORD_CHARS


def _literal_open(sql: str, i: int) -> tuple[int, str, bool, bool] | None:
    """Whether ``sql[i:]`` opens a string literal — None when it does not.

    Otherwise ``(prefix_len, quote, is_raw, tripled)``: prefix_len is 0-2, for
    an optional r/R and/or b/B immediately before the quote (BigQuery's raw
    and/or bytes literal prefixes, in either order) — counted only with NO
    space before the quote, exactly BigQuery's own rule, so e.g. a column
    named ``r`` followed by its own separate string literal is never mistaken
    for a raw-string prefix. is_raw is True when that prefix contains r/R —
    inside a raw literal a backslash is ordinary content, not an escape, so
    it can never hide the literal's true end (or, worse, run past it into
    the rest of the statement). tripled is True for ``'''``/``\"\"\"``,
    which only a run of three consecutive matching quote characters closes.
    """
    n = len(sql)
    prefix_len = 0
    if i < n and sql[i] in "rRbB":
        prefix_len = 1
        if i + 1 < n and sql[i + 1] in "rRbB" and sql[i + 1].lower() != sql[i].lower():
            prefix_len = 2
    q = i + prefix_len
    if q >= n or sql[q] not in ("'", '"'):
        return None
    is_raw = "r" in sql[i:q].lower()
    quote = sql[q]
    tripled = sql[q:q + 3] == quote * 3
    return prefix_len, quote, is_raw, tripled


def tokens(sql: str) -> list[str]:
    """Words and punctuation of ``sql`` with string literals removed. Pure.

    Single/double-quoted spans are string literals and are dropped entirely —
    contributing NO token — so a quoted 'FROM' or 'SELECT' cannot masquerade as
    SQL. An r/R (optionally with b/B) prefix makes one a RAW literal, where a
    backslash is ordinary content, not an escape (see ``_literal_open``) — a
    backslash right before the closing quote must still end the literal
    there, never read past it into whatever follows. A triple-quoted span
    (``'''`` / ``\"\"\"``) ends only at three consecutive matching quote
    characters, so a lone or doubled one inside it is just content, not a
    premature close. Backtick-quoted spans are BigQuery IDENTIFIERS, not
    literals, and are kept — chained together with any surrounding ``.name``
    parts (plain or backtick-quoted) into a single token, so both
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
        lit = _literal_open(sql, i)
        if lit is not None:
            prefix_len, quote, is_raw, tripled = lit
            i += prefix_len
            width = 3 if tripled else 1
            closer = quote * width
            i += width
            while i < n:
                if width == 3:
                    # Only three consecutive matching quotes close a tripled
                    # literal — a lone or doubled one inside is just content,
                    # never the premature close that fooled the doubled-quote
                    # rule below (that rule is for a NON-tripled literal only).
                    if sql[i:i + width] == closer:
                        i += width
                        break
                    i += 1
                    continue
                if not is_raw and sql[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if sql[i] == quote:
                    if not is_raw and i + 1 < n and sql[i + 1] == quote:  # doubled-quote escape
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


def _skip_parens(toks: list[str], open_idx: int) -> int:
    """``toks[open_idx]`` is the ``(`` of a subquery or an UNNEST argument
    list — returns the index just past its matching ``)``, so the
    comma-list walk in classify() never mistakes a comma INSIDE it (a
    subquery's own SELECT list, or a multi-argument UNNEST) for one
    separating this FROM/JOIN's own targets. Counted on ``(``/``)`` TOKENS
    only, which is exact: tokens() has already dropped every string literal,
    so no stray paren from inside one can throw the count off.
    """
    depth = 0
    i, n = open_idx, len(toks)
    while i < n:
        if toks[i] == "(":
            depth += 1
        elif toks[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return i  # unbalanced input (should not happen for real SQL) — stop at the end


def _could_be_alias(token: str) -> bool:
    """A bare alias (``t`` in ``FROM x t``) reads as one identifier token —
    the same shape as a table name — so this only gates a look-ahead-by-one
    in classify() before deciding whether a FROM/JOIN's comma list
    continues; it is never used to decide metadata-ness itself.
    """
    return bool(token) and (token[0] == "`" or _is_word(token[0]))


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
    n = len(toks)
    for i, t in enumerate(toks):
        if t.upper() not in ("FROM", "JOIN"):
            continue
        saw_from_or_join = True
        # An implicit cross join is a COMMA-separated list of targets on one
        # FROM/JOIN (bq_cost.py itself relies on exactly this shape —
        # ``FROM <IS view> AS j, UNNEST(j.referenced_tables) AS rt`` — so the
        # comma form must classify the same as a lone target). Every entry
        # after the first used to be invisible to this check; walk the whole
        # list here, the same test applied to each.
        j = i + 1
        while True:
            target = toks[j] if j < n else ""
            if target == "(":
                j = _skip_parens(toks, j)
            elif target.upper() == "UNNEST":
                # UNNEST's argument is an in-scope array expression (e.g.
                # ``FROM UNNEST(labels) AS l`` over a column already read
                # from a real FROM elsewhere in the statement), never an
                # external table name — skip its own parenthesised argument
                # without a metadata test; its own FROM/JOIN (if the
                # expression were itself a subquery) is checked in turn.
                paren = j + 1
                j = _skip_parens(toks, paren) if paren < n and toks[paren] == "(" else j + 1
            else:
                if not _is_metadata_ref(target):
                    all_metadata = False
                j += 1
            # An alias (``AS x`` or a bare ``x``) may sit between a target
            # and the comma that continues this list — skip AT MOST one such
            # alias before checking for that comma, so a real alias is never
            # mistaken for another table and a real next table is never
            # skipped over as if it merely were one.
            if j < n and toks[j].upper() == "AS":
                j += 2
            elif j < n and _could_be_alias(toks[j]):
                j += 1
            if j < n and toks[j] == ",":
                j += 1
                continue
            break
    return "information_schema" if saw_from_or_join and all_metadata else "select"


def run(client: Any, sql: str, *, budget: Budget | None = None, job_config: Any = None) -> Any:
    """Execute through the gate. Real run only for 'information_schema'; a
    'select' is ALWAYS dry-run (job_config.dry_run forced True); 'refused' raises
    GuardRefused before anything reaches BigQuery. Waits for the job and returns it.

    Whether ``.result()`` is called is decided from the job_config's EFFECTIVE
    ``dry_run`` flag, not from ``kind`` alone: a caller (bq_cost.dry_run) may
    pass its own ``dry_run=True`` config for a statement that classifies as
    'information_schema' — that must still never call ``.result()`` on it.

    ``budget`` (if given) reserves its slot BEFORE the statement reaches the
    client and records bytes only after it actually completes — see Budget.
    There is deliberately no try/except around the client call: whatever
    BigQuery itself raises (e.g. a permission error) must propagate as
    itself, since a caller (bq_cost.hint_for) reads that text to explain the
    failure. The reserved slot already stands at 0 bytes for a failed
    attempt with no further action needed here, so a caller retrying a
    denied read still cannot run past the scan's own cap.
    """
    kind = classify(sql)
    if kind == "refused":
        raise GuardRefused(f"not a read, or more than one statement: {sql[:160]!r}")
    from google.cloud import bigquery

    # A COPY: the caller's config object is theirs, and this function sets
    # dry_run, maximum_bytes_billed and labels on it. Editing it in place would
    # make a reused config carry this gate's decisions into the next statement.
    cfg = bigquery.QueryJobConfig.from_api_repr(job_config.to_api_repr()) if job_config is not None \
        else bigquery.QueryJobConfig()
    if kind == "select":
        cfg.dry_run = True
    is_dry = bool(getattr(cfg, "dry_run", False))
    if not is_dry:
        # Belt-and-braces: even a correctly-classified real run should never
        # be able to bill more than a scan read ever legitimately would.
        cfg.maximum_bytes_billed = _MAX_BYTES_BILLED
    # Every statement this gate issues — real or dry — carries this label, so
    # the scan's own cost-ranking queries can exclude their OWN jobs from the
    # ranking (see bq_cost.py's _shapes_sql/_totals_sql/_table_reads_sql) and
    # so the tool's own footprint is identifiable in JOBS like anyone else's.
    cfg.labels = {**(cfg.labels or {}), "release_copilot": "bq_cost"}
    if budget is not None:
        budget.reserve()
    job = client.query(sql, job_config=cfg)
    if not is_dry:  # dry-run jobs have no rows to wait for
        job.result()
    if budget is not None:
        budget.record(getattr(job, "total_bytes_processed", None) or 0)
    return job
