"""BigQuery cost report — design/BQ_COST.md.

Read-only by construction: every statement goes through bq_guard.run (real
only for INFORMATION_SCHEMA / ``__TABLES__`` reads, dry-run otherwise) with ONE
exception, called out where it happens: reading back this module's OWN
append-only findings table is a plain SELECT on a normal table, which the
guard would (rightly, for any other table) force to a dry run — so that one
read bypasses the guard, hardcoded to that one table name only. No dataViewer
is ever needed — the model sees metadata and numbers, never a row of data.

Two live findings shape the storage/writes sections (see AGENTS.md — verified
against a real sandbox project, not theoretical):

* The REGION-wide views (`region-...`.INFORMATION_SCHEMA.TABLE_STORAGE /
  TABLES / STREAMING_TIMELINE_BY_PROJECT / WRITE_API_TIMELINE_BY_PROJECT) each
  enumerate EVERY dataset in the region — including BigQuery's own hidden
  anonymous cached-result datasets ("_...") that belong to other users. One
  such dataset denies the whole view, for everyone but its owner, in any
  project more than one person has ever queried — the normal case. The
  storage section therefore tries the fast region-wide read once and, on
  denial, falls back to reading each VISIBLE dataset individually (skipping
  "_"-prefixed ones) via the dataset-qualified views plus the legacy
  ``__TABLES__`` meta-table for size. The writes section has no such
  fallback (those two views are region-only) — a denial there is reported as
  ``writes_error``/``writes_hint`` and the section is simply empty.
* Neither STREAMING_TIMELINE_BY_PROJECT nor WRITE_API_TIMELINE_BY_PROJECT
  carries a table identifier (they are aggregated by project + error/stream
  type only) — so a write finding's ``"table"`` is always ``None``; the
  finding is still real and actionable, just project-wide rather than
  per-table.

Config (release_agent.config): bq_cost_project (default: bq_project or
gcp_project), bq_cost_region (INFORMATION_SCHEMA prefix, e.g. "region-us";
EMPTY = feature off), bq_cost_billing ("on-demand" | "reservations"),
bq_cost_days, bq_cost_top, bq_cost_max_queries, bq_cost_usd_per_tib,
bq_cost_dataset (findings memory; empty = off), bq_cost_findings_table,
bq_cost_max_datasets (cap on the per-dataset storage fallback).
"""
from __future__ import annotations

import datetime as _dt
import json
import threading
import uuid
from typing import Any

from ..config import settings
from . import bq_guard

_GIB = 2**30
_TIB = 2**40
# Active (on-demand, US) storage list price — used only for the approximate
# monthly-dollar column on storage findings; not billing-account-accurate.
_STORAGE_USD_PER_GIB_MONTH = 0.02


# ----- BigQuery client --------------------------------------------------------

_client_lock = threading.Lock()
_client = None


def _project() -> str:
    return settings.bq_cost_project or settings.bq_project or settings.gcp_project


def _get_client():
    """Lazy, cached BigQuery client — a thin seam tests monkeypatch."""
    global _client
    with _client_lock:
        if _client is None:
            from google.cloud import bigquery

            _client = bigquery.Client(project=_project())
        return _client


# ----- enablement ------------------------------------------------------------

def enabled() -> bool:
    """True when a project resolves and bq_cost_region is set."""
    return bool(_project() and settings.bq_cost_region)


def _disabled() -> dict[str, Any]:
    return {"ok": False, "disabled": True,
            "error": "BigQuery cost report is disabled (BQ_COST_REGION unset, or no project)."}


# ----- shared helpers ---------------------------------------------------------

def _region_ref(view: str) -> str:
    """`region-...`.INFORMATION_SCHEMA.<view> — backticked because the region
    prefix always contains a hyphen."""
    return f"`{settings.bq_cost_region}`.INFORMATION_SCHEMA.{view}"


def _dataset_ref(project: str, dataset: str, view: str) -> str:
    return f"`{project}.{dataset}`.INFORMATION_SCHEMA.{view}"


def _tables_legacy_ref(project: str, dataset: str) -> str:
    """The legacy meta-table (not INFORMATION_SCHEMA) bq_guard also treats as a
    real-run metadata read — see tools/bq_guard.py's ``_is_metadata_ref``."""
    return f"`{project}.{dataset}.__TABLES__`"


def _int_param(name: str, value: int):
    from google.cloud import bigquery

    return bigquery.ScalarQueryParameter(name, "INT64", int(value))


def _str_param(name: str, value: str):
    from google.cloud import bigquery

    return bigquery.ScalarQueryParameter(name, "STRING", str(value))


def _job_ids_param(job_ids: list[str]):
    from google.cloud import bigquery

    return bigquery.ArrayQueryParameter("job_ids", "STRING", list(job_ids))


def _run_rows(client: Any, sql: str, budget: "bq_guard.Budget | None",
              *, params: list | None = None) -> list[dict[str, Any]]:
    """bq_guard.run + normalise the result into plain dicts. Every caller here
    wants real INFORMATION_SCHEMA/``__TABLES__`` rows back — a caller that wants
    a dry run uses ``dry_run()`` instead, never this.

    Asserted, not just assumed: if a future change to one of this module's SQL
    builders ever stopped its statement classifying as a metadata read,
    bq_guard.run would silently force it to a DRY run rather than refuse
    outright — and on the real client, ``.result()`` on a dry-run job returns
    an EMPTY row iterator, not an error, so the section would just report "no
    findings" with nothing to say why. Checked here, before issuing anything,
    so that failure mode is loud instead.
    """
    from google.cloud import bigquery

    assert bq_guard.classify(sql) == "information_schema", (
        f"_run_rows expects a real INFORMATION_SCHEMA/__TABLES__ read; got: {sql[:200]!r}"
    )
    cfg = bigquery.QueryJobConfig(query_parameters=params) if params else None
    job = bq_guard.run(client, sql, budget=budget, job_config=cfg)
    return [dict(r) for r in job.result()]


def _fmt_ts(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _to_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# Project/dataset characters BigQuery itself allows — letters, digits,
# underscore, dash; a project id may also carry one colon (the legacy
# "domain:project" form). This is what stands between a chat-supplied
# project/dataset and the f-strings in _dataset_ref/_tables_legacy_ref
# (their callers' docstrings explain why those two are unparameterised) —
# anything else, a backtick above all, is refused in _split_table before it
# ever reaches one of those f-strings.
_IDENT_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
)


def _valid_bq_identifier(value: str, *, allow_colon: bool = False) -> bool:
    """PURE: True for a project/dataset id made only of BigQuery's own
    allowed characters. ``allow_colon`` permits exactly one colon splitting
    two otherwise-valid segments (a project's legacy "domain:project" form);
    a dataset id never gets one."""
    if not value:
        return False
    if not allow_colon:
        return all(c in _IDENT_CHARS for c in value)
    segments = value.split(":")
    if len(segments) > 2:
        return False
    return all(seg and all(c in _IDENT_CHARS for c in seg) for seg in segments)


def _split_table(table: str) -> tuple[str, str, str]:
    """'project.dataset.table' or 'dataset.table' (project defaults). Each
    part may be backtick-quoted on its own — BigQuery's console shows both
    ``\\`project\\`.\\`dataset\\`.\\`table\\`` and a single
    ``\\`project.dataset.table\\`` — so backticks are stripped PER PART, never
    just off the two ends of the whole string (that left an interior
    backtick, e.g. from ``\\`proj\\`.\\`ds\\`.\\`tbl\\``, sitting inside the
    'project' part unstripped). project/dataset are then validated against
    BigQuery's own identifier characters — the table NAME is safe because
    every query below binds it as a parameter, but project/dataset are
    f-string-interpolated straight into real-run SQL (_dataset_ref /
    _tables_legacy_ref), so a caller-supplied value with, say, a backtick in
    it must be refused HERE rather than merely happening to fail later
    because this function also requires exactly 2-3 dot-separated parts —
    that requirement is incidental, not a designed defence.
    """
    text = str(table or "").strip()
    if text.startswith("`") and text.endswith("`") and text.count("`") == 2:
        text = text[1:-1]  # one pair of backticks around the WHOLE reference
    parts = [p.strip("`").strip() for p in text.split(".") if p.strip("`").strip()]
    if len(parts) == 3:
        project, dataset, table_name = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        project = _project()
        if not project:
            raise ValueError("no default project configured — pass 'project.dataset.table'.")
        dataset, table_name = parts[0], parts[1]
    else:
        raise ValueError(f"table must be 'dataset.table' or 'project.dataset.table', got {table!r}")
    if not _valid_bq_identifier(project, allow_colon=True):
        raise ValueError(f"invalid project id {project!r} — letters, digits, underscore, dash "
                          "(and one colon) only.")
    if not _valid_bq_identifier(dataset):
        raise ValueError(f"invalid dataset id {dataset!r} — letters, digits, underscore, dash only.")
    return project, dataset, table_name


