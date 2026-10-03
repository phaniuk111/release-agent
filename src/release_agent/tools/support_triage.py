"""Support triage: a first look at a workflow control table.

Many pipelines log one row per run (or per status change) in a control table:
a business date, a run id, a status, often an error message and a job id. This
reads that table — READ-ONLY, one fixed query through tools/bq_guard.py — and
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
    analyse(rows, ...)       -> PURE: the analysis over already-read rows
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


# ----- the query ----------------------------------------------------------------

def build_sql(table_ref: str, columns: dict[str, str]) -> str:
    """ONE fixed statement: the latest row of every run in the date window.
    Columns come back under their ROLE names (r_<role>), so nothing downstream
    knows the table's own names. Dates and the row cap are parameters."""
    select = ", ".join(f"{col} AS r_{role}" for role, col in columns.items())
    sql = (f"SELECT {select} FROM {table_ref} "
           f"WHERE {columns['date']} BETWEEN @start AND @end")
    if "updated_at" in columns:
        sql += (f" QUALIFY ROW_NUMBER() OVER (PARTITION BY {columns['run_id']} "
                f"ORDER BY {columns['updated_at']} DESC) = 1")
    return sql + " LIMIT @max_rows"


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


def _read(table_ref: str, columns: dict[str, str], start: date, end: date) -> tuple[list[dict], int]:
    from google.cloud import bigquery

    sql = build_sql(table_ref, columns)
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("start", "DATE", start),
        bigquery.ScalarQueryParameter("end", "DATE", end),
        bigquery.ScalarQueryParameter("max_rows", "INT64", int(settings.support_max_rows)),
    ])
    job = bq_guard.run(_get_client(), sql, job_config=cfg, allowed_tables=(table_ref,),
                       label="support_triage")
    rows = [{k[2:]: v for k, v in dict(r).items()} for r in job.result()]
    return rows, int(getattr(job, "total_bytes_processed", 0) or 0)


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


def _state(status: str, failed: set[str], done: set[str]) -> str:
    s = status.strip().upper()
    if s in failed:
        return "failed"
    if s in done:
        return "done"
    return "open"


# ----- the analysis (pure) ----------------------------------------------------------

def _normalise(raw: dict[str, Any], dims: list[str], failed: set[str], done: set[str],
               detail_keys: list[str]) -> dict[str, Any] | None:
    d = _to_date(raw.get("date"))
    run_id = _text(raw.get("run_id"))
    if d is None or not run_id:
        return None
    status = _text(raw.get("status"))
    return {
        "date": d,
        "run_id": run_id,
        "status": status,
        "state": _state(status, failed, done),
        "updated_at": _to_dt(raw.get("updated_at")),
        "event_at": _to_dt(raw.get("event_at")),
        "event_id": _text(raw.get("event_id")) or None,
        "job_id": _text(raw.get("job_id")) or None,
        "error": _text(raw.get("error")),
        "details": _detail_values(raw.get("details"), detail_keys),
        "members": _to_int(raw.get("members")),
        "dims": {r: (_text(raw.get(r)) or "(blank)") for r in dims},
    }


def _latest(runs: list[dict]) -> dict:
    """The run written last — rows without a timestamp keep their read order."""
    floor = datetime.min.replace(tzinfo=timezone.utc)
    return max(enumerate(runs), key=lambda p: (p[1]["updated_at"] or floor, p[0]))[1]


def _key(run: dict, key_roles: list[str]) -> tuple:
    return tuple(run["dims"][r] for r in key_roles)


def _key_dict(key: tuple, key_roles: list[str]) -> dict[str, str]:
    return dict(zip(key_roles, key))


def _iso(v: date | datetime | None) -> str | None:
    return v.isoformat() if v else None


