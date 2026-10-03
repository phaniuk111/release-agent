"""PURE: every dated piece of evidence, merged into one time-ordered list.

Release changes, IAM changes, the first and last of each audit-denial group and
each log group, each failed run's own first line, first error and row, the
Dataflow job's milestones, the last success — capped and
de-duplicated, each line short and never a stack trace. It is what lets a person
check a finding against what actually happened, and when.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from ._common import _M_GROUPS, _iso, _line, _list, _ok
from .dataflow_job import _when as _parse_time
from .report import _to_date

_TIMELINE_MAX = 40


_Entry = tuple[datetime, str, str, str]   # (time, kind, source, text)


def _entries(evidence: dict[str, Any], last_success: str | None) -> list[_Entry]:
    out: list[_Entry] = []

    def add(when: Any, kind: str, source: str, text: str) -> None:
        moment = _parse_time(when) if not isinstance(when, datetime) else when
        if moment is not None and text:
            out.append((moment.astimezone(timezone.utc).replace(microsecond=0), kind, source, _line(text)))

    if last_success and (day := _to_date(last_success)):
        add(datetime(day.year, day.month, day.day, tzinfo=timezone.utc), "run", "control_table",
            f"Last successful run of this was on {day.isoformat()}")
    for c in _list((_ok(evidence.get("changes")) or {}).get("changes")):
        moved = (f"{c.get('from_version') or 'new'} → {c['to_version']}" if c.get("to_version") else "removed")
        add(c.get("when"), "release", "release_log", f"{c.get('artifact')} {moved} on {c.get('environment') or '?'}")
    audit = _ok(evidence.get("audit")) or {}
    for c in _list(audit.get("iam_changes")):
        add(c.get("time"), "iam", "audit", f"IAM policy changed on {c.get('resource') or '?'} by {c.get('principal') or '?'}")
    for g in _list(audit.get("denials"))[:_M_GROUPS]:
        what = f"{g.get('method') or '?'} on {g.get('resource') or '?'}"
        if g.get("first_seen") == g.get("last_seen"):
            add(g.get("first_seen"), "denied", "audit", f"Permission denied: {what} (×{g.get('count')})")
        else:
            add(g.get("first_seen"), "denied", "audit", f"First permission denied: {what} (×{g.get('count')} in all)")
            add(g.get("last_seen"), "denied", "audit", f"Last permission denied: {what}")
    logs = _ok(evidence.get("logs")) or {}
    for g in _list(logs.get("groups"))[:_M_GROUPS]:
        kind = "security" if g.get("security") else "log"
        what = _line(g.get("sample") or g.get("signature"), 110)
        if g.get("first_seen") == g.get("last_seen"):
            add(g.get("first_seen"), kind, "logs", f"Source logged: {what} (×{g.get('count')})")
        else:
            add(g.get("first_seen"), kind, "logs", f"Source first logged: {what} (×{g.get('count')} in all)")
            add(g.get("last_seen"), kind, "logs", f"Source last logged: {what}")
    for run in _list(evidence.get("runs")):
        if not _ok(run):
            continue
        tag = f"Run {run.get('run_id')}"
        groups = [g for g in _list(run.get("groups")) if g.get("first_seen")]
        if groups:
            first = min(groups, key=lambda g: g["first_seen"])
            add(first["first_seen"], "run", "run_logs", f"{tag} first logged: {_line(first.get('sample'), 110)}")
        if (err := run.get("first_error")):
            add(err.get("time"), "run", "run_logs",
                f"{tag} first error ({err.get('component') or '?'}): {_line(err.get('text'), 110)}")
        add(run.get("at"), "run", "control_table", f"{tag}: its control-table row written")
    for job in _list(evidence.get("dataflow")):
        if not _ok(job):
            continue
        tag = f"Dataflow job …{str(job.get('job_id') or '')[-8:]}"
        add(job.get("created"), "job", "dataflow", f"{tag} created")
        if job.get("ended"):
            add(job.get("ended"), "job", "dataflow",
                f"{tag} ended {str(job.get('state') or '').replace('JOB_STATE_', '').lower()}"
                + (f" ({job['kind']})" if job.get("kind") not in (None, "none") else ""))
        timed = [(t, e) for e in _list(job.get("errors")) if (t := _parse_time(e.get("time")))]
        if timed:
            _, first = min(timed, key=lambda pair: pair[0])
            add(first.get("time"), "job", "dataflow", f"{tag} first error: {_line(first.get('text'), 110)}")
    return out


def _dedupe(entries: list[_Entry]) -> list[_Entry]:
    seen: set[_Entry] = set()
    out = []
    for e in sorted(entries, key=lambda e: (e[0], e[1], e[3])):
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out


def _capped(items: list[Any], limit: int, group_of: Callable[[Any], tuple]) -> list[Any]:
    """At most `limit` of the ascending `items`: the earliest of each group
    first (how it started), then the latest of the rest (where it ended up)."""
    if len(items) <= limit:
        return items
    keep: dict[int, None] = {}
    firsts: dict[tuple, int] = {}
    for i, item in enumerate(items):
        firsts.setdefault(group_of(item), i)
    for i in firsts.values():
        if len(keep) < limit:
            keep[i] = None
    for i in range(len(items) - 1, -1, -1):
        if len(keep) >= limit:
            break
        keep.setdefault(i, None)
    return [items[i] for i in sorted(keep)]


def build_timeline(evidence: dict[str, Any], last_success: str | None = None) -> list[dict[str, str]]:
    """PURE. Everything with a time, ascending, de-duplicated, at most 40."""
    return [{"time": _iso(t), "kind": kind, "source": source, "text": text}
            for t, kind, source, text in _capped(_dedupe(_entries(evidence, last_success)), _TIMELINE_MAX,
                                                 lambda e: (e[1], e[2]))]