def _referenced_table_strings(structs: list[dict[str, Any]] | None) -> list[str]:
    out = []
    for t in structs or []:
        p, d, tb = t.get("project_id"), t.get("dataset_id"), t.get("table_id")
        if p and d and tb:
            out.append(f"{p}.{d}.{tb}")
    return out


def _insight_flags(insights: list[dict[str, Any]] | None) -> list[str]:
    flags = set()
    for i in insights or []:
        if i.get("slot_contention"):
            flags.add("slot_contention")
        if i.get("insufficient_shuffle_quota"):
            flags.add("insufficient_shuffle_quota")
    return sorted(flags)


# ----- scan: SQL ---------------------------------------------------------------
# Statements 1-4 always run (JOBS_BY_PROJECT — confirmed reachable with only
# jobUser + resourceViewer). 5 tries the fast region-wide storage read, falling
# back to one statement per visible dataset (capped). 6-7 are the write views
# (region-wide only, no fallback). See the module docstring for why.

def _shapes_sql() -> str:
    return f"""
SELECT
  query_info.query_hashes.normalized_literals AS qhash,
  COUNT(*) AS runs,
  ANY_VALUE(NULLIF(user_email, '')) AS any_user_email,
  COUNT(DISTINCT NULLIF(user_email, '')) AS distinct_users,
  ARRAY_AGG(DISTINCT NULLIF(user_email, '') IGNORE NULLS ORDER BY NULLIF(user_email, '') LIMIT 8) AS users,
  SUM(total_slot_ms) AS total_slot_ms,
  SUM(total_bytes_billed) AS total_bytes_billed,
  APPROX_QUANTILES(total_bytes_processed, 100)[OFFSET(50)] AS p50_bytes_processed,
  ANY_VALUE(SUBSTR(query, 1, 180)) AS sql_preview,
  ANY_VALUE(job_id) AS sample_job_id,
  MAX(creation_time) AS last_run
FROM {_region_ref("JOBS_BY_PROJECT")}
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND state = 'DONE'
  AND job_type = 'QUERY'
  AND error_result IS NULL
  AND cache_hit = FALSE
  AND query_info.query_hashes.normalized_literals IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM UNNEST(labels) AS l WHERE l.key = 'release_copilot')
GROUP BY query_info.query_hashes.normalized_literals
ORDER BY total_slot_ms DESC
LIMIT 1000
"""


def _totals_sql() -> str:
    return f"""
SELECT
  COUNT(*) AS queries,
  COUNTIF(cache_hit) AS cache_hits,
  SUM(total_slot_ms) AS total_slot_ms,
  SUM(total_bytes_billed) AS total_bytes_billed,
  COUNT(DISTINCT NULLIF(user_email, '')) AS distinct_users
FROM {_region_ref("JOBS_BY_PROJECT")}
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND state = 'DONE'
  AND job_type = 'QUERY'
  AND error_result IS NULL
  AND NOT EXISTS (SELECT 1 FROM UNNEST(labels) AS l WHERE l.key = 'release_copilot')
"""


def _shape_detail_sql() -> str:
    """Referenced tables + performance insights for the TOP N shapes' sample
    jobs only — never the whole window."""
    return f"""
SELECT
  job_id,
  referenced_tables,
  query_info.performance_insights.stage_performance_standalone_insights AS stage_insights
FROM {_region_ref("JOBS_BY_PROJECT")}
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND job_id IN UNNEST(@job_ids)
"""


def _table_reads_sql() -> str:
    """How many jobs referenced each table this window — feeds the storage
    section's 'unread' / 'large_unpartitioned' kinds."""
    return f"""
SELECT
  rt.project_id, rt.dataset_id, rt.table_id,
  COUNT(*) AS reads,
  MAX(j.creation_time) AS last_read
FROM {_region_ref("JOBS_BY_PROJECT")} AS j, UNNEST(j.referenced_tables) AS rt
WHERE j.creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND j.job_type = 'QUERY'
  AND j.state = 'DONE'
  AND NOT EXISTS (SELECT 1 FROM UNNEST(j.labels) AS l WHERE l.key = 'release_copilot')
GROUP BY rt.project_id, rt.dataset_id, rt.table_id
"""


def _storage_fast_sql() -> str:
    """The fast, region-wide path — denied in any project with a hidden
    anonymous dataset in the region (see module docstring); falls back to
    ``_storage_per_dataset_sql`` per visible dataset."""
    return f"""
SELECT
  s.table_schema, s.table_name,
  s.total_logical_bytes, s.active_logical_bytes, s.total_physical_bytes,
  s.storage_last_modified_time,
  e.expiration_ts,
  d.ddl
FROM {_region_ref("TABLE_STORAGE")} AS s
LEFT JOIN (
  SELECT table_schema, table_name, option_value AS expiration_ts
  FROM {_region_ref("TABLE_OPTIONS")}
  WHERE option_name = 'expiration_timestamp'
) AS e
  ON s.table_schema = e.table_schema AND s.table_name = e.table_name
LEFT JOIN (
  SELECT table_schema, table_name, ddl
  FROM {_region_ref("TABLES")}
  WHERE table_type = 'BASE TABLE'
) AS d
  ON s.table_schema = d.table_schema AND s.table_name = d.table_name
WHERE s.total_logical_bytes >= @min_bytes
"""


def _storage_per_dataset_sql(project: str, dataset: str) -> str:
    """One dataset's tables: size from the legacy ``__TABLES__`` meta-table
    (works with only dataset-level access, unlike region-wide TABLE_STORAGE),
    expiration from TABLE_OPTIONS, partitioning from COLUMNS — joined here so
    one statement covers a whole dataset."""
    return f"""
SELECT
  t.table_id, t.size_bytes, t.row_count, t.last_modified_time,
  o.expiration_ts, p.partition_col
FROM {_tables_legacy_ref(project, dataset)} AS t
LEFT JOIN (
  SELECT table_name, option_value AS expiration_ts
  FROM {_dataset_ref(project, dataset, "TABLE_OPTIONS")}
  WHERE option_name = 'expiration_timestamp'
) AS o
  ON t.table_id = o.table_name
LEFT JOIN (
  SELECT table_name, ANY_VALUE(column_name) AS partition_col
  FROM {_dataset_ref(project, dataset, "COLUMNS")}
  WHERE is_partitioning_column = 'YES'
  GROUP BY table_name
) AS p
  ON t.table_id = p.table_name
WHERE t.type = 1
"""


def _streaming_sql() -> str:
    """Legacy ``tabledata.insertAll`` — success is ``error_code IS NULL``."""
    return f"""
SELECT
  error_code,
  SUM(total_requests) AS total_requests,
  SUM(total_rows) AS total_rows,
  SUM(total_input_bytes) AS total_input_bytes
FROM {_region_ref("STREAMING_TIMELINE_BY_PROJECT")}
WHERE start_timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
GROUP BY error_code
"""


def _write_api_sql() -> str:
    """Storage Write API — success is ``error_code = 'OK'`` (different from
    STREAMING_TIMELINE's NULL — see the module docstring)."""
    return f"""
SELECT
  error_code,
  SUM(total_requests) AS total_requests,
  SUM(total_rows) AS total_rows,
  SUM(total_input_bytes) AS total_input_bytes
FROM {_region_ref("WRITE_API_TIMELINE_BY_PROJECT")}
WHERE start_timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
GROUP BY error_code
"""


# ----- scan: assembly ----------------------------------------------------------

def _reads_info(table_str: str, reads_by_table: dict[str, dict[str, Any]],
                 reads_known: bool) -> dict[str, Any]:
    """{"reads", "last_read"} for one table. When the table-reads probe
    itself failed (``reads_known`` False), the answer is UNKNOWN — never a
    fabricated zero: a table simply absent from ``reads_by_table`` because
    the query never ran must not look identical to one the query genuinely
    found zero reads for."""
    if not reads_known:
        return {"reads": None, "last_read": None}
    return reads_by_table.get(table_str, {"reads": 0, "last_read": None})


