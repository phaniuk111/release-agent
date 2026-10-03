"""Support triage: a first look at a workflow control table.

Many pipelines log one row per run (or per status change) in a control table:
a business date, a run id, a status, often an error message and a job id. This
reads that table — READ-ONLY, a few fixed statements through tools/bq_guard.py,
in which BigQuery does the counting and only problems come back — and
answers what a person on support asks first:

    what failed for this business date, and how widely (one upstream system?
    one process everywhere? one unit?) · the distinct errors behind it · new or
    recurring, and when it last worked · retried already? · stuck runs ·
    work that ran on the previous date but not this one · output volume that
    collapsed on a run that "succeeded".

Nothing here knows any real table. The table, its columns and its status words
are configuration (SUPPORT_TABLE / SUPPORT_COLUMNS / SUPPORT_*_STATUSES), kept
in a private values file or .env — so this module, its tests and its docs carry
only the generic ROLES below.

    triage(business_date="") -> the report dict (the card renders it as is),
                                with `incidents`: the L1 view — per problem a
                                priority, known-issue match, one action (wait /
                                retrigger / check / escalate), steps, an owner
                                and a ticket note
    for_model(report)        -> the same, without error text unless
                                SUPPORT_ERRORS_TO_MODEL (the team's data)
    Query(table, columns)    -> the statements (BigQuery counts; problems only
                                come back), each read through bq_guard
    build_report(...)        -> PURE: those results → the report
"""
from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..config import settings
from . import bq_guard

# role -> what it means. date, run_id and status are required; every other role
# is optional and only switches on the checks that need it.
ROLES: dict[str, str] = {
    "date": "the business date of the run (a DATE column — the table's partition key, ideally)",
    "run_id": "one execution of the workflow",
    "status": "the run's current status",
    "updated_at": "when the row was written — orders a run's rows, ages stuck runs",
    "event_at": "when the triggering event arrived",
    "event_id": "the business event that triggered the run — retries share it",
    "job_id": "the job the run started (e.g. a Dataflow job)",
    "error": "a human-readable error message",
    "details": "structured error details (JSON)",
    "members": "how many members/rows the run produced",
    "system": "the upstream system the data comes from",
    "source": "the component or feed that fetched the data",
    "process": "the report type or process being run",
    "unit": "the unit the run is for (a book, an account, a portfolio)",
    "scope": "a wider grouping of units (an entity, a region)",
}
REQUIRED_ROLES = ("date", "run_id", "status")
# Grouping roles, in the order a pattern is reported: an upstream system first
# (the widest likely cause), a single unit last (the narrowest).
DIMENSIONS = ("system", "source", "process", "scope", "unit")
# The identity of a piece of work across dates (event ids and run ids change).
_KEY_ROLES = ("system", "source", "process", "unit", "scope")

# A shared upstream system/source or process is the CAUSE only when most of its
# runs that day failed — 4 failures out of a feed's 24 runs is not that feed.
_SATURATED = 0.8
# A value is a pattern when it carries at least this share of the failures…
_PATTERN_SHARE = 0.6
# …and at least this many of them (2 failures sharing a value is coincidence).
_PATTERN_MIN = 3
_MAX_LIST = 50
_ERROR_SAMPLE_CHARS = 300
_SIGNATURE_CHARS = 160

_IDENT = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_PROJECT_CHARS = _IDENT | frozenset("-")


class ConfigError(ValueError):
    """SUPPORT_TABLE / SUPPORT_COLUMNS cannot be used as given."""


# ----- configuration ------------------------------------------------------------

