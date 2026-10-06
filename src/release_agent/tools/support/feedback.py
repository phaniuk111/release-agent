"""Was the investigation right? — the memory that says whether it helps.

The investigate agent gives one finding per incident. Each one can be rated
"right", "right direction" or "wrong" (with the real cause, in the person's own
words, when it was not right). Those answers are the only evidence that the
agent gets L1 further, and the wrong ones with their real causes are the next
runbook entries.

Storage mirrors bq_cost's findings memory (AGENTS.md invariant 3): ONE
append-only table, INSERT only (streaming inserts — a correction is a NEW row,
never an UPDATE), derived on read, optional. SUPPORT_FEEDBACK_DATASET empty =
off, cleanly; a BigQuery outage or a missing table degrades to an error dict and
never raises — rating an answer must never break the chat that showed it. The
table's columns are bigquery/support_findings.schema.json (single source of
truth, also consumed by the terraform module).

Who answered is the VERIFIED caller the endpoint passes in ("" when identity is
off) — a typed email is a claim, not an identity. The actual cause is stored as
typed (capped), so it is the person's words, not ours.
"""
from __future__ import annotations

import logging
import math
import threading
import uuid
from datetime import date, datetime, timezone
from typing import Any

from ...config import settings

logger = logging.getLogger(__name__)

VERDICTS = ("right", "direction", "wrong")

# Caps, because these are free text from a browser and live in a shared table.
MAX_CAUSE = 500
MAX_TITLE = 200
_MAX_ID = 100
_MAX_SHORT = 100   # category, action, model

# A fixed backstop so a read never scans a table's whole history (a LIMIT does
# not prune partitions): a year and a bit of daily ratings is a few thousand
# rows, and answers older than this no longer say how the agent does now.
_LOOKBACK_DAYS = 400
_MAX_STATS_DAYS = 365
_MAX_BYTES_BILLED = 2**30
_LABELS = {"release_copilot": "support_feedback"}

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


def enabled() -> bool:
    return bool(settings.support_feedback_dataset and _project())


def _disabled() -> dict[str, Any]:
    return {"ok": False, "disabled": True,
            "error": "Feedback is not being kept (SUPPORT_FEEDBACK_DATASET is empty, or no project)."}


def _table_id() -> str:
    return f"{_project()}.{settings.support_feedback_dataset}.{settings.support_feedback_table}"


def _error(e: Exception) -> dict[str, Any]:
    msg = f"{type(e).__name__}: {str(e)[:300]}"
    out: dict[str, Any] = {"ok": False, "error": msg}
    low = msg.lower()
    if "403" in low or "access denied" in low or "permission" in low or "forbidden" in low:
        out["hint"] = (f"The service account needs roles/bigquery.dataEditor on the dataset "
                       f"{_project()}.{settings.support_feedback_dataset} (to insert and read) and "
                       "roles/bigquery.jobUser on the project the query runs in.")
    elif "404" in low or "not found" in low:
        out["hint"] = (f"Create {_table_id()} from bigquery/support_findings.schema.json "
                       "(terraform: support_findings_table, or bq mk --table).")
    return out


def _cap(value: Any, limit: int) -> str:
    return str(value if value is not None else "").strip()[:limit]