def _storage_entries(table_str: str, gb: float, physical_gb: float, active_gb: float | None,
                      expiration_ts: Any, last_modified: Any, partitioned: bool,
                      reads: int | None, last_read: Any, reads_known: bool) -> list[dict[str, Any]]:
    approx_usd_month = round((active_gb if active_gb is not None else gb) * _STORAGE_USD_PER_GIB_MONTH, 2)
    common = {"table": table_str, "gb": gb, "physical_gb": physical_gb,
              "expiration": _fmt_ts(expiration_ts), "last_modified": _fmt_ts(last_modified),
              "last_read": _fmt_ts(last_read), "reads": reads, "approx_usd_month": approx_usd_month}
    entries = []
    if gb >= 1 and not expiration_ts:
        entries.append({**common, "kind": "no_expiration"})
    # Both kinds below make a claim ABOUT the read count (never read; read
    # often) — a claim this module cannot make when the read count itself is
    # unknown (the table-reads query failed, typically the exact partial
    # grant — jobUser + metadataViewer but no resourceViewer — hint_for()
    # exists to describe). Reporting "unread" from an unknown count is
    # exactly the bug this guards: a permissions gap must never come out the
    # other end as "delete me" advice on every large table.
    if reads_known and gb >= 1 and reads == 0:
        entries.append({**common, "kind": "unread"})
    if reads_known and gb >= 10 and not partitioned and reads >= 5:
        entries.append({**common, "kind": "large_unpartitioned"})
    return entries


def _storage_findings_from_fast(rows: list[dict[str, Any]], reads_by_table: dict[str, dict[str, Any]],
                                 reads_known: bool) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        table_str = f'{_project()}.{r.get("table_schema")}.{r.get("table_name")}'
        gb = round((r.get("total_logical_bytes") or 0) / _GIB, 2)
        if gb < 1:
            continue
        physical_gb = round((r.get("total_physical_bytes") or 0) / _GIB, 2)
        active_gb = (round((r.get("active_logical_bytes") or 0) / _GIB, 2)
                     if r.get("active_logical_bytes") is not None else None)
        partitioned = "PARTITION BY" in (r.get("ddl") or "").upper()
        info = _reads_info(table_str, reads_by_table, reads_known)
        out.extend(_storage_entries(table_str, gb, physical_gb, active_gb, r.get("expiration_ts"),
                                     r.get("storage_last_modified_time"), partitioned,
                                     info["reads"], info["last_read"], reads_known))
    return out


def _storage_findings_from_fallback(rows: list[dict[str, Any]], reads_by_table: dict[str, dict[str, Any]],
                                     reads_known: bool) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        gb = round((r.get("size_bytes") or 0) / _GIB, 2)
        if gb < 1:
            continue
        table_str = f'{_project()}.{r.get("_dataset")}.{r.get("table_id")}'
        partitioned = bool(r.get("partition_col"))
        info = _reads_info(table_str, reads_by_table, reads_known)
        # __TABLES__ has no separate physical-bytes figure; logical size stands
        # in for both — noted, not measured, hence no distinct "physical_gb".
        out.extend(_storage_entries(table_str, gb, gb, gb, r.get("expiration_ts"),
                                     r.get("last_modified_time"), partitioned,
                                     info["reads"], info["last_read"], reads_known))
    return out


def _scan_storage(client: Any, budget: "bq_guard.Budget", project: str,
                   reads_by_table: dict[str, dict[str, Any]], reads_known: bool):
    """-> (storage_list, source, error, hint). Tries the fast region-wide read
    once; on ANY failure (expected — see module docstring) falls back to one
    statement per visible, non-hidden dataset, capped at bq_cost_max_datasets
    and by the shared scan budget. ``reads_known`` is False when the
    table-reads probe itself failed — every entry then carries ``reads: None``
    and neither "unread" nor "large_unpartitioned" is emitted (see
    _storage_entries)."""
    try:
        rows = _run_rows(client, _storage_fast_sql(), budget, params=[_int_param("min_bytes", _GIB)])
        return _storage_findings_from_fast(rows, reads_by_table, reads_known), "table_storage", None, None
    except bq_guard.GuardRefused:
        raise
    except Exception:  # noqa: BLE001 — expected in any shared project, fall back
        pass

    try:
        datasets = [d.dataset_id for d in client.list_datasets(project=project)
                    if not str(d.dataset_id).startswith("_")]
    except Exception as e:  # noqa: BLE001
        return [], None, f"list_datasets: {e}", hint_for(str(e))

    rows: list[dict[str, Any]] = []
    partial = False
    for ds in datasets[: settings.bq_cost_max_datasets]:
        try:
            job = bq_guard.run(client, _storage_per_dataset_sql(project, ds), budget=budget)
        except bq_guard.GuardRefused:
            partial = True
            break
        except Exception:  # noqa: BLE001 — this one dataset denies us; keep going
            continue
        for r in job.result():
            row = dict(r)
            row["_dataset"] = ds
            rows.append(row)
    storage = _storage_findings_from_fallback(rows, reads_by_table, reads_known)
    if partial:
        return (storage, "per_dataset",
                "the query budget ran out partway through the per-dataset storage fallback "
                "— results below are partial",
                "raise BQ_COST_MAX_QUERIES, or lower BQ_COST_MAX_DATASETS, to cover every dataset in one run.")
    return storage, "per_dataset", None, None


def _write_findings(rows: list[dict[str, Any]], *, source: str, ok_code: str | None) -> list[dict[str, Any]]:
    total_requests = sum(int(r.get("total_requests") or 0) for r in rows)
    total_rows = sum(int(r.get("total_rows") or 0) for r in rows)
    total_bytes = sum(int(r.get("total_input_bytes") or 0) for r in rows)
    error_requests = sum(int(r.get("total_requests") or 0) for r in rows if r.get("error_code") != ok_code)
    rows_per_request = round(total_rows / total_requests, 2) if total_requests else 0.0
    # Neither timeline view carries a table identifier (project/error-code
    # aggregated only) — see the module docstring.
    common = {"table": None, "source": source, "requests": total_requests, "rows": total_rows,
              "rows_per_request": rows_per_request, "input_gb": round(total_bytes / _GIB, 2),
              "errors": error_requests}
    out = []
    if error_requests:
        out.append({**common, "kind": "errors"})
    if total_requests >= 10000 and rows_per_request < 5:
        out.append({**common, "kind": "unbatched"})
    return out


def _scan_writes(client: Any, budget: "bq_guard.Budget", days: int):
    """-> (writes_list, error, hint). No per-dataset fallback exists (both
    views are region-only) — a denial just empties the section."""
    streaming_rows, write_rows, error = [], [], None
    try:
        streaming_rows = _run_rows(client, _streaming_sql(), budget, params=[_int_param("days", days)])
    except bq_guard.GuardRefused:
        raise
    except Exception as e:  # noqa: BLE001
        error = str(e)
    try:
        write_rows = _run_rows(client, _write_api_sql(), budget, params=[_int_param("days", days)])
    except bq_guard.GuardRefused:
        raise
    except Exception as e:  # noqa: BLE001
        error = error or str(e)
    writes = _write_findings(streaming_rows, source="streaming", ok_code=None) \
        + _write_findings(write_rows, source="write_api", ok_code="OK")
    if not writes and error:
        return [], error, hint_for(error, region_wide=True)
    return writes, None, None


