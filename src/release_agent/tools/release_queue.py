"""Release intake queue — an APPEND-ONLY event table in BigQuery.

A developer who is ready on Monday registers "put me in the next release"
(chart, version, routing flags, note). DevOps sees the accumulated list when
creating Thursday's release, pre-filled into the Create-release form. After the
release PR merges, queued items are marked released and the queue drains.

Design rules (deliberate):
  * Append-only — every action (queue / withdraw / released) is an INSERT.
    No UPDATE/DELETE ever: streaming-buffer rows can't be mutated anyway, and
    the event log doubles as the audit history ("who asked for what, when,
    and which release it went out in").
  * The queue is DERIVED, not stored: fetch recent events and reduce them
    client-side (`reduce_queue`) — trivially unit-testable, no SQL window
    functions, and at a few rows/week the full scan is free.
  * Git/PRs stay the source of truth for what shipped; `released` events are
    only written after the release PR actually merged, carrying its number.
  * Fire-and-forget: BigQuery being down must never block a release. Every
    entry point returns {"ok": False, ...} instead of raising.
"""
from __future__ import annotations

import datetime as _dt
import threading
import uuid
from typing import Any

from ..config import settings

# Reference schema. In cluster deployments the table is provisioned SEPARATELY
# (terraform: bigquery/terraform consuming bigquery/release_intents.schema.json)
# and dataset/table names arrive via Helm values (BQ_DATASET / BQ_TABLE). This
# list is only used by the dev-mode bootstrap (BQ_AUTO_CREATE=true) — keep the
# JSON schema in sync when adding columns.
_SCHEMA = [
    ("event_id", "STRING"),
    ("event_type", "STRING"),  # queued | withdrawn | released | deployed
    ("event_ts", "TIMESTAMP"),
    ("requested_by", "STRING"),
    ("artifact_name", "STRING"),
    ("artifact_version", "STRING"),
    ("prl1_only", "BOOL"),
    ("df_only", "BOOL"),
    ("note", "STRING"),
    ("deployment_repo", "STRING"),  # target GitHub repo (owner/repo)
    ("release_name", "STRING"),  # set on 'released' events
    ("pr_number", "INT64"),  # set on 'released'/'deployed' events
    ("build_verified", "BOOL"),  # None = not checked at queue time
    ("environment", "STRING"),  # set on 'deployed' events: uat | prod | dataflow-uat
    ("jira_ticket", "STRING"),  # e.g. REL-1234 — audit link, surfaces in CHG draft
    ("change_details", "STRING"),  # dev's what-changed-and-why, feeds change_description
    ("build_run_url", "STRING"),  # the Actions run that built the tag — eligibility evidence
]

_lock = threading.Lock()
_client = None
_table_ready = False
# The banner polls every turn; don't hit BQ more than once a minute for a count.
_count_cache: dict[str, Any] = {"at": 0.0, "count": None}
# Every form open reads the queue (a BQ query job ~2-3s). Cache the reduced
# queue briefly so opening a form twice, or two users at once, is instant.
# Short TTL: the queue changes only when someone queues/withdraws, and both
# paths invalidate it explicitly below.
_QUEUE_TTL_SECONDS = 30.0
_queue_cache: dict[str, Any] = {"at": 0.0, "value": None}


def _bq_project() -> str:
    """BQ may live in a different GCP project than Vertex; empty = same."""
    return settings.bq_project or settings.gcp_project


def queue_enabled() -> bool:
    return bool(settings.bq_dataset and _bq_project())


def _disabled() -> dict[str, Any]:
    return {
        "ok": False,
        "disabled": True,
        "error": "Release queue is disabled (BQ_DATASET or GOOGLE_CLOUD_PROJECT unset).",
    }


