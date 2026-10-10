"""Auto-triage's findings: what it found, where they are kept, who is told.

A pass (adk_release_agent/support_watch.py) triages the control table,
investigates each NEW problem the way the Investigate button does, and turns
the outcome into a FINDING — one per incident, built here from the incident and
the investigation (``finding_from``, PURE). A person then dismisses or resolves
it; the pass marks it cleared when the problem leaves the triage, and re-notifies
when the incident grew (more jobs than when it was found). Every change is a NEW
row in the portal event log (events_store, domain "watch" — AGENTS.md invariant
3): the latest row per (incident, business date) is the finding. A row carries
the typed watch columns; what is long (the timeline, the answer, the ticket
note, the job list) rides in ``details`` as JSON text.

Where findings live is PORTAL_EVENTS_DATASET's decision, like every other event:
a table shared by every pod, or this process's memory. Either way a read or a
write never raises — an error dict, as the feedback memory answers.

Notification: the portal's own bell (tools/notifications.py), one item per
incident — its ``scope`` makes later changes update that item instead of adding
more — to SUPPORT_NOTIFY_RECIPIENTS and the runbook owner when that is an email.
Read-only throughout: nothing here changes a job or a table; an action is a
proposal for a person.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import date, datetime, timezone
from typing import Any

from ...config import settings
from .. import events_store, notifications

_log = logging.getLogger(__name__)

DOMAIN = "watch"
STATUSES = ("open", "dismissed", "resolved", "cleared")
_PERSON_STATUSES = ("open", "dismissed", "resolved")
# A person's status change names its event; the watcher's own are finding,
# updated, cleared, and one "checked" row per pass.
_PERSON_EVENTS = {"open": "reopened", "dismissed": "dismissed", "resolved": "resolved"}
WATCHER = "watcher"
_PASS_SUBJECT = "pass"
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
# The bell knows three levels; what needs attention (open, high or critical)
# is red there, like on the card.
_SEVERITY = {"critical": "critical", "high": "critical", "medium": "warning", "low": "info"}
TIMED_OUT_CAUSE = "no cause established — evidence timed out"
NOT_ESTABLISHED = "Not established from the evidence"
_MAX_ANSWER = 6000
_MAX_NOTE = 2000
_MAX_JOBS = 50
_TIMELINE_ITEMS = 6
_MARKS = set("*_`#>")
# Findings are read back by business date; one written on the day is found with
# the default window, an older date is asked for with a window reaching it.
_MIN_DAYS = 14
_MAX_DAYS = 400


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _plain(line: str) -> str:
    """A model line without markdown marks or a list bullet."""
    text = "".join(ch for ch in line if ch not in _MARKS).strip()
    while text[:1] in ("-", "•"):
        text = text[1:].strip()
    return text


def _labelled(text: str, label: str) -> str:
    """The rest of the first line that starts with ``label`` (case-insensitive)."""
    want = label.lower()
    for raw in (text or "").splitlines():
        line = _plain(raw)
        if line.lower().startswith(want):
            return line[len(label):].strip()
    return ""


def configured() -> bool:
    """Auto-triage reads what Support triage reads: no table, nothing to watch."""
    return bool(settings.support_table.strip())


# ----- PURE: findings from incidents and investigations ---------------------------

def ranked(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PURE. Open first, then by priority, newest first within a priority."""
    newest = sorted(findings, key=lambda f: str(f.get("investigated_at") or ""), reverse=True)
    return sorted(newest, key=lambda f: (0 if f.get("status") == "open" else 1,
                                         _PRIORITY_RANK.get(str(f.get("priority") or ""), 9)))


def job_signature(incident: dict[str, Any]) -> list[str]:
    """PURE. The jobs (else the runs) an incident is made of, as a stable list:
    an incident keeps its id while its membership changes, so the finding keeps
    this list to tell "the same problem" from "the same problem, now wider"."""
    inc = incident or {}
    ids = [str(j) for j in (inc.get("job_ids") or []) if j]
    if not ids:
        ids = [str(r.get("run_id")) for r in (inc.get("runs") or []) if isinstance(r, dict) and r.get("run_id")]
    return sorted(set(ids))[:_MAX_JOBS]


