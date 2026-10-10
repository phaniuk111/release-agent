"""The watcher's findings: what it found, where they are kept, who is told.

A pass (adk_release_agent/support_watch.py) triages the control table,
investigates each NEW problem the way the Investigate button does, and turns
the outcome into a FINDING — one row per incident, built here from the incident
and the investigation (``finding_from``, PURE). A person then dismisses or
resolves it; the watcher marks it cleared when the problem leaves the triage.
Every change is a NEW row (append-only, AGENTS.md invariant 3): the latest row
per (business date, incident) is the finding.

Storage: SUPPORT_WATCH_DATASET set = a BigQuery table (bigquery/support_watch.schema.json),
shared by every pod and kept across restarts; empty = this process's memory.
Either way a read or write never raises — an error dict, like the feedback memory.

Notification: a Backstage notification (the bell), one per incident — its
``scope`` makes later changes update that notification instead of adding more.
Read-only throughout: nothing here changes a job or a table; an action is a
proposal for a person.
"""
from __future__ import annotations

import json
import logging
import threading
import urllib.request
from datetime import date, datetime, timezone
from typing import Any

from ...config import settings

_log = logging.getLogger(__name__)

STATUSES = ("open", "dismissed", "resolved", "cleared")
_PERSON_STATUSES = ("open", "dismissed", "resolved")
_PRIORITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}
_SEVERITY = {"critical": "critical", "high": "high", "medium": "normal", "low": "low"}
_MAX_ANSWER = 6000
_MAX_NOTE = 2000
_TIMELINE_ITEMS = 6
_MARKS = set("*_`#>")


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


def ranked(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """PURE. Open first, then by priority, newest first within a priority."""
    newest = sorted(findings, key=lambda f: str(f.get("investigated_at") or ""), reverse=True)
    return sorted(newest, key=lambda f: (0 if f.get("status") == "open" else 1,
                                         _PRIORITY_RANK.get(str(f.get("priority") or ""), 9)))


def finding_from(incident: dict[str, Any], outcome: dict[str, Any], business_date: str) -> dict[str, Any]:
    """PURE. One finding from a triage incident and its investigation outcome
    ({"text", "bundle", "model", "model_calls", "seconds", "fallback"}).

    The cause and the action are what CODE established (``with_judgement`` in the
    bundle's signals) and the answer's own "Cause:" / "Do now:" lines; when the
    evidence does not establish a cause, the finding says so — it never takes a
    guess for a finding."""
    inc = incident or {}
    out = outcome or {}
    bundle = out.get("bundle") if isinstance(out.get("bundle"), dict) else {}
    signals = bundle.get("signals") if isinstance(bundle.get("signals"), dict) else {}
    hint = signals.get("action_hint") if isinstance(signals.get("action_hint"), dict) else {}
    text = str(out.get("text") or "")
    established = bool(signals.get("conclusive"))
    cause = _labelled(text, "Cause:")
    if not established or not cause or cause.lower().startswith("not established"):
        cause = cause if (established and cause) else "Not established from the evidence"
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
        "fallback": bool(out.get("fallback")),
        "notified": False,
        "investigated_at": _iso(_now()),
        "actor": "watcher",
    }


def new_incidents(report: dict[str, Any], known: set[str]) -> list[dict[str, Any]]:
    """PURE. The report's incidents the watcher has not recorded yet, in the
    triage's own order (most urgent first)."""
    return [i for i in (report.get("incidents") or []) if isinstance(i, dict) and i.get("id")
            and str(i["id"]) not in known]


def gone(findings: list[dict[str, Any]], report: dict[str, Any]) -> list[dict[str, Any]]:
    """PURE. Open findings whose incident is no longer in the triage — the runs
    succeeded since, or the rows changed: the watcher marks them cleared."""
    present = {str(i.get("id")) for i in (report.get("incidents") or []) if isinstance(i, dict)}
    return [f for f in findings if f.get("status") == "open" and f.get("incident_id") not in present]