def _names(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def _is_column(name: str) -> bool:
    return bool(name) and not name[0].isdigit() and all(c in _IDENT for c in name)


def parse_table(raw: str) -> str:
    """'project.dataset.table' (each part optionally backticked) → the quoted
    reference the query uses. Every part is checked character by character —
    the reference is written into the SQL, so nothing but BigQuery's own
    identifier characters may reach it."""
    text = (raw or "").replace("`", "").strip()
    parts = text.split(".")
    if len(parts) != 3 or not all(parts):
        raise ConfigError("SUPPORT_TABLE must be 'project.dataset.table'.")
    project, dataset, table = parts
    if not all(c in _PROJECT_CHARS for c in project):
        raise ConfigError("SUPPORT_TABLE: the project id has characters BigQuery does not allow.")
    if not all(c in _IDENT for c in dataset):
        raise ConfigError("SUPPORT_TABLE: the dataset id has characters BigQuery does not allow.")
    if not all(c in _PROJECT_CHARS for c in table):
        raise ConfigError("SUPPORT_TABLE: the table name has characters this tool does not accept.")
    return f"`{project}.{dataset}.{table}`"


def parse_columns(raw: str) -> tuple[dict[str, str], dict[str, str]]:
    """'role=column[:Label],…' → ({role: column}, {role: label}). Unknown roles,
    a column named twice for one role, or a column name that is not a plain
    identifier are refused; the three required roles must be there."""
    columns: dict[str, str] = {}
    labels: dict[str, str] = {}
    for item in _names(raw):
        role, eq, rest = item.partition("=")
        role = role.strip()
        column, _, label = rest.partition(":")
        column = column.strip()
        if not eq or role not in ROLES:
            raise ConfigError(f"SUPPORT_COLUMNS: unknown role {role!r} — use one of {', '.join(ROLES)}.")
        if role in columns:
            raise ConfigError(f"SUPPORT_COLUMNS: role {role!r} is mapped twice.")
        if not _is_column(column):
            raise ConfigError(f"SUPPORT_COLUMNS: {role}= needs a plain column name (letters, digits, underscore).")
        columns[role] = column
        labels[role] = label.strip() or column
    missing = [r for r in REQUIRED_ROLES if r not in columns]
    if missing:
        raise ConfigError(f"SUPPORT_COLUMNS: map the required role(s) {', '.join(missing)}.")
    return columns, labels


def enabled() -> bool:
    return bool(settings.support_table.strip())


def _disabled() -> dict[str, Any]:
    return {"ok": False, "disabled": True,
            "error": "Support triage is disabled (SUPPORT_TABLE unset)."}


# ----- the queries ---------------------------------------------------------------
# BigQuery does the counting; the portal gets back counts and PROBLEM rows only,
# so a date with a million successful runs costs the portal what one with a
# thousand does. Every statement reads only the dates and columns it needs —
# error text and details for the business date alone — and is written WITHOUT
# a CTE: bq_guard does not resolve CTE names and would dry-run the statement.
#
# A run's latest row (by updated_at) is its state; an ITEM is a piece of work —
# its event (retries share one) or else its run — judged by its latest run.

_FAILURE_CAP = 5000     # failed items read in full; counts stay exact beyond it
_HISTORY_CAP = 50000
_SECTIONS_OPTIONAL = ("values", "stuck", "retries", "missing", "volume", "slowest", "history")


class Query:
    """One statement: the SQL plus its parameters, built from the column map."""

    def __init__(self, table_ref: str, columns: dict[str, str]):
        self.t = table_ref
        self.c = columns

    # -- building blocks --------------------------------------------------------
    def _has(self, role: str) -> bool:
        return role in self.c

    def key_expr(self, prefix: str = "") -> str:
        """The identity of a piece of work across dates, as one string: the
        grouping columns joined with a unit separator. Built in SQL every time,
        so a key read back always matches a key passed in."""
        parts = [f"IFNULL(CAST({prefix}{self.c[r]} AS STRING), '')" for r in _KEY_ROLES if self._has(r)]
        return f"CONCAT({', CHR(31), '.join(parts)})" if parts else "''"

    def _runs(self, where: str, roles: tuple[str, ...]) -> str:
        """Latest row of every run matching ``where``, columns under role names."""
        base = ("date", "run_id", "status", "updated_at", "event_id")
        wanted = [r for r in dict.fromkeys(base + roles) if self._has(r)]
        select = ", ".join(f"{self.c[r]} AS r_{r}" for r in wanted)
        sql = f"SELECT {select}, {self.key_expr()} AS r_key FROM {self.t} WHERE {where}"
        if self._has("updated_at"):
            sql += (f" QUALIFY ROW_NUMBER() OVER (PARTITION BY {self.c['date']}, {self.c['run_id']} "
                    f"ORDER BY {self.c['updated_at']} DESC) = 1")
        return sql

    def items(self, where: str, roles: tuple[str, ...] = ()) -> str:
        """Latest run of every item, with its state, its attempts that day and
        how many of them failed."""
        item = "COALESCE(r_event_id, r_run_id)" if self._has("event_id") else "r_run_id"
        order = "r_updated_at DESC" if self._has("updated_at") else "r_run_id DESC"
        status = "UPPER(TRIM(r_status))"
        state = (f"CASE WHEN {status} IN UNNEST(@failed) THEN 'failed' "
                 f"WHEN {status} IN UNNEST(@done) THEN 'done' ELSE 'open' END")
        window = f"PARTITION BY r_date, {item}"
        return (f"SELECT *, {item} AS r_item, {state} AS r_state, COUNT(*) OVER ({window}) AS r_attempts, "
                f"COUNTIF({status} IN UNNEST(@failed)) OVER ({window}) AS r_failed_attempts "
                f"FROM ({self._runs(where, roles)}) WHERE TRUE "
                f"QUALIFY ROW_NUMBER() OVER ({window} ORDER BY {order}) = 1")

    def _on(self, param: str) -> str:
        return f"{self.c['date']} = @{param}"

    # -- the statements ---------------------------------------------------------
    def dates(self) -> str:
        """Which dates in the window have rows at all (the date column only)."""
        d = self.c["date"]
        return f"SELECT {d} AS d FROM {self.t} WHERE {d} BETWEEN @start AND @end GROUP BY d ORDER BY d"

    def counts(self) -> str:
        return (f"SELECT r_state AS state, COUNT(*) AS n, "
                f"COUNTIF(r_attempts > 1 AND r_failed_attempts > 0) AS retried_after_failure "
                f"FROM ({self.items(self._on('cob'))}) GROUP BY state")

    def failures(self) -> str:
        roles = tuple(r for r in ("job_id", "error", "details") + DIMENSIONS if self._has(r))
        return (f"SELECT * FROM ({self.items(self._on('cob'), roles)}) "
                f"WHERE r_state = 'failed' LIMIT {_FAILURE_CAP}")

    def values(self) -> str | None:
        """Items per value of each low-cardinality grouping column: how much of
        a feed or a process failed, not just how many of its runs."""
        dims = [r for r in ("system", "source", "process", "scope") if self._has(r)]
        if not dims:
            return None
        pairs = ", ".join(f"STRUCT('{r}' AS role, IFNULL(CAST(r_{r} AS STRING), '') AS value)" for r in dims)
        return (f"SELECT p.role AS role, p.value AS value, COUNT(*) AS n "
                f"FROM ({self.items(self._on('cob'), tuple(dims))}), UNNEST([{pairs}]) AS p "
                f"GROUP BY role, value LIMIT 5000")

    def stuck(self) -> str:
        roles = tuple(r for r in ("job_id",) + DIMENSIONS if self._has(r))
        old = " AND (r_updated_at IS NULL OR r_updated_at < @stuck_before)" if self._has("updated_at") else ""
        order = " ORDER BY r_updated_at" if self._has("updated_at") else ""
        return (f"SELECT *, COUNT(*) OVER () AS r_total FROM ({self.items(self._on('cob'), roles)}) "
                f"WHERE r_state = 'open'{old}{order} LIMIT {_MAX_LIST}")

    def retries(self) -> str:
        roles = tuple(r for r in DIMENSIONS if self._has(r))
        return (f"SELECT * FROM ({self.items(self._on('cob'), roles)}) "
                f"WHERE r_attempts > 1 ORDER BY r_attempts DESC LIMIT {_MAX_LIST}")

    def missing(self) -> str | None:
        """Work on the previous date with no row at all on this one."""
        if not any(self._has(r) for r in _KEY_ROLES):
            return None
        roles = tuple(r for r in _KEY_ROLES if self._has(r))
        return (f"SELECT *, COUNT(*) OVER () AS r_total FROM ({self.items(self._on('prev'), roles)}) "
                f"WHERE r_key NOT IN (SELECT DISTINCT {self.key_expr()} FROM {self.t} WHERE {self._on('cob')}) "
                f"ORDER BY r_key LIMIT {_MAX_LIST}")

    def volume(self) -> str | None:
        """Succeeded with far less output than the same work on the previous date."""
        if not self._has("members") or not any(self._has(r) for r in _KEY_ROLES):
            return None
        roles = tuple(r for r in ("members",) + DIMENSIONS if self._has(r))
        now = "SAFE_CAST(d.r_members AS FLOAT64)"
        before = "SAFE_CAST(p.r_members AS FLOAT64)"
        order = "p.r_updated_at DESC" if self._has("updated_at") else "p.r_run_id DESC"
        return (f"SELECT d.*, p.r_members AS r_previous_members "
                f"FROM ({self.items(self._on('cob'), roles)}) AS d "
                f"JOIN ({self.items(self._on('prev'), ('members',))}) AS p ON d.r_key = p.r_key "
                f"WHERE d.r_state = 'done' AND (({now} = 0 AND {before} > 0) OR ({before} >= 10 AND {now} < 0.5 * {before})) "
                f"QUALIFY ROW_NUMBER() OVER (PARTITION BY d.r_item ORDER BY {order}) = 1 "
                f"ORDER BY {now} - {before} LIMIT {_MAX_LIST}")

    def slowest(self) -> str | None:
        if not (self._has("event_at") and self._has("updated_at")):
            return None
        roles = tuple(r for r in ("event_at",) + DIMENSIONS if self._has(r))
        return (f"SELECT *, TIMESTAMP_DIFF(r_updated_at, r_event_at, MINUTE) AS r_minutes "
                f"FROM ({self.items(self._on('cob'), roles)}) "
                f"WHERE r_state = 'done' AND r_event_at IS NOT NULL ORDER BY r_minutes DESC, r_run_id LIMIT 5")

    def history(self) -> str | None:
        """The state of ONLY the work failing on the business date, on every
        date of the window — BigQuery picks the failing keys itself, so this
        runs alongside the other statements instead of after them."""
        if not any(self._has(r) for r in _KEY_ROLES):
            return None
        return (f"SELECT r_key, r_date, r_state, r_updated_at FROM "
                f"({self.items(self.c['date'] + ' BETWEEN @start AND @cob')}) "
                f"WHERE r_key IN (SELECT r_key FROM ({self.items(self._on('cob'))}) WHERE r_state = 'failed') "
                f"LIMIT {_HISTORY_CAP}")

    def statements(self) -> dict[str, str]:
        """Every statement by name; the ones a column map cannot support are left out."""
        out = {"dates": self.dates(), "counts": self.counts(), "failures": self.failures(),
               "stuck": self.stuck(), "retries": self.retries()}
        for name in ("values", "missing", "volume", "slowest", "history"):
            sql = getattr(self, name)()
            if sql:
                out[name] = sql
        return out


class _SharedBudget(bq_guard.Budget):
    """The guard's per-run cap, safe across the statements running side by side."""

    def __init__(self, max_queries: int) -> None:
        super().__init__(max_queries)
        self._lock = threading.Lock()

    def reserve(self) -> None:
        with self._lock:
            super().reserve()

    def record(self, bytes_processed: int) -> None:
        with self._lock:
            super().record(bytes_processed)


_client_lock = threading.Lock()
_client = None


def _project() -> str:
    return settings.support_project or settings.bq_project or settings.gcp_project


def _get_client():
    """Lazy, cached BigQuery client — the seam tests monkeypatch."""
    global _client
    with _client_lock:
        if _client is None:
            from google.cloud import bigquery

            _client = bigquery.Client(project=_project() or None)
        return _client


def _param(name: str, value: Any):
    from google.cloud import bigquery

    if isinstance(value, list):
        return bigquery.ArrayQueryParameter(name, "STRING", [str(v) for v in value])
    if isinstance(value, datetime):
        return bigquery.ScalarQueryParameter(name, "TIMESTAMP", value)
    if isinstance(value, date):
        return bigquery.ScalarQueryParameter(name, "DATE", value)
    return bigquery.ScalarQueryParameter(name, "STRING", str(value))


def _run(table_ref: str, sql: str, params: dict[str, Any], budget: bq_guard.Budget) -> list[dict]:
    """One statement through the guard → role-keyed rows (r_ prefix dropped).
    Only the parameters the statement names are sent — BigQuery refuses unused ones."""
    from google.cloud import bigquery

    used = [_param(k, v) for k, v in params.items() if f"@{k}" in sql]
    cfg = bigquery.QueryJobConfig(query_parameters=used)
    job = bq_guard.run(_get_client(), sql, budget=budget, job_config=cfg,
                       allowed_tables=(table_ref,), label="support_triage")
    return [{(k[2:] if k.startswith("r_") else k): v for k, v in dict(r).items()} for r in job.result()]


def _gather(table_ref: str, columns: dict[str, str], wanted: date | None, now: datetime) -> dict[str, Any]:
    """Run the statements — all but the first side by side — and return
    their rows by name, plus the dates, bytes and any section that failed.
    Raises only when the dates, the counts or the failures cannot be read."""
    q = Query(table_ref, columns)
    sql = q.statements()
    budget = _SharedBudget(max_queries=len(sql) + 1)
    end = wanted or now.date()
    start = end - timedelta(days=max(1, int(settings.support_lookback_days)))
    params: dict[str, Any] = {"start": start, "end": end,
                              "failed": [s.upper() for s in _names(settings.support_failed_statuses)],
                              "done": [s.upper() for s in _names(settings.support_done_statuses)],
                              "stuck_before": now - timedelta(minutes=int(settings.support_stuck_minutes))}
    dates = sorted(d for d in (_to_date(r.get("d")) for r in _run(table_ref, sql["dates"], params, budget)) if d)
    cob = wanted or (dates[-1] if dates else None)
    out: dict[str, Any] = {"dates": dates, "cob": cob, "previous": None, "rows": {}, "errors": {},
                           "window": (start, end), "budget": budget}
    if cob is None or cob not in dates:
        return out
    earlier = [d for d in dates if d < cob]
    out["previous"] = earlier[-1] if earlier else None
    params.update(cob=cob, prev=out["previous"] or cob)
    names = [n for n in ("counts", "failures", "values", "stuck", "retries", "missing", "volume", "slowest",
                         "history")
             if n in sql and not (n in ("missing", "volume") and out["previous"] is None)]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=len(names)) as pool:
        futures = {n: pool.submit(_run, table_ref, sql[n], params, budget) for n in names}
        for n, f in futures.items():
            try:
                out["rows"][n] = f.result()
            except Exception as e:  # an optional section degrades to a note
                if n not in _SECTIONS_OPTIONAL:
                    raise
                out["errors"][n] = f"{type(e).__name__}: {str(e)[:200]}"
    return out


