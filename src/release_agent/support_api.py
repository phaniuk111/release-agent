"""The Support routes: triage, the live mapping check, and investigation feedback.

Every route is behind the `support-triage` preview feature. They share the app's
helpers (the caller's verified identity, the single-flight cache, who is acting)
by importing them inside the handler: app_fastapi includes this router, so a
module-level import back into it would be circular.
"""
from __future__ import annotations

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
