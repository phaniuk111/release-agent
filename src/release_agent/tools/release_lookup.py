"""Release lookup: what is deployed where for a chart/image, and what changed lately.

READ-ONLY over the `release_intents` event log (invariant 3: the log is
append-only and optional). It answers the two questions an investigator asks
first when a run broke — "which version is live on prd, since when, on whose
ticket?" and "what changed in the days before it broke?" — from the same
"latest event wins" rule `release_queue.aggregate_env_state` uses: an artifact
is deployed in an environment while its latest deployed/removed event there is
'deployed'.

    lookup(name, days=14, as_of=None) -> {ok, name, match, matched, current,
                                          by_artifact, changes, summary, ...}
    what_changed(names, before, days=7) -> the changes of several names at once,
                                           merged newest first, for the triage card

Two parameterised statements per call (the name matches, then their events);
nothing is ever interpolated into SQL but the configured table id. Dates are
UTC days: `as_of` and `before` count up to the END of that day, so "what had
changed by that COB" includes the day itself.
"""
from __future__ import annotations

import string
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ..config import settings
from . import release_queue

MAX_MATCHES = 5          # a substring that hits more artifacts is too vague to answer
MAX_CHANGES = 50
MAX_NAMES = 10
MAX_NAME_LENGTH = 128
_LISTED_WHEN_TOO_MANY = 20
_NAME_CHARS = frozenset(string.ascii_letters + string.digits + "-_./")
# 'deployed'/'removed' make the state; 'released'/'queued' only lend a deploy the
# release name and ticket that deploy events were not written with.
_STATE_TYPES = ("deployed", "removed")
_CONTEXT_TYPES = ("released", "queued")
_ENV_ORDER = ("sit", "uat", "prl1", "prd")


def _permission_hint() -> str:
    return ("This account needs BigQuery Data Viewer (roles/bigquery.dataViewer) on the "
            f"{settings.bq_dataset or 'release_intents'} dataset, and BigQuery Job User "
            "on the project the query runs in.")


# ----- pure rules ---------------------------------------------------------------

def validate_name(name: Any) -> tuple[str, str | None]:
    """(clean name, problem). Letters, digits and - _ . / only — checked char by
    char because the name goes into a query as a parameter and into a sentence
    the model reads back."""
    clean = str(name or "").strip()
    if not clean:
        return "", "Give a chart or image name."
    if len(clean) > MAX_NAME_LENGTH:
        return "", f"The name is longer than {MAX_NAME_LENGTH} characters."
    bad = sorted({c for c in clean if c not in _NAME_CHARS})
    if bad:
        return "", ("A name may hold letters, digits and - _ . / only; found "
                    + " ".join(repr(c) for c in bad) + ".")
    return clean, None


def pick_match(name: str, candidates: list[str]) -> tuple[list[str], str, str | None]:
    """(matched artifacts, 'exact' | 'substring' | 'none', refusal). An exact
    name wins even when longer names contain it; otherwise a case-insensitive
    substring, refused above MAX_MATCHES so a vague word never reads as an answer."""
    names = sorted({c for c in candidates if c})
    if name in names:
        return [name], "exact", None
    low = name.lower()
    hits = [c for c in names if low in c.lower()]
    if not hits:
        return [], "none", None
    if len(hits) > MAX_MATCHES:
        shown = ", ".join(hits[:_LISTED_WHEN_TOO_MANY])
        more = f" (and {len(hits) - _LISTED_WHEN_TOO_MANY} more)" if len(hits) > _LISTED_WHEN_TOO_MANY else ""
        return hits, "substring", (f"{name!r} matches {len(hits)} artifacts — more than {MAX_MATCHES}. "
                                   f"Use a fuller name: {shown}{more}.")
    return hits, "substring", None


def _ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _until(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=timezone.utc) + timedelta(days=1)


def _context_for(events: list[dict[str, Any]]) -> dict[tuple[str, str], list[dict[str, Any]]]:
    """released/queued events by (artifact, version), oldest first."""
    out: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for ev in events:
        if ev.get("event_type") in _CONTEXT_TYPES and ev.get("artifact_version"):
            out.setdefault((ev.get("artifact_name") or "", ev["artifact_version"]), []).append(ev)
    return out