def with_status(finding: dict[str, Any], status: str, actor: str) -> dict[str, Any]:
    """PURE. The next row for a finding: same finding, new status, who and when."""
    row = dict(finding)
    row.update(status=status, actor=actor or "", changed_at=_iso(_now()))
    return row


# ----- storage --------------------------------------------------------------------

def _row_out(row: dict[str, Any]) -> dict[str, Any]:
    """A stored row as the API returns it (timeline/JSON columns decoded)."""
    out = {k: _iso(v) for k, v in row.items()}
    if isinstance(out.get("timeline"), str):
        try:
            out["timeline"] = json.loads(out["timeline"] or "[]")
        except ValueError:
            out["timeline"] = []
    return out


class _MemoryStore:
    """This process's findings. One replica; a restart forgets them."""

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    def append(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        with self._lock:
            self._rows.extend(dict(r) for r in rows)
        return {"ok": True, "recorded": len(rows)}

    def latest(self, business_date: str) -> dict[str, Any]:
        with self._lock:
            newest: dict[str, dict[str, Any]] = {}
            for row in self._rows:
                if row.get("business_date") == business_date:
                    newest[str(row.get("incident_id"))] = row
        return {"ok": True, "findings": [_row_out(r) for r in newest.values()]}


class _BigQueryStore:
    """An append-only table, latest row per incident wins. Streaming inserts —
    a change is a NEW row, never an UPDATE."""

    _client = None
    _client_lock = threading.Lock()

    @staticmethod
    def _project() -> str:
        return settings.support_project or settings.bq_project or settings.gcp_project

    def _table(self) -> str:
        return f"{self._project()}.{settings.support_watch_dataset}.{settings.support_watch_table}"

    def _get_client(self):
        with self._client_lock:
            if _BigQueryStore._client is None:
                from google.cloud import bigquery

                _BigQueryStore._client = bigquery.Client(project=self._project() or None)
            return _BigQueryStore._client

    def append(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        stamped = []
        for r in rows:
            row = dict(r)
            row["timeline"] = json.dumps(row.get("timeline") or [], ensure_ascii=False)
            row["created_at"] = _iso(_now())
            stamped.append(row)
        try:
            errors = self._get_client().insert_rows_json(self._table(), stamped)
        except Exception as e:  # noqa: BLE001 — a store outage is an answer, not a crash
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        if errors:
            return {"ok": False, "error": f"insert refused: {str(errors)[:300]}"}
        return {"ok": True, "recorded": len(stamped)}

    def latest(self, business_date: str) -> dict[str, Any]:
        from google.cloud import bigquery

        sql = (f"SELECT * EXCEPT(rn) FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY incident_id "
               f"ORDER BY created_at DESC) AS rn FROM `{self._table()}` WHERE business_date = @d) WHERE rn = 1")
        try:
            day = date.fromisoformat(business_date)
            cfg = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("d", "DATE", day)],
                                          maximum_bytes_billed=2**30, labels={"release_copilot": "support_watch"})
            job = self._get_client().query(sql, job_config=cfg, location=settings.bq_location or None)
            return {"ok": True, "findings": [_row_out(dict(r)) for r in job.result()]}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}", "findings": []}


_memory = _MemoryStore()


def store():
    """BigQuery when SUPPORT_WATCH_DATASET is set, else this process's memory."""
    return _BigQueryStore() if settings.support_watch_dataset else _memory


def latest(business_date: str) -> dict[str, Any]:
    """{"ok", "findings"} — the latest row of every incident on that date, ranked."""
    if not business_date:
        return {"ok": True, "findings": []}
    res = store().latest(business_date)
    res["findings"] = ranked(res.get("findings") or [])
    return res


