"""The portal's event log: notifications, Auto-triage findings, deployment tracking.

ONE append-only table, `portal_events` (AGENTS.md invariant 3 applies as it does
to the release log): INSERT only, and "read", "dismissed", "cleared" are NEW rows.
Every read is "the latest row per (subject, subject_version) wins". The table is
FLAT and typed — an envelope, the human-readable fields, the shared fields and
each domain's own columns; `details` is JSON text for long overflow only, never a
field a query filters on. bigquery/portal_events.schema.json is the single source
of truth (also consumed by the terraform module); `_SCHEMA` below mirrors it and a
test fails when they differ. Add a column to both together, additive nullable only.

Where rows live: PORTAL_EVENTS_DATASET set = BigQuery (streaming inserts, the
event_id as insertId so a retried insert is not doubled); empty = a per-process
memory store, so the features run without any table. The memory store is lost on
restart and not shared between replicas, which is why the chart says to keep one
replica until a dataset is set.

Nothing here raises: a BigQuery outage, a missing table or a bad row comes back
as {"ok": False, "error": ...}. Telemetry and notifications must never block or
break the work they report on.
"""
from __future__ import annotations

import datetime as _dt
import json
import logging
import math
import threading
import uuid
from typing import Any

from ..config import settings

logger = logging.getLogger(__name__)

# Mirrors bigquery/portal_events.schema.json, in the same order.
_SCHEMA: list[tuple[str, str]] = [
    ("event_id", "STRING"),
    ("event_ts", "TIMESTAMP"),
    ("domain", "STRING"),
    ("event_type", "STRING"),
    ("subject", "STRING"),
    ("subject_version", "STRING"),
    ("actor", "STRING"),
    ("recipient", "STRING"),
    ("scope", "STRING"),
    ("status", "STRING"),
    ("severity", "STRING"),
    ("title", "STRING"),
    ("body", "STRING"),
    ("link", "STRING"),
    ("repo", "STRING"),
    ("pr_number", "INT64"),
    ("environment", "STRING"),
    ("jira", "STRING"),
    ("model", "STRING"),
    ("model_calls", "INT64"),
    ("seconds", "FLOAT64"),
    # release / deploy
    ("prl1_only", "BOOL"),
    ("df_only", "BOOL"),
    ("build_verified", "BOOL"),
    ("build_run_url", "STRING"),
    ("target_envs", "STRING"),
    ("allowed_failures", "STRING"),
    ("note", "STRING"),
    ("run_id", "INT64"),
    ("run_url", "STRING"),
    ("workflow", "STRING"),
    ("conclusion", "STRING"),
    ("merge_sha", "STRING"),
    ("chg", "STRING"),
    ("rmg", "STRING"),
    ("chg_state", "STRING"),
    ("rmg_state", "STRING"),
    # watch (also: a notification's kind rides in `category`)
    ("category", "STRING"),
    ("count", "INT64"),
    ("owner", "STRING"),
    ("runbook", "STRING"),
    ("cause", "STRING"),
    ("established", "BOOL"),
    ("action", "STRING"),
    ("action_why", "STRING"),
    ("fallback", "BOOL"),
    ("notified", "BOOL"),
    ("business_date", "DATE"),
    ("incident_id", "STRING"),
    ("details", "STRING"),
]
_TYPES = dict(_SCHEMA)

# Free text arrives from browsers and models and lives in a shared table.
_DEFAULT_CAP = 2000
_CAPS = {"body": 4000, "details": 30000, "event_id": 100, "domain": 50, "event_type": 50}

# Readers filter on these only — they decide WHICH rows exist, so a "latest wins"
# view never hides a newer row behind a filter on what that row says.
_LATEST_FILTERS = ("recipient", "subject", "subject_version")
# `history` may also narrow by what a row says: nothing derives state from it.
_HISTORY_FILTERS = _LATEST_FILTERS + ("scope", "event_type", "status", "actor")

_MAX_DAYS = 400
_LATEST_LIMIT = 500
_HISTORY_LIMIT = 1000
_MAX_BYTES_BILLED = 2**30
_LABELS = {"release_copilot": "portal_events"}
# The memory store is a stand-in for a table, not an archive: past this many rows
# the oldest are let go so a long-running pod cannot grow without bound.
_MEMORY_MAX_ROWS = 5000

