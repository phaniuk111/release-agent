"""Close the loop on PRs the portal raised but could not merge.

A deploy into a branch-protected repo stops at a PR waiting on review. The
portal cannot record it as deployed — nothing has landed — and the person who
approves it merges it in GitHub, outside any chat turn, so no event would ever
be written for it. The event log would then be missing a deploy that happened.

So the raising path writes a 'pending' event naming the PR, and this module
checks those PRs later against GitHub:

    merged               -> the event the merge stands for ('deployed' /
                            'removed'), stamped with the MERGE time
    closed, not merged   -> 'abandoned', so it stops being checked
    still open           -> left alone; checked again next time

It runs lazily from the read paths (chat stats, the Insights panel) rather
than on a timer: no background thread per pod, no inbound webhook through the
mesh's external auth, no new credentials — just the GitHub token the portal
already holds. The cost is that an outside merge shows up the next time
someone looks, which is the only time the answer matters.

Only PRs the portal raised are covered. A change made in the deploy repo by
hand never produced a 'pending' event, so nothing here can find it.
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
import uuid
from typing import Any

logger = logging.getLogger("release_copilot")

_PREFIX = "awaiting_merge"
# Events that settle a pending PR. Any one of them for the same (repo, PR) means
# there is nothing left to check.
_TERMINAL = frozenset({"deployed", "removed", "abandoned"})
_ON_MERGE = frozenset({"deployed", "removed"})

# Throttle: GitHub is asked at most this often per process, and never twice at
# once — the Insights panel and a chat turn can easily land together.
_INTERVAL_SECONDS = 120.0
_MAX_PRS_PER_RUN = 20
_state: dict[str, float] = {"at": float("-inf")}   # first read always checks
_lock = threading.Lock()


def pending_note(on_merge: str, tag: str = "") -> str:
    """What a pending event must remember: which event its merge stands for,
    and the action tag the resolved event should carry."""
    if on_merge not in _ON_MERGE:
        raise ValueError(f"on_merge must be one of {sorted(_ON_MERGE)}, got {on_merge!r}")
    return f"{_PREFIX}:{on_merge}:{tag}"


def parse_pending_note(note: str | None) -> tuple[str, str] | None:
    """(on_merge, tag) from a pending event's note, or None if it is not one."""
    prefix, _, rest = str(note or "").partition(":")
    on_merge, _, tag = rest.partition(":")
    if prefix != _PREFIX or on_merge not in _ON_MERGE:
        return None
    return on_merge, tag


def record_pending(
    environment: str,
    artifacts: list[dict[str, str]],
    deployment_repo: str,
    pr_number: int | None,
    on_merge: str = "deployed",
    tag: str = "",
) -> dict[str, Any]:
    """Log that a change is waiting on `pr_number`. Best-effort, like every
    event write — a failure here must never fail the deploy that raised it.

    Only call this for the PR whose merge COMPLETES the change. If an earlier
    hop in a chain is the one blocked, merging it lands nothing downstream —
    the next PR has not been raised yet — so it must not be recorded as the
    thing that will finish the deploy.
    """
    if not pr_number or not deployment_repo:
        return {"ok": False, "error": "a pending event needs a repo and a PR number"}
    from . import release_queue as _rq

    return _rq.record_deployment(
        environment=environment,
        artifacts=artifacts,
        deployment_repo=deployment_repo,
        pr_number=int(pr_number),
        note=pending_note(on_merge, tag),
        event_type="pending",
    )