def grew(finding: dict[str, Any], incident: dict[str, Any]) -> bool:
    """PURE. The incident has more runs than its finding, or a job the finding
    never saw — what a person must hear about again."""
    try:
        if int(incident.get("count") or 0) > int(finding.get("count") or 0):
            return True
    except (TypeError, ValueError):
        return False
    return bool(set(job_signature(incident)) - set(finding.get("jobs") or []))


def finding_from(incident: dict[str, Any], outcome: dict[str, Any], business_date: str) -> dict[str, Any]:
    """PURE. One finding from a triage incident and its investigation outcome
    ({"text", "bundle", "model", "model_calls", "seconds", "fallback",
    "timed_out"?}).

    The cause and the action are what CODE established (``with_judgement`` in the
    bundle's signals) and the answer's own "Cause:" / "Do now:" lines; when the
    evidence does not establish a cause, the finding says so — it never takes a
    guess for a finding. An investigation that ran out of time is a finding too,
    saying exactly that, so the pass moves on and the incident is not re-tried
    every few minutes."""
    inc = incident or {}
    out = outcome or {}
    bundle = out.get("bundle") if isinstance(out.get("bundle"), dict) else {}
    signals = bundle.get("signals") if isinstance(bundle.get("signals"), dict) else {}
    hint = signals.get("action_hint") if isinstance(signals.get("action_hint"), dict) else {}
    text = str(out.get("text") or "")
    timed_out = bool(out.get("timed_out"))
    established = bool(signals.get("conclusive")) and not timed_out
    cause = _labelled(text, "Cause:")
    if timed_out:
        cause = TIMED_OUT_CAUSE
    elif not established or not cause or cause.lower().startswith("not established"):
        cause = cause if (established and cause) else NOT_ESTABLISHED
    timeline = []
    for item in (bundle.get("timeline") or [])[:_TIMELINE_ITEMS]:
        if isinstance(item, dict):
            timeline.append({"time": str(item.get("time") or ""), "source": str(item.get("source") or ""),
                             "text": str(item.get("text") or "")[:240]})
    return {
        "business_date": business_date,
        "incident_id": str(inc.get("id") or ""),
        "status": "open",
        "title": str(inc.get("title") or ""),
        "category": str(inc.get("category") or ""),
        "priority": str(inc.get("priority") or ""),
        "count": int(inc.get("count") or 0),
        "jobs": job_signature(inc),
        # The source the incident's runs share (the control table's own value),
        # for the card's "source · owner · category" line.
        "source": str((inc.get("shared") or {}).get("source") or "") if isinstance(inc.get("shared"), dict) else "",
        "owner": str(inc.get("owner") or ""),
        "runbook": str(inc.get("runbook") or ""),
        "cause": cause[:400],
        "established": established,
        "do_now": _labelled(text, "Do now:")[:400],
        "action": str(hint.get("action") or inc.get("action") or ""),
        "action_why": str(hint.get("why") or ""),
        "timeline": timeline,
        "note": str(inc.get("note") or "")[:_MAX_NOTE],
        "answer": text[:_MAX_ANSWER],
        "model": str(out.get("model") or ""),
        "model_calls": int(out.get("model_calls") or 0),
        "seconds": float(out.get("seconds") or 0.0),
        "fallback": bool(out.get("fallback")) or timed_out,
        "notified": False,
        "investigated_at": _iso(_now()),
        "changed_at": "",
        "actor": WATCHER,
        "event_type": "finding",
    }


def updated_from(finding: dict[str, Any], incident: dict[str, Any]) -> dict[str, Any]:
    """PURE. The finding again, as wide as the incident is now: its count, jobs
    and title move, the investigation stays — a wider incident is the same
    problem, not a new one to investigate."""
    row = dict(finding)
    inc = incident or {}
    row.update(count=int(inc.get("count") or 0), jobs=job_signature(inc),
               title=str(inc.get("title") or finding.get("title") or ""),
               previous_count=int(finding.get("count") or 0),
               actor=WATCHER, event_type="updated", changed_at=_iso(_now()))
    return row


def new_incidents(report: dict[str, Any], known: set[str]) -> list[dict[str, Any]]:
    """PURE. The report's incidents the watcher has not recorded yet, in the
    triage's own order (most urgent first)."""
    return [i for i in (report.get("incidents") or []) if isinstance(i, dict) and i.get("id")
            and str(i["id"]) not in known]


