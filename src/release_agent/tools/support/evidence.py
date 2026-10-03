"""Investigation context: the evidence about ONE incident, gathered in code.

An investigation used to be the chat model calling six evidence tools one model
round-trip at a time (minutes, several expensive calls). The evidence does not
depend on what the model thinks, so code gathers all of it at once, prunes it,
lays it out as a TIMELINE and works out the obvious leads by rule — and the
model is called once, on the result:

    collect(business_date, incident_id)  -> the bundle: incident, evidence from
                                            the source's metrics, logs, the
                                            audit log, the Dataflow jobs and the
                                            release log (read in parallel),
                                            the timeline, signals and leads
    for_model(bundle)                    -> a pruned, bounded copy for the model
    render_markdown(bundle)              -> the whole answer with NO model:
                                            what was checked, timeline, leads,
                                            what L1 does

READ-ONLY, and nothing here raises: a step that could not be read is named in
`unavailable` with its hint — said, never hidden — and the rest still answers.
The evidence functions are the same ones the chat tools wrap (source_logs,
dataflow_job, release_lookup, monitoring); this module adds no new access.
"""
from __future__ import annotations

import copy
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as StepTimeout
from datetime import date, datetime, timezone
from typing import Any, Callable

from ...config import settings
from .. import monitoring
from . import cache, dataflow_job, release_lookup, source_logs
from . import triage as support_triage
from ._common import _CHANGE_DAYS, _M_GROUPS, _WINDOW_HOURS, _iso, _line, _list, _name_ok, _ok, cob_window
from .report import _to_date
from .signals import compute_signals, with_judgement
from .timeline import _TIMELINE_MAX, _capped, build_timeline
from .dataflow_job import _when as _parse_time  # RFC 3339 up to nanoseconds, Z or offset

_log = logging.getLogger(__name__)

# One shared pool: a pool per call was a load bug (threads piled up under a
# burst of investigations). A bundle needs up to nine reads; the rest queue.
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="investigate")
_STEP_TIMEOUT = 45.0          # seconds from the start of a collect, for every read
_CACHE_TTL = 600.0
_CACHE_TTL_PARTIAL = 60.0     # a bundle with an unread step is retried sooner
_CACHE_MAX = 64
_MAX_JOBS = 3


# Bounds of what the model sees (for_model).
_M_JOB_ERRORS = 3
_M_JOB_GROUPS = 4
_M_CHANGES = 8
_M_SAMPLE = 200
_M_BYTES = 11 * 1024 + 512   # what for_model aims at, so the result stays under 12 KB



# ----- the source's metrics (same expressions as the chat tool's) -----------------

# name=expr;… with {source} {namespace} {cluster} {window} filled in. The pod
# label the person's clusters carry is `app` (SUPPORT_SOURCE_LABEL) — these
# defaults assume kube-state-metrics and an http_requests_total counter; a
# deployment overrides the string (SUPPORT_SOURCE_PROMQL) for its own exporters.
DEFAULT_SOURCE_PROMQL = (
    'restarts=sum(increase(kube_pod_container_status_restarts_total{{namespace="{namespace}",pod=~"{source}.*"}}[{window}]));'
    'up=min(up{{app="{source}",namespace="{namespace}"}});'
    'error_rate=sum(rate(http_requests_total{{app="{source}",namespace="{namespace}",code=~"5.."}}[{window}]))'
    ' / sum(rate(http_requests_total{{app="{source}",namespace="{namespace}"}}[{window}]))'
)