def _lend(field: str, ev: dict[str, Any], ts: datetime | None,
          context: dict[tuple[str, str], list[dict[str, Any]]]) -> Any:
    """The value on the event itself, else the latest earlier released/queued
    event for the same artifact and version that has one."""
    if ev.get(field):
        return ev[field]
    for cand in reversed(context.get((ev.get("artifact_name") or "", ev.get("artifact_version") or ""), [])):
        cts = _ts(cand.get("event_ts"))
        if cand.get(field) and (ts is None or cts is None or cts <= ts):
            return cand[field]
    return None


def _entry(ev: dict[str, Any], ts: datetime, context) -> dict[str, Any]:
    return {
        "version": ev.get("artifact_version"),
        "since": ts.isoformat(),
        "event_type": ev.get("event_type"),
        "requested_by": ev.get("requested_by"),
        "jira_ticket": _lend("jira_ticket", ev, ts, context),
        "release_name": _lend("release_name", ev, ts, context),
        "pr_number": ev.get("pr_number"),
    }


def derive(name: str, events: list[dict[str, Any]], *, until: datetime) -> dict[str, Any]:
    """One artifact's picture from its events (any order): what is deployed in
    each environment as of `until`, every state change with the version it
    replaced (oldest first), and its routing flags."""
    rows = sorted(((ts, ev) for ev in events if (ts := _ts(ev.get("event_ts"))) is not None and ts < until),
                  key=lambda pair: pair[0])
    context = _context_for([ev for _when, ev in rows])
    state: dict[str, dict[str, Any] | None] = {}
    changes: list[dict[str, Any]] = []
    flags: dict[str, bool] = {}
    for ts, ev in rows:
        etype = ev.get("event_type")
        if etype == "queued":
            for key in ("prl1_only", "df_only"):
                if ev.get(key) is not None:
                    flags[key] = bool(ev[key])
        if etype not in _STATE_TYPES:
            continue
        env = release_queue._norm_env(ev.get("environment"))
        if not env:
            continue
        previous = state.get(env)
        entry = _entry(ev, ts, context)
        changes.append({
            "artifact": name,
            "when": ts.isoformat(),
            "environment": env,
            "from_version": previous["version"] if previous else None,
            "to_version": None if etype == "removed" else ev.get("artifact_version"),
            "event_type": etype,
            "requested_by": entry["requested_by"],
            "jira_ticket": entry["jira_ticket"],
            "release_name": entry["release_name"],
            "pr_number": entry["pr_number"],
            "note": ev.get("note") or None,
        })
        state[env] = None if etype == "removed" else entry
    return {"current": {env: e for env, e in state.items() if e}, "changes": changes, "flags": flags}


def _env_sort(env: str) -> tuple[int, str]:
    return (_ENV_ORDER.index(env), env) if env in _ENV_ORDER else (len(_ENV_ORDER), env)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _on(version: str | None, env: str, entry: dict[str, Any]) -> str:
    since = (entry.get("since") or "")[:10]
    said = [t for t in (entry.get("jira_ticket"),
                        f"release {entry['release_name']}" if entry.get("release_name") else None) if t]
    return f"{version} on {env} since {since}" + (f" ({', '.join(said)})" if said else "")


def _current_sentence(name: str, current: dict[str, dict[str, Any]]) -> str:
    if not current:
        return f"{name}: not deployed on any environment the log records"
    parts = [_on(current[env]["version"], env, current[env]) for env in sorted(current, key=_env_sort)]
    return f"{name}: " + ", ".join(parts)


def _days_between(later: date, change_when: str) -> int:
    stamp = _ts(change_when)
    return max((later - stamp.date()).days, 0) if stamp else 0