_client_lock = threading.Lock()
_client = None
_memory_lock = threading.Lock()
_memory: list[dict[str, Any]] = []


# ----- where rows live -------------------------------------------------------------

def enabled() -> bool:
    """True when a BigQuery dataset is configured; False = the memory store."""
    return bool(settings.portal_events_dataset)


def store_name() -> str:
    """"bigquery" | "memory" — the cards say where findings are kept."""
    return "bigquery" if enabled() else "memory"


def _project() -> str:
    return settings.portal_events_project or settings.bq_project or settings.gcp_project


def _table_id() -> str:
    parts = [_project(), settings.portal_events_dataset, settings.portal_events_table]
    return ".".join(p for p in parts if p)


def _get_client():
    """Lazy, cached BigQuery client — the seam tests monkeypatch."""
    global _client
    with _client_lock:
        if _client is None:
            from google.cloud import bigquery

            _client = bigquery.Client(project=_project() or None)
        return _client


def _error(e: Exception) -> dict[str, Any]:
    msg = f"{type(e).__name__}: {str(e)[:300]}"
    out: dict[str, Any] = {"ok": False, "error": msg}
    low = msg.lower()
    if "403" in low or "access denied" in low or "permission" in low or "forbidden" in low:
        out["hint"] = (f"The service account needs roles/bigquery.dataEditor on dataset "
                       f"{settings.portal_events_dataset} (to insert and read) and "
                       "roles/bigquery.jobUser on the project the query runs in.")
    elif "404" in low or "not found" in low:
        out["hint"] = (f"Create {_table_id()} from bigquery/portal_events.schema.json "
                       "(terraform: portal_events_table, or bq mk --table).")
    return out


def clear_memory() -> None:
    """Empty the memory store (tests; there is no other reason to forget)."""
    with _memory_lock:
        _memory.clear()


# ----- turning a caller's dict into a row ------------------------------------------

def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _parse_ts(value: Any) -> _dt.datetime:
    if isinstance(value, _dt.datetime):
        ts = value
    else:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        ts = _dt.datetime.fromisoformat(text)
    return ts if ts.tzinfo else ts.replace(tzinfo=_dt.timezone.utc)


def _coerce(name: str, kind: str, value: Any) -> Any:
    """The value in its column's JSON form, or None (= leave the column NULL).
    Raises ValueError for a value the column cannot hold — _prepare reports it."""
    if value is None:
        return None
    if kind == "STRING":
        if isinstance(value, (dict, list)):
            value = json.dumps(value, separators=(",", ":"), default=str)
        text = str(value).strip()[: _CAPS.get(name, _DEFAULT_CAP)]
        return text or None
    if kind == "TIMESTAMP":
        return _parse_ts(value).astimezone(_dt.timezone.utc).isoformat()
    if kind == "DATE":
        if isinstance(value, _dt.datetime):
            return value.date().isoformat()
        if isinstance(value, _dt.date):
            return value.isoformat()
        return _dt.date.fromisoformat(str(value).strip()[:10]).isoformat()
    if kind == "BOOL":
        if not isinstance(value, bool):
            raise ValueError("needs true or false")
        return value
    if isinstance(value, bool) or (isinstance(value, str) and not value.strip()):
        raise ValueError("needs a number")
    if kind == "INT64":
        return int(value)
    number = float(value)
    if not math.isfinite(number):   # NaN and inf are not JSON
        raise ValueError("needs a finite number")
    return number