def metric_checks(source: str) -> dict[str, str]:
    """The configured health expressions for one source, placeholders filled.
    ValueError when the configuration cannot be filled (a placeholder it does
    not know) — said to the person, not raised past collect()."""
    fills = {"source": source, "namespace": getattr(settings, "support_namespace", ""),
             "cluster": getattr(settings, "support_cluster", ""), "window": f"{_WINDOW_HOURS}h"}
    raw = str(getattr(settings, "support_source_promql", "") or DEFAULT_SOURCE_PROMQL)
    checks: dict[str, str] = {}
    for item in raw.split(";"):
        name, sep, expr = item.partition("=")
        if sep and name.strip() and expr.strip():
            try:
                checks[name.strip()] = expr.strip().format(**fills)
            except (KeyError, IndexError, ValueError) as e:
                raise ValueError(f"SUPPORT_SOURCE_PROMQL check {name.strip()!r} cannot be filled: {e!r}") from e
    if not checks:
        raise ValueError("no source health checks are configured (SUPPORT_SOURCE_PROMQL)")
    return checks


def _series_value(res: dict[str, Any]) -> float | None:
    """The check's number: the largest value across its series; None when it
    measured nothing. NaN (0 / 0 on a source with no traffic) is nothing."""
    values = [s.get("value") for s in _list(res.get("series"))
              if isinstance(s.get("value"), (int, float)) and s["value"] == s["value"]]
    return max(values) if values else None