def _newest_first(changes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(changes, key=lambda c: c["when"], reverse=True)


def _in_window(changes: list[dict[str, Any]], since: datetime) -> list[dict[str, Any]]:
    return [c for c in changes if (_ts(c["when"]) or since) >= since]


# ----- BigQuery -----------------------------------------------------------------

def _run(sql: str, params: list[Any]) -> tuple[list[dict[str, Any]], int]:
    """(rows, bytes billed) through release_queue's client — the same project,
    location and credentials as every other read of the log."""
    from google.cloud import bigquery

    client = release_queue._get_client()
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=params),
        location=settings.bq_location or None,
    )
    rows = [dict(r) for r in job.result()]
    return rows, int(getattr(job, "total_bytes_billed", 0) or 0)


def _candidates(needles: list[str]) -> tuple[list[str], int]:
    from google.cloud import bigquery

    sql = (
        f"SELECT DISTINCT artifact_name FROM `{release_queue._table_id()}` "
        "WHERE artifact_name IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM UNNEST(@needles) AS n WHERE STRPOS(LOWER(artifact_name), n) > 0) "
        "ORDER BY artifact_name LIMIT 500"
    )
    rows, billed = _run(sql, [bigquery.ArrayQueryParameter("needles", "STRING", [n.lower() for n in needles])])
    return [r["artifact_name"] for r in rows], billed


def _events(artifacts: list[str], until: datetime) -> tuple[list[dict[str, Any]], int]:
    from google.cloud import bigquery

    sql = (
        "SELECT event_id, event_type, event_ts, requested_by, artifact_name, artifact_version, "
        "prl1_only, df_only, note, release_name, pr_number, environment, jira_ticket "
        f"FROM `{release_queue._table_id()}` "
        "WHERE artifact_name IN UNNEST(@names) "
        "AND event_type IN UNNEST(@types) AND event_ts < @until "
        "ORDER BY event_ts, event_id"
    )
    return _run(sql, [
        bigquery.ArrayQueryParameter("names", "STRING", artifacts),
        bigquery.ArrayQueryParameter("types", "STRING", list(_STATE_TYPES + _CONTEXT_TYPES)),
        bigquery.ScalarQueryParameter("until", "TIMESTAMP", until),
    ])


def _failure(e: Exception) -> dict[str, Any]:
    msg = f"{type(e).__name__}: {str(e)[:300]}"
    out: dict[str, Any] = {"ok": False, "error": msg}
    low = msg.lower()
    if "403" in low or "forbidden" in low or "access denied" in low or "permission" in low:
        out["hint"] = _permission_hint()
    return out


def _gather(names: list[str], until: datetime) -> dict[str, Any]:
    """Resolve each name to artifacts, then read all their events once."""
    found, billed = _candidates(names)
    resolved = {n: pick_match(n, found) for n in names}
    wanted = sorted({a for matched, _how, refusal in resolved.values() if not refusal for a in matched})
    events: list[dict[str, Any]] = []
    if wanted:
        events, more = _events(wanted, until)
        billed += more
    by_artifact: dict[str, list[dict[str, Any]]] = {a: [] for a in wanted}
    for ev in events:
        by_artifact.setdefault(ev.get("artifact_name") or "", []).append(ev)
    return {"resolved": resolved, "events": by_artifact, "bytes_billed": billed}


def _bad_day(label: str, value: Any) -> str | None:
    if isinstance(value, datetime) or not isinstance(value, date):
        return f"{label} must be a date."
    return None


# ----- entry points -------------------------------------------------------------