# ----- value helpers ------------------------------------------------------------

def _to_date(v: Any) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10]) if v not in (None, "") else None
    except ValueError:
        return None


def _to_dt(v: Any) -> datetime | None:
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=timezone.utc)
    if v in (None, ""):
        return None
    try:
        d = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def _to_int(v: Any) -> int | None:
    if v in (None, ""):
        return None
    try:
        return int(float(str(v).strip()))
    except ValueError:
        return None


def _text(v: Any) -> str:
    return "" if v is None else str(v).strip()


def signature(message: str) -> str:
    """The error with its changing parts taken out, so 40 runs of one failure
    group together: the first line, every word containing a digit (dates,
    ids, counts, file names with dates in them) replaced by '#'."""
    line = _text(message).splitlines()[0] if _text(message) else ""
    if not line:
        return "(no error message)"
    words = ["#" if any(c.isdigit() for c in w) else w for w in line.split()]
    out: list[str] = []
    for w in words:   # one '#' for a run of them
        if not (w == "#" and out and out[-1] == "#"):
            out.append(w)
    return " ".join(out)[:_SIGNATURE_CHARS]


def _detail_values(raw: Any, keys: list[str]) -> dict[str, str]:
    """The configured top-level keys of an error-details JSON object — never
    the whole object (stack traces stay out of the report)."""
    if not keys or raw in (None, ""):
        return {}
    try:
        obj = raw if isinstance(raw, dict) else json.loads(str(raw))
    except ValueError:
        return {}
    if not isinstance(obj, dict):
        return {}
    return {k: _text(obj.get(k))[:80] for k in keys if _text(obj.get(k))}


