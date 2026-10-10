"""One pass of the support watcher.

triage the control table → the incidents not seen yet → investigate each the
way the Investigate button does (``investigate_workflow.run_once``: evidence in
code, one capped model step, the model-free summary when the model is off or
fails) → keep the finding → notify. Open findings whose incident has left the
triage are marked cleared.

At most SUPPORT_WATCH_MAX_INVESTIGATIONS investigations a pass, one after
another: the model quota is shared with everyone's chat, and an incident left
over is simply new on the next pass. One pass at a time per process. It never
raises — a pass that fails says why in the watcher's status — and it never
changes anything outside its own findings.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from release_agent.config import settings
from release_agent.tools.support import triage as support_triage
from release_agent.tools.support import watch

from . import investigate_workflow

logger = logging.getLogger(__name__)


async def run_pass(business_date: str = "") -> dict[str, Any]:
    """{"ok", "business_date", "investigated", "waiting", "cleared", "notified"} or {"ok": False, "error"}."""
    if not watch.begin():
        return {"ok": False, "error": "An auto-triage pass is already running."}
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

        cleared = [watch.with_status(f, "cleared", "watcher") for f in watch.gone(findings, report)]
        if cleared:
            await asyncio.to_thread(watch.append, cleared)

        new = watch.new_incidents(report, {str(f.get("incident_id")) for f in findings})
        cap = max(0, settings.support_watch_max_investigations)
        investigated, notified = [], 0
        for incident in new[:cap]:
            outcome = await investigate_workflow.run_once(day, str(incident["id"]), thread_id="support-watcher")
            finding = watch.finding_from(incident, outcome, day)
            finding["notified"] = await asyncio.to_thread(watch.notify, finding)
            notified += int(finding["notified"])
            saved = await asyncio.to_thread(watch.append, [finding])
            if not saved.get("ok"):
                logger.warning("Watcher could not keep a finding | incident=%s | %s",
                               finding["incident_id"], saved.get("error"))
            investigated.append(finding["incident_id"])
        summary = {"ok": True, "business_date": day, "investigated": investigated,
                   "waiting": max(0, len(new) - cap), "cleared": [f["incident_id"] for f in cleared],
                   "notified": notified}
        watch.finish(business_date=day, summary=summary)
        logger.info("Watcher pass | date=%s investigated=%d waiting=%d cleared=%d notified=%d",
                    day, len(investigated), summary["waiting"], len(cleared), notified)
        return summary
    except Exception as e:  # noqa: BLE001 — a pass that fails is reported, never raised
        logger.exception("Watcher pass failed")
        error = f"{type(e).__name__}: {str(e)[:300]}"
        watch.finish(business_date=day, error=error)
        return {"ok": False, "error": error}


async def watch_forever() -> None:
    """Every SUPPORT_WATCH_MINUTES, one pass on the triage's own default date."""
    minutes = settings.support_watch_minutes
    await asyncio.sleep(10)   # let the app finish starting before the first pass
    while True:
        await run_pass()
        await asyncio.sleep(minutes * 60)
