"""Notifications: one bell per person, kept in the portal event log.

A notification is rows in `portal_events` (domain "notification", see
events_store.py) addressed to a recipient's email. Nothing is updated in place:

  * a notification that changes (a PR merges, then its runs finish) is a new
    `updated` row with the SAME subject as the first, so the bell shows ONE item
    whose text changed — the thread's `scope` says which notifications are one;
  * reading or dismissing is a new `read` / `dismissed` row carrying the item's
    text, so the latest row per subject is always a complete item.

The kind (release_pr, uat_pr, df_run, requeue, chg, deploy_started,
deploy_finished, auto_triage) rides in the `category` column; `link` says where a
click goes (a URL, `chat:<message>`, or a `portal:` action the page handles).

The recipient is the VERIFIED email of the person (identity.py) — the routes
decide that; this module trusts what it is given. Emails are compared lowercased,
because mail addresses are not case-sensitive and a gateway may change the case.

Every function is best-effort and NEVER raises: a notification is a courtesy, and
the release, deploy or investigation that sends one must not wait for it or fail
with it.
"""
from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from . import events_store

logger = logging.getLogger(__name__)

DOMAIN = "notification"
SEVERITIES = ("info", "warning", "critical")
MARKS = ("read", "dismissed")

# How far back a thread is looked for when a notification repeats its scope, and
# how far back `mark` finds the item it acknowledges. Older items are not on the
# bell any more, so they start a new thread / cannot be marked.
_THREAD_DAYS = 30
_MARK_DAYS = 30
# `mark` checks each subject with a point read, so a call is capped; the bell's
# "Mark all read" is `read_all`, which reads the recipient's list in one query.
_MAX_MARKS = 200
# The most items one recipient's list holds (their own newest N — the recipient
# filter is applied before the cap). `read_all` writes in batches of _MARK_BATCH.
_LIST_LIMIT = 2000
_MARK_BATCH = 500


def _who(recipient: Any) -> str:
    return str(recipient or "").strip().lower()


def _fail(e: Exception) -> dict[str, Any]:
    logger.warning("notification failed: %s: %s", type(e).__name__, e)
    return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}


def notify(recipient: str, *, kind: str, title: str, body: str = "", link: str = "",
           scope: str = "", severity: str = "info", subject: str = "", **cols: Any) -> dict:
    """Tell ``recipient``. {"ok": True, "event_id", "subject", "event_type"} or
    {"ok": False, "error"} — never raises.

    ``event_type`` is "created", or "updated" when ``scope`` already has a
    notification for this recipient: then the SAME subject is reused (whatever
    ``subject`` says), so the bell shows one item with its text changed and
    unread again. ``subject`` names a notification of your own; by default one
    is made up (``notification:<scope>`` for a scope, else a fresh id).
    ``cols`` are any other portal_events columns (repo, pr_number, actor, ...);
    the actor defaults to "portal"."""
    try:
        to = _who(recipient)
        scope = str(scope or "").strip()
        if not to:
            return {"ok": False, "error": "recipient is required"}
        if not str(kind or "").strip() or not str(title or "").strip():
            return {"ok": False, "error": "kind and title are required"}
        if severity not in SEVERITIES:
            return {"ok": False, "error": f"severity must be one of {', '.join(SEVERITIES)}"}
        event_type = "created"
        subject = str(subject or "").strip()
        if scope:
            seen = events_store.history(DOMAIN, since_days=_THREAD_DAYS, recipient=to, scope=scope)
            thread = [r for r in seen.get("rows") or [] if r.get("subject")]
            if thread:     # newest first: its subject is the item on the bell now
                event_type, subject = "updated", thread[0]["subject"]
            elif not subject:
                subject = f"notification:{scope}"
        subject = subject or f"notification:{uuid.uuid4().hex}"
        row = {"actor": "portal", **cols,
               "domain": DOMAIN, "event_type": event_type, "subject": subject, "recipient": to,
               "scope": scope, "status": "unread", "severity": severity, "title": title,
               "body": body, "link": link, "category": kind}
        out = events_store.record(row)
        if not out.get("ok"):
            return out
        return {"ok": True, "event_id": out["event_id"], "subject": subject, "event_type": event_type}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def _item(row: dict[str, Any]) -> dict[str, Any]:
    """A bell item from the latest row of a subject. ``at`` is when the text last
    changed: a read / dismissed row carries that time in its details, so
    acknowledging an item does not float it to the top."""
    at = row.get("event_ts", "")
    try:
        carried = json.loads(row.get("details") or "{}").get("notified_at")
    except (ValueError, AttributeError):
        carried = None
    if row.get("event_type") in MARKS and carried:
        at = carried
    return {"subject": row["subject"], "kind": row.get("category", ""), "title": row.get("title", ""),
            "body": row.get("body", ""), "link": row.get("link", ""), "severity": row.get("severity", "info"),
            "status": row.get("status", "unread"), "scope": row.get("scope", ""),
            "event_type": row.get("event_type", ""), "at": at,
            "repo": row.get("repo", ""), "pr_number": row.get("pr_number")}