def assemble_metrics(source: str, start: datetime, end: datetime, results: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """PURE. One result per check → the metrics evidence. ok only when at
    least one check measured something; a check that failed is 'not measured'."""
    values: dict[str, float | None] = {}
    verdicts = []
    for name, res in results.items():
        if not res.get("ok"):
            values[name] = None
            verdicts.append(f"{name} not measured")
            continue
        values[name] = _series_value(res)
        verdicts.append(f"{name} no series" if values[name] is None else f"{name} {values[name]:g}")
    failed = [res for res in results.values() if not res.get("ok")]
    if failed and len(failed) == len(results):
        first = failed[0]
        return {"ok": False, "error": str(first.get("error") or "the metrics query failed")[:300],
                "hint": str(first.get("hint") or "")}
    return {"ok": True, "source": source, "window": {"start": _iso(start), "end": _iso(end)},
            "checks": results, "values": values, "summary": " · ".join(verdicts)}


# ----- the `checked` lines ------------------------------------------------------------

def _checked(incident: dict[str, Any] | None, evidence: dict[str, Any], target: dict[str, Any]) -> list[str]:
    lines = []
    if incident:
        lines.append(f"Control table (BigQuery): {incident.get('title')}")
    if (m := _ok(evidence.get("metrics"))):
        lines.append(f"Source metrics (PromQL): {m['summary']}")
    if (g := _ok(evidence.get("logs"))):
        lines.append(f"Source logs (Cloud Logging, {target['source']}): {g.get('summary')}")
    if (a := _ok(evidence.get("audit"))):
        lines.append(f"Audit logs (Cloud Audit Logs): {a.get('summary')}")
    for job in _list(evidence.get("dataflow")):
        if _ok(job):
            lines.append(f"Dataflow job {job.get('job_id')} (Dataflow API): {job.get('summary')}")
    if not target["job_ids"]:
        lines.append("Dataflow: this incident's runs logged no job id — no job to inspect.")
    if (c := _ok(evidence.get("changes"))):
        lines.append(f"Release log (BigQuery release_intents): {c.get('summary')}")
    elif not [n for n in (target["source"], target["process"]) if n]:
        lines.append("Release log: no source or process name to look up.")
    return lines


# ----- reading the evidence ------------------------------------------------------------

def _guard(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """The evidence functions never raise by contract; a bug in one must still
    come back as a named failed step, not take the whole bundle with it."""
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}


def _run_steps(steps: dict[str, Callable[[], dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """All steps at once on the shared pool, each given until one deadline
    (counted from here, so queueing behind other investigations counts too). A
    step that is not done by then is a failed step — it is not waited for."""
    deadline = time.monotonic() + _STEP_TIMEOUT
    futures = {name: _POOL.submit(_guard, fn) for name, fn in steps.items()}
    out: dict[str, dict[str, Any]] = {}
    for name, future in futures.items():
        try:
            out[name] = future.result(timeout=max(0.0, deadline - time.monotonic()))
        except StepTimeout:
            future.cancel()
            out[name] = {"ok": False, "error": f"timed out after {_STEP_TIMEOUT:g}s",
                         "hint": "the read is slow right now — try again in a minute"}
    return out


def _resolve_target(incident: dict[str, Any] | None, source: str, process: str,
                    job_ids: list[str] | None) -> dict[str, Any]:
    shared = (incident or {}).get("shared") or {}
    jobs = list((incident or {}).get("job_ids") or job_ids or [])
    clean_jobs: list[str] = []
    for j in jobs:
        j = str(j).strip()
        if j and j not in clean_jobs:
            clean_jobs.append(j)
    return {"source": str(shared.get("source") or source or "").strip(),
            "process": str(shared.get("process") or process or "").strip(),
            "job_ids": clean_jobs[:_MAX_JOBS]}


def _incident_facts(report: dict[str, Any], incident: dict[str, Any] | None) -> tuple[int | None, str | None]:
    """(max streak, last success date) of the incident's error group — they sit
    on the report's error entry, not on the incident."""
    if not incident or incident.get("kind") != "error":
        return None, None
    errors = _list(report.get("errors"))
    i = incident.get("error_index")
    err = errors[i] if isinstance(i, int) and 0 <= i < len(errors) else {}
    streak = err.get("max_streak")
    return (streak if isinstance(streak, int) else None), (str(err["last_success"]) if err.get("last_success") else None)


def _unavailable(step: str, res: dict[str, Any]) -> dict[str, str]:
    _log.warning("Investigation step unavailable | %s | %s", step, str(res.get("error") or "")[:300])
    out = {"step": step, "error": str(res.get("error") or "not available")}
    if res.get("hint"):
        out["hint"] = str(res["hint"])
    return out


def _failure(business_date: str, error: str, hint: str = "", started: float | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"ok": False, "error": error, "business_date": business_date}
    if hint:
        out["hint"] = hint
    if started is not None:
        out["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    return out


def _audit_principal() -> str:
    # The pipelines' own service account is the principal that matters: a
    # project-wide read is dominated by cluster bootstrap denials.
    return str(getattr(settings, "support_service_account", "") or "").strip()


def _gather(business: date, target: dict[str, Any], no_source_reason: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Every read of one bundle, in parallel → (evidence, unavailable)."""
    start, end = cob_window(business)
    day = cache.day(business)
    source, process, jobs = target["source"], target["process"], target["job_ids"]
    evidence: dict[str, Any] = {"metrics": None, "logs": None, "audit": None, "dataflow": [], "changes": None}
    unavailable: list[dict[str, str]] = []
    steps: dict[str, Callable[[], dict[str, Any]]] = {}

    metric_names: list[str] = []
    if not source:
        for step in ("metrics", "logs"):
            unavailable.append({"step": step, "error": no_source_reason})
    elif not _name_ok(source):
        for step in ("metrics", "logs"):
            unavailable.append({"step": step, "error": "the source must be a service name (letters, digits, '-', '_', '.')"})
    else:
        try:
            checks = metric_checks(source)
        except ValueError as e:
            checks = {}
            unavailable.append({"step": "metrics", "error": str(e)})
        # Every read goes through the shared read cache, on the SAME keys the
        # single-source chat tools use (support_tools.py) — so a follow-up
        # question after an investigation reuses these reads.
        for name, expr in checks.items():
            metric_names.append(name)
            steps[f"metric:{name}"] = lambda e=expr: cache.remembered(
                ("promql", e, day), lambda: monitoring.run_query(e, at=_iso(end)))
        steps["logs"] = lambda: cache.remembered(("logs", source, day, "ERROR"),
                                                 lambda: source_logs.read(source, start, end))
    principal = _audit_principal()
    steps["audit"] = lambda: cache.remembered(("audit", day, principal),
                                              lambda: source_logs.audit(start, end, principal=principal))
    for job_id in jobs:
        steps[f"dataflow:{job_id}"] = lambda j=job_id: cache.remembered(
            ("dataflow", j, ""), lambda: dataflow_job.inspect(j))
    names = list(dict.fromkeys(n for n in (source, process) if n))
    if names:
        steps["changes"] = lambda: release_lookup.what_changed(names, business, days=_CHANGE_DAYS)

    got = _run_steps(steps)
    if metric_names:
        metrics = assemble_metrics(source, start, end, {n: got[f"metric:{n}"] for n in metric_names})
        if metrics.get("ok"):
            evidence["metrics"] = metrics
        else:
            unavailable.append(_unavailable("metrics", metrics))
    for step in ("logs", "audit", "changes"):
        if step in got:
            if got[step].get("ok"):
                evidence[step] = got[step]
            else:
                unavailable.append(_unavailable(step, got[step]))
    for job_id in jobs:
        res = got[f"dataflow:{job_id}"]
        if res.get("ok"):
            evidence["dataflow"].append(res)
        else:
            unavailable.append(_unavailable(f"dataflow {job_id}", res))
    return evidence, unavailable


def _collect(business_date: str, incident_id: str, source: str, process: str, job_ids: list[str] | None) -> dict[str, Any]:
    started = time.monotonic()
    incident: dict[str, Any] | None = None
    report: dict[str, Any] = {}
    day_text = (business_date or "").strip()
    if incident_id:
        report = support_triage.triage(day_text)
        if not report.get("ok"):
            return _failure(day_text, str(report.get("error") or "the triage could not be read"),
                            str(report.get("hint") or ""), started)
        day_text = str(report.get("business_date") or day_text)
        incident = next((i for i in _list(report.get("incidents")) if i.get("id") == incident_id), None)
        if incident is None:
            return _failure(day_text, f"no incident {incident_id} on {day_text} — the list may have changed; "
                                      "reopen Support triage", started=started)
    if day_text:
        business = _to_date(day_text)
        if business is None:
            return _failure(day_text, f"{day_text!r} is not a date (use YYYY-MM-DD).", started=started)
    else:
        business = datetime.now(timezone.utc).date()
    target = _resolve_target(incident, source, process, job_ids)
    if incident is None and not (target["source"] or target["process"] or target["job_ids"]):
        return _failure(business.isoformat(), "give an incident id, or a source, process or job id to look at",
                        started=started)
    start, end = cob_window(business)
    if end <= start:
        return _failure(business.isoformat(), f"{business.isoformat()} has not started yet — nothing to investigate",
                        started=started)

    reason = "this incident's runs do not share one source" if incident else "no source was given"
    evidence, unavailable = _gather(business, target, reason)
    streak, last_success = _incident_facts(report, incident)
    signals = compute_signals(business=business, evidence=evidence, category=(incident or {}).get("category"),
                              streak=streak, last_success=last_success, audit_principal=_audit_principal())
    return {"ok": True, "business_date": business.isoformat(), "window": {"start": _iso(start), "end": _iso(end)},
            "incident": incident, "target": target, "evidence": evidence, "unavailable": unavailable,
            "timeline": build_timeline(evidence, last_success), "checked": _checked(incident, evidence, target),
            "signals": signals, "elapsed_ms": int((time.monotonic() - started) * 1000)}


# ----- shared cache, single flight ------------------------------------------------------

class _Flight:
    """One computation others wait on."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: dict[str, Any] | None = None


_cache: dict[tuple, tuple[float, float, dict[str, Any]]] = {}   # key -> (stored at, ttl, bundle)
_flights: dict[tuple, _Flight] = {}
_state_lock = threading.Lock()


def _cached(key: tuple) -> dict[str, Any] | None:
    hit = _cache.get(key)
    return hit[2] if hit and time.monotonic() - hit[0] < hit[1] else None


def _remember(key: tuple, bundle: dict[str, Any]) -> None:
    now = time.monotonic()
    ttl = _CACHE_TTL_PARTIAL if bundle.get("unavailable") else _CACHE_TTL
    for k in [k for k, (at, t, _) in _cache.items() if now - at >= t]:
        del _cache[k]
    while len(_cache) >= _CACHE_MAX:
        del _cache[min(_cache, key=lambda k: _cache[k][0])]
    _cache[key] = (now, ttl, bundle)


def _single_flight(key: tuple, compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """Ten people investigating one incident cost ONE collect: the first
    computes, the rest wait for that answer — a failed one too, so a failure
    is not retried ten times in a row. Only a successful bundle is remembered."""
    with _state_lock:
        hit = _cached(key)
        if hit is not None:
            return {**copy.deepcopy(hit), "cached": True}
        flight = _flights.get(key)
        leader = flight is None
        if leader:
            flight = _flights[key] = _Flight()
    assert flight is not None
    if not leader:
        flight.done.wait()
        if flight.value is not None:
            return copy.deepcopy(flight.value)
        return _single_flight(key, compute)   # the leader died without an answer
    try:
        flight.value = compute()
        if flight.value.get("ok"):
            with _state_lock:
                _remember(key, flight.value)
        return copy.deepcopy(flight.value)
    finally:
        with _state_lock:
            _flights.pop(key, None)
        flight.done.set()


def clear_cache() -> None:
    with _state_lock:
        _cache.clear()


def collect(business_date: str, incident_id: str = "", *, source: str = "", process: str = "",
            job_ids: list[str] | None = None) -> dict[str, Any]:
    """Everything the investigation of one incident needs, read in parallel.
    Never raises — see the module docstring for the bundle."""
    business_date = (business_date or "").strip()
    incident_id = (incident_id or "").strip()
    source, process = (source or "").strip(), (process or "").strip()
    jobs = [str(j).strip() for j in (job_ids or []) if str(j).strip()]
    key = ("incident", business_date, incident_id) if incident_id \
        else ("free", business_date, source, process, tuple(jobs))
    try:
        return _single_flight(key, lambda: _collect(business_date, incident_id, source, process, jobs))
    except Exception as e:  # noqa: BLE001 — the contract is "never raises"
        _log.exception("Investigation collect failed")
        return _failure(business_date, f"{type(e).__name__}: {str(e)[:200]}")


# ----- for the model --------------------------------------------------------------------

def _when_of(group: dict[str, Any], key: str) -> str | None:
    t = _parse_time(group.get(key))
    return _iso(t) if t else None


def for_model(bundle: dict[str, Any]) -> dict[str, Any]:
    """A pruned, bounded copy: capped groups, short samples, no raw payloads.
    The incident's note leaves the error text out unless SUPPORT_ERRORS_TO_MODEL
    allows it (as support_triage.for_model does)."""
    if not bundle.get("ok"):
        return {k: v for k, v in bundle.items() if k in ("ok", "error", "hint", "business_date")}
    ev = bundle.get("evidence") or {}
    inc = bundle.get("incident")
    out: dict[str, Any] = {"ok": True, "business_date": bundle.get("business_date"), "window": bundle.get("window")}
    if inc:
        note = inc.get("note") if settings.support_errors_to_model else inc.get("note_without_error") or inc.get("note")
        out["incident"] = {"id": inc.get("id"), "title": inc.get("title"), "category": inc.get("category"),
                           "priority": inc.get("priority"), "facts": inc.get("facts"), "action": inc.get("action"),
                           "steps": inc.get("steps"), "owner": inc.get("owner"), "runbook": inc.get("runbook"),
                           "note": note}
    else:
        out["incident"] = None
    out["evidence"] = {"metrics": None, "logs": None, "audit": None, "dataflow": [], "changes": None}
    if m := _ok(ev.get("metrics")):
        out["evidence"]["metrics"] = {"source": m.get("source"), "summary": m.get("summary"), "values": m.get("values")}
    if g := _ok(ev.get("logs")):
        groups = _list(g.get("groups"))
        out["evidence"]["logs"] = {
            "summary": g.get("summary"), "groups_total": len(groups),
            "groups": [{"signature": _line(x.get("signature"), 100), "count": x.get("count"),
                        "first": _when_of(x, "first_seen"), "last": _when_of(x, "last_seen"),
                        "security": bool(x.get("security")), "sample": _line(x.get("sample"), _M_SAMPLE)}
                       for x in groups[:_M_GROUPS]]}
    if a := _ok(ev.get("audit")):
        denials, iam = _list(a.get("denials")), _list(a.get("iam_changes"))
        out["evidence"]["audit"] = {
            "summary": a.get("summary"), "denials_total": a.get("denials_total"),
            "denials": [{"method": _line(x.get("method"), 80), "resource": _line(x.get("resource"), 100),
                         "count": x.get("count"), "first": _when_of(x, "first_seen"), "last": _when_of(x, "last_seen"),
                         "principals": _list(x.get("principals"))[:2]} for x in denials[:_M_GROUPS]],
            "iam_changes": [{"time": _when_of(x, "time"), "principal": _line(x.get("principal"), 80),
                             "resource": _line(x.get("resource"), 100)} for x in iam[:_M_GROUPS]]}
    for job in _list(ev.get("dataflow")):
        if _ok(job):
            out["evidence"]["dataflow"].append({
                "job_id": job.get("job_id"), "state": job.get("state"), "kind": job.get("kind"),
                "summary": _line(job.get("summary"), 300),
                "errors": [{"text": _line(e.get("text"), _M_SAMPLE), "time": _when_of(e, "time"), "count": e.get("count")}
                           for e in _list(job.get("errors"))[:_M_JOB_ERRORS]],
                "log_groups": [{"sample": _line(x.get("sample"), _M_SAMPLE), "count": x.get("count"),
                                "first": _when_of(x, "first_seen"), "last": _when_of(x, "last_seen")}
                               for x in _list(job.get("log_groups"))[:_M_JOB_GROUPS]]})
    if c := _ok(ev.get("changes")):
        out["evidence"]["changes"] = {
            "summary": c.get("summary"), "total": c.get("total_changes"),
            "changes": [{"artifact": x.get("artifact"), "environment": x.get("environment"),
                         "from": x.get("from_version"), "to": x.get("to_version"), "when": x.get("when"),
                         "jira": x.get("jira_ticket"), "release": x.get("release_name")}
                        for x in _list(c.get("changes"))[:_M_CHANGES]]}
    out["unavailable"] = [{k: _line(v, 200) for k, v in u.items()} for u in _list(bundle.get("unavailable"))]
    out["timeline"] = _list(bundle.get("timeline"))[:_TIMELINE_MAX]
    out["checked"] = [_line(x, 300) for x in _list(bundle.get("checked"))]
    out["signals"] = with_judgement(bundle.get("signals"))
    return _fit(out)


def _size(value: Any) -> int:
    return len(json.dumps(value, default=str, separators=(",", ":")).encode("utf-8"))


def _shrink_jobs(out: dict[str, Any]) -> None:
    for job in out["evidence"]["dataflow"]:
        job["log_groups"], job["errors"] = job["log_groups"][:2], job["errors"][:2]


def _shrink_audit(out: dict[str, Any]) -> None:
    # IAM changes and denials are on the timeline too; the groups keep the detail.
    audit = out["evidence"]["audit"]
    if audit:
        audit["denials"], audit["iam_changes"] = audit["denials"][:5], audit["iam_changes"][:4]


def _shrink_logs(out: dict[str, Any]) -> None:
    logs = out["evidence"]["logs"]
    if logs:
        logs["groups"] = [{**g, "sample": _line(g["sample"], 140), "signature": _line(g["signature"], 60)}
                          for g in logs["groups"][:5]]


def _shrink_changes(out: dict[str, Any]) -> None:
    changes = out["evidence"]["changes"]
    if changes:
        changes["changes"] = changes["changes"][:5]


def _shrink_timeline(out: dict[str, Any]) -> None:
    out["timeline"] = _capped(out["timeline"], 26, lambda e: (e["kind"], e["source"]))


def _fit(out: dict[str, Any]) -> dict[str, Any]:
    """Under about _M_BYTES: the caps above hold the common case; when a case
    is wide on every axis at once, take the detail the timeline and the signals
    already carry, one step at a time, until it fits."""
    for shrink in (_shrink_jobs, _shrink_audit, _shrink_logs, _shrink_changes, _shrink_timeline):
        if _size(out) <= _M_BYTES:
            break
        shrink(out)
    # Still over after every step ran once: the timeline is what is left to give.
    # Keep its start (what came first is what matters most) and its last two.
    for _ in range(6):
        timeline = _list(out.get("timeline"))
        if _size(out) <= _M_BYTES or len(timeline) <= 8:
            break
        keep = max(8, len(timeline) * 2 // 3)
        out["timeline"] = timeline[:keep - 2] + timeline[-2:]
    return out


# ----- with no model ----------------------------------------------------------------------

def _stamp(iso: str) -> str:
    t = _parse_time(iso)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else iso


def render_markdown(bundle: dict[str, Any]) -> str:
    """The answer with no model: what was checked, the timeline, the leads (as
    leads — rules, not conclusions) and what L1 does. Nothing here is a
    conclusion the evidence did not state."""
    if not bundle.get("ok"):
        hint = f"\n\n{bundle['hint']}" if bundle.get("hint") else ""
        return f"I could not gather the evidence: {bundle.get('error') or 'unknown error'}{hint}"
    lines = ["### What I checked", ""]
    lines += [f"- {c}" for c in _list(bundle.get("checked"))] or ["- Nothing could be read."]
    for u in _list(bundle.get("unavailable")):
        lines.append(f"- Could not read {u.get('step')}: {_line(u.get('error'), 160)}"
                     + (f" ({u['hint']})" if u.get("hint") else ""))
    lines += ["", "### Timeline", ""]
    timeline = _list(bundle.get("timeline"))
    lines += [f"- {_stamp(t['time'])} — {t['text']}" for t in timeline] or ["- No dated evidence was found."]
    judged = with_judgement(bundle.get("signals"))
    if judged.get("action_hint"):
        lines += ["", f"**The evidence suggests: {judged['action_hint']['action']}** — {judged['action_hint']['why']}."]
    elif not judged.get("conclusive"):
        lines += ["", "**The evidence does not establish a cause.** What was checked is above; "
                      "the triage's own action below stands."]
    lines += ["", "### Leads", "", "Where to look first — rules over the evidence above, not conclusions:", ""]
    leads = _list((bundle.get("signals") or {}).get("leads"))
    lines += [f"- {x}" for x in leads] or ["- The evidence read gives no lead."]
    inc = bundle.get("incident")
    if inc:
        hint = judged.get("action_hint")
        note = str(inc.get("note") or "")
        if hint and hint["action"] != inc.get("action"):
            # The evidence overrules the triage's generic steps (an out-of-memory
            # failure is not "re-trigger once") — in the answer AND in the note
            # the person will paste, or the ticket contradicts the finding.
            lines += ["", "### What L1 should do", "",
                      f"Do now: {hint['action']} — {hint['why']}. Owner: {inc.get('owner')}",
                      "", f"(The triage's general advice for this kind of incident was \"{inc.get('action')}\"; "
                          "the evidence above changes it.)"]
            note = "\n".join(f"- Next steps: {hint['action']} — {hint['why']}."
                             if line.startswith("- Next steps:") else line for line in note.splitlines())
        else:
            lines += ["", "### What L1 should do", "", f"Do now: {inc.get('action')} · Owner: {inc.get('owner')}", ""]
            lines += [f"{n}. {s}" for n, s in enumerate(_list(inc.get("steps")), 1)]
        if note:
            lines += ["", "Ticket note:", "", "```", note, "```"]
    return "\n".join(lines)


def model_size(bundle: dict[str, Any]) -> int:
    """Bytes of the model's copy as JSON (what a test and an operator watch)."""
    return _size(for_model(bundle))