def _coerce_int(value: Any, default: int) -> int:
    """int(value), or ``default`` when value is falsy or cannot be coerced.

    scan()'s days/top are model-driven (an ADK tool call) — a non-integer
    argument from Gemini (a word, a decimal-looking string, the wrong type
    entirely) must degrade to the configured default, never raise out of a
    function the whole design relies on never raising.
    """
    if not value:
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def scan(days: int | None = None, top: int | None = None) -> dict[str, Any]:
    """The ranked report. Never raises.

    {"ok": True, "project", "region", "days", "billing", "scanned_at",
     "report_cost_bytes", "queries_run",
     "totals": {"queries", "slot_hours", "gb_billed", "cache_hit_pct"},
     "shapes": [{"qhash", "runs", "who", "users": [...] (capped 8), "users_count",
                 "slot_hours", "gb_billed", "approx_usd", "p50_gb", "p50_partitions",
                 "sql_preview", "sample_job", "referenced_tables": [...], "insights": [...],
                 "last_run"}],            # ordered by billing model, top N
     "storage": [{"table", "gb", "physical_gb", "expiration", "last_modified",
                  "last_read", "reads": int | None, "kind": "no_expiration|unread|large_unpartitioned",
                  "approx_usd_month"}],     # "reads" is None, and "unread"/"large_unpartitioned"
                                            # never emitted, on any table when the read count
                                            # itself is unknown — see "storage_reads_error" below
     "storage_source": "table_storage" | "per_dataset" | None,
     "writes":  [{"table", "source": "write_api|streaming", "requests", "rows",
                  "rows_per_request", "input_gb", "errors", "kind": "unbatched|errors"}],
     "history": {"adoption": [...], "adopted", "still_open", "new", "gone"} | None,
     "hint": str | None,                  # e.g. only your own jobs are visible
     "storage_error"/"storage_hint", "writes_error"/"writes_hint": present only
     when that section could not be completed,
     "storage_reads_error"/"storage_reads_hint": present only when the table
     read-COUNTS probe itself failed — the storage section otherwise still
     completed, but every entry's "reads" is None (an unknown count must
     never be reported as a verified zero — see _storage_entries)}
    or {"ok": False, "disabled": True, "error"} / {"ok": False, "error", "hint"}.
    """
    if not enabled():
        return _disabled()
    days = _coerce_int(days, settings.bq_cost_days)
    top = _coerce_int(top, settings.bq_cost_top)
    region = settings.bq_cost_region
    project = _project()
    budget = bq_guard.Budget(settings.bq_cost_max_queries)
    try:
        client = _get_client()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"BigQuery unavailable: {e}", "hint": None}

    try:
        shape_rows = _run_rows(client, _shapes_sql(), budget, params=[_int_param("days", days)])
    except Exception as e:  # noqa: BLE001
        error = str(e)
        return {"ok": False, "error": error, "hint": hint_for(error)}
    try:
        shapes = rank_shapes(shape_rows, settings.bq_cost_billing, top)
    except Exception as e:  # noqa: BLE001 — ranking must not be able to raise out of scan() either
        error = str(e)
        return {"ok": False, "error": error, "hint": hint_for(error)}

    try:
        totals_rows = _run_rows(client, _totals_sql(), budget, params=[_int_param("days", days)])
    except Exception:  # noqa: BLE001 — totals are a nicety; the shapes stand alone
        totals_rows = []
    trow = totals_rows[0] if totals_rows else {}
    tqueries = int(trow.get("queries") or 0)
    totals = {
        "queries": tqueries,
        "slot_hours": round((trow.get("total_slot_ms") or 0) / 1000.0 / 3600.0, 2),
        "gb_billed": round((trow.get("total_bytes_billed") or 0) / _GIB, 2),
        "cache_hit_pct": round(100.0 * (trow.get("cache_hits") or 0) / tqueries, 1) if tqueries else 0.0,
    }
    hint = hint_for("", rows_seen=tqueries, users_seen=int(trow.get("distinct_users") or 0))

    job_ids = [s["sample_job"] for s in shapes if s.get("sample_job")]
    if job_ids:
        try:
            detail_rows = _run_rows(client, _shape_detail_sql(), budget,
                                     params=[_int_param("days", days), _job_ids_param(job_ids)])
            by_job = {r.get("job_id"): r for r in detail_rows}
            for s in shapes:
                d = by_job.get(s.get("sample_job")) or {}
                s["referenced_tables"] = _referenced_table_strings(d.get("referenced_tables"))
                s["insights"] = _insight_flags(d.get("stage_insights"))
        except Exception:  # noqa: BLE001 — the ranking still stands without the extra detail
            pass

    try:
        read_rows = _run_rows(client, _table_reads_sql(), budget, params=[_int_param("days", days)])
        reads_known, reads_error = True, None
    except Exception as e:  # noqa: BLE001 — e.g. exactly the jobUser+metadataViewer-but-not-
        # resourceViewer partial grant hint_for() describes: an unknown read count must never
        # be reported as a verified zero (see _storage_entries) — that turned a permissions gap
        # into "delete me" advice on every large table.
        read_rows, reads_known, reads_error = [], False, str(e)
    reads_by_table: dict[str, dict[str, Any]] = {}
    for r in read_rows:
        key = f'{r.get("project_id")}.{r.get("dataset_id")}.{r.get("table_id")}'
        reads_by_table[key] = {"reads": int(r.get("reads") or 0), "last_read": r.get("last_read")}

    try:
        storage, storage_source, storage_error, storage_hint = _scan_storage(
            client, budget, project, reads_by_table, reads_known)
    except bq_guard.GuardRefused as e:
        storage, storage_source, storage_error = [], None, str(e)
        storage_hint = "the scan's own query budget (BQ_COST_MAX_QUERIES) ran out before storage could be checked."
    except Exception as e:  # noqa: BLE001 — a bug in this section must not fail the whole scan
        storage, storage_source, storage_hint = [], None, None
        storage_error = f"storage section failed: {e}"

    try:
        writes, writes_error, writes_hint = _scan_writes(client, budget, days)
    except bq_guard.GuardRefused as e:
        writes, writes_error = [], str(e)
        writes_hint = "the scan's own query budget (BQ_COST_MAX_QUERIES) ran out before writes could be checked."
    except Exception as e:  # noqa: BLE001 — a bug in this section must not fail the whole scan
        writes, writes_hint = [], None
        writes_error = f"writes section failed: {e}"

    history = None
    if settings.bq_cost_dataset:
        try:
            prior_rows = _read_own_findings_table(None, runs=20, budget=budget)
            adoption_list = adoption(prior_rows, shapes)
            counts = {s: sum(1 for a in adoption_list if a["status"] == s)
                      for s in ("adopted", "still_open", "new", "gone")}
            history = {"adoption": adoption_list, **counts}
        except Exception:  # noqa: BLE001 — best effort, never fails the scan
            history = None

    out: dict[str, Any] = {
        "ok": True,
        "project": project,
        "region": region,
        "days": days,
        "billing": settings.bq_cost_billing,
        "scanned_at": _now_iso(),
        "report_cost_bytes": budget.bytes,
        "queries_run": budget.queries,
        "totals": totals,
        "shapes": shapes,
        "storage": storage,
        "storage_source": storage_source,
        "writes": writes,
        "history": history,
        "hint": hint,
    }
    if storage_error:
        out["storage_error"], out["storage_hint"] = storage_error, storage_hint
    if not reads_known:
        out["storage_reads_error"] = reads_error
        out["storage_reads_hint"] = hint_for(reads_error)
    if writes_error:
        out["writes_error"], out["writes_hint"] = writes_error, writes_hint
    return out


# ----- query_detail ------------------------------------------------------------

def _percentile(values: list[int], pct: float) -> int:
    if not values:
        return 0
    idx = min(len(values) - 1, int(round(pct * (len(values) - 1))))
    return values[idx]


def _percentiles(values: list[int]) -> dict[str, int]:
    values = sorted(values)
    return {"p50": _percentile(values, 0.50), "p95": _percentile(values, 0.95),
            "max": values[-1] if values else 0}


def _percentiles_gb(values_bytes: list[int]) -> dict[str, float]:
    return {k: round(v / _GIB, 2) for k, v in _percentiles(values_bytes).items()}