def list_for(recipient: str, *, since_days: int = 14) -> dict:
    """The recipient's notifications, one per subject (its latest row), newest
    first: {"ok": True, "items": [{subject, kind, title, body, link, severity,
    status, scope, event_type, at, repo, pr_number}], "unread": n} — dismissed
    ones included, with their status, for the caller to hide. {"ok": False,
    "error"} when the store fails; never raises."""
    try:
        to = _who(recipient)
        if not to:
            return {"ok": False, "error": "recipient is required"}
        got = events_store.latest(DOMAIN, since_days=since_days, recipient=to, limit=_LIST_LIMIT)
        if not got.get("ok"):
            return got
        items = sorted((_item(r) for r in got["rows"]), key=lambda i: i["at"], reverse=True)
        return {"ok": True, "items": items, "unread": sum(1 for i in items if i["status"] == "unread")}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def _mark_row(current: dict[str, Any], to: str, status: str, actor: str) -> dict[str, Any]:
    """The row that acknowledges ``current`` (a subject's latest row): its text
    carried over, so the latest row per subject stays a complete item."""
    return {
        "domain": DOMAIN, "event_type": status, "subject": current["subject"], "recipient": to,
        "scope": current.get("scope"), "status": status, "severity": current.get("severity"),
        "title": current.get("title"), "body": current.get("body"), "link": current.get("link"),
        "category": current.get("category"), "repo": current.get("repo"),
        "pr_number": current.get("pr_number"), "actor": actor or None,
        "details": {"notified_at": _item(current)["at"]},
    }


def mark(recipient: str, subjects: list[str], status: str, actor: str) -> dict:
    """Acknowledge ``subjects`` as "read" or "dismissed" by appending one row each.
    Only the recipient's OWN notifications can be marked: any other subject, an
    unknown one, or one already in that state is skipped and written to nothing.
    Each subject is checked with its own point read (recipient + subject), so how
    old it is among the recipient's items does not matter; more than 200 distinct
    subjects in one call is refused. {"ok": True, "marked": [...], "skipped": [...]}
    or {"ok": False, "error"}; ``actor`` is who did it (the verified email, ""
    when identity is off)."""
    try:
        to = _who(recipient)
        if not to:
            return {"ok": False, "error": "recipient is required"}
        if status not in MARKS:
            return {"ok": False, "error": f"status must be one of {', '.join(MARKS)}"}
        wanted = list(dict.fromkeys(str(s).strip() for s in subjects or [] if str(s).strip()))
        if len(wanted) > _MAX_MARKS:
            return {"ok": False, "error": f"at most {_MAX_MARKS} notifications can be marked in one call "
                                          f"({len(wanted)} given); use read-all to mark everything"}
        if not wanted:
            return {"ok": True, "marked": [], "skipped": []}
        rows, marked = [], []
        for subject in wanted:     # read everything first: a failed read writes nothing
            got = events_store.latest(DOMAIN, since_days=_MARK_DAYS, recipient=to, subject=subject)
            if not got.get("ok"):
                return got
            current = (got.get("rows") or [None])[0]
            if current is None or current.get("status") == status:
                continue
            marked.append(subject)
            rows.append(_mark_row(current, to, status, actor))
        if rows:
            out = events_store.record_many(rows)
            if not out.get("ok"):
                return out
        return {"ok": True, "marked": marked, "skipped": [s for s in wanted if s not in marked]}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def read_all(recipient: str, actor: str, *, since_days: int = 14) -> dict:
    """Mark every unread notification the recipient has as read — everything
    `list_for` returns, however many (it is their own newest 2000, not a cap on
    what one `mark` call may name). {"ok": True, "marked": [...], "skipped": []}
    or {"ok": False, "error"}; never raises."""
    try:
        to = _who(recipient)
        if not to:
            return {"ok": False, "error": "recipient is required"}
        got = events_store.latest(DOMAIN, since_days=since_days, recipient=to, limit=_LIST_LIMIT)
        if not got.get("ok"):
            return got
        unread = [r for r in got["rows"] if r.get("status") == "unread"]
        for i in range(0, len(unread), _MARK_BATCH):
            out = events_store.record_many([_mark_row(r, to, "read", actor) for r in unread[i:i + _MARK_BATCH]])
            if not out.get("ok"):
                return out
        return {"ok": True, "marked": [r["subject"] for r in unread], "skipped": []}
    except Exception as e:  # noqa: BLE001
        return _fail(e)


def unread_count(recipient: str) -> int:
    """How many notifications the recipient has not read; 0 when the store is
    down (a badge that cannot be known is no badge)."""
    out = list_for(recipient)
    return int(out.get("unread", 0)) if out.get("ok") else 0
