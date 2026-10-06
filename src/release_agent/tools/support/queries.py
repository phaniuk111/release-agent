"""The statements support triage issues, and the live check of the mapping.

Every statement goes through tools/bq_guard.py with the ONE configured table
allowed — never anything else, never a CTE (the guard would dry-run it).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from typing import Any

from ...config import settings
from .. import bq_guard
from .config import (
    _KEY_ROLES,
    _MAX_LIST,
    DIMENSIONS,
    ConfigError,
    _disabled,
    _names,
    _permission_hint,
    enabled,
    parse_table,
)
from .discover import DEFAULT_DONE, DEFAULT_FAILED
from .report import _to_date

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

    def status_words(self) -> str:
        """The distinct status words of the window — read when the failed/done
        lists are not configured, so they come from the table itself."""
        return (f"SELECT DISTINCT UPPER(TRIM(CAST({self.c['status']} AS STRING))) AS s FROM {self.t} "
                f"WHERE {self.c['date']} BETWEEN @start AND @end LIMIT 200")

    def statements(self) -> dict[str, str]:
        """Every statement by name; the ones a column map cannot support are left out."""
        out = {"dates": self.dates(), "statuses": self.status_words(), "counts": self.counts(),
               "failures": self.failures(), "stuck": self.stuck(), "retries": self.retries()}
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


def _statuses(table_ref: str, sql: str, params: dict[str, Any], budget: bq_guard.Budget,
              errors: dict[str, str]) -> dict[str, Any]:
    """The failed / done words: as configured, or — when neither list was set —
    the defaults plus every word the table really uses, sorted by
    discover.classify_status; a word it cannot place counts as still running."""
    from .discover import classify_status, configured

    failed, done = list(params["failed"]), list(params["done"])
    if configured("support_failed_statuses", "support_done_statuses"):
        return {"failed": failed, "done": done, "running": [], "how": "configured"}
    try:
        words = [str(r.get("s") or "") for r in _run(table_ref, sql, params, budget)]
    except Exception as e:  # the defaults still work; say what was not read
        errors["statuses"] = f"{type(e).__name__}: {str(e)[:200]}"
        return {"failed": failed, "done": done, "running": [], "how": "defaults"}
    running = []
    for w in sorted(w for w in words if w):
        kind = classify_status(w)
        if kind == "failed" and w not in failed:
            failed.append(w)
        elif kind == "done" and w not in done:
            done.append(w)
        elif kind is None and w not in failed and w not in done:
            running.append(w)
    return {"failed": failed, "done": done, "running": running, "how": "discovered"}


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
                              "failed": [s.upper() for s in _names(settings.support_failed_statuses)
                                         or DEFAULT_FAILED],
                              "done": [s.upper() for s in _names(settings.support_done_statuses) or DEFAULT_DONE],
                              "stuck_before": now - timedelta(minutes=int(settings.support_stuck_minutes))}
    dates = sorted(d for d in (_to_date(r.get("d")) for r in _run(table_ref, sql["dates"], params, budget)) if d)
    cob = wanted or (dates[-1] if dates else None)
    out: dict[str, Any] = {"dates": dates, "cob": cob, "previous": None, "rows": {}, "errors": {},
                           "window": (start, end), "budget": budget}
    out["statuses"] = _statuses(table_ref, sql["statuses"], params, budget, out["errors"])
    params.update(failed=out["statuses"]["failed"], done=out["statuses"]["done"])
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


def check_config() -> dict[str, Any]:
    """Compare the configured mapping with the LIVE table — the mistake this
    feature invites is a mapped column the table does not have (a typo, a
    rename), and without this it surfaces as a query error on someone's first
    click. Reads the table's metadata only (free): never a row.

    {"ok", "table", "roles": {role: column}, "problems": [str], "notes": [str],
     "unmapped": [column names]} — never raises."""
    if not enabled():
        return _disabled()
    try:
        from .discover import discovered, mapping

        table_ref = parse_table(settings.support_table)
        columns, _labels, how = mapping()
    except ConfigError as e:
        return {"ok": False, "problems": [str(e)], "notes": [], "unmapped": []}
    out: dict[str, Any] = {"table": table_ref.strip("`"), "roles": dict(columns), "mapping": how,
                           "problems": [], "notes": [], "unmapped": []}
    if how == "discovered":
        found = discovered(table_ref)
        out["notes"].append("SUPPORT_COLUMNS is empty: the columns were matched from the table's schema "
                            "— set SUPPORT_COLUMNS (or the chart's supportTable.columns) to change any of them.")
        if found.get("confirm"):
            out["notes"].append("check these groupings, they decide each failure's category: " + ", ".join(
                f"{r} = {columns[r]}" for r in found["confirm"] if r in columns))
    try:
        table = _get_client().get_table(table_ref.strip("`"))
    except Exception as e:  # BigQuery's own errors carry the useful text
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        return {**out, "ok": False, "problems": [f"The table could not be read: {msg}"],
                "hint": _permission_hint(msg) or ""}
    import difflib

    fields = {f.name.lower(): f for f in table.schema}
    for role, column in columns.items():
        if column.lower() in fields:
            continue
        near = difflib.get_close_matches(column.lower(), list(fields), n=1)
        out["problems"].append(f"role {role} is mapped to column {column!r}, which the table does not have"
                               + (f" — did you mean {fields[near[0]].name!r}?" if near else "."))
    date_field = fields.get(columns["date"].lower())
    if date_field is not None and date_field.field_type != "DATE":
        out["problems"].append(f"the date column {columns['date']!r} is {date_field.field_type}, not DATE — "
                               "the queries filter it with DATE values.")
    partition = getattr(getattr(table, "time_partitioning", None), "field", None)
    if date_field is not None and (partition or "").lower() != columns["date"].lower():
        out["notes"].append("the table is not partitioned on the date column: every read scans the whole table "
                            "instead of a few days.")
    for role in ("updated_at", "event_id"):
        if role not in columns:
            out["notes"].append(f"no {role} column mapped: " + (
                "every unfinished run is reported as stuck." if role == "updated_at"
                else "a retry counts as a separate run."))
    mapped = {c.lower() for c in columns.values()}
    out["unmapped"] = [f.name for f in table.schema if f.name.lower() not in mapped]
    out["ok"] = not out["problems"]
    return out