def _stage_list(stages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for s in stages or []:
        out.append({
            "name": s.get("name"),
            "records_read": s.get("records_read"),
            "records_written": s.get("records_written"),
            "shuffle_output_gb": round((s.get("shuffle_output_bytes") or 0) / _GIB, 2),
            "slot_ms": s.get("slot_ms"),
            "status": s.get("status"),
        })
    return out


def _query_detail_jobs_sql() -> str:
    return f"""
SELECT job_id, user_email, total_bytes_processed, total_slot_ms, creation_time, query
FROM {_region_ref("JOBS_BY_PROJECT")}
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND state = 'DONE'
  AND job_type = 'QUERY'
  AND error_result IS NULL
  AND query_info.query_hashes.normalized_literals = @qhash
ORDER BY creation_time DESC
LIMIT 500
"""


def _query_detail_stage_sql() -> str:
    return f"""
SELECT
  job_stages,
  query_info.performance_insights.stage_performance_standalone_insights AS stage_insights,
  referenced_tables
FROM {_region_ref("JOBS_BY_PROJECT")}
WHERE creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND job_id = @job_id
LIMIT 1
"""


def query_detail(qhash: str, days: int | None = None) -> dict[str, Any]:
    """One shape in depth. {"ok", "qhash", "runs", "users": [...], "sql",
    "sample_job", "bytes": {"p50","p95","max"} (raw bytes), "gb": the same in GiB,
    "slot_ms": {"p50","p95","max"},
    "stages": [{"name","records_read","records_written","shuffle_output_gb","slot_ms","status"}],
    "insights": [...], "referenced_tables": [...], "tables": [table_layout(...) per table]}"""
    if not enabled():
        return _disabled()
    days = int(days) if days else int(settings.bq_cost_days)
    try:
        client = _get_client()
        rows = _run_rows(client, _query_detail_jobs_sql(), None,
                          params=[_int_param("days", days), _str_param("qhash", qhash)])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "hint": hint_for(str(e))}
    if not rows:
        return {"ok": False, "error": f"no runs for {qhash} in the last {days} days."}

    bytes_vals = [int(r.get("total_bytes_processed") or 0) for r in rows]
    slot_vals = [int(r.get("total_slot_ms") or 0) for r in rows]
    users = sorted({r.get("user_email") for r in rows if r.get("user_email")})
    sample_job_id = rows[0].get("job_id")
    sample_sql = rows[0].get("query") or ""

    stages, insights, referenced = [], [], []
    try:
        stage_rows = _run_rows(client, _query_detail_stage_sql(), None,
                                params=[_int_param("days", days), _str_param("job_id", sample_job_id)])
        if stage_rows:
            d = stage_rows[0]
            stages = _stage_list(d.get("job_stages"))
            insights = _insight_flags(d.get("stage_insights"))
            referenced = _referenced_table_strings(d.get("referenced_tables"))
    except Exception:  # noqa: BLE001 — the run list still stands without stage detail
        pass

    tables = [table_layout(t) for t in referenced[:5]]
    return {
        "ok": True, "qhash": qhash, "runs": len(rows), "users": users,
        "sql": sample_sql, "sample_job": sample_job_id,
        "bytes": _percentiles(bytes_vals),        # raw, so a 21 KB query is not "0.0"
        "gb": _percentiles_gb(bytes_vals),
        "slot_ms": _percentiles(slot_vals),
        "stages": stages, "insights": insights,
        "referenced_tables": referenced, "tables": tables,
    }


# ----- table_layout --------------------------------------------------------------

def _table_layout_definition_sql(project: str, dataset: str) -> str:
    return f"""
SELECT
  t.table_name, t.ddl, t.creation_time,
  o_exp.option_value AS expiration_ts,
  o_pexp.option_value AS partition_expiration_days,
  c.partition_field, c.clustering_columns, c.columns
FROM {_dataset_ref(project, dataset, "TABLES")} AS t
LEFT JOIN (
  SELECT table_name, option_value
  FROM {_dataset_ref(project, dataset, "TABLE_OPTIONS")}
  WHERE option_name = 'expiration_timestamp'
) AS o_exp ON t.table_name = o_exp.table_name
LEFT JOIN (
  SELECT table_name, option_value
  FROM {_dataset_ref(project, dataset, "TABLE_OPTIONS")}
  WHERE option_name = 'partition_expiration_days'
) AS o_pexp ON t.table_name = o_pexp.table_name
LEFT JOIN (
  SELECT
    table_name,
    ANY_VALUE(CASE WHEN is_partitioning_column = 'YES' THEN column_name END) AS partition_field,
    ARRAY_AGG(CASE WHEN clustering_ordinal_position IS NOT NULL THEN column_name END IGNORE NULLS
              ORDER BY clustering_ordinal_position) AS clustering_columns,
    ARRAY_AGG(STRUCT(column_name AS name, data_type AS type) ORDER BY ordinal_position LIMIT 60) AS columns
  FROM {_dataset_ref(project, dataset, "COLUMNS")}
  GROUP BY table_name
) AS c ON t.table_name = c.table_name
WHERE t.table_name = @table_name
"""


def _table_layout_storage_sql(project: str, dataset: str) -> str:
    return f"""
SELECT total_rows, total_logical_bytes, total_physical_bytes, storage_last_modified_time
FROM {_dataset_ref(project, dataset, "TABLE_STORAGE")}
WHERE table_name = @table_name
"""


def _table_layout_partitions_sql(project: str, dataset: str) -> str:
    return f"""
SELECT
  COUNT(*) AS partition_count,
  APPROX_QUANTILES(total_logical_bytes, 100)[OFFSET(50)] AS p50_bytes,
  MAX(total_logical_bytes) AS max_bytes
FROM {_dataset_ref(project, dataset, "PARTITIONS")}
WHERE table_name = @table_name
  AND partition_id NOT IN ('__NULL__', '__UNPARTITIONED__')
"""


def _partition_expr_from_ddl(ddl: str | None) -> str | None:
    """The raw 'PARTITION BY ...' clause text out of a table's DDL — e.g.
    "DATE(created_at)" — since COLUMNS.is_partitioning_column says WHICH column
    but not the time unit. No regex: a plain marker search."""
    text = ddl or ""
    marker = "PARTITION BY "
    idx = text.find(marker)
    if idx == -1:
        return None
    rest = text[idx + len(marker):]
    stops = [p for p in (rest.find("\n"), rest.find(";"), rest.find("CLUSTER BY"), rest.find("OPTIONS("))
             if p != -1]
    end = min(stops) if stops else len(rest)
    return rest[:end].strip().rstrip(",;") or None


def table_layout(table: str) -> dict[str, Any]:
    """{"ok", "table": "project.dataset.table", "rows", "gb", "physical_gb",
    "partitioning": {"type", "field", "expiration_days"} | None, "clustering": [...],
    "expiration", "created", "last_modified", "columns": [{"name","type"}] (capped 60),
    "partitions": {"count", "p50_gb", "max_gb"} | None}"""
    try:
        project, dataset, table_name = _split_table(table)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    full = f"{project}.{dataset}.{table_name}"
    try:
        client = _get_client()
        def_rows = _run_rows(client, _table_layout_definition_sql(project, dataset), None,
                              params=[_str_param("table_name", table_name)])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "hint": hint_for(str(e))}
    if not def_rows:
        return {"ok": False, "error": f"{full} not found (or not visible)."}
    row = def_rows[0]

    storage_row: dict[str, Any] = {}
    try:
        storage_rows = _run_rows(client, _table_layout_storage_sql(project, dataset), None,
                                  params=[_str_param("table_name", table_name)])
        storage_row = storage_rows[0] if storage_rows else {}
    except Exception:  # noqa: BLE001 — size is a nicety; the definition still stands
        pass

    partitions = None
    try:
        part_rows = _run_rows(client, _table_layout_partitions_sql(project, dataset), None,
                               params=[_str_param("table_name", table_name)])
        if part_rows and part_rows[0].get("partition_count"):
            p = part_rows[0]
            partitions = {"count": int(p.get("partition_count") or 0),
                          "p50_gb": round((p.get("p50_bytes") or 0) / _GIB, 2),
                          "max_gb": round((p.get("max_bytes") or 0) / _GIB, 2)}
    except Exception:  # noqa: BLE001
        pass

    partitioning = None
    if row.get("partition_field"):
        partitioning = {
            "type": _partition_expr_from_ddl(row.get("ddl")) or "unknown",
            "field": row.get("partition_field"),
            "expiration_days": _to_float(row.get("partition_expiration_days")),
        }
    return {
        "ok": True, "table": full,
        "rows": int(storage_row.get("total_rows") or 0),
        "gb": round((storage_row.get("total_logical_bytes") or 0) / _GIB, 2),
        "physical_gb": round((storage_row.get("total_physical_bytes") or 0) / _GIB, 2),
        "partitioning": partitioning,
        "clustering": row.get("clustering_columns") or [],
        "expiration": _fmt_ts(row.get("expiration_ts")),
        "created": _fmt_ts(row.get("creation_time")),
        "last_modified": _fmt_ts(storage_row.get("storage_last_modified_time")),
        "columns": (row.get("columns") or [])[:60],
        "partitions": partitions,
    }


# ----- prune_estimate --------------------------------------------------------------

def _table_reads_detail_sql() -> str:
    return f"""
SELECT total_bytes_processed
FROM {_region_ref("JOBS_BY_PROJECT")} AS j, UNNEST(j.referenced_tables) AS rt
WHERE j.creation_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY)
  AND j.job_type = 'QUERY'
  AND j.state = 'DONE'
  AND rt.project_id = @project_id
  AND rt.dataset_id = @dataset_id
  AND rt.table_id = @table_id
"""