def _prepare(row: Any) -> tuple[dict[str, Any], str]:
    """(the row as stored, "") or ({}, why-not). event_id and event_ts are filled
    when absent; domain, event_type and subject are required, because every
    reader keys on them."""
    if not isinstance(row, dict):
        return {}, "a row must be a dict"
    unknown = sorted(set(row) - set(_TYPES))
    if unknown:
        return {}, f"unknown column(s) {', '.join(unknown)} - add them to portal_events.schema.json and _SCHEMA first"
    out: dict[str, Any] = {}
    for name, kind in _SCHEMA:
        try:
            value = _coerce(name, kind, row.get(name))
        except (ValueError, TypeError, OverflowError) as e:
            return {}, f"{name}: {e}"
        if value is not None:
            out[name] = value
    for required in ("domain", "event_type", "subject"):
        if not out.get(required):
            return {}, f"{required} is required"
    out.setdefault("event_id", uuid.uuid4().hex)
    out.setdefault("event_ts", _now().isoformat())
    return out, ""


# ----- write -----------------------------------------------------------------------

def record(row: dict) -> dict:
    """Append one row. {"ok": True, "event_id"} or {"ok": False, "error"} — never
    raises. Fills event_id and event_ts; nothing here updates or deletes."""
    out = record_many([row])
    if not out.get("ok"):
        return out
    return {"ok": True, "event_id": out["event_ids"][0]}


def record_many(rows: list[dict]) -> dict:
    """Append several rows in ONE insert; all or none are accepted by validation
    (a bad row writes nothing). {"ok": True, "recorded", "event_ids"} or
    {"ok": False, "error"} — never raises."""
    try:
        prepared = []
        for i, row in enumerate(rows or []):
            clean, why = _prepare(row)
            if why:
                return {"ok": False, "error": f"row {i}: {why}"}
            prepared.append(clean)
        if not prepared:
            return {"ok": True, "recorded": 0, "event_ids": []}
        ids = [r["event_id"] for r in prepared]
        if enabled():
            # row_ids give best-effort dedup if a network retry repeats the insert.
            errors = _get_client().insert_rows_json(_table_id(), prepared, row_ids=ids)
            if errors:
                return {"ok": False, "error": f"{_table_id()} insert failed: {str(errors)[:300]}"}
        else:
            _remember(prepared)
        return {"ok": True, "recorded": len(prepared), "event_ids": ids}
    except Exception as e:  # noqa: BLE001 — the table missing, a 403, the network
        logger.warning("portal_events write failed: %s", e)
        return _error(e)


def _remember(rows: list[dict[str, Any]]) -> None:
    with _memory_lock:
        known = {r["event_id"] for r in _memory}
        for row in rows:
            if row["event_id"] not in known:   # the same insertId twice is one row
                _memory.append(dict(row))
                known.add(row["event_id"])
        if len(_memory) > _MEMORY_MAX_ROWS:
            del _memory[: len(_memory) - _MEMORY_MAX_ROWS]


# ----- read ------------------------------------------------------------------------

def _days(since_days: Any) -> int:
    try:
        return max(1, min(int(since_days), _MAX_DAYS))
    except (TypeError, ValueError):
        return 14


def _filters(allowed: tuple[str, ...], given: dict[str, Any]) -> tuple[dict[str, str], str]:
    unknown = sorted(set(given) - set(allowed))
    if unknown:
        return {}, f"cannot filter on {', '.join(unknown)} (use {', '.join(allowed)})"
    return {name: str(given.get(name) or "").strip() for name in allowed}, ""


def _public(row: dict[str, Any]) -> dict[str, Any]:
    """A stored or fetched row as plain JSON: no NULL columns, times as ISO text."""
    out = {}
    for k, v in row.items():
        if v is None:
            continue
        out[k] = v.isoformat() if isinstance(v, (_dt.datetime, _dt.date)) else v
    return out


def latest(domain: str, *, since_days: int = 14, recipient: str = "", subject: str = "",
           subject_version: str = "") -> dict:
    """The newest row per (subject, subject_version) of ``domain`` written within
    ``since_days``, newest first, optionally only for one recipient / subject /
    version. {"ok": True, "rows": [...]} or {"ok": False, "error"} — never raises."""
    want, why = _filters(_LATEST_FILTERS, {"recipient": recipient, "subject": subject,
                                           "subject_version": subject_version})
    return _read(domain, since_days, want, why, latest_only=True)