def append(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return store().append(rows) if rows else {"ok": True, "recorded": 0}


def set_status(business_date: str, incident_id: str, status: str, actor: str) -> dict[str, Any]:
    """A person's change: open again, dismissed or resolved. A new row."""
    if status not in _PERSON_STATUSES:
        return {"ok": False, "error": f"status must be one of {', '.join(_PERSON_STATUSES)}"}
    res = store().latest(business_date)
    current = next((f for f in res.get("findings") or [] if f.get("incident_id") == incident_id), None)
    if current is None:
        return {"ok": False, "error": f"no finding {incident_id} on {business_date}"}
    row = with_status(current, status, actor)
    saved = append([row])
    if not saved.get("ok"):
        return saved
    row["notified"] = notify(row) or bool(current.get("notified"))
    return {"ok": True, "finding": row}


# ----- Backstage notification ------------------------------------------------------

def should_notify(finding: dict[str, Any]) -> bool:
    """PURE. Configured, and the finding is at or above the minimum priority."""
    if not settings.support_notify_url:
        return False
    floor = _PRIORITY_RANK.get(settings.support_notify_min_priority.strip().lower(), 2)
    return _PRIORITY_RANK.get(str(finding.get("priority") or ""), 9) <= floor


def recipients() -> dict[str, Any]:
    """PURE. SUPPORT_NOTIFY_RECIPIENTS as the notifications API wants it."""
    raw = [r.strip() for r in (settings.support_notify_recipients or "").split(",") if r.strip()]
    if not raw or raw == ["broadcast"]:
        return {"type": "broadcast"}
    return {"type": "entity", "entityRef": raw}


def notification(finding: dict[str, Any]) -> dict[str, Any]:
    """PURE. The notification for a finding: title, what to do, a link straight
    to it, and a scope per incident so a change updates it in place."""
    status = finding.get("status") or "open"
    lead = {"open": "", "dismissed": "Dismissed — ", "resolved": "Resolved — ", "cleared": "Cleared — "}.get(status, "")
    cause = finding.get("cause") or ""
    todo = finding.get("do_now") or finding.get("action") or ""
    description = " · ".join(p for p in (f"Cause: {cause}" if cause else "", f"Do now: {todo}" if todo else "",
                                         f"Owner: {finding['owner']}" if finding.get("owner") else "") if p)
    return {
        "recipients": recipients(),
        "payload": {
            "title": f"Auto-triage: {lead}{finding.get('title') or finding.get('incident_id')}",
            "description": description[:900],
            "link": f"/operations?view=support&incident={finding.get('incident_id')}&date={finding.get('business_date')}",
            "severity": _SEVERITY.get(str(finding.get("priority") or ""), "normal"),
            "topic": "support-watcher",
            "scope": f"support-watch:{finding.get('business_date')}:{finding.get('incident_id')}",
        },
    }


def notify(finding: dict[str, Any]) -> bool:
    """Send (or update) the finding's notification. True when Backstage took it;
    never raises — a notification that cannot be sent leaves the finding as is."""
    if not should_notify(finding):
        return False
    body = json.dumps(notification(finding)).encode("utf-8")
    req = urllib.request.Request(settings.support_notify_url, data=body, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "Authorization": f"Bearer {settings.support_notify_token}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:   # noqa: S310 — a configured URL
            return 200 <= resp.status < 300
    except Exception as e:  # noqa: BLE001
        _log.warning("Backstage notification failed | incident=%s | %s", finding.get("incident_id"),
                     f"{type(e).__name__}: {str(e)[:200]}")
        return False


# ----- the watcher's own state (this process) ---------------------------------------

_status_lock = threading.Lock()
_status: dict[str, Any] = {"running": False, "last_run_at": None, "last_business_date": "",
                           "last_error": "", "last_summary": None}


def status() -> dict[str, Any]:
    with _status_lock:
        out = dict(_status)
    out["interval_minutes"] = settings.support_watch_minutes
    out["enabled"] = settings.support_watch_minutes > 0
    out["store"] = "bigquery" if settings.support_watch_dataset else "memory"
    out["notifications"] = bool(settings.support_notify_url)
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
        _status.update(running=False, last_run_at=_iso(_now()), last_error=error,
                       last_summary=summary)
        if business_date:
            _status["last_business_date"] = business_date