# ----- the report (pure) -------------------------------------------------------------

def _dims_of(row: dict[str, Any], dims: list[str]) -> dict[str, str]:
    return {r: (_text(row.get(r)) or "(blank)") for r in dims}


def _iso(v: date | datetime | None) -> str | None:
    return v.isoformat() if v else None


def _history_by_key(rows: list[dict[str, Any]]) -> dict[str, dict[date, str]]:
    """{key: {date: state}} — on a date with several items for one key, the
    one written last decides."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    best: dict[tuple[str, date], tuple[datetime, str]] = {}
    for r in rows:
        k, d = r.get("key"), _to_date(r.get("date"))
        if k is None or d is None:
            continue
        at = _to_dt(r.get("updated_at")) or floor
        if (k, d) not in best or at >= best[(k, d)][0]:
            best[(k, d)] = (at, _text(r.get("state")))
    out: dict[str, dict[date, str]] = {}
    for (k, d), (_, state) in best.items():
        out.setdefault(k, {})[d] = state
    return out


def _streak(seen: dict[date, str], cob: date) -> tuple[int, str | None]:
    """(dates failing in a row up to and including ``cob``, the last earlier
    date it succeeded). Dates the work did not run (weekends) are skipped."""
    streak, last_ok, counting = 0, None, True
    for d in sorted((d for d in seen if d <= cob), reverse=True):
        if counting and seen[d] == "failed":
            streak += 1
            continue
        counting = False
        if seen[d] == "done":
            last_ok = d
            break
    return streak, _iso(last_ok)


def build_report(*, roles: list[str], cob: date | None, previous: date | None, dates: list[date],
                 rows: dict[str, list[dict[str, Any]]], now: datetime,
                 detail_keys: list[str] | None = None, errors: dict[str, str] | None = None) -> dict[str, Any]:
    """PURE. The query results (rows by statement name, role-keyed) → the triage
    report: counts, patterns, error groups with a category, failures with their
    streaks, stuck, missing, retries, volume drops, slowest. See triage()."""
    dims = [r for r in DIMENSIONS if r in roles]
    has_keys = any(r in roles for r in _KEY_ROLES)
    detail_keys = detail_keys or []
    notes: list[str] = [f"The {name} check could not run ({err})." for name, err in (errors or {}).items()]
    empty_counts = {"items": 0, "failed": 0, "done": 0, "open": 0, "stuck": 0, "missing": 0, "recovered": 0}
    base = {"business_date": _iso(cob), "dates_read": [d.isoformat() for d in dates],
            "previous_date": _iso(previous)}
    if cob is None or cob not in dates:
        return {**base, "previous_date": None, "empty": True, "counts": empty_counts, "patterns": [],
                "errors": [], "failures": [], "stuck": [], "missing": [], "retries": [], "volume": [],
                "slowest": [], "notes": notes}
    if "event_id" not in roles:
        notes.append("No event_id column mapped: a retry counts as a separate run.")
    if not has_keys:
        notes.append("No system/source/process/unit/scope column mapped: "
                     "recurrence and missing work cannot be worked out.")
    if "updated_at" not in roles:
        notes.append("No updated_at column mapped: every unfinished run is reported as stuck.")

    by_state = {_text(r.get("state")): r for r in rows.get("counts", [])}
    def n(state: str, field: str = "n") -> int:
        return int((by_state.get(state) or {}).get(field) or 0)
    failed_total = n("failed")

    failures = []
    for r in rows.get("failures", []):
        failures.append({"run_id": _text(r.get("run_id")), "status": _text(r.get("status")),
                         "key": r.get("key"), "dims": _dims_of(r, dims), "job_id": _text(r.get("job_id")) or None,
                         "error": _text(r.get("error")), "details": _detail_values(r.get("details"), detail_keys),
                         "attempts": int(r.get("attempts") or 1)})
    if len(failures) < failed_total:
        notes.append(f"{failed_total} runs failed; the first {len(failures)} were read in full "
                     "(errors and patterns are worked out from those).")

    history = _history_by_key(rows.get("history", []))
    def hist(f: dict[str, Any]) -> tuple[int | None, str | None]:
        if not has_keys or f["key"] not in history:
            return (None, None) if not has_keys else (1, None)
        return _streak(history[f["key"]], cob)

    # how much of each feed/process failed: items per value (from BigQuery)
    runs_with: dict[str, Counter] = {r: Counter() for r in dims}
    for r in rows.get("values", []):
        role = _text(r.get("role"))
        if role in runs_with:
            runs_with[role][_text(r.get("value")) or "(blank)"] = int(r.get("n") or 0)
    failed_with = {r: Counter(f["dims"][r] for f in failures) for r in dims}

    def saturated(role: str, value: str) -> bool:
        total = runs_with[role][value]
        return bool(total) and failed_with[role][value] / total >= _SATURATED

    clusters: dict[str, list[dict]] = {}
    for f in failures:
        clusters.setdefault(signature(f["error"]), []).append(f)
    errs = []
    for sig, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        spread = {r: Counter(m["dims"][r] for m in members) for r in dims}
        shared = {r: next(iter(c)) for r, c in spread.items() if len(c) == 1}
        units = len(spread["unit"]) if "unit" in spread else len(members)
        upstream = next((r for r in ("system", "source") if r in shared and saturated(r, shared[r])), None)
        if upstream and units >= 2:
            category, cause = "upstream", upstream
        elif "process" in shared and units >= 2:
            # every unit of the process, or a subset sharing one error: either way
            # the process (or its input for that subset), not a feed outage
            category, cause = "process", "process"
        elif "unit" in spread and units == 1:
            category, cause = "unit", "unit"
        elif len(members) == 1:
            category, cause = "single", None
        elif units >= 2 and ("system" in shared or "source" in shared):
            category, cause = "upstream", "system" if "system" in shared else "source"
        else:
            category, cause = "spread", None
        hists = [hist(m) for m in members] if has_keys else []
        recurring = sum(1 for streak, _ in hists if (streak or 0) > 1)
        last_oks = [ok for _, ok in hists if ok]
        details = Counter(f"{k}={v}" for m in members for k, v in m["details"].items())
        errs.append({
            "signature": sig,
            "sample": (members[0]["error"] or "")[:_ERROR_SAMPLE_CHARS],
            "count": len(members),
            "category": category,
            "cause": {cause: shared[cause]} if cause and cause in shared else {},
            "shared": shared,
            "spread": {r: len(c) for r, c in spread.items()},
            "top": {r: c.most_common(3) for r, c in spread.items() if len(c) > 1},
            "values": {r: sorted(c)[:200] for r, c in spread.items()},
            "details": details.most_common(3),
            "recurring": recurring,
            "new": len(members) - recurring if has_keys else None,
            "job_ids": [m["job_id"] for m in members if m["job_id"]][:3],
            "max_streak": max((s or 0 for s, _ in hists), default=None),
            "last_success": max(last_oks) if last_oks else None,
        })

    patterns = []
    for r in dims:
        if not failures:
            break
        value, count = failed_with[r].most_common(1)[0]
        if count >= _PATTERN_MIN and count / len(failures) >= _PATTERN_SHARE:
            patterns.append({"role": r, "value": value, "failed": count, "of_failed": len(failures),
                             "runs_with_value": runs_with[r][value] or None})

    sig_index = {e["signature"]: i for i, e in enumerate(errs)}
    failure_rows = []
    for f in failures:
        streak, last_ok = hist(f)
        failure_rows.append({"run_id": f["run_id"], "status": f["status"], "dims": f["dims"],
                             "job_id": f["job_id"], "error_index": sig_index[signature(f["error"])],
                             "streak": streak, "last_success": last_ok, "attempts": f["attempts"]})
    failure_rows.sort(key=lambda x: (-(x["streak"] or 0), x["error_index"]))

    stuck_rows = rows.get("stuck", [])
    stuck = []
    for r in stuck_rows:
        at = _to_dt(r.get("updated_at"))
        stuck.append({"run_id": _text(r.get("run_id")), "status": _text(r.get("status")),
                      "dims": _dims_of(r, dims), "job_id": _text(r.get("job_id")) or None,
                      "minutes_since_update": int((now - at).total_seconds() // 60) if at else None})
    stuck_total = int(stuck_rows[0].get("total") or len(stuck_rows)) if stuck_rows else 0

    missing_rows = rows.get("missing", [])
    key_dims = [r for r in _KEY_ROLES if r in roles]
    missing = [{"dims": {r: (_text(m.get(r)) or "(blank)") for r in key_dims},
                "previous_status": _text(m.get("status"))} for m in missing_rows]
    missing_total = int(missing_rows[0].get("total") or len(missing_rows)) if missing_rows else 0

    retries = [{"item": _text(r.get("item")), "attempts": int(r.get("attempts") or 0),
                "final_status": _text(r.get("status")), "final_state": _text(r.get("state")),
                "dims": _dims_of(r, dims)} for r in rows.get("retries", [])]

    volume = []
    for r in rows.get("volume", []):
        cur, before = _to_int(r.get("members")), _to_int(r.get("previous_members"))
        volume.append({"run_id": _text(r.get("run_id")), "dims": _dims_of(r, dims),
                       "members": cur, "previous_members": before})

    slowest = [{"run_id": _text(r.get("run_id")), "dims": _dims_of(r, dims), "minutes": int(r.get("minutes") or 0)}
               for r in rows.get("slowest", [])]

    counts = {"items": sum(int(r.get("n") or 0) for r in by_state.values()),
              "failed": failed_total, "done": n("done"), "open": n("open"),
              "stuck": stuck_total, "missing": missing_total,
              "recovered": n("done", "retried_after_failure")}
    return {**base, "empty": False, "counts": counts, "patterns": patterns, "errors": errs,
            "failures": failure_rows[:_MAX_LIST], "stuck": stuck, "missing": missing,
            "retries": retries, "volume": volume, "slowest": slowest, "notes": notes}


# ----- the L1 layer: what to DO about each problem ---------------------------------
# An L1 engineer does not want analytics; they want, per problem: how urgent,
# is it a known issue, what to do now (wait / re-trigger / check / escalate),
# and to whom it goes — with a ticket note ready to paste. The runbook and the
# owners are the team's own (private config); without them each category has a
# sensible built-in action. Everything here is a SUGGESTION for a person: the
# portal never re-runs, restarts or changes a job.

ACTIONS = ("wait", "retrigger", "check", "escalate")
_FALLBACK_OWNER = "L2 support"
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def parse_owners(raw: str) -> dict[str, str]:
    """'role:value=Team,…,default=Team' → {'role:value': team, 'default': team}."""
    owners: dict[str, str] = {}
    for item in _names(raw):
        left, eq, team = item.partition("=")
        if eq and left.strip() and team.strip():
            owners[left.strip()] = team.strip()
    return owners


def load_runbook(path: str) -> tuple[list[dict[str, Any]], str | None]:
    """The runbook file → (entries, problem). Each entry: match (text, or a list
    of texts that must ALL appear in the error, case-insensitive), title,
    action (one of ACTIONS), steps (list), optional escalate_to, optional
    category and when ({role: value}) to narrow it. Bad entries are skipped."""
    if not path:
        return [], None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [], f"The runbook file could not be read ({type(e).__name__})."
    entries = data.get("entries") if isinstance(data, dict) else data
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        match = e.get("match")
        texts = [match] if isinstance(match, str) else match if isinstance(match, list) else []
        texts = [str(t).lower() for t in texts if str(t).strip()]
        if not texts or not _text(e.get("title")):
            continue
        action = _text(e.get("action")).lower()
        out.append({"match": texts, "title": _text(e.get("title")),
                    "action": action if action in ACTIONS else "check",
                    "steps": [_text(x) for x in e.get("steps") or [] if _text(x)],
                    "escalate_to": _text(e.get("escalate_to")) or None,
                    "category": _text(e.get("category")) or None,
                    "when": {str(k): str(v) for k, v in (e.get("when") or {}).items()}})
    return out, None


def _runbook_match(err: dict[str, Any], runbook: list[dict[str, Any]]) -> dict[str, Any] | None:
    haystack = f"{err.get('signature') or ''} {err.get('sample') or ''}".lower()
    for entry in runbook:
        if entry["category"] and entry["category"] != err.get("category"):
            continue
        if any(err.get("shared", {}).get(r) != v for r, v in entry["when"].items()):
            continue
        if all(t in haystack for t in entry["match"]):
            return entry
    return None


def _owner(shared: dict[str, str], owners: dict[str, str], cause: dict[str, str] | None = None) -> str:
    """The cause's owner first (the feed of an upstream failure, the process of
    a process one), then any other shared value, then the default."""
    for role, value in (cause or {}).items():
        if f"{role}:{value}" in owners:
            return owners[f"{role}:{value}"]
    for role in DIMENSIONS:
        if role in shared and f"{role}:{shared[role]}" in owners:
            return owners[f"{role}:{shared[role]}"]
    return owners.get("default", _FALLBACK_OWNER)


def _named(shared: dict[str, str], labels: dict[str, str]) -> str:
    """"system SYS-A · process REPORT-B" — what a group of runs has in common."""
    return " · ".join(f"{labels.get(r, r)} {v}" for r, v in shared.items())


def _builtin_action(err: dict[str, Any], labels: dict[str, str], owner: str) -> tuple[str, list[str]]:
    shared = err.get("shared") or {}
    cat = err.get("category")
    def one(role: str) -> str:
        return f"{labels.get(role, role)} {shared.get(role, '')}".strip()
    if cat == "upstream":
        who = one("system") if "system" in shared else one("source")
        return "wait", [f"Check that {who} delivered its data for this date.",
                        "Do not re-run before it has landed — the same error will repeat.",
                        "Once it has landed, re-trigger the failed runs.",
                        f"Not landed by your cut-off: escalate to {owner}."]
    if cat == "process":
        n_units = (err.get("spread") or {}).get("unit")
        who = (f"{n_units} {labels.get('unit', 'unit')} values of {one('process')} fail"
               if n_units else f"Runs of {one('process')} fail")
        return "escalate", [f"{who} with the same error: likely a change or defect in that process "
                            "(or its input for them), not one unit's data.",
                            "Check what was deployed for it recently (the release history).",
                            f"Escalate to {owner} with the note; re-running will not help."]
    if cat == "unit":
        steps = [f"Only {one('unit')} fails: check that unit's input and static data."]
        if (err.get("max_streak") or 0) > 1:
            return "escalate", steps + [f"It has failed {err['max_streak']} dates in a row: "
                                        f"escalate to {owner} rather than retry."]
        return "check", steps + [f"Input looks right: re-trigger once, then escalate to {owner}."]
    if cat == "single":
        if (err.get("max_streak") or 0) > 1:
            return "escalate", [f"This run has failed {err['max_streak']} dates in a row — "
                                "a retry is unlikely to help.", f"Escalate to {owner}."]
        return "retrigger", ["One run failed: re-trigger it once.",
                             f"Fails again with the same error: escalate to {owner}."]
    return "escalate", ["No shared system, process or unit: this points at the platform "
                        "(quota, permissions, capacity).", f"Escalate to {owner}."]


def parse_critical(raw: str) -> set[tuple[str, str]]:
    """'role:value,…' → {(role, value)} — work that is always high priority
    (a regulatory report, a key entity). Unknown roles are ignored."""
    out = set()
    for item in _names(raw):
        role, sep, value = item.partition(":")
        if sep and role.strip() in DIMENSIONS and value.strip():
            out.add((role.strip(), value.strip()))
    return out


def cutoff_for(business_date: date | None, hhmm: str, tz: str, days_after: int) -> tuple[datetime | None, str | None]:
    """The deadline for a business date's work: ``hhmm`` in ``tz``, ``days_after``
    days after the date (06:00 the next morning, say). (None, problem) when unset
    or unusable."""
    if not hhmm.strip() or business_date is None:
        return None, None
    h, sep, m = hhmm.strip().partition(":")
    try:
        hour, minute = int(h), int(m) if sep else 0
        day = business_date + timedelta(days=int(days_after))
        at = datetime(day.year, day.month, day.day, hour, minute)
    except ValueError:
        return None, f"SUPPORT_CUTOFF {hhmm!r} is not HH:MM — no cut-off applied."
    if (tz or "UTC").upper() == "UTC":
        return at.replace(tzinfo=timezone.utc), None   # needs no time-zone data
    try:
        from zoneinfo import ZoneInfo

        return at.replace(tzinfo=ZoneInfo(tz)), None
    except (ValueError, KeyError, OSError):
        # an unknown name, or an image without time-zone data (/usr/share/zoneinfo)
        return None, (f"SUPPORT_TIMEZONE {tz!r} is unknown here (no time-zone data in the image?) — "
                      "no cut-off applied; give SUPPORT_CUTOFF in UTC with SUPPORT_TIMEZONE=UTC instead.")


class PriorityRules:
    """How urgent an incident is. Built-in defaults; a deployment tunes them
    (SUPPORT_HIGH_COUNT, SUPPORT_MEDIUM_COUNT, SUPPORT_CRITICAL, SUPPORT_CUTOFF…)."""

    def __init__(self, *, high_count: int = 10, medium_count: int = 3,
                 critical: set[tuple[str, str]] | None = None, cutoff: datetime | None = None,
                 warn_minutes: int = 120, now: datetime | None = None):
        self.high_count = max(1, int(high_count))
        self.medium_count = max(1, int(medium_count))
        self.critical = critical or set()
        self.cutoff = cutoff
        self.warn = timedelta(minutes=max(0, int(warn_minutes)))
        self.now = now or datetime.now(timezone.utc)

    def cutoff_state(self) -> tuple[bool, str | None]:
        """(does the cut-off raise priority now, how to say it). It counts from
        ``warn`` before the deadline until a day after it — an older date is
        history, not urgency."""
        if self.cutoff is None:
            return False, None
        delta = self.cutoff - self.now
        mins = int(abs(delta.total_seconds()) // 60)
        span = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
        when = self.cutoff.strftime("%H:%M")
        if delta.total_seconds() >= 0:
            return delta <= self.warn, f"cut-off {when} in {span}"
        return -delta <= timedelta(hours=24), f"cut-off {when} passed {span} ago"


_UP = {"low": "medium", "medium": "high", "high": "high"}


def _runs(n: int) -> str:
    return f"{n} run" if n == 1 else f"{n} runs"


def _critical_hits(values: dict[str, Any], rules: PriorityRules) -> list[str]:
    hits = []
    for role, value in sorted(rules.critical):
        have = values.get(role)
        if have is not None and (value in have if isinstance(have, (list, set, tuple)) else value == have):
            hits.append(f"{role} {value}")
    return hits


def _priority(kind: str, count: int, err: dict[str, Any] | None, rules: PriorityRules,
              values: dict[str, Any]) -> tuple[str, str]:
    """(priority, why) — the first rule that applies decides; the cut-off then
    raises an unfinished problem one level as the deadline nears."""
    hits = _critical_hits(values, rules)
    streak = (err or {}).get("max_streak") or 0
    if hits:
        level, why = "high", "critical: " + ", ".join(hits)
    elif kind == "volume":
        level, why = "high", "output collapsed on a run that succeeded"
    elif count >= rules.high_count:
        level, why = "high", f"{_runs(count)} (≥ {rules.high_count})"
    elif err and err.get("category") in ("process", "spread", "upstream") and count >= rules.medium_count:
        cause = {"upstream": "one upstream system", "process": "one process",
                 "spread": "the same error everywhere (platform)"}[err["category"]]
        level, why = "high", f"{_runs(count)}, {cause}"
    elif kind in ("stuck", "missing"):
        level, why = "medium", f"{kind} work will not finish on its own"
    elif count >= rules.medium_count:
        level, why = "medium", f"{_runs(count)} (≥ {rules.medium_count})"
    elif streak > 1:
        level, why = "medium", f"failing {streak} dates in a row"
    else:
        level, why = "low", "one run, first time" if count == 1 else _runs(count)
    raise_it, said = rules.cutoff_state()
    if raise_it and kind in ("error", "stuck", "missing"):
        if level != "high":
            level = _UP[level]
        why += f"; {said}"
    return level, why


def _note(inc: dict[str, Any], report: dict[str, Any], *, with_error_text: bool) -> str:
    """The ticket note: what L1 saw, what it checked, who it goes to."""
    head = f"[Support triage] {report.get('date_label', 'Business date')} {report.get('business_date')}: {inc['title']}"
    lines = [head, f"Priority: {inc['priority']} ({inc['priority_reason']}) · Action: {inc['action']}"]
    lines += [f"- {fact}" for fact in inc["facts"]]
    if with_error_text and inc.get("error_text"):
        lines.append(f"- Error: {inc['error_text']}")
    if inc.get("job_ids"):
        lines.append(f"- Jobs: {', '.join(inc['job_ids'])}")
    lines.append(f"- Known issue: {inc['runbook'] or 'no runbook match'}")
    lines.append("- Next steps: " + " ".join(inc["steps"]))
    lines.append(f"- Suggested owner: {inc['owner']}")
    return "\n".join(lines)


def _row_values(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    vals: dict[str, set[str]] = {}
    for r in rows:
        for role, v in (r.get("dims") or {}).items():
            vals.setdefault(role, set()).add(v)
    return {k: sorted(v) for k, v in vals.items()}


def incidents(report: dict[str, Any], *, labels: dict[str, str], runbook: list[dict[str, Any]],
              owners: dict[str, str], rules: PriorityRules | None = None) -> list[dict[str, Any]]:
    """PURE. The report's problems as L1 incidents, most urgent first."""
    rules = rules or PriorityRules()
    out: list[dict[str, Any]] = []
    unit_label = labels.get("unit", "unit")
    for i, err in enumerate(report.get("errors") or []):
        owner = _owner(err.get("shared") or {}, owners, err.get("cause"))
        known = _runbook_match(err, runbook)
        if known:
            action, steps = known["action"], known["steps"] or ["Follow the runbook entry."]
            owner = known["escalate_to"] or owner
        else:
            action, steps = _builtin_action(err, labels, owner)
        shared = _named(err.get("shared") or {}, labels)
        facts = [f"{err['count']} run(s) failed" + (f", all {shared}" if shared else "") + "."]
        if err.get("spread", {}).get("unit", 0) > 1:
            facts.append(f"{err['spread']['unit']} different {unit_label} values affected.")
        if err.get("recurring"):
            facts.append(f"{err['recurring']} of them failed on earlier dates too"
                         + (f" (up to {err['max_streak']} in a row)" if err.get("max_streak") else "")
                         + (f"; last success {err['last_success']}." if err.get("last_success")
                            else "; no success in the dates read."))
        elif err.get("new") is not None:
            facts.append("New today: these succeeded on the previous date they ran.")
        cause = _named(err.get("cause") or {}, labels)
        out.append({"id": f"error-{i + 1}", "kind": "error", "error_index": i,
                    "title": f"{err['count']} failed · {err['category']}" + (f" · {cause}" if cause else ""),
                    "category": err["category"], "count": err["count"], "facts": facts,
                    "error_text": err.get("sample") or err.get("signature"),
                    "job_ids": err.get("job_ids") or [], "runbook": known["title"] if known else None,
                    "action": action, "steps": steps, "owner": owner,
                    "_rank": ("error", err["count"], err, err.get("values") or err.get("shared") or {})})
    stuck = report.get("stuck") or []
    if stuck:
        no_job = sum(1 for s in stuck if not s.get("job_id"))
        owner = owners.get("default", _FALLBACK_OWNER)
        out.append({"id": "stuck", "kind": "stuck", "title": f"{len(stuck)} stuck", "category": "stuck",
                    "count": len(stuck),
                    "facts": [f"{len(stuck)} run(s) neither finished nor failed, with no update for a while.",
                              f"{no_job} of them never logged a job id." if no_job else "All of them logged a job id."],
                    "job_ids": [s["job_id"] for s in stuck if s.get("job_id")][:3], "runbook": None,
                    "action": "check",
                    "steps": ["Open the job of each stuck run.",
                              "No job id: the job never started — re-trigger once.",
                              f"Still running far beyond normal, or fails again: escalate to {owner}."],
                    "owner": owner, "_rank": ("stuck", len(stuck), None, _row_values(stuck))})
    missing = report.get("missing") or []
    if missing:
        owner = owners.get("default", _FALLBACK_OWNER)
        out.append({"id": "missing", "kind": "missing", "title": f"{len(missing)} missing", "category": "missing",
                    "count": len(missing),
                    "facts": [f"{len(missing)} piece(s) of work ran on {report.get('previous_date')} "
                              "but have no run at all for this date."],
                    "job_ids": [], "runbook": None, "action": "check",
                    "steps": ["Check that the triggering event arrived (upstream, scheduler).",
                              "Confirm it was expected for this date (holiday, decommissioned).",
                              f"Expected and not triggered: escalate to {owner}."],
                    "owner": owner, "_rank": ("missing", len(missing), None, _row_values(missing))})
    volume = report.get("volume") or []
    if volume:
        owner = owners.get("default", _FALLBACK_OWNER)
        out.append({"id": "volume", "kind": "volume", "title": f"{len(volume)} low output", "category": "volume",
                    "count": len(volume),
                    "facts": [f"{len(volume)} run(s) succeeded with far less output than on "
                              f"{report.get('previous_date')} (or none at all)."],
                    "job_ids": [], "runbook": None, "action": "escalate",
                    "steps": ["Tell the data owner before downstream consumers use it.",
                              "Re-run once the source data is complete.", f"Escalate to {owner}."],
                    "owner": owner, "_rank": ("volume", len(volume), None, _row_values(volume))})
    for inc in out:
        kind, count, err, values = inc.pop("_rank")
        inc["priority"], inc["priority_reason"] = _priority(kind, count, err, rules, values)
    out.sort(key=lambda x: (_PRIORITY_ORDER[x["priority"]], -x["count"]))
    for inc in out:
        inc["note"] = _note(inc, report, with_error_text=True)
        inc["note_without_error"] = _note(inc, report, with_error_text=False)
    return out