def _get_client():
    """BigQuery client. The table is expected to EXIST (provisioned separately);
    the dev-only BQ_AUTO_CREATE flag turns on the dataset/table bootstrap and
    additive column migration for local hacking."""
    global _client, _table_ready
    with _lock:
        if _client is not None and _table_ready:
            return _client
        from google.cloud import bigquery

        if _client is None:
            _client = bigquery.Client(project=_bq_project())
        if not _table_ready and settings.bq_auto_create:
            dataset_ref = bigquery.Dataset(f"{_bq_project()}.{settings.bq_dataset}")
            dataset_ref.location = settings.bq_location
            _client.create_dataset(dataset_ref, exists_ok=True)
            table = bigquery.Table(
                _table_id(),
                schema=[bigquery.SchemaField(n, t) for n, t in _SCHEMA],
            )
            table.time_partitioning = bigquery.TimePartitioning(field="event_ts")
            _client.create_table(table, exists_ok=True)
            # Additive schema migration: new nullable columns are appended to a
            # table created by an older version of this module (dev mode only —
            # in cluster the DDL owner runs ALTER TABLE).
            live = _client.get_table(_table_id())
            have = {f.name for f in live.schema}
            missing = [bigquery.SchemaField(n, t) for n, t in _SCHEMA if n not in have]
            if missing:
                live.schema = list(live.schema) + missing
                _client.update_table(live, ["schema"])
        _table_ready = True
        return _client


def _table_id() -> str:
    return f"{_bq_project()}.{settings.bq_dataset}.{settings.bq_table}"


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _insert(rows: list[dict[str, Any]]) -> dict[str, Any]:
    if not queue_enabled():
        return _disabled()
    try:
        client = _get_client()
        # row_ids gives best-effort dedup on network retries.
        errors = client.insert_rows_json(
            _table_id(), rows, row_ids=[r["event_id"] for r in rows]
        )
        if errors:
            return {"ok": False, "error": f"BigQuery insert failed: {errors}"}
        # state changed — drop the derived caches
        _count_cache["at"] = 0.0
        _queue_cache["at"] = 0.0
        return {"ok": True}
    except Exception as e:  # never let queue telemetry break a release path
        return {"ok": False, "error": f"BigQuery unavailable: {e}"}


def _split_artifact(artifact: str) -> tuple[str, str]:
    """'name:version' (or a full artifactory URL ending in name:version)."""
    last = str(artifact).strip().rstrip("/").split("/")[-1]
    name, _, version = last.partition(":")
    return name.strip(), version.strip()


def _norm_env(env: str) -> str:
    """One canonical name per environment: prod → prd (applied on both write
    and read, so rows captured before the rename still aggregate together)."""
    e = str(env or "").strip().lower()
    return {"prod": "prd", "dataflow-prod": "dataflow-prd"}.get(e, e)


# --- writes ------------------------------------------------------------------
def add_intent(
    artifact: str,
    requested_by: str,
    prl1_only: bool = False,
    df_only: bool = False,
    note: str = "",
    deployment_repo: str = "",
    build_verified: bool | None = None,
    jira_ticket: str = "",
    change_details: str = "",
    build_run_url: str = "",
) -> dict[str, Any]:
    """Queue an artifact for the next release. Re-queuing the same chart
    replaces it in the derived queue (latest event wins) — that's how a dev
    bumps the version without a separate edit action. jira_ticket and
    change_details are the dev's context — they feed the aggregated CHG draft
    when DevOps opens the Create-release form."""
    name, version = _split_artifact(artifact)
    if not name or not version:
        return {"ok": False, "error": f"Need chart:version (got {artifact!r})."}
    if not str(requested_by).strip():
        return {"ok": False, "error": "requested_by (your email) is required."}
    row = {
        "event_id": uuid.uuid4().hex,
        "event_type": "queued",
        "event_ts": _now_iso(),
        "requested_by": requested_by.strip(),
        "artifact_name": name,
        "artifact_version": version,
        "prl1_only": bool(prl1_only),
        "df_only": bool(df_only),
        "note": str(note or "").strip(),
        "deployment_repo": str(deployment_repo or "").strip(),
        "release_name": None,
        "pr_number": None,
        "build_verified": build_verified,
        "jira_ticket": str(jira_ticket or "").strip().upper() or None,
        "change_details": str(change_details or "").strip() or None,
        "build_run_url": str(build_run_url or "").strip() or None,
    }
    result = _insert([row])
    if result.get("ok"):
        result["intent"] = {k: v for k, v in row.items() if v is not None}
    return result


