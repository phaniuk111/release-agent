"""A dispatched DF deploy run, followed to its end for the bell.

The DF deploy reply links the run and stops there — a flex-template build
takes minutes, and the chat turn must not wait for it. So the dispatch hands
the run to a thread here, which reads it until GitHub says it completed and
then tells the person who started it how it went (tools/notifications.py,
kind `df_run`, one item per run: scope `df-run:<run id>`). Nothing here
re-runs, cancels or merges anything: it reads one run and writes one
notification.

The thread keeps the Repository object the dispatch used — it carries the
caller's own token — rather than resolving one again: the session PAT lives in
a contextvar the request owns, and the request is long gone by the time the
run ends. That token is the thread's to hold only for SESSION_IDLE_MINUTES
(invariant 5), so a watcher begun with a session PAT stops, quietly, as soon as
the thread no longer holds it — before any further call to GitHub — and never
waits longer than that either.

Best-effort throughout: a run that cannot be read, a store that is down, a
run still going after `MAX_MINUTES` — each is logged or said, never raised,
and never changes what the deploy reported.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..config import settings

logger = logging.getLogger("release_copilot")

POLL_SECONDS = 30.0
# Never longer than a session PAT is held (0 = the sweep is off: no cap from it).
MAX_MINUTES = float(min(90, settings.session_idle_minutes or 90))
# Consecutive read failures before the watcher gives up: a token revoked or a
# proxy outage, not a blip, and a thread per run must not poll forever.
_MAX_READ_FAILURES = 5


def outcome_notification(image: str, tag: str, run_id: Any, run_url: str, conclusion: str,
                         failed_steps: list[dict] | None = None) -> dict[str, Any]:
    """PURE: the notification for a completed run — the kwargs `notify` takes.
    Success links the run; anything else is a warning whose link asks the chat
    why, naming the first failed step when the run's jobs say which."""
    what = f"{image}:{tag}"
    base = {"kind": "df_run", "scope": f"df-run:{run_id}", "run_id": str(run_id) if run_id else None,
            "run_url": run_url or None, "conclusion": conclusion or None}
    if conclusion == "success":
        return {**base, "title": f"DF run finished: {what}", "body": "", "link": run_url, "severity": "info"}
    step = next((s.get("name") for s in failed_steps or [] if s.get("name")), "")
    job = next((s.get("job") for s in failed_steps or [] if s.get("job")), "")
    title = f"DF run failed: {what}" + (f" — step {step}" if step else "")
    body = f"conclusion: {conclusion or 'unknown'}" + (f" · job {job}" if job else "") + (f" · {run_url}" if run_url else "")
    return {**base, "title": title, "body": body, "link": f"chat:why did {what} fail?", "severity": "warning"}


def still_running_notification(image: str, tag: str, run_id: Any, run_url: str, minutes: float) -> dict[str, Any]:
    """PURE: what is said when the watcher stops waiting — the run may well
    finish later; the person is told where to look, not that it failed."""
    return {"kind": "df_run", "scope": f"df-run:{run_id}", "run_id": str(run_id) if run_id else None,
            "run_url": run_url or None, "severity": "info", "link": run_url,
            "title": f"DF run still running: {image}:{tag} — not finished after {int(minutes)} min",
            "body": "The portal stopped following it; the run itself carries on."}


def _pat_gone(thread_id: str) -> bool:
    """True when the run was dispatched with a thread's session PAT and the
    thread no longer holds it (swept idle, or cleared). No thread id means the
    server's own token — nothing to outlive."""
    if not thread_id:
        return False
    from ..session_creds import get_store

    return not get_store().holds(thread_id)


def watch(repo: Any, run_id: Any, image: str, tag: str, recipient: str, run_url: str = "",
          thread_id: str = "") -> dict[str, Any]:
    """Follow run ``run_id`` in ``repo`` until it completes, then notify
    ``recipient``. Blocking — the dispatch calls `watch_in_background`.
    ``thread_id`` is the thread whose session PAT ``repo`` carries: once that
    thread no longer holds it the watcher stops without telling anyone.
    Returns what happened ({"ok", "conclusion" | "reason"}); never raises."""
    try:
        from . import controls, notifications

        if not recipient:
            return {"ok": False, "reason": "no recipient"}
        deadline = time.monotonic() + MAX_MINUTES * 60
        failures = 0
        while True:
            if _pat_gone(thread_id):
                logger.info("DF run %s: the thread's GitHub token is gone; no longer followed", run_id)
                return {"ok": False, "reason": "token dropped"}
            try:
                run = repo.get_workflow_run(int(run_id))
                failures = 0
            except Exception as e:  # noqa: BLE001
                failures += 1
                if failures >= _MAX_READ_FAILURES:
                    logger.warning("DF run %s: could not be read (%s: %s); no longer followed",
                                   run_id, type(e).__name__, e)
                    return {"ok": False, "reason": "unreadable"}
                run = None
            if run is not None and getattr(run, "status", "") == "completed":
                conclusion = str(getattr(run, "conclusion", "") or "")
                url = run_url or str(getattr(run, "html_url", "") or "")
                steps = [] if conclusion == "success" else controls._failed_steps(run)
                out = notifications.notify(recipient, **outcome_notification(image, tag, run_id, url, conclusion, steps))
                return {"ok": bool(out.get("ok")), "conclusion": conclusion, "notified": out}
            if time.monotonic() >= deadline:
                notifications.notify(recipient, **still_running_notification(image, tag, run_id, run_url, MAX_MINUTES))
                return {"ok": False, "reason": "timeout"}
            time.sleep(POLL_SECONDS)
    except Exception as e:  # noqa: BLE001
        logger.warning("DF run %s: watcher stopped: %s: %s", run_id, type(e).__name__, e)
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:300]}


def watch_in_background(repo: Any, run_id: Any, image: str, tag: str, recipient: str, run_url: str = "",
                        thread_id: str = "") -> bool:
    """Start `watch` on a daemon thread. False (and nothing started) without a
    recipient or a run id — a notification nobody would get is not worth a thread."""
    if not recipient or not run_id:
        return False
    threading.Thread(target=watch, args=(repo, run_id, image, tag, recipient, run_url, thread_id),
                     name=f"df-run-{run_id}", daemon=True).start()
    return True