def _number(value: Any, kind: type) -> int | float | None:
    """A count or a duration, or None — a bad value is dropped, not an error:
    the rating matters, the timing is context."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        n = kind(value)
    except (TypeError, ValueError):
        return None
    if kind is float and not math.isfinite(n):   # NaN and inf are not JSON
        return None
    return n if n >= 0 else None


def _to_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value).strip()[:10])
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ----- write ---------------------------------------------------------------------

def record(*, business_date: str, incident_id: str, title: str, verdict: str, actual_cause: str = "",
           category: str = "", action: str = "", model: str = "", model_calls: int | None = None,
           seconds: float | None = None, actor: str = "") -> dict[str, Any]:
    """Append one rating. Returns {"ok": True, "recorded": 1} or
    {"ok": False, "error", "hint"?} — never raises. Nothing here updates or
    deletes: rating again appends a row, and the readers take the latest."""
    if not enabled():
        return _disabled()
    verdict = _cap(verdict, 20).lower()
    if verdict not in VERDICTS:
        return {"ok": False, "error": f"verdict must be one of {', '.join(VERDICTS)}."}
    day = _to_date(business_date)
    if day is None:
        return {"ok": False, "error": f"{business_date!r} is not a date (use YYYY-MM-DD)."}
    incident = _cap(incident_id, _MAX_ID)
    if not incident:
        return {"ok": False, "error": "incident_id is required."}
    row = {
        "created_at": _now().isoformat(),
        "business_date": day.isoformat(),
        "incident_id": incident,
        "title": _cap(title, MAX_TITLE),
        "verdict": verdict,
        "actual_cause": _cap(actual_cause, MAX_CAUSE),
        "category": _cap(category, _MAX_SHORT),
        "action": _cap(action, _MAX_SHORT),
        "model": _cap(model, _MAX_SHORT),
        "model_calls": _number(model_calls, int),
        "seconds": _number(seconds, float),
        "actor": _cap(actor, 320),
    }
    try:
        # row_ids give best-effort dedup if a network retry repeats the insert.
        errors = _get_client().insert_rows_json(_table_id(), [row], row_ids=[uuid.uuid4().hex])
    except Exception as e:  # noqa: BLE001 — the table missing, a 403, the network
        return _error(e)
    if errors:
        return {"ok": False, "error": f"{_table_id()} insert failed: {str(errors)[:300]}"}
    return {"ok": True, "recorded": 1}


# ----- read ----------------------------------------------------------------------

# Each person's LATEST answer per incident counts — a person may correct
# themselves, and the table only ever grows, so "latest wins" is decided here, on
# read. Anonymous rows (identity off) share the empty actor, so they collapse to
# one voice per incident: undercounted, never double-counted.
_LATEST = (
    "SELECT * FROM `{table}` "
    "WHERE created_at > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY) {extra}"
    "QUALIFY ROW_NUMBER() OVER (PARTITION BY business_date, incident_id, actor "
    "ORDER BY created_at DESC) = 1"
)

_STATS_SQL = """
WITH latest AS (""" + _LATEST + """)
SELECT
  COUNT(*) AS total,
  COUNTIF(verdict = 'right') AS right_n,
  COUNTIF(verdict = 'direction') AS direction_n,
  COUNTIF(verdict = 'wrong') AS wrong_n,
  APPROX_QUANTILES(seconds, 2)[SAFE_OFFSET(1)] AS median_seconds,
  APPROX_QUANTILES(model_calls, 2)[SAFE_OFFSET(1)] AS median_model_calls,
  ARRAY(SELECT AS STRUCT IFNULL(category, '') AS name, COUNT(*) AS total,
          COUNTIF(verdict = 'right') AS right_n, COUNTIF(verdict = 'direction') AS direction_n,
          COUNTIF(verdict = 'wrong') AS wrong_n
        FROM latest GROUP BY name ORDER BY total DESC, name LIMIT 20) AS by_category,
  ARRAY(SELECT AS STRUCT IFNULL(model, '') AS name, COUNT(*) AS total,
          COUNTIF(verdict = 'right') AS right_n, COUNTIF(verdict = 'direction') AS direction_n,
          COUNTIF(verdict = 'wrong') AS wrong_n
        FROM latest GROUP BY name ORDER BY total DESC, name LIMIT 20) AS by_model,
  ARRAY(SELECT AS STRUCT business_date, incident_id, title, actual_cause, category, created_at
        FROM latest WHERE verdict = 'wrong' ORDER BY created_at DESC LIMIT 10) AS recent_wrong