def history(domain: str, *, since_days: int = 14, **filters: str) -> dict:
    """Every row of ``domain`` within ``since_days``, newest first. ``filters``
    narrow by recipient, subject, subject_version, scope, event_type, status or
    actor (equal, case-sensitive). Same result shape as ``latest``."""
    want, why = _filters(_HISTORY_FILTERS, filters)
    return _read(domain, since_days, want, why, latest_only=False)


def _read(domain: str, since_days: Any, want: dict[str, str], why: str, *, latest_only: bool) -> dict:
    if why:
        return {"ok": False, "error": why}
    domain = str(domain or "").strip()
    if not domain:
        return {"ok": False, "error": "domain is required"}
    days = _days(since_days)
    try:
        rows = _read_bigquery(domain, days, want, latest_only) if enabled() \
            else _read_memory(domain, days, want, latest_only)
    except Exception as e:  # noqa: BLE001
        logger.warning("portal_events read failed: %s", e)
        return _error(e)
    return {"ok": True, "rows": rows}


def _read_memory(domain: str, days: int, want: dict[str, str], latest_only: bool) -> list[dict[str, Any]]:
    cutoff = _now() - _dt.timedelta(days=days)
    with _memory_lock:
        snapshot = list(enumerate(_memory))
    # Oldest first, the later append winning a tie — the order BigQuery reads back.
    stamped = sorted(((_parse_ts(r["event_ts"]), i, r) for i, r in snapshot), key=lambda t: (t[0], t[1]))
    keep: dict[Any, tuple[_dt.datetime, int, dict[str, Any]]] = {}
    for ts, i, row in stamped:
        if row["domain"] != domain or ts < cutoff:
            continue
        if any(v and row.get(k, "") != v for k, v in want.items()):
            continue
        keep[(row["subject"], row.get("subject_version", "")) if latest_only else i] = (ts, i, row)
    ordered = sorted(keep.values(), key=lambda t: (t[0], t[1]), reverse=True)
    return [_public(t[2]) for t in ordered[: _LATEST_LIMIT if latest_only else _HISTORY_LIMIT]]


# Filters are bound parameters, never text: an empty one matches everything, so
# the statement is fixed and only the table id is interpolated into it.
_WHERE = (
    "domain = @domain AND event_ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)"
    " AND (@recipient = '' OR IFNULL(recipient, '') = @recipient)"
    " AND (@subject = '' OR IFNULL(subject, '') = @subject)"
    " AND (@subject_version = '' OR IFNULL(subject_version, '') = @subject_version)"
    " AND (@scope = '' OR IFNULL(scope, '') = @scope)"
    " AND (@event_type = '' OR IFNULL(event_type, '') = @event_type)"
    " AND (@status = '' OR IFNULL(status, '') = @status)"
    " AND (@actor = '' OR IFNULL(actor, '') = @actor)"
)
_LATEST_SQL = (
    "SELECT * FROM `{table}` WHERE " + _WHERE +
    " QUALIFY ROW_NUMBER() OVER (PARTITION BY subject, IFNULL(subject_version, '')"
    " ORDER BY event_ts DESC, event_id DESC) = 1"
    " ORDER BY event_ts DESC, event_id DESC LIMIT @limit"
)
_HISTORY_SQL = (
    "SELECT * FROM `{table}` WHERE " + _WHERE +
    " ORDER BY event_ts DESC, event_id DESC LIMIT @limit"
)


def _read_bigquery(domain: str, days: int, want: dict[str, str], latest_only: bool) -> list[dict[str, Any]]:
    from google.cloud import bigquery

    params = [bigquery.ScalarQueryParameter("domain", "STRING", domain),
              bigquery.ScalarQueryParameter("days", "INT64", days),
              bigquery.ScalarQueryParameter("limit", "INT64", _LATEST_LIMIT if latest_only else _HISTORY_LIMIT)]
    params += [bigquery.ScalarQueryParameter(name, "STRING", want.get(name, "")) for name in _HISTORY_FILTERS]
    sql = (_LATEST_SQL if latest_only else _HISTORY_SQL).format(table=_table_id())
    cfg = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=_MAX_BYTES_BILLED,
                                  labels=dict(_LABELS))
    job = _get_client().query(sql, job_config=cfg, location=settings.bq_location or None)
    return [_public(dict(r)) for r in job.result()]