def grown(findings: list[dict[str, Any]], report: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """PURE. (finding, incident) pairs where an OPEN finding's incident is wider
    now. Closed ones are a person's decision and stay closed."""
    present = {str(i.get("id")): i for i in (report.get("incidents") or []) if isinstance(i, dict) and i.get("id")}
    out = []
    for f in findings:
        inc = present.get(str(f.get("incident_id")))
        if f.get("status") == "open" and inc is not None and grew(f, inc):
            out.append((f, inc))
    return out


def gone(findings: list[dict[str, Any]], report: dict[str, Any]) -> list[dict[str, Any]]:
    """PURE. Open findings whose incident is no longer in the triage — the runs
    succeeded since, or the rows changed: the watcher marks them cleared."""
    present = {str(i.get("id")) for i in (report.get("incidents") or []) if isinstance(i, dict)}
    return [f for f in findings if f.get("status") == "open" and f.get("incident_id") not in present]


def with_status(finding: dict[str, Any], status: str, actor: str) -> dict[str, Any]:
    """PURE. The next row for a finding: same finding, new status, who and when."""
    row = dict(finding)
    row.update(status=status, actor=actor or "", changed_at=_iso(_now()),
               event_type=_PERSON_EVENTS.get(status, status) if actor != WATCHER else status)
    return row


def summary_line(finding: dict[str, Any]) -> str:
    """PURE. One line a bell item or a row's body says: what grew, the cause,
    what to do, who owns it."""
    parts = []
    previous = finding.get("previous_count")
    if finding.get("event_type") == "updated" and previous is not None:
        parts.append(f"Now {finding.get('count') or 0} jobs (was {previous})")
    if finding.get("cause"):
        parts.append(f"Cause: {finding['cause']}")
    todo = finding.get("do_now") or finding.get("action") or ""
    if todo:
        parts.append(f"Do now: {todo}")
    if finding.get("owner"):
        parts.append(f"Owner: {finding['owner']}")
    return " · ".join(parts)[:900]


# ----- rows <-> findings ------------------------------------------------------------

_DETAIL_KEYS = ("do_now", "timeline", "note", "answer", "jobs", "source", "investigated_at", "changed_at",
                "priority", "previous_count")


def link_for(business_date: str, incident_id: str) -> str:
    """Where the bell sends a click: the finding on the Auto-triage card."""
    return f"portal:support:{incident_id}:{business_date}"


def scope_for(business_date: str, incident_id: str) -> str:
    return f"watch:{business_date}:{incident_id}"


def to_row(finding: dict[str, Any]) -> dict[str, Any]:
    """PURE. A finding as a portal_events row: the typed watch columns, the
    priority as the severity, the long parts in details."""
    f = finding
    day, inc = str(f.get("business_date") or ""), str(f.get("incident_id") or "")
    return {
        "domain": DOMAIN, "event_type": str(f.get("event_type") or "finding"),
        "subject": f"incident:{inc}", "subject_version": day,
        "actor": str(f.get("actor") or WATCHER), "scope": scope_for(day, inc),
        "status": str(f.get("status") or "open"), "severity": str(f.get("priority") or "") or None,
        "title": str(f.get("title") or ""), "body": summary_line(f), "link": link_for(day, inc),
        "model": str(f.get("model") or "") or None, "model_calls": int(f.get("model_calls") or 0),
        "seconds": float(f.get("seconds") or 0.0),
        "category": str(f.get("category") or "") or None, "count": int(f.get("count") or 0),
        "owner": str(f.get("owner") or "") or None, "runbook": str(f.get("runbook") or "") or None,
        "cause": str(f.get("cause") or "") or None, "established": bool(f.get("established")),
        "action": str(f.get("action") or "") or None, "action_why": str(f.get("action_why") or "") or None,
        "fallback": bool(f.get("fallback")), "notified": bool(f.get("notified")),
        "business_date": day or None, "incident_id": inc,
        "details": {k: f.get(k) for k in _DETAIL_KEYS if f.get(k) not in (None, "", [])},
    }


def from_row(row: dict[str, Any]) -> dict[str, Any]:
    """PURE. A stored row back as the finding the API returns."""
    try:
        details = json.loads(row.get("details") or "{}")
    except ValueError:
        details = {}
    if not isinstance(details, dict):
        details = {}
    finding = {
        "business_date": str(row.get("business_date") or row.get("subject_version") or ""),
        "incident_id": str(row.get("incident_id") or str(row.get("subject") or "").partition(":")[2]),
        "status": str(row.get("status") or "open"),
        "title": str(row.get("title") or ""),
        "category": str(row.get("category") or ""),
        "priority": str(details.get("priority") or row.get("severity") or ""),
        "count": int(row.get("count") or 0),
        "jobs": [str(j) for j in (details.get("jobs") or [])],
        "source": str(details.get("source") or ""),
        "owner": str(row.get("owner") or ""),
        "runbook": str(row.get("runbook") or ""),
        "cause": str(row.get("cause") or ""),
        "established": bool(row.get("established")),
        "do_now": str(details.get("do_now") or ""),
        "action": str(row.get("action") or ""),
        "action_why": str(row.get("action_why") or ""),
        "timeline": [t for t in (details.get("timeline") or []) if isinstance(t, dict)],
        "note": str(details.get("note") or ""),
        "answer": str(details.get("answer") or ""),
        "model": str(row.get("model") or ""),
        "model_calls": int(row.get("model_calls") or 0),
        "seconds": float(row.get("seconds") or 0.0),
        "fallback": bool(row.get("fallback")),
        "notified": bool(row.get("notified")),
        "investigated_at": str(details.get("investigated_at") or row.get("event_ts") or ""),
        "changed_at": str(details.get("changed_at") or ""),
        "actor": str(row.get("actor") or ""),
        "event_type": str(row.get("event_type") or ""),
    }
    if details.get("previous_count") is not None:
        finding["previous_count"] = int(details["previous_count"])
    return finding


# ----- storage (the portal event log) -----------------------------------------------

def _since_days(business_date: str) -> int:
    try:
        age = (date.today() - date.fromisoformat(str(business_date)[:10])).days
    except ValueError:
        age = 0
    return max(_MIN_DAYS, min(_MAX_DAYS, age + 2))


def _rows(business_date: str) -> dict[str, Any]:
    return events_store.latest(DOMAIN, since_days=_since_days(business_date), subject_version=business_date)


def latest(business_date: str) -> dict[str, Any]:
    """{"ok", "findings"} — the latest row of every incident on that date, ranked."""
    if not business_date:
        return {"ok": True, "findings": []}
    res = _rows(business_date)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"), "findings": []}
    rows = [r for r in res.get("rows") or [] if str(r.get("subject") or "").startswith("incident:")]
    return {"ok": True, "findings": ranked([from_row(r) for r in rows])}


def append(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep findings, one row each. {"ok", "recorded"} or {"ok": False, "error"}."""
    if not findings:
        return {"ok": True, "recorded": 0}
    out = events_store.record_many([to_row(f) for f in findings])
    return {"ok": True, "recorded": out.get("recorded", len(findings))} if out.get("ok") else out


def record_pass(business_date: str, summary: dict[str, Any]) -> dict[str, Any]:
    """One "checked" row per pass: when the table was last looked at, kept
    with the findings so a restart does not forget it."""
    row = {"domain": DOMAIN, "event_type": "checked", "subject": _PASS_SUBJECT, "subject_version": business_date,
           "actor": WATCHER, "status": "ok" if summary.get("ok") else "error",
           "title": f"Auto-triage pass for {business_date}", "body": str(summary.get("error") or "")[:900] or None,
           "count": int(summary.get("incidents") or 0), "business_date": business_date or None,
           "details": {k: v for k, v in summary.items() if k not in ("ok",)}}
    return events_store.record(row)


def set_status(business_date: str, incident_id: str, status: str, actor: str) -> dict[str, Any]:
    """A person's change: open again, dismissed or resolved. A new row, and the
    bell item updated to say so."""
    if status not in _PERSON_STATUSES:
        return {"ok": False, "error": f"status must be one of {', '.join(_PERSON_STATUSES)}"}
    res = latest(business_date)
    if not res.get("ok"):
        return res
    current = next((f for f in res.get("findings") or [] if f.get("incident_id") == incident_id), None)
    if current is None:
        return {"ok": False, "error": f"no finding {incident_id} on {business_date}"}
    row = with_status(current, status, actor)
    row["notified"] = bool(current.get("notified"))
    saved = append([row])
    if not saved.get("ok"):
        return saved
    row["notified"] = notify(row) or row["notified"]
    return {"ok": True, "finding": row}


# ----- the bell --------------------------------------------------------------------

def _is_email(text: str) -> bool:
    """An owner that is an address, not a team name: one @ with something on
    both sides and a dot after it — the shape the bell can deliver to."""
    local, at, domain = str(text or "").strip().partition("@")
    return bool(at) and bool(local) and "." in domain and " " not in text


def configured_recipients() -> list[str]:
    return [r.strip().lower() for r in (settings.support_notify_recipients or "").split(",") if r.strip()]


def recipients(finding: dict[str, Any]) -> list[str]:
    """PURE. Who hears about a finding: the configured list, plus its runbook
    owner when that owner is an email. Lowercased, each once."""
    who = list(configured_recipients())
    owner = str(finding.get("owner") or "").strip()
    if _is_email(owner):
        who.append(owner.lower())
    return list(dict.fromkeys(who))


def notifications_on() -> bool:
    return bool(configured_recipients())


def should_notify(finding: dict[str, Any]) -> bool:
    """PURE. Someone to tell, and the finding is at or above the minimum priority."""
    if not recipients(finding):
        return False
    floor = _PRIORITY_RANK.get(settings.support_notify_min_priority.strip().lower(), 2)
    return _PRIORITY_RANK.get(str(finding.get("priority") or ""), 9) <= floor


def notification(finding: dict[str, Any]) -> dict[str, Any]:
    """PURE. The bell item for a finding: title, what to do, a link straight to
    it, and a scope per incident so a change updates it in place."""
    status = finding.get("status") or "open"
    lead = {"open": "", "dismissed": "Dismissed — ", "resolved": "Resolved — ", "cleared": "Cleared — "}.get(status, "")
    day, inc = str(finding.get("business_date") or ""), str(finding.get("incident_id") or "")
    return {
        "kind": "auto_triage",
        "title": f"Auto-triage: {lead}{finding.get('title') or inc}",
        "body": summary_line(finding),
        "link": link_for(day, inc),
        "scope": scope_for(day, inc),
        "severity": _SEVERITY.get(str(finding.get("priority") or ""), "info"),
        "incident_id": inc,
        "business_date": day or None,
    }


def notify(finding: dict[str, Any]) -> bool:
    """Send (or update) the finding's bell item to everyone it concerns. True
    when at least one was kept; never raises — a bell that cannot be rung
    leaves the finding as is."""
    if not should_notify(finding):
        return False
    item = notification(finding)
    told = False
    for who in recipients(finding):
        out = notifications.notify(who, **item)
        if out.get("ok"):
            told = True
        else:
            _log.warning("Auto-triage notification failed | incident=%s | to=%s | %s",
                         finding.get("incident_id"), who, out.get("error"))
    return told


# ----- the watcher's own state (this process) ---------------------------------------

_status_lock = threading.Lock()
_status: dict[str, Any] = {"running": False, "last_run_at": None, "next_at": None, "last_business_date": "",
                           "error": "", "last_summary": None}


def status() -> dict[str, Any]:
    with _status_lock:
        out = dict(_status)
    out["interval_minutes"] = settings.support_watch_minutes
    out["enabled"] = settings.support_watch_minutes > 0 and configured()
    out["configured"] = configured()
    out["store"] = events_store.store_name()
    out["notifications"] = notifications_on()
    return out


def begin() -> bool:
    """Claim the pass; False when one is already running in this process."""
    with _status_lock:
        if _status["running"]:
            return False
        _status["running"] = True
        return True


def finish(*, business_date: str = "", error: str = "", summary: dict[str, Any] | None = None) -> None:
    with _status_lock:
        _status.update(running=False, last_run_at=_iso(_now()), error=error, last_summary=summary)
        if business_date:
            _status["last_business_date"] = business_date


def schedule(next_at: datetime | None) -> None:
    """When the loop will look again (None = only when someone presses Run now)."""
    with _status_lock:
        _status["next_at"] = _iso(next_at) if next_at else None


def reset() -> None:
    """Forget this process's watcher state (tests)."""
    with _status_lock:
        _status.update(running=False, last_run_at=None, next_at=None, last_business_date="",
                       error="", last_summary=None)