def unresolved_pending(events: list[dict[str, Any]]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    """Pending events with no settling event yet, grouped by (repo, PR). Pure."""
    settled = {
        (ev.get("deployment_repo"), ev.get("pr_number"))
        for ev in events
        if ev.get("event_type") in _TERMINAL and ev.get("pr_number")
    }
    open_: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for ev in events:
        if ev.get("event_type") != "pending" or not parse_pending_note(ev.get("note")):
            continue
        key = (ev.get("deployment_repo"), ev.get("pr_number"))
        if not key[0] or not key[1] or key in settled:
            continue
        open_.setdefault(key, []).append(ev)
    return open_


def _iso(when: Any) -> str:
    if isinstance(when, _dt.datetime):
        if when.tzinfo is None:           # PyGithub returns naive UTC on some versions
            when = when.replace(tzinfo=_dt.timezone.utc)
        return when.isoformat()
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def resolution_rows(
    pending: list[dict[str, Any]], outcome: str, when: Any, merged_by: str = "",
) -> list[dict[str, Any]]:
    """The events that settle a pending PR. Pure.

    event_ts is the MERGE (or close) time, not now. Derived state is "latest
    event per artifact per environment wins", ordered by event_ts, so stamping
    a merge noticed late with the time it was noticed would let a stale version
    overwrite a newer deploy made in between.

    event_id is derived from the PR and artifact rather than random, so two
    pods resolving the same PR at once collapse onto BigQuery's insertId dedup
    instead of double-counting the deploy.
    """
    rows = []
    for ev in pending:
        parsed = parse_pending_note(ev.get("note"))
        if not parsed:
            continue
        on_merge, tag = parsed
        event_type = on_merge if outcome == "merged" else "abandoned"
        if outcome == "merged":
            note = (tag + " — " if tag else "") + "merged after review" + (
                f" by {merged_by}" if merged_by else "")
        else:
            note = (tag + " — " if tag else "") + "abandoned, PR closed without merging"
        identity = "|".join(str(x) for x in (
            ev.get("deployment_repo"), ev.get("pr_number"), ev.get("environment"),
            ev.get("artifact_name"), event_type,
        ))
        rows.append({
            "event_id": uuid.uuid5(uuid.NAMESPACE_URL, identity).hex,
            "event_type": event_type,
            "event_ts": _iso(when),
            "requested_by": None,
            "artifact_name": ev.get("artifact_name"),
            "artifact_version": ev.get("artifact_version"),
            "prl1_only": None,
            "df_only": None,
            "note": note,
            "deployment_repo": ev.get("deployment_repo"),
            "release_name": None,
            "pr_number": ev.get("pr_number"),
            "build_verified": None,
            "environment": ev.get("environment"),
        })
    return rows


def _check(gh: Any, repo_full: str, pr_number: int) -> tuple[str, Any, str] | None:
    """('merged'|'closed', when, merged_by) — or None while the PR is open."""
    pr = gh.get_repo(repo_full).get_pull(int(pr_number))
    if getattr(pr, "merged", False):
        by = getattr(getattr(pr, "merged_by", None), "login", "") or ""
        return "merged", getattr(pr, "merged_at", None), by
    if getattr(pr, "state", "") == "closed":
        return "closed", getattr(pr, "closed_at", None), ""
    return None


def reconcile_pending(events: list[dict[str, Any]], github: Any = None) -> int:
    """Settle whatever pending PRs have merged or closed. Returns rows written.

    Never raises: this runs inside a read, and a GitHub or BigQuery hiccup has
    to leave the read working on the events it already has.
    """
    open_ = unresolved_pending(events)
    if not open_:
        return 0
    now = time.monotonic()
    if now - _state["at"] < _INTERVAL_SECONDS or not _lock.acquire(blocking=False):
        return 0
    try:
        _state["at"] = now
        from . import release_queue as _rq

        if github is None:
            from ._common import _get_github_client

            github = _get_github_client()
        written = 0
        # Newest first: a recently raised PR is the one most likely to have moved.
        keys = sorted(open_, key=lambda k: -int(k[1]))[:_MAX_PRS_PER_RUN]
        for repo_full, pr_number in keys:
            try:
                result = _check(github, repo_full, pr_number)
            except Exception:
                logger.warning("pr_reconcile: could not read %s#%s", repo_full, pr_number,
                               exc_info=True)
                continue
            if result is None:
                continue
            outcome, when, by = result
            rows = resolution_rows(open_[(repo_full, pr_number)], outcome, when, by)
            if rows and _rq._insert(rows).get("ok"):
                written += len(rows)
                logger.info("pr_reconcile: %s#%s %s — wrote %d event(s)",
                            repo_full, pr_number, outcome, len(rows))
        return written
    except Exception:
        logger.warning("pr_reconcile: reconciliation skipped", exc_info=True)
        return 0
    finally:
        _lock.release()