def prune_estimate(table: str, column: str, days: int | None = None) -> dict[str, Any]:
    """ESTIMATED, never measured: what partitioning/clustering ``table`` on
    ``column`` would have saved for the runs in the window.
    {"ok", "table", "column", "column_type", "runs_reading_table", "gb_per_run_now",
     "partitioned": bool, "partition_field", "estimate": {"gb_per_run_if_partitioned",
     "basis": "partition_stats|table_size|none"}, "measured": False, "note"}"""
    if not enabled():
        return _disabled()
    try:
        project, dataset, table_name = _split_table(table)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    days = int(days) if days else int(settings.bq_cost_days)
    full_table = f"{project}.{dataset}.{table_name}"
    try:
        client = _get_client()
        job_rows = _run_rows(client, _table_reads_detail_sql(), None,
                              params=[_int_param("days", days), _str_param("project_id", project),
                                      _str_param("dataset_id", dataset), _str_param("table_id", table_name)])
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "hint": hint_for(str(e))}

    runs = len(job_rows)
    total_bytes = sum(int(r.get("total_bytes_processed") or 0) for r in job_rows)
    gb_per_run_now = round((total_bytes / runs) / _GIB, 2) if runs else 0.0

    layout = table_layout(full_table)
    column_type = None
    if layout.get("ok"):
        column_type = next((c.get("type") for c in layout.get("columns") or []
                             if c.get("name") == column), None)
    partitioned = bool(layout.get("ok") and layout.get("partitioning"))
    partition_field = (layout.get("partitioning") or {}).get("field") if partitioned else None

    if partitioned and layout.get("partitions"):
        estimate = {"gb_per_run_if_partitioned": layout["partitions"]["p50_gb"], "basis": "partition_stats"}
        note = ("assumes a one-day filter on the partition column prunes to a single partition "
                "— the median partition's size, not a per-query measurement.")
    elif partitioned:
        estimate = {"gb_per_run_if_partitioned": None, "basis": "partition_stats"}
        note = "table is partitioned, but no partition stats were available to estimate from."
    else:
        estimate = {"gb_per_run_if_partitioned": None, "basis": "table_size"}
        note = ("table has no partitioning yet — the saving could be up to the whole table per "
                "run; partition it to get a measurable number here.")
    return {
        "ok": True, "table": full_table, "column": column, "column_type": column_type,
        "runs_reading_table": runs, "gb_per_run_now": gb_per_run_now,
        "partitioned": partitioned, "partition_field": partition_field,
        "estimate": estimate, "measured": False, "note": note,
    }


# ----- the verifier (deterministic) ------------------------------------------

def _job_referenced_table_strings(job: Any) -> list[str]:
    """job.referenced_tables holds TableReference-like objects (.project,
    .dataset_id, .table_id) for a real client, or plain dicts for a fake one —
    both are handled."""
    out = []
    for t in getattr(job, "referenced_tables", None) or []:
        if isinstance(t, dict):
            project, dataset, tbl = t.get("project"), t.get("dataset_id"), t.get("table_id")
        else:
            project = getattr(t, "project", None)
            dataset = getattr(t, "dataset_id", None)
            tbl = getattr(t, "table_id", None)
        if project and dataset and tbl:
            out.append(f"{project}.{dataset}.{tbl}")
    return out


def _sql_parameters(sql: str) -> list[str]:
    """Names of @parameters referenced in sql — an '@' punctuation token
    followed by a word token, from bq_guard.tokens() (no regex).

    VERIFIER HOLE this closes: BigQuery's dry run silently prices an
    undeclared @parameter as if its predicate does not exist (0 extra bytes)
    rather than refusing — so a "before" written with a parameter looks free,
    and an "after" that merely adds one would look like a 100% saving.
    verdict() rejects whenever either side carries a parameter, precisely
    because the byte count cannot be trusted in that case.
    """
    toks = bq_guard.tokens(sql)
    return [toks[i + 1] for i, t in enumerate(toks) if t == "@" and i + 1 < len(toks)]


def dry_run(sql: str) -> dict[str, Any]:
    """ALWAYS a dry run, whatever the statement. {"ok": True, "bytes", "gb",
    "schema": [{"name","type"}], "referenced_tables": [...], "parameters": [...]}
    or {"ok": False, "error"}. A non-empty "parameters" means the byte count
    cannot be trusted (see _sql_parameters) — verify_rewrite()/verdict() act on it."""
    try:
        from google.cloud import bigquery

        client = _get_client()
        cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
        job = bq_guard.run(client, sql, job_config=cfg)
    except Exception as e:  # noqa: BLE001 — includes bq_guard.GuardRefused
        return {"ok": False, "error": str(e)}
    bytes_ = getattr(job, "total_bytes_processed", None) or 0
    schema = [{"name": f.name, "type": f.field_type} for f in (job.schema or [])]
    return {"ok": True, "bytes": bytes_, "gb": round(bytes_ / _GIB, 2),
            "schema": schema, "referenced_tables": _job_referenced_table_strings(job),
            "parameters": _sql_parameters(sql)}


_ROW_LEVEL_NOTE = (
    "row-level equivalence is not proven — a dry run only compares bytes and the result "
    "schema; a filter rewritten wrongly can return different rows with identical schema "
    "and fewer bytes. Review is a person's job."
)


def verdict(before: dict[str, Any], after: dict[str, Any], *, declared_schema_change: bool = False) -> dict[str, Any]:
    """PURE: the §5 table over two dry-run results. Tested without BigQuery.

    schema_changed/dropped_columns/added_columns/new_tables are ALWAYS
    computed from both dry runs and always present in the result, whatever
    the verdict — a schema change must be visible even when some OTHER check
    also fails (e.g. not cheaper either), because the report leads with the
    schema change and the model uses these fields to revise the rewrite.
    Decided in this order once both dry runs are valid: schema changed (and
    not declared) -> new tables -> parameterised (bytes cannot be trusted,
    see _sql_parameters) -> not cheaper -> accept.
    """
    if not before.get("ok") or not after.get("ok"):
        return {"ok": True, "verdict": "reject", "reason": "invalid",
                "before": before, "after": after,
                "saving_gb": None, "saving_pct": None,
                "schema_changed": False, "dropped_columns": [], "added_columns": [],
                "new_tables": [], "parameters": [], "note": _ROW_LEVEL_NOTE}

    before_schema = before.get("schema") or []
    after_schema = after.get("schema") or []
    before_pairs = {(c.get("name"), c.get("type")) for c in before_schema}
    after_pairs = {(c.get("name"), c.get("type")) for c in after_schema}
    schema_changed = before_pairs != after_pairs
    before_names = {c.get("name") for c in before_schema}
    after_names = {c.get("name") for c in after_schema}
    dropped = sorted(before_names - after_names)
    added = sorted(after_names - before_names)

    before_tables = set(before.get("referenced_tables") or [])
    after_tables = set(after.get("referenced_tables") or [])
    new_tables = sorted(after_tables - before_tables)

    parameters = sorted(set(before.get("parameters") or []) | set(after.get("parameters") or []))

    bytes_before = before.get("bytes") or 0
    bytes_after = after.get("bytes") or 0

    common = {"before": before, "after": after, "schema_changed": schema_changed,
              "dropped_columns": dropped, "added_columns": added, "new_tables": new_tables,
              "parameters": parameters, "note": _ROW_LEVEL_NOTE}

    def _reject(reason: str, note: str | None = None) -> dict[str, Any]:
        result = {"ok": True, "verdict": "reject", "reason": reason,
                  "saving_gb": None, "saving_pct": None, **common}
        if note:
            result["note"] = note
        return result

    if schema_changed and not declared_schema_change:
        return _reject("schema_changed")
    if new_tables:
        return _reject("new_tables")
    if parameters:
        return _reject("parameterised",
                        note=(f"substitute a literal for each @parameter ({', '.join(parameters)}, "
                              "e.g. 14 for a day window) and re-run — a dry run cannot price a "
                              "parameter."))
    if bytes_after >= bytes_before:
        return _reject("not_cheaper")

    saving_bytes = bytes_before - bytes_after
    saving_pct = round(100.0 * saving_bytes / bytes_before, 1) if bytes_before else None
    return {"ok": True, "verdict": "accept", "reason": "ok",
            "saving_gb": round(saving_bytes / _GIB, 2), "saving_pct": saving_pct, **common}