def lookup(name: str, *, days: int = 14, as_of: date | None = None) -> dict[str, Any]:
    """What is deployed where for ``name`` (exact artifact name, else a
    case-insensitive substring) and what changed in the last ``days`` days up
    to ``as_of`` (default today, UTC). Never raises."""
    if not release_queue.queue_enabled():
        return release_queue._disabled()
    clean, problem = validate_name(name)
    if problem:
        return {"ok": False, "error": problem}
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        return {"ok": False, "error": "days must be a whole number from 1 to 365."}
    day = as_of or datetime.now(timezone.utc).date()
    problem = _bad_day("as_of", day)
    if problem:
        return {"ok": False, "error": problem}
    until = _until(day)
    try:
        got = _gather([clean], until)
    except Exception as e:  # BigQuery's own errors carry the useful text
        return _failure(e)
    matched, how, refusal = got["resolved"][clean]
    base: dict[str, Any] = {"name": clean, "as_of": day.isoformat(), "days": days,
                            "match": how, "matched": matched, "bytes_billed": got["bytes_billed"]}
    if refusal:
        return {"ok": False, "error": refusal, **base}
    if not matched:
        return {"ok": True, **base, "current": {}, "by_artifact": {}, "changes": [],
                "total_changes": 0, "changed_within_days": None,
                "summary": f"{clean}: no artifact with that name in the release log."}
    since = until - timedelta(days=days)
    derived = {a: derive(a, got["events"].get(a, []), until=until) for a in matched}
    every = _newest_first([c for d in derived.values() for c in d["changes"]])
    window = _in_window(every, since)
    current = {a: d["current"] for a, d in derived.items()}
    sentence = " | ".join(_current_sentence(a, current[a]) for a in matched)
    out = {
        "ok": True, **base,
        "current": current[matched[0]] if len(matched) == 1 else {},
        "by_artifact": current,
        "flags": {a: d["flags"] for a, d in derived.items() if d["flags"]},
        "changes": window[:MAX_CHANGES],
        "total_changes": len(window),
        "changed_within_days": _days_between(day, every[0]["when"]) if every else None,
        "summary": f"{sentence}; {_plural(len(window), 'change')} in {days} days",
    }
    if len(matched) > 1:
        out["note"] = "Several artifacts matched; read by_artifact — `current` is left empty."
    return out


def what_changed(names: list[str], before: date, days: int = 7) -> dict[str, Any]:
    """The deployments and removals of several charts/images in the ``days``
    days up to the end of ``before`` (UTC), merged newest first — what the
    triage shows beside a failure. At most MAX_NAMES names; each is matched as
    in `lookup`. Never raises."""
    if not release_queue.queue_enabled():
        return release_queue._disabled()
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 365:
        return {"ok": False, "error": "days must be a whole number from 1 to 365."}
    problem = _bad_day("before", before)
    if problem:
        return {"ok": False, "error": problem}
    wanted: list[str] = []
    skipped: dict[str, str] = {}
    for raw in list(names or []):
        clean, bad = validate_name(raw)
        if bad:
            skipped[str(raw)[:MAX_NAME_LENGTH]] = bad
        elif clean not in wanted:
            wanted.append(clean)
    dropped = wanted[MAX_NAMES:]
    wanted = wanted[:MAX_NAMES]
    if not wanted:
        return {"ok": False, "error": "Give at least one valid chart or image name.", "skipped": skipped}
    until = _until(before)
    try:
        got = _gather(wanted, until)
    except Exception as e:
        return _failure(e)
    matched: dict[str, list[str]] = {}
    for n in wanted:
        hits, _how, refusal = got["resolved"][n]
        if refusal:
            skipped[n] = refusal
        elif not hits:
            skipped[n] = "no artifact with that name in the release log"
        else:
            matched[n] = hits
    since = until - timedelta(days=days)
    artifacts = sorted({a for hits in matched.values() for a in hits})
    every = _newest_first([c for a in artifacts for c in derive(a, got["events"].get(a, []), until=until)["changes"]])
    window = _in_window(every, since)
    shown = window[:MAX_CHANGES]
    named = ", ".join(artifacts) or "no known artifact"
    if window:
        newest = window[0]
        latest = f"{newest['artifact']} {newest['to_version'] or 'removed'} on {newest['environment']}, {newest['when'][:10]}"
        summary = (f"{_plural(len(window), 'change')} in the {days} days up to {before.isoformat()} "
                   f"across {named} (latest: {latest})")
    else:
        summary = f"No changes in the {days} days up to {before.isoformat()} across {named}"
    out: dict[str, Any] = {
        "ok": True, "names": wanted, "matched": matched, "before": before.isoformat(), "days": days,
        "changes": shown, "total_changes": len(window), "summary": summary,
        "bytes_billed": got["bytes_billed"],
    }
    if skipped:
        out["skipped"] = skipped
    if dropped:
        out["dropped"] = dropped
    return out