# ----- entry points -------------------------------------------------------------

def _permission_hint(error: str) -> str | None:
    low = error.lower()
    if "access denied" in low or "permission" in low or "403" in low:
        return ("This account needs BigQuery Data Viewer on the control table and BigQuery "
                "Job User on the project the query runs in (SUPPORT_PROJECT).")
    return None


def triage(business_date: str = "") -> dict[str, Any]:
    """Read the control table for ``business_date`` (ISO; empty = the latest
    date in the table up to today) plus the lookback window before it, and
    analyse it. Errors come back as {"ok": False, "error": …} — never raised."""
    if not enabled():
        return _disabled()
    try:
        table_ref = parse_table(settings.support_table)
        columns, labels = parse_columns(settings.support_columns)
    except ConfigError as e:
        return {"ok": False, "error": str(e)}
    wanted = None
    if business_date.strip():
        wanted = _to_date(business_date.strip())
        if wanted is None:
            return {"ok": False, "error": f"{business_date!r} is not a date (use YYYY-MM-DD)."}
    now = datetime.now(timezone.utc)
    try:
        got = _gather(table_ref, columns, wanted, now)
    except Exception as e:  # BigQuery's own errors carry the useful text
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        out = {"ok": False, "error": msg}
        hint = _permission_hint(msg)
        if hint:
            out["hint"] = hint
        return out
    report = build_report(roles=list(columns), cob=got["cob"], previous=got["previous"], dates=got["dates"],
                          rows=got["rows"], now=now, detail_keys=_names(settings.support_detail_keys),
                          errors=got["errors"])
    start, end = got["window"]
    out = {"ok": True, **report,
           "date_label": settings.support_date_label,
           "labels": {r: labels[r] for r in DIMENSIONS if r in labels},
           "window": {"start": start.isoformat(), "end": end.isoformat()},
           "bytes_processed": got["budget"].bytes,
           "queries": got["budget"].queries,
           "scanned_at": now.replace(microsecond=0).isoformat(),
           "job_url": settings.support_job_url}
    runbook, problem = load_runbook(settings.support_runbook_file)
    if problem:
        out["notes"].append(problem)
    out["runbook_entries"] = len(runbook)
    cutoff, cut_problem = cutoff_for(got["cob"], settings.support_cutoff, settings.support_timezone,
                                     settings.support_cutoff_days_after)
    if cut_problem:
        out["notes"].append(cut_problem)
    rules = PriorityRules(high_count=settings.support_high_count, medium_count=settings.support_medium_count,
                          critical=parse_critical(settings.support_critical), cutoff=cutoff,
                          warn_minutes=settings.support_cutoff_warn_minutes, now=now)
    if cutoff is not None:
        out["cutoff"] = {"at": cutoff.isoformat(), "timezone": settings.support_timezone or "UTC",
                         "minutes_left": int((cutoff - now).total_seconds() // 60),
                         "said": rules.cutoff_state()[1]}
    out["incidents"] = incidents(out, labels=out["labels"], runbook=runbook,
                                 owners=parse_owners(settings.support_owners), rules=rules)
    return out


def for_model(report: dict[str, Any]) -> dict[str, Any]:
    """What the chat model may see. Without SUPPORT_ERRORS_TO_MODEL the error
    text is the team's data and stays out: errors become "error #n" with their
    counts, categories and spread — the card still shows the text."""
    if not report.get("ok"):
        return report
    out = dict(report)
    if settings.support_errors_to_model:
        out["incidents"] = [{k: v for k, v in inc.items() if k != "note_without_error"}
                            for inc in report.get("incidents") or []]
        return out
    out["errors"] = [{**e, "signature": f"error #{i + 1}", "sample": None}
                     for i, e in enumerate(report.get("errors") or [])]
    out["incidents"] = [{**{k: v for k, v in inc.items() if k not in ("note_without_error", "error_text")},
                         "note": inc["note_without_error"]}
                        for inc in report.get("incidents") or []]
    out["error_text"] = "withheld (SUPPORT_ERRORS_TO_MODEL is off) — the Support triage card shows it"
    out.pop("job_url", None)
    return out