def withdraw_intent(artifact_name: str, actor: str) -> dict[str, Any]:
    name = str(artifact_name).strip().split(":")[0]
    if not name:
        return {"ok": False, "error": "artifact_name is required."}
    row = {
        "event_id": uuid.uuid4().hex,
        "event_type": "withdrawn",
        "event_ts": _now_iso(),
        "requested_by": str(actor or "").strip(),
        "artifact_name": name,
        "artifact_version": None,
        "prl1_only": None,
        "df_only": None,
        "note": None,
        "deployment_repo": None,
        "release_name": None,
        "pr_number": None,
        "build_verified": None,
    }
    result = _insert([row])
    if result.get("ok"):
        result["withdrawn"] = name
    return result


def mark_released(
    release_name: str, pr_number: int | None, artifacts: list[dict[str, str]]
) -> dict[str, Any]:
    """Drain the queue after a release PR merged: one 'released' event per
    shipped artifact ({'name': ..., 'tag': ...}). Best-effort — callers must
    not fail the release on a queue error."""
    now = _now_iso()
    rows = [
        {
            "event_id": uuid.uuid4().hex,
            "event_type": "released",
            "event_ts": now,
            "requested_by": None,
            "artifact_name": a.get("name"),
            "artifact_version": a.get("tag"),
            "prl1_only": None,
            "df_only": None,
            "note": None,
            "deployment_repo": None,
            "release_name": release_name,
            "pr_number": pr_number,
            "build_verified": None,
        }
        for a in artifacts
        if a.get("name")
    ]
    if not rows:
        return {"ok": True, "note": "no artifacts to mark"}
    return _insert(rows)


def record_deployment(
    environment: str,
    artifacts: list[dict[str, str]],
    deployment_repo: str = "",
    pr_number: int | None = None,
    note: str = "",
    event_type: str = "deployed",
) -> dict[str, Any]:
    """Capture a confirmed deployment (UAT override / PRD staging / DF dispatch /
    release promotion): one event per chart ({'name': ..., 'tag': ...}) with the
    target GitHub repo and the PR number. event_type 'deployed' (default) or
    'removed' (live removals) — together they make the per-environment deployed
    state derivable from the log. Pure telemetry — best-effort, callers must
    never fail a deploy on a queue error. The queue reduction ignores these
    events."""
    now = _now_iso()
    rows = [
        {
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "event_ts": now,
            "requested_by": None,
            "artifact_name": a.get("name"),
            "artifact_version": a.get("tag"),
            "prl1_only": None,
            "df_only": None,
            "note": str(note or "").strip() or None,
            "deployment_repo": str(deployment_repo or "").strip() or None,
            "release_name": None,
            "pr_number": pr_number,
            "build_verified": None,
            "environment": _norm_env(environment) or None,
        }
        for a in artifacts
        if a.get("name")
    ]
    if not rows:
        return {"ok": True, "note": "no artifacts to record"}
    return _insert(rows)


def _pattern_match(name: str, pattern: str) -> bool:
    """'' matches all; a pattern with * is a glob; otherwise substring match —
    so both 'acme-capability*' and 'capability' find acme-capability-svc."""
    if not pattern:
        return True
    import fnmatch

    pattern = pattern.strip().lower()
    name = (name or "").lower()
    if "*" in pattern or "?" in pattern:
        return fnmatch.fnmatch(name, pattern)
    return pattern in name


