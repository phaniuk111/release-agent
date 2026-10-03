"""PURE: what code reads off the evidence before any model sees it.

The signals (is the source healthy, security lines, denials, IAM changes, the
failure kinds, a release just before the first failure), the leads — plain
sentences built by rule — and the judgement: does the evidence establish a
cause at all, and which action does a failure kind imply. The eval showed a
model copying "re-trigger once" after an out-of-memory failure and guessing a
cause where there was none; these rules are why it no longer decides either.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from ._common import _CHANGE_DAYS, _hhmm, _iso, _line, _list, _ok, _plural
from .dataflow_job import _headline, classify
from .dataflow_job import _when as _parse_time  # RFC 3339 up to nanoseconds, Z or offset

_LEADS_MAX = 4
_HEALTHY_ERROR_RATE = 0.05    # at or above this a source counts as erroring
_IAM_BEFORE_DENIAL = timedelta(hours=24)


def source_is_healthy(values: dict[str, float | None]) -> bool | None:
    """True only when restarts, up and error_rate were ALL measured and fine;
    False as soon as any measured one is not (an unmeasured check cannot make
    a failing source look healthy, nor hide that it is failing); else None."""
    restarts, up, error_rate = values.get("restarts"), values.get("up"), values.get("error_rate")
    if (restarts is not None and restarts > 0) or (up is not None and up < 1) \
            or (error_rate is not None and error_rate >= _HEALTHY_ERROR_RATE):
        return False
    if restarts is None or up is None or error_rate is None:
        return None
    return True


def first_failure(business: date, streak: int | None, evidence: dict[str, Any]) -> tuple[datetime, bool]:
    """(when the failure started, whether that is only a DAY) as well as the
    evidence says: a run that failed N dates in a row started on the first of
    them; else the earliest Dataflow job that failed, source error or denial in
    the window; else the business date itself. A day-only answer means anything
    released during that day counts as before it."""
    if streak and streak > 1:
        d = business - timedelta(days=streak - 1)
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc), True
    seen: list[datetime] = []
    for run in _list(evidence.get("runs")):
        if _ok(run) and (when := _parse_time((run.get("first_error") or {}).get("time"))):
            seen.append(when)
    for job in _list(evidence.get("dataflow")):
        if _ok(job) and job.get("kind") not in (None, "none") and (when := _parse_time(job.get("created"))):
            seen.append(when)
    for group in _list((_ok(evidence.get("logs")) or {}).get("groups")):
        if when := _parse_time(group.get("first_seen")):
            seen.append(when)
    for group in _list((_ok(evidence.get("audit")) or {}).get("denials")):
        if when := _parse_time(group.get("first_seen")):
            seen.append(when)
    if seen:
        return min(seen), False
    return datetime(business.year, business.month, business.day, tzinfo=timezone.utc), True


def recent_change(changes: dict[str, Any] | None, failed_at: datetime, whole_day: bool = False) -> dict[str, Any] | None:
    """The newest release-log change at or before the first failure, within the
    lookback; None when nothing moved."""
    cutoff = failed_at + timedelta(days=1) if whole_day else failed_at
    best: tuple[datetime, dict[str, Any]] | None = None
    for c in _list((_ok(changes) or {}).get("changes")):
        when = _parse_time(c.get("when"))
        if when is None or when > cutoff or failed_at - when > timedelta(days=_CHANGE_DAYS):
            continue
        if best is None or when > best[0]:
            best = (when, c)
    if best is None:
        return None
    when, c = best
    return {"artifact": c.get("artifact"), "to_version": c.get("to_version"), "from_version": c.get("from_version"),
            "environment": c.get("environment"), "when": _iso(when),
            "days_before": max(0, (failed_at.date() - when.date()).days)}


def _earliest(groups: list[Any]) -> datetime | None:
    times = [t for g in groups if (t := _parse_time(g.get("first_seen")))]
    return min(times) if times else None


def run_errors(evidence: dict[str, Any]) -> list[str]:
    """The error-or-worse lines the failed runs themselves logged (by group)."""
    texts = []
    for run in _list(evidence.get("runs")):
        for g in _list((_ok(run) or {}).get("groups")):
            if g.get("severity") in ("ERROR", "CRITICAL", "ALERT", "EMERGENCY") and g.get("sample"):
                texts.append(str(g["sample"]))
    return texts


def _lead_run(sig: dict[str, Any], ev: dict[str, Any]) -> str | None:
    runs = [r for r in _list(ev.get("runs")) if _ok(r) and r.get("first_error")]
    if not runs:
        return None
    run = min(runs, key=lambda r: r["first_error"].get("time") or "")
    err = run["first_error"]
    when = _parse_time(err.get("time"))
    at = f" at {_hhmm(when)}" if when else ""
    return (f"Run {run.get('run_id')} logged its own error{at} in {err.get('component') or '?'}: "
            f"{_line(err.get('text'), 90)} — start there; it names this run, not just its source.")


def _lead_unhealthy(sig: dict[str, Any], ev: dict[str, Any]) -> str | None:
    if sig["source_healthy"] is not False:
        return None
    return f"The source is unhealthy by its metrics ({_ok(ev.get('metrics'))['summary']}) — start with the source."


def _lead_security(sig: dict[str, Any], ev: dict[str, Any]) -> str | None:
    n = sig["security_in_logs"]
    if not n:
        return None
    groups = [g for g in _list((_ok(ev.get("logs")) or {}).get("groups")) if g.get("security")]
    top = max(groups, key=lambda g: g.get("count") or 0) if groups else {}
    what = f" ({_line(top['sample'], 70)})" if top.get("sample") else ""
    return f"The source's own logs show {_plural(n, 'security line')}{what} — look at what it was denied."


def _unnarrowed(sig: dict[str, Any]) -> str:
    # Without the pipelines' service account the audit read is the whole project,
    # which is mostly cluster bootstrap noise: say so beside the lead it feeds.
    return "" if sig["audit_scope"] == "service account" else " (project-wide: SUPPORT_SERVICE_ACCOUNT is not set)"


def _lead_iam(sig: dict[str, Any], ev: dict[str, Any]) -> str | None:
    audit = _ok(ev.get("audit")) or {}
    first_denial = _earliest(_list(audit.get("denials")))
    if first_denial is None:
        return None
    before = [t for c in _list(audit.get("iam_changes")) if (t := _parse_time(c.get("time")))
              and t <= first_denial and first_denial - t <= _IAM_BEFORE_DENIAL]
    if not before:
        return None
    changed = max(before)
    minutes = int((first_denial - changed).total_seconds() // 60)
    gap = "just before" if minutes < 1 else f"{_plural(minutes, 'minute')} before"
    return f"An IAM policy changed at {_hhmm(changed)} UTC, {gap} the first denial.{_unnarrowed(sig)}"


def _lead_denials(sig: dict[str, Any], ev: dict[str, Any]) -> str | None:
    audit = _ok(ev.get("audit")) or {}
    groups = _list(audit.get("denials"))
    if not sig["denials"] or not groups:
        return None
    top = groups[0]
    return (f"{_plural(sig['denials'], 'permission-denied call')} in the audit log (most: "
            f"{_line(top.get('method') or '?', 50)} on {_line(top.get('resource') or '?', 60)}) "
            f"— look at which access was removed or never granted.{_unnarrowed(sig)}")


def _lead_change(sig: dict[str, Any], ev: dict[str, Any]) -> str | None:
    change = sig["recent_change"]
    if not change:
        return None
    days = change["days_before"]
    gap = "on the same day as" if days == 0 else f"{_plural(days, 'day')} before"
    env = f" on {change['environment']}" if change.get("environment") else ""
    if not change.get("to_version"):
        what = f"{change['artifact']} was removed{env}"
    elif change.get("from_version"):
        what = f"{change['artifact']} went {change['from_version']} → {change['to_version']}{env}"
    else:
        what = f"{change['artifact']} was deployed at {change['to_version']}{env}"
    return f"{what} {gap} the first failure."


def _lead_dataflow(sig: dict[str, Any], ev: dict[str, Any]) -> str | None:
    for job in _list(ev.get("dataflow")):
        if _ok(job) and job.get("kind") not in (None, "none"):
            state = str(job.get("state") or "").replace("JOB_STATE_", "").lower() or "failed"
            first = next(iter(_list(job.get("errors"))), {}).get("text") \
                or next(iter(_list(job.get("log_groups"))), {}).get("sample") or ""
            why = f": {_line(_headline(str(first)), 90)}" if first else ""
            return _line(f"Dataflow job …{str(job.get('job_id') or '')[-8:]} {state}, kind {job['kind']}{why}")
    return None


def _lead_healthy(sig: dict[str, Any], ev: dict[str, Any]) -> str | None:
    # A source that logs permission errors is not "fine" because its pods are up.
    if sig["source_healthy"] is not True or sig["security_in_logs"] or sig["denials"]:
        return None
    return "The source is healthy by its metrics — the problem is downstream of it."


# In the order a person would read them; the first _LEADS_MAX that apply are kept.
_LEAD_RULES: tuple[Callable[[dict[str, Any], dict[str, Any]], str | None], ...] = (
    _lead_run, _lead_unhealthy, _lead_security, _lead_iam, _lead_denials, _lead_change, _lead_dataflow, _lead_healthy)


def compute_signals(*, business: date, evidence: dict[str, Any], category: str | None,
                    streak: int | None, last_success: str | None, audit_principal: str = "") -> dict[str, Any]:
    """PURE. The deterministic leads the model should not have to rediscover."""
    metrics = _ok(evidence.get("metrics"))
    logs = _ok(evidence.get("logs"))
    audit = _ok(evidence.get("audit"))
    kinds: list[str] = []
    for job in _list(evidence.get("dataflow")):
        kind = job.get("kind") if _ok(job) else None
        if kind and kind != "none" and kind not in kinds:
            kinds.append(kind)
    failed_at, whole_day = first_failure(business, streak, evidence)
    own = run_errors(evidence)
    run_kind = classify(own) if own else None
    sig: dict[str, Any] = {
        # what the failed runs' OWN lines say — "unknown" when they erred in words
        # no rule knows, None when they logged no error (or were not searched)
        "run_kind": run_kind,
        "run_errors": len(own),
        "security_in_run": sum(int((_ok(r) or {}).get("security_lines") or 0) for r in _list(evidence.get("runs"))),
        "source_healthy": source_is_healthy(metrics.get("values") or {}) if metrics else None,
        "security_in_logs": int((logs or {}).get("security_lines") or 0),
        "denials": int((audit or {}).get("denials_total") or 0),
        "iam_changes": len(_list((audit or {}).get("iam_changes"))),
        "audit_scope": "service account" if audit_principal else "project-wide",
        "job_kinds": kinds,
        "recent_change": recent_change(evidence.get("changes"), failed_at, whole_day),
        "streak": streak,
        "category": category,
        "last_success": last_success,
    }
    leads = [text for rule in _LEAD_RULES if (text := rule(sig, evidence))]
    sig["leads"] = leads[:_LEADS_MAX]
    return with_judgement(sig)


# What a failure KIND means for the person on support — decided here, not by the
# model: the eval showed it copying the triage's generic "re-trigger once" after
# an out-of-memory failure (which fails again) and guessing a cause when there
# was none. kind → (action, why).
_KIND_ACTIONS: dict[str, tuple[str, str]] = {
    "out_of_memory": ("escalate", "a retry fails the same way until the job gets more memory or its input is split"),
    "permission": ("escalate", "a permission error repeats until the access is restored — a re-run will not help"),
    "schema": ("escalate", "the data and the code disagree — a re-run will not help"),
    "quota": ("retrigger", "a quota failure is usually momentary — re-trigger once after a short wait, escalate if it repeats"),
    "worker_lost": ("retrigger", "a lost worker is usually transient — re-trigger once, escalate if it repeats"),
    "not_found": ("wait", "an input was missing — confirm it has arrived before anything is re-triggered"),
}


def with_judgement(signals: dict[str, Any] | None) -> dict[str, Any]:
    """PURE. ``signals`` plus the two calls code makes for the model:

    conclusive   does ANY piece of evidence establish a cause? False means the
                 answer must say the cause could not be determined — the model
                 may not fill the gap with a guess.
    action_hint  {"action", "why"} when the evidence implies an action (first
                 rule that applies), else None: keep the triage's own.

    Applied at collect time and again in for_model, so a bundle built without
    it (an eval case, an older cache entry) is judged the same way."""
    sig = dict(signals or {})
    kinds = [k for k in _list(sig.get("job_kinds")) if k and k not in ("unknown", "none")]
    run_kind = sig.get("run_kind") if sig.get("run_kind") not in (None, "unknown", "none") else None
    run_security = int(sig.get("security_in_run") or 0)
    scoped_denials = int(sig.get("denials") or 0) if sig.get("audit_scope") != "project-wide" else 0
    security = int(sig.get("security_in_logs") or 0)
    change = sig.get("recent_change")
    unhealthy = sig.get("source_healthy") is False
    sig["conclusive"] = bool(run_kind or run_security or kinds or security or scoped_denials or change or unhealthy)
    hint: tuple[str, str] | None = None
    # The run's own lines first: they say what happened to THIS run, where the
    # source's logs and the audit log are everything that happened that day.
    if run_kind in _KIND_ACTIONS:
        hint = _KIND_ACTIONS[run_kind]
    elif run_security:
        hint = _KIND_ACTIONS["permission"]
    elif unhealthy:
        hint = ("wait", "the source itself is unhealthy — a re-run reads the same broken source; start there")
    elif security or scoped_denials:
        hint = _KIND_ACTIONS["permission"]
    elif change and sig.get("category") in ("process", None):
        hint = ("escalate", "a release changed this shortly before the first failure — a re-run will not help")
    else:
        hint = next((_KIND_ACTIONS[k] for k in kinds if k in _KIND_ACTIONS), None)
    sig["action_hint"] = {"action": hint[0], "why": hint[1]} if hint else None
    return sig
