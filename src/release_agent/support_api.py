"""The Support routes: triage, the live mapping check, investigation feedback,
and Auto-triage.

Every route checks the `support-triage` feature gate — open to everyone unless
PREVIEW_FEATURES lists it (it is out of preview by default); Auto-triage's
routes check their own, `auto-triage`, a preview feature by default. They share the app's
helpers (the caller's verified identity, the single-flight cache, who is acting)
by importing them inside the handler: app_fastapi includes this router, so a
module-level import back into it would be circular.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import features

router = APIRouter()


@router.get("/api/support/config-check")
def support_config_check(request: Request):
    """Does the configured column mapping match the live control table? Metadata
    only — nothing is queried. For whoever sets the feature up."""
    from fastapi.responses import JSONResponse

    from .app_fastapi import _caller

    from .tools.support import queries

    if not features.allowed("support-triage", _caller(request)):
        return JSONResponse(status_code=403, content={"ok": False, "error": features.refusal("support-triage")})
    return queries.check_config()


# Support triage reads a real table, so the team opening the card together must
# not each run the query: one cached answer per business date, for a minute.
_SUPPORT_TTL_SECONDS = 60.0
_support_caches: dict[str, dict] = {}


@router.get("/api/support/triage")
def support_triage_report(request: Request, date: str = "", fresh: int = 0):
    """First-line triage of the workflow control table for one business date
    (empty = the latest in the table)."""
    from fastapi.responses import JSONResponse

    from .app_fastapi import _cached, _caller

    from .tools.support import triage

    if not features.allowed("support-triage", _caller(request)):
        return JSONResponse(status_code=403, content={"ok": False, "error": features.refusal("support-triage")})

    key = date.strip()[:10]
    if key not in _support_caches and len(_support_caches) >= 32:
        _support_caches.clear()   # a bounded cache: any date can be asked for
    cache = _support_caches.setdefault(key, {"at": 0.0, "value": None})
    return _cached(cache, _SUPPORT_TTL_SECONDS, lambda: triage.triage(key), fresh=bool(fresh))


class SupportFeedbackRequest(BaseModel):
    """One "was this right?" answer. No actor field on purpose: who answered is
    the verified caller, never something the browser says."""
    business_date: str
    incident_id: str
    title: str = ""
    verdict: str
    actual_cause: str = ""
    category: str = ""
    action: str = ""
    model: str = ""
    model_calls: int | None = None
    seconds: float | None = None


@router.post("/api/support/feedback")
def support_feedback_record(req: SupportFeedbackRequest, request: Request):
    """Append one rating of an investigation (right / direction / wrong, with the
    real cause in the person's words). Answers {ok, recorded} or {ok: False,
    error, hint?}; a memory that is off says disabled."""
    from fastapi.responses import JSONResponse

    from .app_fastapi import _actor_or_refusal, _caller

    from .tools.support import feedback

    caller = _caller(request)
    if not features.allowed("support-triage", caller):
        return JSONResponse(status_code=403, content={"ok": False, "error": features.refusal("support-triage")})
    # Nothing typed stands in for the caller: with identity off the answer is
    # anonymous, and IDENTITY_REQUIRED refuses an unverified one.
    actor, refused = _actor_or_refusal("", caller)
    if refused:
        return refused
    return feedback.record(
        business_date=req.business_date, incident_id=req.incident_id, title=req.title, verdict=req.verdict,
        actual_cause=req.actual_cause, category=req.category, action=req.action, model=req.model,
        model_calls=req.model_calls, seconds=req.seconds, actor=actor)


@router.get("/api/support/feedback/stats")
def support_feedback_stats(request: Request, days: int = 30):
    """How the investigations were rated: accuracy, by category and model, and
    the newest wrong answers with their real causes."""
    from fastapi.responses import JSONResponse

    from .app_fastapi import _caller

    from .tools.support import feedback

    if not features.allowed("support-triage", _caller(request)):
        return JSONResponse(status_code=403, content={"ok": False, "error": features.refusal("support-triage")})
    return feedback.stats(days)


@router.get("/api/support/feedback")
def support_feedback_for_incident(request: Request, date: str = "", incident_id: str = ""):
    """What people said about one incident before ("2 people marked this right")."""
    from fastapi.responses import JSONResponse

    from .app_fastapi import _caller

    from .tools.support import feedback

    if not features.allowed("support-triage", _caller(request)):
        return JSONResponse(status_code=403, content={"ok": False, "error": features.refusal("support-triage")})
    return feedback.for_incident(date, incident_id)


# ----- Auto-triage (tools/support/watch.py, adk_release_agent/support_watch.py) ------
# Its own preview feature, `auto-triage`: the findings are investigations run
# without anyone asking, so they are shown only to the people testing that.

def _auto_triage_refusal(caller):
    from fastapi.responses import JSONResponse

    if features.allowed("auto-triage", caller):
        return None
    return JSONResponse(status_code=403, content={"ok": False, "error": features.refusal("auto-triage")})


@router.get("/api/support/watch")
def support_watch_feed(request: Request, date: str = ""):
    """Auto-triage's findings for one business date (default: the date of its
    last pass), ranked, and its own status — when it last ran, when it will
    again, and what went wrong if it did."""
    from .app_fastapi import _caller

    from .tools.support import watch

    refused = _auto_triage_refusal(_caller(request))
    if refused:
        return refused
    status = watch.status()
    day = (date or "").strip()[:10] or status.get("last_business_date") or ""
    res = watch.latest(day)
    out = {"ok": bool(res.get("ok")), "business_date": day, "findings": res.get("findings") or [], "status": status}
    if res.get("error"):
        out["error"] = res["error"]
    return out


class SupportWatchRunRequest(BaseModel):
    business_date: str = ""


# The pass runs detached from the request (it can take minutes, past an ingress
# timeout); the set keeps a reference so the task is not collected mid-flight.
_watch_tasks: set[asyncio.Task] = set()


@router.post("/api/support/watch/run")
async def support_watch_run(req: SupportWatchRunRequest, request: Request):
    """Start one Auto-triage pass now (the "Run now" button; the timer runs the
    same) and answer at once — the card polls the watcher's status. Read-only,
    but it spends model calls, so it is a write in identity terms. Refused
    while a pass is already running."""
    from adk_release_agent.support_watch import run_pass

    from .app_fastapi import _actor_or_refusal, _resolve_caller

    from .tools.support import watch

    caller = await _resolve_caller(request)
    refused = _auto_triage_refusal(caller)
    if refused:
        return refused
    _, refused = _actor_or_refusal("", caller)
    if refused:
        return refused
    if watch.status().get("running"):
        return {"ok": False, "error": "An Auto-triage pass is already running."}
    task = asyncio.create_task(run_pass(req.business_date.strip()[:10]), name="auto-triage-run-now")
    _watch_tasks.add(task)
    task.add_done_callback(_watch_tasks.discard)
    return {"ok": True, "started": True}


class SupportWatchStatusRequest(BaseModel):
    """A person's change to a finding. No actor field: who is the verified caller."""
    business_date: str
    incident_id: str
    status: str


@router.post("/api/support/watch/status")
def support_watch_set_status(req: SupportWatchStatusRequest, request: Request):
    """Dismiss, resolve, or reopen a finding — a new row, and the bell item is
    updated in place."""
    from .app_fastapi import _actor_or_refusal, _caller

    from .tools.support import watch

    caller = _caller(request)
    refused = _auto_triage_refusal(caller)
    if refused:
        return refused
    actor, refused = _actor_or_refusal("", caller)
    if refused:
        return refused
    return watch.set_status(req.business_date.strip()[:10], req.incident_id.strip(), req.status.strip(), actor)