def aggregate_history(
    events: list[dict[str, Any]],
    pattern: str = "",
    event_types: tuple[str, ...] = ("released",),
) -> dict[str, Any]:
    """Pure aggregation over the event log: per-chart stats for the charts
    matching pattern, for the given event types (released / deployed / queued)."""
    charts: dict[str, dict[str, Any]] = {}
    total = 0
    for ev in events:
        if ev.get("event_type") not in event_types:
            continue
        name = ev.get("artifact_name") or ""
        if not _pattern_match(name, pattern):
            continue
        total += 1
        c = charts.setdefault(
            name,
            {"artifact_name": name, "count": 0, "versions": [], "releases": [],
             "environments": {}, "last_at": None},
        )
        c["count"] += 1
        v = ev.get("artifact_version")
        if v and v not in c["versions"]:
            c["versions"].append(v)
        rel = ev.get("release_name")
        entry = {"release": rel, "version": v, "pr": ev.get("pr_number"), "at": ev.get("event_ts")}
        if ev.get("event_type") == "released" and rel:
            c["releases"].append(entry)
        env = _norm_env(ev.get("environment"))
        if env:
            c["environments"][env] = c["environments"].get(env, 0) + 1
        ts = ev.get("event_ts")
        if ts and (c["last_at"] is None or ts > c["last_at"]):
            c["last_at"] = ts
    ranked = sorted(charts.values(), key=lambda c: (-c["count"], c["artifact_name"]))
    return {"total_events": total, "chart_count": len(ranked), "charts": ranked}


def aggregate_env_state(events: list[dict[str, Any]], pattern: str = "") -> dict[str, Any]:
    """Per-environment DEPLOYED STATE derived from the event log: for each
    (artifact, environment) the latest deployed/removed event wins — an
    artifact counts as deployed in an env while its latest event there is
    'deployed'. This is the source of truth for "how many X images are on
    UAT/PRD/PRL1" — the governance workflow files can't answer it because the
    updater script regenerates them per release (they describe the LAST
    release, not the cumulative estate)."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for ev in events:  # events arrive in ts order — later overwrite earlier
        if ev.get("event_type") not in ("deployed", "removed"):
            continue
        name, env = ev.get("artifact_name") or "", _norm_env(ev.get("environment"))
        if not name or not env or not _pattern_match(name, pattern):
            continue
        latest[(name, env)] = ev
    envs: dict[str, dict[str, Any]] = {}
    for (name, env), ev in latest.items():
        if ev.get("event_type") != "deployed":
            continue
        e = envs.setdefault(env, {"environment": env, "count": 0, "images": []})
        e["count"] += 1
        e["images"].append({
            "artifact_name": name,
            "version": ev.get("artifact_version"),
            "since": ev.get("event_ts"),
        })
    for e in envs.values():
        e["images"].sort(key=lambda i: i["artifact_name"])
    return {
        "environments": sorted(envs.values(), key=lambda e: e["environment"]),
        "distinct_images": len({n for (n, _env), ev in latest.items()
                                if ev.get("event_type") == "deployed"}),
    }


def history_stats(
    pattern: str = "", days: int = 90, event_type: str = "released"
) -> dict[str, Any]:
    """Stats over the release/deploy history: which charts matched, how often,
    which versions/releases, when last. event_type: released | deployed | queued
    | all — or 'state' for the CURRENT per-environment deployed state (latest
    deployed/removed event per artifact per env)."""
    if not queue_enabled():
        return _disabled()
    et = (event_type or "released").strip().lower()
    try:
        # State questions look across the whole log, not just the stats window.
        events = _fetch_events(365 if et == "state" else days)
    except Exception as e:
        return {"ok": False, "error": f"BigQuery unavailable: {e}"}
    if et == "state":
        out = aggregate_env_state(events, pattern=pattern)
        out.update({"ok": True, "pattern": pattern or "*", "event_type": "state"})
        return out
    types = ("released", "deployed", "queued", "withdrawn") if et == "all" else (et,)
    out = aggregate_history(events, pattern=pattern, event_types=types)
    out.update({"ok": True, "pattern": pattern or "*", "days": days, "event_type": et})
    return out


def recent_deployments(days: int = 30) -> dict[str, Any]:
    """Deployment history from the event log — newest first, per chart per env."""
    if not queue_enabled():
        return _disabled()
    try:
        events = _fetch_events(days)
    except Exception as e:
        return {"ok": False, "error": f"BigQuery unavailable: {e}"}
    deploys = [e for e in reversed(events) if e.get("event_type") == "deployed"]
    return {"ok": True, "deployments": deploys, "count": len(deploys)}


# --- reads -------------------------------------------------------------------
def _fetch_events(days: int = 120) -> list[dict[str, Any]]:
    client = _get_client()
    query = (
        f"SELECT * FROM `{_table_id()}` "
        "WHERE event_ts > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL @days DAY) "
        "ORDER BY event_ts"
    )
    from google.cloud import bigquery

    job = client.query(
        query,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("days", "INT64", days)]
        ),
    )
    out = []
    for r in job.result():
        row = dict(r)
        ts = row.get("event_ts")
        if hasattr(ts, "isoformat"):
            row["event_ts"] = ts.isoformat()
        out.append(row)
    return out


def reduce_queue(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pure reduction: replay events chronologically; an artifact is queued when
    its LATEST event is 'queued' (withdrawn/released clear it). Re-queue after a
    release naturally re-enters the next queue."""
    state: dict[str, dict[str, Any]] = {}
    for ev in events:
        name = ev.get("artifact_name")
        if not name:
            continue
        etype = ev.get("event_type")
        if etype == "queued":
            state[name] = {
                "artifact_name": name,
                "artifact_version": ev.get("artifact_version"),
                "requested_by": ev.get("requested_by"),
                "requested_at": ev.get("event_ts"),
                "prl1_only": bool(ev.get("prl1_only")),
                "df_only": bool(ev.get("df_only")),
                "note": ev.get("note") or "",
                "deployment_repo": ev.get("deployment_repo") or "",
                "build_verified": ev.get("build_verified"),
                "jira_ticket": ev.get("jira_ticket") or "",
                "change_details": ev.get("change_details") or "",
                "build_run_url": ev.get("build_run_url") or "",
            }
        elif etype in ("withdrawn", "released"):
            state.pop(name, None)
    return sorted(state.values(), key=lambda x: x.get("requested_at") or "")


