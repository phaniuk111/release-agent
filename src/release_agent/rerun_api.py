"""The "Re-run a job" routes (design/RERUN_JOB.md), included by app_fastapi.

Gated by the `rerun-job` feature (a preview feature by default). The GitHub
token is the chat's: the thread's connected session PAT when the request names
a thread its caller owns, else the server's token. Helpers come from
app_fastapi inside each handler — a module-level import would be circular.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from . import features

router = APIRouter()
FEATURE = "rerun-job"


def _refused(caller):
    from fastapi.responses import JSONResponse

    if features.allowed(FEATURE, caller):
        return None
    return JSONResponse(status_code=403, content={"ok": False, "error": features.refusal(FEATURE)})


class PreviewRequest(BaseModel):
    job: str
    images: list[str]
    include_passed: bool = False
    thread_id: str = ""


class StartRequest(BaseModel):
    plan_id: str
    requested_by: str = ""
    thread_id: str = ""


@router.get("/api/rerun/candidates")
async def rerun_candidates(request: Request):
    """The queued charts with the run that built each — no GitHub call."""
    from .app_fastapi import _resolve_caller
    from .tools import rerun_job

    caller = await _resolve_caller(request)
    if (no := _refused(caller)) is not None:
        return no
    return await asyncio.to_thread(rerun_job.candidates)


@router.post("/api/rerun/preview")
async def rerun_preview(req: PreviewRequest, request: Request):
    """Read-only: what re-running the job for the ticked charts would do, and the
    single-use plan id that starts exactly that."""
    from .app_fastapi import _owner, _resolve_caller, _session_store
    from .tools import rerun_job

    caller = await _resolve_caller(request)
    if (no := _refused(caller)) is not None:
        return no
    owner = _owner(caller)

    def run():
        with _session_store.activate(req.thread_id, owner=owner) if req.thread_id else _nothing():
            return rerun_job.preview(req.job, req.images, include_passed=req.include_passed,
                                     owner=caller.email if caller else None, thread_id=req.thread_id)

    return await asyncio.to_thread(run)


@router.post("/api/rerun/start")
async def rerun_start(req: StartRequest, request: Request):
    """Spend the plan and start the re-runs; the batch runs on its own thread
    and outlives this request (and the tab)."""
    from .app_fastapi import _actor_or_refusal, _owner, _resolve_caller, _session_store
    from .tools import rerun_job

    caller = await _resolve_caller(request)
    if (no := _refused(caller)) is not None:
        return no
    who, refusal = _actor_or_refusal(req.requested_by, caller)
    if refusal:
        return refusal
    owner = _owner(caller)

    def run():
        with _session_store.activate(req.thread_id, owner=owner) if req.thread_id else _nothing():
            # Followed with this thread's session PAT only while the thread holds it.
            holds = bool(req.thread_id) and _session_store.get(req.thread_id, owner=owner) is not None
            tid = req.thread_id
            return rerun_job.start(req.plan_id, owner=caller.email if caller else None, thread_id=tid,
                                   requested_by=who,
                                   pat_gone=(lambda: not _session_store.holds(tid)) if holds else (lambda: False))

    return await asyncio.to_thread(run)


@router.get("/api/rerun/{batch_id}")
async def rerun_batch(batch_id: str, request: Request):
    """A batch as its card shows it: per image, per run, and the lines so far."""
    from .app_fastapi import _resolve_caller
    from .tools import rerun_job

    caller = await _resolve_caller(request)
    if (no := _refused(caller)) is not None:
        return no
    return await asyncio.to_thread(rerun_job.get_batch, batch_id)


class _nothing:
    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False
