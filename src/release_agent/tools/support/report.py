"""PURE: the query results → the triage report.

What failed for the business date, how widely (one upstream system? one
process? one unit?), the distinct errors, new or recurring, stuck and missing
runs, retries, output that collapsed on a run that "succeeded".
"""
from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime, timezone
from typing import Any

from .config import _KEY_ROLES, _MAX_LIST, DIMENSIONS

# A shared upstream system/source or process is the CAUSE only when most of its
# runs that day failed — 4 failures out of a feed's 24 runs is not that feed.
_SATURATED = 0.8
# A value is a pattern when it carries at least this share of the failures…
_PATTERN_SHARE = 0.6
# …and at least this many of them (2 failures sharing a value is coincidence).
_PATTERN_MIN = 3
_ERROR_SAMPLE_CHARS = 300
_SIGNATURE_CHARS = 160

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