def last_shipped(events: list[dict[str, Any]], artifact_name: str) -> dict[str, Any] | None:
    """Most recent 'released' event for a chart — powers "same as last time?"
    suggestions (which release, which version)."""
    for ev in reversed(events):
        if ev.get("event_type") == "released" and ev.get("artifact_name") == artifact_name:
            return {
                "release_name": ev.get("release_name"),
                "version": ev.get("artifact_version"),
                "released_at": ev.get("event_ts"),
            }
    return None


def last_queued_flags(events: list[dict[str, Any]], artifact_name: str) -> dict[str, Any] | None:
    """Flags from the chart's most recent PAST queue event (before the current
    one) — lets the bot suggest "PRL1-only again, like last time?"."""
    seen_current = False
    for ev in reversed(events):
        if ev.get("event_type") == "queued" and ev.get("artifact_name") == artifact_name:
            if not seen_current:
                seen_current = True  # skip the entry just written
                continue
            return {"prl1_only": bool(ev.get("prl1_only")), "df_only": bool(ev.get("df_only"))}
    return None


def current_queue(use_cache: bool = True) -> dict[str, Any]:
    """The derived next-release queue. Cached for _QUEUE_TTL_SECONDS because a
    BQ query job costs seconds and every form open needs this; writes clear the
    cache, so a queue/withdraw is reflected immediately."""
    import time

    if not queue_enabled():
        return _disabled()
    if use_cache and _queue_cache["value"] is not None:
        if time.time() - _queue_cache["at"] < _QUEUE_TTL_SECONDS:
            return _queue_cache["value"]
    try:
        events = _fetch_events()
        result = {"ok": True}
        queue = reduce_queue(events)
        result.update(queue=queue, count=len(queue), events_considered=len(events))
    except Exception as e:
        # Cache FAILURES too (briefly): a misconfigured or not-yet-provisioned
        # table would otherwise cost a doomed BQ round trip on every form open.
        result = {"ok": False, "error": f"BigQuery unavailable: {e}"}
    _queue_cache["at"] = time.time()
    _queue_cache["value"] = result
    return result


def cached_queue_count() -> int | None:
    """Banner-friendly count: at most one BQ query per minute; None on any issue."""
    import time

    if not queue_enabled():
        return None
    now = time.time()
    if now - _count_cache["at"] < 60:
        return _count_cache["count"]
    result = current_queue()
    _count_cache["at"] = now
    _count_cache["count"] = result.get("count") if result.get("ok") else None
    return _count_cache["count"]
