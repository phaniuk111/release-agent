"""Deployment tracking: a timed sweep that WATCHES the PRs the portal raised.

A deploy or release that stops at a PR awaiting review is finished by a person
in GitHub, outside any chat turn, and the workflow runs its merge starts finish
later still. Nobody is told. This sweep closes that gap, with the facts in the
portal event log (domain "deploy", one `scope` per PR: `deploy:<repo>#<pr>`)
and a bell item per stage for the requester:

    the PR merges         -> deploy/merged (the merge SHA), "Deployment starting"
    every run concluded   -> deploy/finished (conclusion, the decisive run, the
                             failing jobs and steps), "Deployment completed" or
                             "Deployment failed at <job> > <step>" — the SAME
                             scope, so the bell shows one item whose text changed
    a run re-run later    -> a new deploy/finished, "… (after re-run)"
    TRACK_RUN_MAX_MINUTES -> one "still running" item, deploy/abandoned_tracking,
    with no conclusion       and the merge is left alone

It watches and tells; it never merges, re-runs, retries or rolls anything back.
The one GitHub write is dispatching the team's own send-notification workflow
(NOTIFY_WORKFLOW) at each stage — named in approvals.py as the one write that
never pauses for a yes, because it changes no deployment.

Cadence (next_delay, pure): nothing pending -> one event-log read per
TRACK_OPEN_MINUTES and no GitHub call; a PR open -> every TRACK_OPEN_MINUTES; a
merge with runs in flight -> every TRACK_RUN_SECONDS; a pass that failed -> the
slow cadence, never the fast one. One pass at a time per process (begin/finish,
like the Auto-triage watcher), started from the app lifespan only when
DEPLOY_TRACKING_ENABLED is true. A pass never raises: each PR's failure is a
line in the pass summary and the log, and the next pass tries again.

Which PRs: the ones with a `pending` event in the release log — read the way
pr_reconcile reads them — while unresolved, and for TRACK_RUN_MAX_MINUTES after
pr_reconcile settles them as merged (a read path may settle a PR between two
passes; the tracker must still follow its runs). What is known about a merge
comes from the event log, not memory, so a restarted pod carries on.

Token: `_get_github_client()` as everywhere — there is no session on a timed
sweep, so this is GH_TOKEN, the bot (values.yaml says so).
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import itertools
import json
import logging
import threading
from typing import Any

from release_agent.config import settings
from release_agent.tools import events_store, notifications, pr_reconcile, tickets

from . import approvals

logger = logging.getLogger(__name__)

DOMAIN = "deploy"
_MERGED, _FINISHED, _ABANDONED = "merged", "finished", "abandoned_tracking"
_TRACKED_TYPES = (_MERGED, _FINISHED, _ABANDONED)
_PENDING_DAYS = 30      # how far back a pending PR is still looked for
_KNOWN_DAYS = 2         # how far back the tracker's own rows are read (the window is 90 min)
_MAX_RUNS = 50
_MAX_CHARTS_NAMED = 3

# ----- the sweep's own state (this process) ----------------------------------------

_status_lock = threading.Lock()
_status: dict[str, Any] = {"running": False, "last_run_at": None, "last_error": "", "last_summary": None}


def status() -> dict[str, Any]:
    with _status_lock:
        out = dict(_status)
    out["enabled"] = bool(settings.deploy_tracking_enabled)
    out["store"] = events_store.store_name()
    out["notify_workflow"] = bool(str(settings.notify_workflow or "").strip())
    return out


def begin() -> bool:
    """Claim the pass; False when one is already running in this process."""
    with _status_lock:
        if _status["running"]:
            return False
        _status["running"] = True
        return True


def finish(*, error: str = "", summary: dict[str, Any] | None = None) -> None:
    with _status_lock:
        _status.update(running=False, last_run_at=_now().isoformat(), last_error=error,
                       last_summary=summary)


# ----- cadence (pure) --------------------------------------------------------------

def next_delay(state: dict[str, Any]) -> float:
    """Seconds until the next pass, from what the last one found: ``in_flight``
    merges whose runs have not concluded, ``open`` PRs still awaiting a merge,
    ``error`` when the pass failed. A failing pass never runs at the fast
    cadence — GitHub being down is not a reason to ask it more often."""
    slow = max(30.0, float(settings.track_open_minutes) * 60.0)
    if state.get("error"):
        return slow
    if int(state.get("in_flight") or 0) > 0:
        return max(10.0, float(settings.track_run_seconds))
    return slow


def state_of(summary: dict[str, Any] | None) -> dict[str, Any]:
    """The cadence inputs a pass summary carries."""
    summary = summary or {}
    return {"open": int(summary.get("open") or 0), "in_flight": int(summary.get("in_flight") or 0),
            "error": str(summary.get("error") or "")}


# ----- what to follow (pure) -------------------------------------------------------

def scope_for(repo_full: str, pr_number: int) -> str:
    return f"deploy:{repo_full}#{int(pr_number)}"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _when(value: Any) -> _dt.datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, _dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=_dt.timezone.utc)
    try:
        text = str(value).strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        out = _dt.datetime.fromisoformat(text)
        return out if out.tzinfo else out.replace(tzinfo=_dt.timezone.utc)
    except ValueError:
        return None


def window() -> _dt.timedelta:
    """How long after its merge a PR is followed."""
    return _dt.timedelta(minutes=max(1, int(settings.track_run_max_minutes)))


def candidates(events: list[dict[str, Any]], now: _dt.datetime | None = None) -> dict[tuple[str, int], dict[str, Any]]:
    """The PRs to visit this pass, from the release log's events. Pure.

    A PR with a `pending` event is followed while nothing settles it, and for
    the tracking window after a `deployed`/`removed` event settles it as merged
    (pr_reconcile may have noticed the merge first). A PR settled as abandoned,
    or merged longer ago than the window, is not."""
    now = now or _now()
    unresolved = pr_reconcile.unresolved_pending(events)
    pending_by_key: dict[tuple[str, int], list[dict[str, Any]]] = {}
    settled: dict[tuple[str, int], tuple[str, _dt.datetime | None]] = {}
    for ev in events:
        key = (ev.get("deployment_repo"), ev.get("pr_number"))
        if not key[0] or not key[1]:
            continue
        kind = ev.get("event_type")
        if kind == "pending" and pr_reconcile.parse_pending_note(ev.get("note")):
            pending_by_key.setdefault(key, []).append(ev)
        elif kind in ("deployed", "removed"):
            settled[key] = ("merged", _when(ev.get("event_ts")))
        elif kind == "abandoned":
            settled[key] = ("closed", _when(ev.get("event_ts")))
    out: dict[tuple[str, int], dict[str, Any]] = {}
    for key, pending in pending_by_key.items():
        if key in unresolved:
            out[key] = {"pending": pending, "settled": ""}
            continue
        outcome, when = settled.get(key, ("", None))
        if outcome == "merged" and when is not None and now - when <= window():
            out[key] = {"pending": pending, "settled": "merged"}
    return out


def describe(pending: list[dict[str, Any]], pr_number: int) -> str:
    """What the PR deploys, as a person names it: the release, else the charts."""
    for ev in pending:
        if ev.get("release_name"):
            return str(ev["release_name"])
    charts = []
    for ev in pending:
        name = ev.get("artifact_name")
        if name:
            label = f"{name}:{ev.get('artifact_version')}" if ev.get("artifact_version") else str(name)
            if label not in charts:
                charts.append(label)
    if not charts:
        return f"PR #{int(pr_number)}"
    shown = ", ".join(charts[:_MAX_CHARTS_NAMED])
    extra = len(charts) - _MAX_CHARTS_NAMED
    return shown + (f" +{extra} more" if extra > 0 else "")


def _first(pending: list[dict[str, Any]], field: str) -> str:
    for ev in pending:
        if ev.get(field):
            return str(ev[field])
    return ""


def known_merges(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The tracker's latest row per scope (merged / finished / abandoned_tracking),
    from a newest-first history read. Pure."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        scope = row.get("scope")
        if scope and row.get("event_type") in _TRACKED_TYPES and scope not in out:
            out[scope] = row
    return out


# ----- the runs a merge started ---------------------------------------------------

def _wanted_workflows() -> set[str]:
    return {w.strip().lower() for w in str(settings.deploy_workflows or "").split(",") if w.strip()}


def run_matches(run: dict[str, Any], wanted: set[str]) -> bool:
    """Does this run count as the deployment? Every run on the merge SHA when
    DEPLOY_WORKFLOWS is empty; else one whose workflow name or file is listed."""
    if not wanted:
        return True
    return run.get("name", "").lower() in wanted or run.get("workflow", "").lower() in wanted


def _run_dict(run: Any) -> dict[str, Any]:
    path = str(getattr(run, "path", "") or "")
    return {"id": int(getattr(run, "id", 0) or 0), "name": str(getattr(run, "name", "") or ""),
            "workflow": path.split("/")[-1], "status": str(getattr(run, "status", "") or ""),
            "conclusion": str(getattr(run, "conclusion", "") or ""),
            "url": str(getattr(run, "html_url", "") or ""),
            "attempt": int(getattr(run, "run_attempt", 1) or 1), "obj": run}


def runs_for(repo_obj: Any, merge_sha: str) -> list[dict[str, Any]]:
    """The workflow runs on the merge commit that count as the deployment. No
    commit, no runs — an empty SHA would list every run in the repo."""
    if not merge_sha:
        return []
    wanted = _wanted_workflows()
    found = []
    for run in itertools.islice(repo_obj.get_workflow_runs(head_sha=merge_sha), _MAX_RUNS):
        item = _run_dict(run)
        if run_matches(item, wanted):
            found.append(item)
    return found


def decisive(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """The run the outcome is named after: the first that did not succeed, else
    the last one."""
    for run in runs:
        if run["conclusion"] != "success":
            return run
    return runs[-1]


def failing_steps(run: dict[str, Any]) -> list[dict[str, Any]]:
    """The failing jobs and steps of a run, read through controls.py's reader
    (the same one the build report uses); [] when the jobs cannot be read."""
    from release_agent.tools import controls

    try:
        return [{"job": s.get("job"), "step": s.get("name"), "conclusion": s.get("conclusion")}
                for s in controls._failed_steps(run["obj"])]
    except Exception:  # noqa: BLE001
        return []


def _attempts(runs: list[dict[str, Any]]) -> dict[str, int]:
    return {str(r["id"]): r["attempt"] for r in runs}


def _details(row: dict[str, Any] | None) -> dict[str, Any]:
    try:
        out = json.loads((row or {}).get("details") or "{}")
        return out if isinstance(out, dict) else {}
    except ValueError:
        return {}


def is_rerun(runs: list[dict[str, Any]], last_finished: dict[str, Any]) -> bool:
    """A run id the last `finished` row knew, now at a higher attempt. Pure."""
    known = _details(last_finished).get("attempts") or {}
    return any(int(known.get(run_id, 0) or 0) < attempt for run_id, attempt in _attempts(runs).items()
               if run_id in known)


# ----- the one GitHub write --------------------------------------------------------

def notify_workflow_target() -> tuple[str, str]:
    """("owner/repo", "workflow-file.yml") from NOTIFY_WORKFLOW, or ("", "")."""
    raw = str(settings.notify_workflow or "").strip()
    repo, sep, workflow = raw.rpartition(":")
    if not sep or "/" not in repo or not workflow:
        return "", ""
    return repo.strip(), workflow.strip()


def dispatch_notify_workflow(github: Any, stage: str, *, release: str, chg: str, rmg: str, pr: str,
                             requested_by: str) -> dict[str, Any]:
    """Trigger the team's send-notification workflow with what the tracker saw.
    {"ok": True, "run_id", "run_url"} ({"ok": True, "skipped": why} when unset),
    or {"ok": False, "error"}; never raises. The run id comes from GitHub's
    `return_run_details` (the DF dispatch's `_dispatch`), never a guess."""
    repo_full, workflow_file = notify_workflow_target()
    if not repo_full:
        return {"ok": True, "skipped": "NOTIFY_WORKFLOW is not set"}
    inputs = {"stage": stage, "release": release, "chg": chg, "rmg": rmg, "pr": pr, "requested_by": requested_by}
    # The dispatch is made only while approvals.py names it as the write that
    # needs no yes: remove the rule there and the tracker stops dispatching
    # rather than writing without one.
    if ("dispatch_notify_workflow" not in approvals.ALLOWED_WITHOUT_YES
            or approvals.needs_approval("dispatch_notify_workflow", inputs)):
        return {"ok": False, "error": "dispatch_notify_workflow is no longer allowed without approval"}
    try:
        from release_agent.tools.dataflow import _dispatch

        repo_obj = github.get_repo(repo_full)
        workflow = repo_obj.get_workflow(workflow_file)
        run = _dispatch(workflow, getattr(repo_obj, "default_branch", "main") or "main", inputs)
        return {"ok": True, "run_id": (run or {}).get("id"), "run_url": (run or {}).get("url", "")}
    except Exception as e:  # noqa: BLE001 — a notification workflow that will not start is reported, not raised
        logger.warning("deploy_tracker: NOTIFY_WORKFLOW dispatch (%s) failed: %s", stage, e)
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


# ----- one PR -----------------------------------------------------------------------

def _record(row: dict[str, Any]) -> None:
    out = events_store.record({"domain": DOMAIN, "actor": "portal", **row})
    if not out.get("ok"):
        logger.warning("deploy_tracker: could not record %s for %s: %s", row.get("event_type"),
                       row.get("scope"), out.get("error"))


def _follow(github: Any, key: tuple[str, int], cand: dict[str, Any], known: dict[str, dict[str, Any]],
            now: _dt.datetime, summary: dict[str, Any],
            outcomes: dict[tuple[str, int], Any] | None = None) -> None:
    """Follow one pending PR. ``outcomes``, when given, is filled with this
    pass's reading of the PR (``pr_reconcile.pr_outcome``) so the settlement that
    follows the pass does not fetch it a second time."""
    repo_full, pr_number = key[0], int(key[1])
    pending = cand["pending"]
    what = describe(pending, pr_number)
    requester = _first(pending, "requested_by")
    jira = _first(pending, "jira_ticket")
    scope = scope_for(repo_full, pr_number)

    repo_obj = github.get_repo(repo_full)
    pr = repo_obj.get_pull(pr_number)
    pr_url = str(getattr(pr, "html_url", "") or "")
    outcome = pr_reconcile.pr_outcome(pr)
    if outcomes is not None:
        outcomes[(repo_full, pr_number)] = outcome
    if outcome is None:
        summary["open"] += 1
        tk = tickets.pr_tickets(repo_full, pr_number, github=github, pr=pr)
        tickets.note_first_seen(repo_full, pr_number, tk, requested_by=requester, what=what)
        return
    kind, when, merged_by, _url = outcome
    if kind != "merged":
        return                                    # pr_reconcile records the abandonment
    merge_sha = str(getattr(pr, "merge_commit_sha", "") or "")
    if not merge_sha:
        # GitHub omits merge_commit_sha for a moment after a merge. Without it
        # there is nothing to follow — and a run lookup on "" lists every run in
        # the repo. Nothing is recorded or sent; the next pass reads it again.
        summary["in_flight"] += 1
        return
    merged_at = _when(when) or now
    last = known.get(scope)
    beyond_window = now - merged_at > window()
    if last is not None and (last.get("event_type") == _ABANDONED
                             or (last.get("event_type") == _FINISHED and beyond_window)):
        return                       # given up on, or a re-run after the window: not followed
    tk = tickets.pr_tickets(repo_full, pr_number, github=github, pr=pr)
    cols = tickets.columns(tk)
    if last is None:                 # newly merged: the one time the tickets are noted for a merge
        tickets.note_first_seen(repo_full, pr_number, tk, requested_by=requester, what=what)
    common = {"subject": scope, "scope": scope, "repo": repo_full, "pr_number": pr_number,
              "merge_sha": merge_sha, "jira": jira or None, "title": what,
              "environment": _first(pending, "environment") or None, **cols}

    if last is None:
        dispatched = dispatch_notify_workflow(github, "starting", release=what, chg=cols["chg"], rmg=cols["rmg"],
                                              pr=str(pr_number), requested_by=requester)
        _record({**common, "event_type": _MERGED, "event_ts": merged_at,
                 "body": f"merged by {merged_by}" if merged_by else None,
                 "details": {"notify_run_id": dispatched.get("run_id"), "notify_error": dispatched.get("error")}})
        if requester:
            notifications.notify(
                requester, kind="deploy_started", title=f"Deployment starting: {what} — PR #{pr_number} merged",
                body=(f"Merged by {merged_by}. " if merged_by else "") + f"Following the runs on {merge_sha[:7]}.",
                link=pr_url, scope=scope, repo=repo_full, pr_number=pr_number)
        summary["started"].append(scope)
        last = {"event_type": _MERGED, "event_ts": merged_at.isoformat()}

    runs = runs_for(repo_obj, merge_sha)
    rerun = last.get("event_type") == _FINISHED
    if rerun and not is_rerun(runs, last):
        return
    concluded = bool(runs) and all(r["conclusion"] for r in runs)
    if not concluded:
        if beyond_window:
            minutes = int(window().total_seconds() // 60)
            link = next((r["url"] for r in runs if r["url"]), "") or f"portal:{scope}"
            _record({**common, "event_type": _ABANDONED, "details": {"attempts": _attempts(runs)},
                     "body": f"no conclusion after {minutes} minutes"})
            if requester:
                notifications.notify(
                    requester, kind="deploy_finished", severity="warning",
                    title=f"Deployment still running after {minutes} min: {what}",
                    body="The runs on the merge have not concluded; the portal has stopped following them.",
                    link=link, scope=scope, repo=repo_full, pr_number=pr_number)
            summary["abandoned"].append(scope)
        else:
            summary["in_flight"] += 1
        return

    run = decisive(runs)
    ok = all(r["conclusion"] == "success" for r in runs)
    failed = [] if ok else failing_steps(run)
    stage = "completed" if ok else "failed"
    dispatched = dispatch_notify_workflow(github, stage, release=what, chg=cols["chg"], rmg=cols["rmg"],
                                          pr=str(pr_number), requested_by=requester)
    _record({**common, "event_type": _FINISHED, "conclusion": "success" if ok else "failure",
             "run_id": run["id"] or None, "run_url": run["url"] or None, "workflow": run["workflow"] or run["name"],
             "details": {"attempts": _attempts(runs), "failed_steps": failed, "rerun": rerun,
                         "runs": [{"id": r["id"], "name": r["name"], "conclusion": r["conclusion"]} for r in runs],
                         "notify_run_id": dispatched.get("run_id"), "notify_error": dispatched.get("error")}})
    if requester:
        after = " (after re-run)" if rerun else ""
        if ok:
            title = f"Deployment completed{after}: {what}"
            body = f"{run['name'] or run['workflow']} run {run['id']} succeeded."
            severity = "info"
        else:
            at = f" at {failed[0]['job']} › {failed[0]['step']}" if failed else ""
            title = f"Deployment failed{at}{after}: {what}"
            body = f"{run['name'] or run['workflow']} run {run['id']} ended {run['conclusion']}."
            severity = "critical"
        notifications.notify(requester, kind="deploy_finished", title=title, body=body, severity=severity,
                             link=run["url"] or f"portal:{scope}", scope=scope, repo=repo_full,
                             pr_number=pr_number)
    summary["finished"].append(scope)


# ----- one pass ---------------------------------------------------------------------

def _read_events() -> list[dict[str, Any]]:
    from release_agent.tools import release_queue

    if not release_queue.queue_enabled():
        return []
    return release_queue._fetch_events(_PENDING_DAYS)


def run_pass(*, github: Any = None, events: list[dict[str, Any]] | None = None,
             now: _dt.datetime | None = None) -> dict[str, Any]:
    """One sweep. {"ok", "open", "in_flight", "started", "finished", "abandoned",
    "errors"} or {"ok": False, "error"}; never raises. ``events`` and ``github``
    are for tests — a pass reads the release log and uses the portal's client."""
    if not begin():
        return {"ok": False, "error": "A tracking pass is already running."}
    summary: dict[str, Any] = {"ok": True, "open": 0, "in_flight": 0, "started": [], "finished": [],
                               "abandoned": [], "errors": []}
    try:
        now = now or _now()
        events = _read_events() if events is None else events
        cands = candidates(events, now)
        if not cands:
            finish(summary=summary)
            return summary
        if github is None:
            from release_agent.tools._common import _get_github_client

            github = _get_github_client()
        read = events_store.history(DOMAIN, since_days=_KNOWN_DAYS)
        if not read.get("ok"):
            raise RuntimeError(f"the event log could not be read: {read.get('error')}")
        known = known_merges(read.get("rows") or [])
        outcomes: dict[tuple[str, int], Any] = {}
        for key in sorted(cands, key=lambda k: -int(k[1])):
            try:
                _follow(github, key, cands[key], known, now, summary, outcomes)
            except Exception as e:  # noqa: BLE001 — one PR's failure must not stop the others
                line = f"{key[0]}#{key[1]}: {type(e).__name__}: {str(e)[:200]}"
                summary["errors"].append(line)
                logger.warning("deploy_tracker: %s", line)
        # The release log is settled too (throttled, locked, never raises) so a
        # merge the tracker saw first does not wait for a read path to notice it.
        # It is handed what this pass just read, so no PR is fetched twice.
        pr_reconcile.reconcile_pending(events, github=github, outcomes=outcomes)
        finish(summary=summary)
        logger.info("deploy_tracker pass | open=%d in_flight=%d started=%d finished=%d abandoned=%d errors=%d",
                    summary["open"], summary["in_flight"], len(summary["started"]), len(summary["finished"]),
                    len(summary["abandoned"]), len(summary["errors"]))
        return summary
    except Exception as e:  # noqa: BLE001 — a pass that fails is reported, never raised
        logger.exception("deploy_tracker pass failed")
        error = f"{type(e).__name__}: {str(e)[:300]}"
        finish(error=error, summary=summary)
        return {**summary, "ok": False, "error": error}


async def track_forever() -> None:
    """The sweep, started from the app lifespan when DEPLOY_TRACKING_ENABLED."""
    while True:
        summary = await asyncio.to_thread(run_pass)
        await asyncio.sleep(next_delay(state_of(summary)))