def analyse(rows: list[dict[str, Any]], *, roles: list[str], business_date: date | None,
            now: datetime, failed_statuses: list[str], done_statuses: list[str],
            stuck_minutes: int = 120, detail_keys: list[str] | None = None) -> dict[str, Any]:
    """PURE. ``rows`` are role-keyed (date, run_id, status, …) — already the
    latest row per run, or several rows per run (the latest is taken here
    too). Returns the triage report; see triage()."""
    failed_set = {s.upper() for s in failed_statuses}
    done_set = {s.upper() for s in done_statuses}
    dims = [r for r in DIMENSIONS if r in roles]
    key_roles = [r for r in _KEY_ROLES if r in roles]
    detail_keys = detail_keys or []
    notes: list[str] = []

    by_run: dict[str, list[dict]] = {}
    for raw in rows:
        n = _normalise(raw, dims, failed_set, done_set, detail_keys)
        if n is not None:
            by_run.setdefault(f"{n['date'].isoformat()}|{n['run_id']}", []).append(n)
    runs = [_latest(v) for v in by_run.values()]
    dates = sorted({r["date"] for r in runs})
    if business_date is None:
        business_date = dates[-1] if dates else None
    base = {"business_date": _iso(business_date), "dates_read": [d.isoformat() for d in dates]}
    if business_date is None or business_date not in dates:
        return {**base, "previous_date": None, "empty": True,
                "counts": {"items": 0, "failed": 0, "done": 0, "open": 0, "stuck": 0,
                           "missing": 0, "recovered": 0},
                "patterns": [], "errors": [], "failures": [], "stuck": [], "missing": [],
                "retries": [], "volume": [], "slowest": [], "notes": notes}
    earlier = [d for d in dates if d < business_date]
    previous = earlier[-1] if earlier else None

    # A piece of work on one date: retries of one event are ONE item, judged by
    # its latest run — a failure that a retry then fixed is not a failure now.
    def items_on(d: date) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for r in runs:
            if r["date"] == d:
                out.setdefault(r["event_id"] or r["run_id"], []).append(r)
        return out

    today_items = items_on(business_date)
    latest_today = {k: _latest(v) for k, v in today_items.items()}
    failures = [r for r in latest_today.values() if r["state"] == "failed"]
    if "event_id" not in roles:
        notes.append("No event_id column mapped: a retry counts as a separate run.")

    # Key state per date, for "new or recurring" and "missing".
    state_by_key: dict[tuple, dict[date, dict]] = {}
    if key_roles:
        for d in dates:
            for runs_of_item in items_on(d).values():
                last = _latest(runs_of_item)
                k = _key(last, key_roles)
                prior = state_by_key.setdefault(k, {}).get(d)
                if prior is None or _latest([prior, last]) is last:
                    state_by_key[k][d] = last
    else:
        notes.append("No system/source/process/unit/scope column mapped: "
                     "recurrence and missing work cannot be worked out.")

    def history(k: tuple) -> tuple[int, str | None]:
        """(dates failing in a row up to and including the business date,
        the last earlier date it succeeded)."""
        seen = state_by_key.get(k, {})
        streak, last_ok, counting = 0, None, True
        # Dates the work did not run at all (weekends, holidays) are skipped.
        for d in sorted((d for d in seen if d <= business_date), reverse=True):
            st = seen[d]["state"]
            if counting and st == "failed":
                streak += 1
                continue
            counting = False
            if st == "done":
                last_ok = d
                break
        return streak, _iso(last_ok)

    # Errors: one cluster per signature.
    clusters: dict[str, list[dict]] = {}
    for r in failures:
        clusters.setdefault(signature(r["error"]), []).append(r)
    errors = []
    runs_with = {r: Counter(x["dims"][r] for x in latest_today.values()) for r in dims}
    failed_with = {r: Counter(f["dims"][r] for f in failures) for r in dims}

    def saturated(role: str, value: str) -> bool:
        total = runs_with[role][value]
        return bool(total) and failed_with[role][value] / total >= _SATURATED

    for sig, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        spread = {r: Counter(m["dims"][r] for m in members) for r in dims}
        shared = {r: next(iter(c)) for r, c in spread.items() if len(c) == 1}
        units = len(spread["unit"]) if "unit" in spread else len(members)
        upstream = next((r for r in ("system", "source") if r in shared and saturated(r, shared[r])), None)
        if upstream and units >= 2:
            category, cause = "upstream", upstream
        elif "process" in shared and units >= 2:
            # every unit of the process, or a subset of it sharing one error:
            # either way the process (or its input for that subset), not a feed outage
            category, cause = "process", "process"
        elif "unit" in spread and units == 1:
            category, cause = "unit", "unit"
        elif len(members) == 1:
            category, cause = "single", None
        elif units >= 2 and ("system" in shared or "source" in shared):
            category, cause = "upstream", "system" if "system" in shared else "source"
        else:
            category, cause = "spread", None
        hist = [history(_key(m, key_roles)) for m in members] if key_roles else []
        recurring = sum(1 for streak, _ in hist if streak > 1)
        last_oks = [ok for _, ok in hist if ok]
        details = Counter(f"{k}={v}" for m in members for k, v in m["details"].items())
        errors.append({
            "signature": sig,
            "sample": (members[0]["error"] or "")[:_ERROR_SAMPLE_CHARS],
            "count": len(members),
            "category": category,
            "cause": {cause: shared[cause]} if cause and cause in shared else {},
            "shared": shared,
            "spread": {r: len(c) for r, c in spread.items()},
            "top": {r: c.most_common(3) for r, c in spread.items() if len(c) > 1},
            "details": details.most_common(3),
            "recurring": recurring,
            "new": len(members) - recurring if key_roles else None,
            "job_ids": [m["job_id"] for m in members if m["job_id"]][:3],
            "max_streak": max((streak for streak, _ in hist), default=None),
            "last_success": max(last_oks) if last_oks else None,
        })

    # Patterns across all failures: one value carrying most of them.
    patterns = []
    for r in dims:
        if not failures:
            break
        value, n = Counter(f["dims"][r] for f in failures).most_common(1)[0]
        if n >= _PATTERN_MIN and n / len(failures) >= _PATTERN_SHARE:
            runs_with = sum(1 for x in latest_today.values() if x["dims"][r] == value)
            patterns.append({"role": r, "value": value, "failed": n, "of_failed": len(failures),
                             "runs_with_value": runs_with})

    sig_index = {e["signature"]: i for i, e in enumerate(errors)}
    failure_rows = []
    for f in failures:
        streak, last_ok = history(_key(f, key_roles)) if key_roles else (None, None)
        failure_rows.append({"run_id": f["run_id"], "status": f["status"], "dims": f["dims"],
                             "job_id": f["job_id"], "error_index": sig_index[signature(f["error"])],
                             "streak": streak, "last_success": last_ok,
                             "attempts": len(today_items[f["event_id"] or f["run_id"]])})
    failure_rows.sort(key=lambda x: (-(x["streak"] or 0), x["error_index"]))

    # Stuck: neither failed nor done, and not written to for too long.
    limit = now - timedelta(minutes=int(stuck_minutes))
    stuck = []
    for r in latest_today.values():
        if r["state"] != "open":
            continue
        if r["updated_at"] is not None and r["updated_at"] > limit:
            continue
        age = int((now - r["updated_at"]).total_seconds() // 60) if r["updated_at"] else None
        stuck.append({"run_id": r["run_id"], "status": r["status"], "dims": r["dims"],
                      "job_id": r["job_id"], "minutes_since_update": age})
    stuck.sort(key=lambda x: -(x["minutes_since_update"] or 0))
    if "updated_at" not in roles:
        notes.append("No updated_at column mapped: every unfinished run is reported as stuck.")

    # Missing: worked on the previous date, nothing at all on this one.
    missing = []
    if key_roles and previous is not None:
        today_keys = {_key(r, key_roles) for r in latest_today.values()}
        for k, seen in state_by_key.items():
            if previous in seen and k not in today_keys:
                missing.append({"dims": _key_dict(k, key_roles), "previous_status": seen[previous]["status"]})

    # Retries: an item with more than one run today.
    retries = []
    recovered = 0
    for item, item_runs in today_items.items():
        if len(item_runs) < 2:
            continue
        last = _latest(item_runs)
        if last["state"] == "done" and any(x["state"] == "failed" for x in item_runs):
            recovered += 1
        retries.append({"item": item, "attempts": len(item_runs), "final_status": last["status"],
                        "final_state": last["state"], "dims": last["dims"]})
    retries.sort(key=lambda x: -x["attempts"])

    # Volume: a "successful" run whose output collapsed against the previous date.
    volume = []
    if key_roles and previous is not None and "members" in roles:
        for r in latest_today.values():
            if r["state"] != "done" or r["members"] is None:
                continue
            prev = state_by_key.get(_key(r, key_roles), {}).get(previous)
            before = prev["members"] if prev else None
            if before is None:
                continue
            if (r["members"] == 0 and before > 0) or (before >= 10 and r["members"] < before * 0.5):
                volume.append({"run_id": r["run_id"], "dims": r["dims"], "members": r["members"],
                               "previous_members": before})
        volume.sort(key=lambda x: x["members"] - x["previous_members"])

    # Slowest end to end: trigger → last row, for finished runs.
    slowest = []
    if "event_at" in roles and "updated_at" in roles:
        durations = [(int((r["updated_at"] - r["event_at"]).total_seconds() // 60), r)
                     for r in latest_today.values()
                     if r["state"] == "done" and r["updated_at"] and r["event_at"]]
        durations.sort(key=lambda p: -p[0])
        slowest = [{"run_id": r["run_id"], "dims": r["dims"], "minutes": m} for m, r in durations[:5]]

    counts = {
        "items": len(latest_today),
        "failed": len(failures),
        "done": sum(1 for r in latest_today.values() if r["state"] == "done"),
        "open": sum(1 for r in latest_today.values() if r["state"] == "open"),
        "stuck": len(stuck),
        "missing": len(missing),
        "recovered": recovered,
    }
    return {**base, "previous_date": _iso(previous), "empty": False, "counts": counts,
            "patterns": patterns, "errors": errors, "failures": failure_rows[:_MAX_LIST],
            "stuck": stuck[:_MAX_LIST], "missing": missing[:_MAX_LIST], "retries": retries[:_MAX_LIST],
            "volume": volume[:_MAX_LIST], "slowest": slowest, "notes": notes}


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


def _priority(kind: str, count: int, err: dict[str, Any] | None = None) -> str:
    if kind == "volume" or count >= 10:
        return "high"
    if err and err.get("category") in ("process", "spread", "upstream") and count >= 3:
        return "high"
    if kind in ("stuck", "missing") or count >= 3 or (err and (err.get("max_streak") or 0) > 1):
        return "medium"
    return "low"


def _note(inc: dict[str, Any], report: dict[str, Any], *, with_error_text: bool) -> str:
    """The ticket note: what L1 saw, what it checked, who it goes to."""
    head = f"[Support triage] {report.get('date_label', 'Business date')} {report.get('business_date')}: {inc['title']}"
    lines = [head, f"Priority: {inc['priority']} · Action: {inc['action']}"]
    lines += [f"- {fact}" for fact in inc["facts"]]
    if with_error_text and inc.get("error_text"):
        lines.append(f"- Error: {inc['error_text']}")
    if inc.get("job_ids"):
        lines.append(f"- Jobs: {', '.join(inc['job_ids'])}")
    lines.append(f"- Known issue: {inc['runbook'] or 'no runbook match'}")
    lines.append("- Next steps: " + " ".join(inc["steps"]))
    lines.append(f"- Suggested owner: {inc['owner']}")
    return "\n".join(lines)


def incidents(report: dict[str, Any], *, labels: dict[str, str], runbook: list[dict[str, Any]],
              owners: dict[str, str]) -> list[dict[str, Any]]:
    """PURE. The report's problems as L1 incidents, most urgent first."""
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
                    "priority": _priority("error", err["count"], err)})
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
                    "owner": owner, "priority": _priority("stuck", len(stuck))})
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
                    "owner": owner, "priority": _priority("missing", len(missing))})
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
                    "owner": owner, "priority": _priority("volume", len(volume))})
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
    end = wanted or now.date()
    start = end - timedelta(days=max(1, int(settings.support_lookback_days)))
    try:
        rows, scanned = _read(table_ref, columns, start, end)
    except Exception as e:  # BigQuery's own errors carry the useful text
        msg = f"{type(e).__name__}: {str(e)[:300]}"
        out = {"ok": False, "error": msg}
        hint = _permission_hint(msg)
        if hint:
            out["hint"] = hint
        return out
    report = analyse(rows, roles=list(columns), business_date=wanted, now=now,
                     failed_statuses=_names(settings.support_failed_statuses),
                     done_statuses=_names(settings.support_done_statuses),
                     stuck_minutes=settings.support_stuck_minutes,
                     detail_keys=_names(settings.support_detail_keys))
    truncated = len(rows) >= int(settings.support_max_rows)
    if truncated:
        report["notes"].append(f"Read the first {settings.support_max_rows} runs only (SUPPORT_MAX_ROWS).")
    out = {"ok": True, **report,
           "date_label": settings.support_date_label,
           "labels": {r: labels[r] for r in DIMENSIONS if r in labels},
           "window": {"start": start.isoformat(), "end": end.isoformat()},
           "bytes_processed": scanned,
           "scanned_at": now.replace(microsecond=0).isoformat(),
           "job_url": settings.support_job_url}
    runbook, problem = load_runbook(settings.support_runbook_file)
    if problem:
        out["notes"].append(problem)
    out["runbook_entries"] = len(runbook)
    out["incidents"] = incidents(out, labels=out["labels"], runbook=runbook,
                                 owners=parse_owners(settings.support_owners))
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