def verify_rewrite(before_sql: str, after_sql: str, *, declared_schema_change: bool = False) -> dict[str, Any]:
    """Two dry runs and the verdict table of design §5.
    {"ok", "verdict": "accept|reject", "reason": "ok|invalid|not_cheaper|schema_changed|new_tables",
     "before": {"bytes","gb","schema"}, "after": {...}, "saving_gb", "saving_pct",
     "schema_changed": bool, "dropped_columns": [...], "added_columns": [...],
     "new_tables": [...], "note": "row-level equivalence is not proven …"}"""
    before = dry_run(before_sql)
    after = dry_run(after_sql)
    return verdict(before, after, declared_schema_change=declared_schema_change)


# ----- memory across runs (append-only, optional) -----------------------------

def record_findings(report: dict[str, Any], proposals: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Append this run's shapes (+ any proposals) to bq_cost_findings. No-op
    when bq_cost_dataset is empty; a missing table degrades to a warning."""
    if not settings.bq_cost_dataset:
        return {"ok": True, "note": "no memory across runs — BQ_COST_DATASET is empty."}
    if not report or not report.get("ok"):
        return {"ok": True, "note": "nothing to record for a disabled/failed scan."}

    proposals_by_hash = {p.get("qhash"): p for p in (proposals or []) if p.get("qhash")}
    now = _now_iso()
    rows: list[dict[str, Any]] = []
    for s in report.get("shapes") or []:
        qhash = s.get("qhash")
        if not qhash:
            continue
        proposal = proposals_by_hash.get(qhash)
        rows.append({
            "report_id": uuid.uuid4().hex, "run_ts": now, "qhash": qhash, "kind": "query",
            "cost_before_bytes": int(round((s.get("gb_billed") or 0) * _GIB)),
            "cost_before_slot_ms": int(round((s.get("slot_hours") or 0) * 3600 * 1000)),
            "proposal": json.dumps(proposal) if proposal else None,
            "verdict": proposal.get("verdict") if proposal else None,
            "bytes_after": (proposal.get("after") or {}).get("bytes") if proposal else None,
        })
    for st in report.get("storage") or []:
        rows.append({
            "report_id": uuid.uuid4().hex, "run_ts": now, "qhash": st.get("table"), "kind": "storage",
            "cost_before_bytes": int(round((st.get("gb") or 0) * _GIB)),
            "cost_before_slot_ms": None, "proposal": None, "verdict": st.get("kind"), "bytes_after": None,
        })
    for w in report.get("writes") or []:
        rows.append({
            "report_id": uuid.uuid4().hex, "run_ts": now, "qhash": w.get("table"), "kind": "write",
            "cost_before_bytes": int(round((w.get("input_gb") or 0) * _GIB)),
            "cost_before_slot_ms": None, "proposal": None, "verdict": w.get("kind"), "bytes_after": None,
        })
    if not rows:
        return {"ok": True, "note": "no shapes/storage/writes to record."}
    try:
        client = _get_client()
        table_id = f"{_project()}.{settings.bq_cost_dataset}.{settings.bq_cost_findings_table}"
        errors = client.insert_rows_json(table_id, rows, row_ids=[r["report_id"] for r in rows])
        if errors:
            return {"ok": False, "warning": f"bq_cost_findings insert failed: {errors}"}
        return {"ok": True, "recorded": len(rows)}
    except Exception as e:  # noqa: BLE001 — e.g. the table doesn't exist yet
        return {"ok": False, "warning": f"bq_cost_findings unavailable: {e}"}


# Neither caller (scan()'s own history lookup, or a person asking
# bq_findings for a handful of past runs) ever needs more than a few dozen
# rows back — this is a generous, FIXED backstop, not a cadence-aware one,
# whose only job is to stop this being a full-history scan of a
# day-partitioned table on every call (LIMIT alone does not prune
# partitions). It comfortably covers even a daily cron for well over a year.
_FINDINGS_LOOKBACK_DAYS = 400

# The same real-run backstop bq_guard.run applies to every other statement
# in this module — duplicated as a literal (not imported from bq_guard)
# because this one query deliberately bypasses bq_guard.run itself (see the
# function's own docstring below), and bq_guard's own constant is private.
_FINDINGS_MAX_BYTES_BILLED = 2**30


def _read_own_findings_table(qhash: str | None, runs: int,
                              budget: "bq_guard.Budget | None" = None) -> list[dict[str, Any]]:
    """Read bq_cost_findings — and ONLY bq_cost_findings — directly, bypassing
    bq_guard: a plain SELECT on the tool's own append-only table is not an
    INFORMATION_SCHEMA/``__TABLES__`` read, so the guard would (correctly, for
    any OTHER table) force it to a dry run, which returns no rows at all. This
    is the one place in the module that queries an ordinary table, and it is
    hardcoded to this one table name — never anything caller-supplied.

    Bypassing bq_guard.run means its protections are applied BY HAND here
    instead, so this read is held to the same standard as everything else in
    the module: the ``release_copilot=bq_cost`` label (so this read excludes
    itself from its own "top query shapes" next run, exactly like every
    statement bq_guard.run issues — see _shapes_sql/_totals_sql/_table_reads_sql),
    a maximum_bytes_billed backstop, a run_ts lookback so LIMIT is not the
    only thing bounding the scan of a day-partitioned table (LIMIT reduces
    rows RETURNED, never bytes SCANNED), and — when the caller is itself
    inside a scan() — that scan's own query budget, so this read's cost is
    part of the ``report_cost_bytes`` scan() states about itself.
    """
    from google.cloud import bigquery

    client = _get_client()
    table_id = f"{_project()}.{settings.bq_cost_dataset}.{settings.bq_cost_findings_table}"
    sql = (f"SELECT * FROM `{table_id}` "
           "WHERE run_ts > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @lookback_days DAY) ")
    params = [bigquery.ScalarQueryParameter("lookback_days", "INT64", _FINDINGS_LOOKBACK_DAYS)]
    if qhash:
        sql += "AND qhash = @qhash "
        params.append(bigquery.ScalarQueryParameter("qhash", "STRING", qhash))
    sql += "ORDER BY run_ts DESC LIMIT @row_limit"
    # qhash given: "the last N runs for A hash" -> N rows. qhash absent: "all
    # hashes' latest" -> a generous, approximate cap (each run reports at most
    # a few hundred shapes/storage/write findings combined).
    row_limit = int(runs) if qhash else int(runs) * 200
    params.append(bigquery.ScalarQueryParameter("row_limit", "INT64", row_limit))
    cfg = bigquery.QueryJobConfig(query_parameters=params,
                                   maximum_bytes_billed=_FINDINGS_MAX_BYTES_BILLED,
                                   labels={"release_copilot": "bq_cost"})
    # Same reserve-before-issue, record-after-complete discipline bq_guard.run
    # itself uses (see Budget) — applied by hand because this one query
    # deliberately bypasses bq_guard.run.
    if budget is not None:
        budget.reserve()
    job = client.query(sql, job_config=cfg)
    rows = list(job.result())
    if budget is not None:
        budget.record(getattr(job, "total_bytes_processed", None) or 0)
    out = []
    for r in rows:
        row = dict(r)
        ts = row.get("run_ts")
        if hasattr(ts, "isoformat"):
            row["run_ts"] = ts.isoformat()
        out.append(row)
    return out


def findings(qhash: str | None = None, runs: int = 5) -> dict[str, Any]:
    """What was reported before: {"ok", "runs": [...], "adoption": [...]}."""
    if not settings.bq_cost_dataset:
        return {"ok": False, "disabled": True, "error": "No memory across runs (BQ_COST_DATASET is empty)."}
    try:
        rows = _read_own_findings_table(qhash, runs)
    except Exception as e:  # noqa: BLE001 — e.g. the table doesn't exist yet
        return {"ok": False, "error": f"bq_cost_findings unavailable: {e}"}
    # rows are DESC by run_ts; each hash's "current" snapshot is its latest row
    # — reusing the same pure comparison scan() uses against a live report.
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        h = row.get("qhash")
        if h and h not in latest:
            latest[h] = row
    current_shapes = [{"qhash": h, "gb_billed": round((r.get("cost_before_bytes") or 0) / _GIB, 2)}
                       for h, r in latest.items()]
    return {"ok": True, "runs": rows, "adoption": adoption(rows, current_shapes)}


def _row_gb(row: dict[str, Any]) -> float:
    return round((row.get("cost_before_bytes") or 0) / _GIB, 2)


def adoption(previous: list[dict[str, Any]], current_shapes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PURE: per qhash seen before -> {"qhash", "status": "adopted|still_open|new|gone",
    "times_reported", "cost_before_gb", "cost_now_gb", "change_pct"}. adopted =
    cost fell >= 30% vs the first report; still_open = unchanged after 3 reports."""
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in previous:
        q = row.get("qhash")
        if q:
            by_hash.setdefault(q, []).append(row)
    current_by_hash = {s["qhash"]: s for s in current_shapes if s.get("qhash")}

    out = []
    for qhash in set(by_hash) | set(current_by_hash):
        rows_sorted = sorted(by_hash.get(qhash, []), key=lambda r: r.get("run_ts") or "")
        times_reported = len(rows_sorted)
        current = current_by_hash.get(qhash)
        if not rows_sorted:
            status, cost_before, cost_now, change_pct = "new", None, current.get("gb_billed"), None
        elif current is None:
            status, cost_before, cost_now, change_pct = "gone", _row_gb(rows_sorted[0]), None, None
        else:
            cost_before = _row_gb(rows_sorted[0])
            cost_now = current.get("gb_billed")
            change_pct = round(100.0 * (cost_now - cost_before) / cost_before, 1) if cost_before else None
            status = "adopted" if (change_pct is not None and change_pct <= -30) else "still_open"
        out.append({"qhash": qhash, "status": status, "times_reported": times_reported,
                    "cost_before_gb": cost_before, "cost_now_gb": cost_now, "change_pct": change_pct})
    return out


# ----- pure helpers the tests pin ---------------------------------------------

def rank_shapes(rows: list[dict[str, Any]], billing: str, top: int) -> list[dict[str, Any]]:
    """PURE: JOBS_BY_PROJECT rows already grouped by shape (one row per qhash,
    the shape of ``_shapes_sql()``'s result) -> the shapes list scan() returns,
    ordered by slot_hours (reservations) or gb_billed (on-demand)."""
    shaped = []
    for r in rows:
        slot_ms = r.get("total_slot_ms") or 0
        bytes_billed = r.get("total_bytes_billed") or 0
        bytes_p50 = r.get("p50_bytes_processed") or 0
        last_run = r.get("last_run")
        shaped.append({
            "qhash": r.get("qhash"),
            "runs": int(r.get("runs") or 0),
            "who": r.get("any_user_email") or None,
            "users": [u for u in (r.get("users") or []) if u][:8],
            "users_count": int(r.get("distinct_users") or 0),
            "slot_hours": round(slot_ms / 1000.0 / 3600.0, 2),
            "gb_billed": round(bytes_billed / _GIB, 2),
            "approx_usd": approx_usd(bytes_billed, settings.bq_cost_usd_per_tib),
            "p50_gb": round(bytes_p50 / _GIB, 2),
            "p50_bytes": int(bytes_p50),  # the UI prefers this over p50_gb when it would round to 0.0
            # JOBS(_BY_PROJECT) has no total_partitions_processed column (that
            # figure is Jobs REST API statistics only) — left absent rather
            # than invented; the UI tolerates a missing/None value here.
            "p50_partitions": None,
            "sql_preview": r.get("sql_preview") or "",
            "sample_job": r.get("sample_job_id") or "",
            "referenced_tables": [],
            "insights": [],
            "last_run": last_run.isoformat() if hasattr(last_run, "isoformat") else last_run,
        })
    key = (lambda s: s["slot_hours"]) if billing == "reservations" else (lambda s: s["gb_billed"])
    shaped.sort(key=key, reverse=True)
    return shaped[: max(0, int(top))]


def approx_usd(bytes_billed: int, usd_per_tib: float) -> float:
    return round((bytes_billed or 0) / _TIB * usd_per_tib, 2)


_HINT_REGION_WIDE = (
    "a region-wide INFORMATION_SCHEMA view needs bigquery.tables.list on EVERY dataset in the "
    "region, including anonymous cached-result datasets (`_...`) that belong to other users and "
    "service accounts — no role grants that, so this is expected in any project where more than "
    "one principal runs queries. Storage falls back to per-dataset reads; the write timelines "
    "have no per-dataset form, so that section stays unavailable. Only if the account lacks "
    "roles/bigquery.metadataViewer on the project is there something to grant."
)
_HINT_DATASET_LEVEL = (
    "a region-wide INFORMATION_SCHEMA view needs tables.list on EVERY dataset in the "
    "region, including BigQuery's hidden anonymous result datasets (`_...`) that belong "
    "to other users — this account cannot read those, so the report fell back to reading "
    "each visible dataset; that is expected in any shared project."
)
_HINT_PROJECT_LEVEL = "grant roles/bigquery.metadataViewer (bigquery.tables.list at project level)."


def hint_for(error: str, *, rows_seen: int = 0, users_seen: int = 0, region_wide: bool = False) -> str | None:
    """PURE: the one-line hint for a failure or an empty-looking result —
    missing jobs.listAll (only own jobs visible), wrong region (nothing at all),
    a 403 on INFORMATION_SCHEMA (missing resourceViewer / metadataViewer).

    ``region_wide=True`` is set by a caller that KNOWS the failing statement
    was a region-wide view (STREAMING_TIMELINE_BY_PROJECT / WRITE_API_TIMELINE_BY_PROJECT
    — the write views have no per-dataset fallback, so this is the only way to
    fail there): BigQuery's own wording ("project level" vs "dataset level")
    does not reliably distinguish "one hidden dataset denies the whole
    region-wide enumeration" from "this account is missing a role outright",
    so that distinction is made by the CALLER, not guessed from the text.
    """
    err = (error or "").strip()
    if err:
        low = err.lower()
        if region_wide and ("permission" in low or "denied" in low or "403" in err or "forbidden" in low):
            return _HINT_REGION_WIDE
        if "at the dataset level" in low:
            return _HINT_DATASET_LEVEL
        if "at the project level" in low:
            return _HINT_PROJECT_LEVEL
        if "403" in err or "permission" in low or "forbidden" in low or "access denied" in low or "denied" in low:
            if "job" in low:
                return "grant roles/bigquery.resourceViewer (bigquery.jobs.listAll) to see every principal's jobs."
            if any(v in low for v in ("table_storage", "table_options", "streaming_timeline",
                                       "write_api_timeline", "__tables__", "tables", "columns", "partitions")):
                return _HINT_PROJECT_LEVEL
            return "grant roles/bigquery.jobUser (bigquery.jobs.create) to run a dry run or metadata query."
        return None
    if rows_seen == 0:
        return f"nothing in {settings.bq_cost_region} — is BQ_COST_REGION the project's region?"
    if users_seen <= 1:
        # One principal in a whole window is either a one-person project (fine) or
        # a missing jobs.listAll (everyone else's jobs invisible) — say both.
        return ("only one principal's jobs are visible in this window — if other people or service "
                "accounts query this project, grant roles/bigquery.resourceViewer (bigquery.jobs.listAll).")
    return None


# ----- CronJob entry point: python -m release_agent.tools.bq_cost ------------

def _markdown_table(report: dict[str, Any]) -> str:
    if not report.get("ok"):
        return f"BQ cost report: {report.get('error') or report.get('hint') or 'disabled'}"
    lines = [f"BQ cost report — {report['project']} / {report['region']} — last {report['days']}d "
             f"({report['billing']})", "",
             "| qhash | runs | who | slot_h | GB billed | ~$ |", "|---|---|---|---|---|---|"]
    for s in report.get("shapes") or []:
        lines.append(f"| {(s['qhash'] or '')[:10]} | {s['runs']} | {s.get('who') or '?'} | "
                      f"{s['slot_hours']} | {s['gb_billed']} | {s['approx_usd']} |")
    if report.get("hint"):
        lines += ["", f"_hint: {report['hint']}_"]
    return "\n".join(lines)


def _main() -> None:
    import argparse
    import json as _json

    p = argparse.ArgumentParser(description="BigQuery cost report (design/BQ_COST.md)")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--top", type=int, default=None)
    p.add_argument("--record", action="store_true", help="append this run's findings to bq_cost_findings")
    p.add_argument("--markdown", action="store_true", help="print a compact table instead of JSON")
    args = p.parse_args()
    report = scan(days=args.days, top=args.top)
    if args.record:
        record_findings(report)
    print(_markdown_table(report) if args.markdown else _json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    _main()