FROM latest
"""

_FOR_INCIDENT_SQL = (
    "SELECT verdict, actual_cause, created_at FROM (" + _LATEST + ") ORDER BY created_at DESC LIMIT 50"
)


def _query(sql: str, params: list[Any]) -> list[dict[str, Any]]:
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(query_parameters=params, maximum_bytes_billed=_MAX_BYTES_BILLED,
                                  labels=dict(_LABELS))
    job = _get_client().query(sql, job_config=cfg, location=settings.bq_location or None)
    return [dict(r) for r in job.result()]


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _ratio(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def _split(row: dict[str, Any]) -> dict[str, Any]:
    right, direction, wrong = (int(row.get(k) or 0) for k in ("right_n", "direction_n", "wrong_n"))
    total = int(row.get("total") or 0)
    return {"name": row.get("name") or "", "total": total, "right": right, "direction": direction,
            "wrong": wrong, "accuracy": _ratio(right, total),
            "right_or_direction": _ratio(right + direction, total)}


def stats(days: int = 30) -> dict[str, Any]:
    """How the investigations were rated over ``days``: totals per verdict,
    accuracy (right / rated), right-or-direction, the split by category and by
    model, median seconds and model calls, and the 10 newest "wrong" answers with
    the real cause people gave — the runbook candidates. {"ok": False, …} on
    failure; with nothing rated, total is 0 and the ratios are None."""
    if not enabled():
        return _disabled()
    from google.cloud import bigquery

    try:
        days = max(1, min(int(days), _MAX_STATS_DAYS))
    except (TypeError, ValueError):
        days = 30
    sql = _STATS_SQL.format(table=_table_id(), extra="")
    try:
        rows = _query(sql, [bigquery.ScalarQueryParameter("days", "INT64", days)])
    except Exception as e:  # noqa: BLE001
        return _error(e)
    row = rows[0] if rows else {}
    out = _split(row)
    out.pop("name", None)
    out.update({
        "ok": True, "days": days,
        "median_seconds": row.get("median_seconds"),
        "median_model_calls": row.get("median_model_calls"),
        "by_category": [_split(dict(r)) for r in row.get("by_category") or []],
        "by_model": [_split(dict(r)) for r in row.get("by_model") or []],
        "recent_wrong": [
            {"business_date": _iso(r.get("business_date")), "incident_id": r.get("incident_id") or "",
             "title": r.get("title") or "", "actual_cause": r.get("actual_cause") or "",
             "category": r.get("category") or "", "created_at": _iso(r.get("created_at"))}
            for r in (dict(x) for x in row.get("recent_wrong") or [])
        ],
    })
    return out


def for_incident(business_date: str, incident_id: str) -> dict[str, Any]:
    """What people said about this incident before, one answer per person (their
    latest): {"ok", "total", "right", "direction", "wrong", "answers": [{verdict,
    actual_cause, created_at}]}. Who said it is not returned — the UI shows
    "2 people marked this right", and the causes are what is useful."""
    if not enabled():
        return _disabled()
    from google.cloud import bigquery

    day = _to_date(business_date)
    if day is None:
        return {"ok": False, "error": f"{business_date!r} is not a date (use YYYY-MM-DD)."}
    incident = _cap(incident_id, _MAX_ID)
    if not incident:
        return {"ok": False, "error": "incident_id is required."}
    sql = _FOR_INCIDENT_SQL.format(table=_table_id(),
                                   extra="AND business_date = @business_date AND incident_id = @incident_id ")
    params = [bigquery.ScalarQueryParameter("days", "INT64", _LOOKBACK_DAYS),
              bigquery.ScalarQueryParameter("business_date", "DATE", day),
              bigquery.ScalarQueryParameter("incident_id", "STRING", incident)]
    try:
        rows = _query(sql, params)
    except Exception as e:  # noqa: BLE001
        return _error(e)
    counts = {v: sum(1 for r in rows if r.get("verdict") == v) for v in VERDICTS}
    return {"ok": True, "business_date": day.isoformat(), "incident_id": incident,
            "total": sum(counts.values()), **counts,
            "answers": [{"verdict": r.get("verdict"), "actual_cause": r.get("actual_cause") or "",
                         "created_at": _iso(r.get("created_at"))} for r in rows]}
