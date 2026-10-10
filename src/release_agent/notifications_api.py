"""The notification routes: the bell's list, marking, and mark-all-read.

Every route checks the `notifications` preview feature and then works for ONE
recipient: the caller's verified email (release_agent.identity). With identity
off there is nobody to verify, so the email the page sends is used and the answer
says `unverified: true`; with identity on, a request that is not signed in is
refused — a typed email never stands in for a failed sign-in, or anyone could read
anyone's bell. Like support_api, the app's helpers are imported inside the
handlers (app_fastapi includes this router).
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import features, identity

router = APIRouter()

_FEATURE = "notifications"


class MarkRequest(BaseModel):
    subjects: list[str] = Field(default_factory=list)
    status: str = "read"
    email: str = ""   # used only when identity is off


class ReadAllRequest(BaseModel):
    email: str = ""   # used only when identity is off


def _gate(request: Request) -> tuple[Any, JSONResponse | None]:
    from .app_fastapi import _caller

    caller = _caller(request)
    if not features.allowed(_FEATURE, caller):
        return caller, JSONResponse(status_code=403, content={"ok": False, "error": features.refusal(_FEATURE)})
    return caller, None


def _recipient(caller, typed: str) -> tuple[str, bool, dict | None]:
    """(email, unverified, refusal-or-None)."""
    if caller:
        return caller.email, False, None
    if identity.enabled():
        return "", False, {"ok": False, "error": "Sign-in required: notifications are kept per verified identity."}
    return (typed or "").strip(), True, None


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@router.get("/api/notifications")
def notifications_list(request: Request, since_days: int = 14, email: str = ""):
    """The caller's bell: {ok, unread, items, checked_at, unverified?}, newest
    first. With identity off and no email typed there is no bell to show: empty."""
    from .tools import notifications

    caller, refused = _gate(request)
    if refused:
        return refused
    who, unverified, refusal = _recipient(caller, email)
    if refusal:
        return refusal
    out: dict[str, Any] = {"ok": True, "unread": 0, "items": []}
    if who:
        out = notifications.list_for(who, since_days=since_days)
    if out.get("ok"):
        out["checked_at"] = _now()
    if unverified:
        out["unverified"] = True
    return out


@router.post("/api/notifications/mark")
def notifications_mark(req: MarkRequest, request: Request):
    """Mark the caller's own notifications read or dismissed. A subject that is
    not theirs is skipped, never written. {ok, marked, skipped, unread}."""
    from .tools import notifications

    caller, refused = _gate(request)
    if refused:
        return refused
    who, unverified, refusal = _recipient(caller, req.email)
    if refusal:
        return refusal
    if not who:
        return {"ok": False, "error": "Your email is needed to mark notifications."}
    out = notifications.mark(who, req.subjects, req.status, caller.email if caller else "")
    return _after_mark(out, who, unverified)


@router.post("/api/notifications/read-all")
def notifications_read_all(request: Request, req: ReadAllRequest = ReadAllRequest()):
    """Mark everything the caller has not read as read. {ok, marked, skipped, unread}."""
    from .tools import notifications

    caller, refused = _gate(request)
    if refused:
        return refused
    who, unverified, refusal = _recipient(caller, req.email)
    if refusal:
        return refusal
    if not who:
        return {"ok": False, "error": "Your email is needed to mark notifications."}
    listed = notifications.list_for(who)
    if not listed.get("ok"):
        return listed
    unread = [i["subject"] for i in listed["items"] if i["status"] == "unread"]
    out = notifications.mark(who, unread, "read", caller.email if caller else "")
    return _after_mark(out, who, unverified)


def _after_mark(out: dict, who: str, unverified: bool) -> dict:
    from .tools import notifications

    if out.get("ok"):
        out["unread"] = notifications.unread_count(who)
    if unverified:
        out["unverified"] = True
    return out
