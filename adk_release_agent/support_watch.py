"""One pass of Auto-triage, and the loop that runs it.

triage the control table → the incidents not seen yet → investigate each the
way the Investigate button does (``investigate_workflow.run_once``: evidence in
code, one capped model step, the model-free summary when the model is off or
fails) → keep the finding → ring the bell. Open findings whose incident has
left the triage are marked cleared; open findings whose incident grew (more
jobs than when it was found) are updated and told again.

At most SUPPORT_WATCH_MAX_INVESTIGATIONS investigations a pass, one after
another: the model quota is shared with everyone's chat, and an incident left
over is simply new on the next pass. Each investigation has
SUPPORT_WATCH_INVESTIGATE_SECONDS: past it the finding says the evidence timed
out and the pass moves on — one slow source must not hold the table unread.
A finding is kept and notified as soon as ITS investigation completes, not at
the end of the pass. One pass at a time per process. It never raises — a pass
that fails says why in the watcher's status — and it never changes anything
outside its own findings.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from release_agent.config import settings
from release_agent.tools.support import triage as support_triage
from release_agent.tools.support import watch

from . import investigate_workflow

logger = logging.getLogger(__name__)

_STARTUP_GRACE_SECONDS = 10.0
# A pass that finishes late still leaves the app a minute before the next one.
_MIN_GAP = timedelta(minutes=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def scheduled() -> bool:
    """The loop runs only when asked to (SUPPORT_WATCH_MINUTES > 0) and there
    is a table to watch."""
    return settings.support_watch_minutes > 0 and watch.configured()


def next_run_at(start: datetime, end: datetime, minutes: int) -> datetime:
    """PURE. The interval is measured from the pass START, so a pass that took
    four minutes does not drift the schedule — but never sooner than a minute
    after it ended."""
    return max(start + timedelta(minutes=max(0, minutes)), end + _MIN_GAP)


async def investigate(business_date: str, incident_id: str) -> dict[str, Any]:
    """``run_once`` under the deadline. A timed-out or failed investigation is
    an outcome that says so, never an exception out of the pass."""
    seconds = max(1, int(settings.support_watch_investigate_seconds))
    try:
        return await asyncio.wait_for(
            investigate_workflow.run_once(business_date, incident_id, thread_id="auto-triage"), timeout=seconds)
    except asyncio.TimeoutError:
        logger.warning("Auto-triage investigation timed out | incident=%s | after %ss", incident_id, seconds)
        return {"text": "", "bundle": {}, "model": "", "model_calls": 0, "seconds": float(seconds),
                "fallback": True, "timed_out": True}
    except Exception as e:  # noqa: BLE001 — the pass goes on to the next incident
        logger.exception("Auto-triage investigation failed | incident=%s", incident_id)
        return {"text": f"The investigation failed: {type(e).__name__}: {str(e)[:200]}", "bundle": {},
                "model": "", "model_calls": 0, "seconds": 0.0, "fallback": True}


async def _keep(finding: dict[str, Any]) -> dict[str, Any]:
    """Ring the bell, then keep the finding with whether that worked."""
    finding["notified"] = await asyncio.to_thread(watch.notify, finding)
    saved = await asyncio.to_thread(watch.append, [finding])
    if not saved.get("ok"):
        logger.warning("Auto-triage could not keep a finding | incident=%s | %s",
                       finding.get("incident_id"), saved.get("error"))
    return finding


async def run_pass(business_date: str = "") -> dict[str, Any]:
    """{"ok", "business_date", "incidents", "investigated", "waiting", "updated",
    "cleared", "notified"} or {"ok": False, "error"}."""
    if not watch.begin():
        return {"ok": False, "error": "An Auto-triage pass is already running."}
    day = ""
    try:
        report = await asyncio.to_thread(support_triage.triage, business_date)
        if not report.get("ok"):
            error = str(report.get("error") or "the triage could not be read")
            watch.finish(error=error)
            return {"ok": False, "error": error}
        day = str(report.get("business_date") or business_date)
        known = await asyncio.to_thread(watch.latest, day)
        if not known.get("ok"):
            error = f"The findings could not be read: {known.get('error')}"
            watch.finish(business_date=day, error=error)
            return {"ok": False, "error": error}
        findings = known.get("findings") or []
        notified = 0

        cleared = []
        for f in watch.gone(findings, report):
            row = watch.with_status(f, "cleared", watch.WATCHER)
            row["notified"] = bool(f.get("notified"))
            await _keep(row)
            cleared.append(row["incident_id"])

        updated = []
        for f, incident in watch.grown(findings, report):
            row = await _keep(watch.updated_from(f, incident))
            notified += int(row["notified"])
            updated.append(row["incident_id"])

        new = watch.new_incidents(report, {str(f.get("incident_id")) for f in findings})
        cap = max(0, settings.support_watch_max_investigations)
        investigated = []
        for incident in new[:cap]:
            outcome = await investigate(day, str(incident["id"]))
            finding = await _keep(watch.finding_from(incident, outcome, day))
            notified += int(finding["notified"])
            investigated.append(finding["incident_id"])
        summary = {"ok": True, "business_date": day, "incidents": len(report.get("incidents") or []),
                   "investigated": investigated, "waiting": max(0, len(new) - cap), "updated": updated,
                   "cleared": cleared, "notified": notified}
        await asyncio.to_thread(watch.record_pass, day, summary)
        watch.finish(business_date=day, summary=summary)
        logger.info("Auto-triage pass | date=%s investigated=%d waiting=%d updated=%d cleared=%d notified=%d",
                    day, len(investigated), summary["waiting"], len(updated), len(cleared), notified)
        return summary
    except Exception as e:  # noqa: BLE001 — a pass that fails is reported, never raised
        logger.exception("Auto-triage pass failed")
        error = f"{type(e).__name__}: {str(e)[:300]}"
        watch.finish(business_date=day, error=error)
        return {"ok": False, "error": error}


async def watch_forever() -> None:
    """Every SUPPORT_WATCH_MINUTES from each pass's start, one pass on the
    triage's own default date. A pass refused because "Run now" is already
    running counts as this interval's pass."""
    await asyncio.sleep(_STARTUP_GRACE_SECONDS)   # let the app finish starting before the first pass
    while True:
        start = _now()
        await run_pass()
        next_at = next_run_at(start, _now(), settings.support_watch_minutes)
        watch.schedule(next_at)
        await asyncio.sleep(max(0.0, (next_at - _now()).total_seconds()))
