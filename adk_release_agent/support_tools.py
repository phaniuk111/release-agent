"""The Support chat tools: L1 triage, and the evidence about ONE incident.

The free-form chat agent reaches them through the support-triage and
support-investigate skills; adk_release_agent/tools.py lists them in
ADK_CHAT_TOOLS with the rest. All read-only, all behind the `support-triage`
preview feature. The docstrings are what the model reads to choose a tool.
"""
from __future__ import annotations

import pathlib
from typing import Any

_PRIORITY_SKILL = pathlib.Path(__file__).parent / "skills" / "support-priority" / "SKILL.md"


def priority_policy() -> str:
    """The support-priority skill's text: the team's plain-English priority
    policy and how to apply it."""
    from release_agent.tools.support.runbook import skill_body

    return skill_body(_PRIORITY_SKILL)


def team_guidance() -> dict[str, str]:
    """What the team wrote for the AI, as skills in the image: the runbook (how
    to read the control table, each known issue) and the priority policy. ONE
    copy of each; the chat gets them inside every support_triage /
    investigate_evidence result (so "Ask why" never depends on the model
    choosing to load a skill) and the Investigate finding step in its
    instruction. Read on every call."""
    from release_agent.tools.support import runbook

    out = {"runbook": runbook.text(), "priority_policy": priority_policy()}
    return {k: v for k, v in out.items() if v}


def support_triage(business_date: str = "") -> dict[str, Any]:
    """First-line triage of the workflow control table for one business date
    (YYYY-MM-DD; empty = the latest date in the table): how many runs failed,
    are stuck, or ran on the previous date but not this one; the distinct
    errors (count, category upstream/process/unit/single/spread, which values
    they share, how many are recurring vs new); patterns where one value carries
    most failures; per failure the dates failing in a row and the last success;
    retries; output volume that collapsed on a successful run; and the team's
    own words: `runbook` (how to read the table, each known issue — how to
    recognise it and what to do) and `priority_policy` (how urgent each incident
    is). Explain and judge every incident by those. Read-only."""
    from release_agent import features, identity
    from release_agent.tools.support import triage as _st

    if not features.allowed("support-triage", identity.current()):
        return {"ok": False, "error": features.refusal("support-triage")}
    out = _st.for_model(_st.triage(business_date))
    if out.get("ok"):
        # the team's runbook and priority policy: how to read and judge the result
        out.update(team_guidance())
    return out


# ----- support-investigate: read-only evidence about ONE incident -----------------
# Each tool names the access it lacks instead of raising, so the investigation
# routine (skills/support-investigate) can carry on with the steps it can take.

# Evidence reads go through the ONE shared read cache (tools/support/cache.py):
# the Investigate collector and these tools read the same things, so a follow-up
# question after an investigation reuses its reads.
from release_agent.tools.support import cache as _cache  # noqa: E402


def _support_gate():
    from release_agent import features, identity

    if not features.allowed("support-triage", identity.current()):
        return {"ok": False, "error": features.refusal("support-triage")}
    return None


def _window(business_date: str):
    """The business date's overnight window as datetimes (evidence.cob_window
    — one definition for these tools and the collector); no date = the last 36
    hours."""
    from datetime import datetime, timedelta, timezone

    from release_agent.tools.support import evidence
    from release_agent.tools.support.report import _to_date

    d = _to_date(business_date.strip()) if business_date.strip() else None
    if d is not None:
        return evidence.cob_window(d)
    end = datetime.now(timezone.utc).replace(microsecond=0)
    return end - timedelta(hours=36), end


def _cob_window(business_date: str) -> tuple[str, str]:
    """:func:`_window` as ISO strings, for the log and audit readers."""
    start, end = _window(business_date)
    return start.isoformat(), end.isoformat()


def source_metrics(source: str, business_date: str = "") -> dict[str, Any]:
    """The standard health checks of a SOURCE service (the image/workload that
    produced a pipeline's data) over a business date's window, via PromQL:
    restarts, whether it was up, its error rate — each as the configured
    expression (SUPPORT_SOURCE_PROMQL) evaluated at the window's end. A healthy
    source rules the source out; an unhealthy one is the lead. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from release_agent.tools.support import evidence
    from release_agent.tools import monitoring as _mon

    source = (source or "").strip()
    if not source or not all(c.isalnum() or c in "-_." for c in source):
        return {"ok": False, "error": "source must be a service name (letters, digits, '-', '_', '.')."}
    start, end = _window(business_date)
    at = end.isoformat()
    try:
        checks = evidence.metric_checks(source)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    day = _cache.day(business_date)
    results = {name: _cache.remembered(("promql", expr, day), lambda e=expr: _mon.run_query(e, at=at))
               for name, expr in checks.items()}
    return evidence.assemble_metrics(source, start, end, results)


def source_logs(source: str, business_date: str = "", min_severity: str = "ERROR") -> dict[str, Any]:
    """What a SOURCE service logged in a business date's window, grouped: each
    distinct error with its count, first/last time and a sample line, and
    whether it is a SECURITY line (permission, 403, token, certificate —
    decided by code, not by you). Read-only; needs SUPPORT_CLUSTER and
    SUPPORT_NAMESPACE."""
    refused = _support_gate()
    if refused:
        return refused
    from datetime import datetime

    from release_agent.tools.support import source_logs as _logs

    start, end = _cob_window(business_date)
    return _cache.remembered(("logs", source, _cache.day(business_date), min_severity or "ERROR"),
                       lambda: _logs.read(source, datetime.fromisoformat(start), datetime.fromisoformat(end),
                                          min_severity=min_severity or "ERROR"))


def source_audit(business_date: str = "", principal: str = "") -> dict[str, Any]:
    """Cloud Audit Logs for a business date's window: what was PERMISSION_DENIED
    (grouped by API method and resource, optionally for one service account)
    and any IAM policy change — the evidence behind a security error in the
    source's logs. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from datetime import datetime

    from release_agent.config import settings
    from release_agent.tools.support import source_logs as _logs

    start, end = _cob_window(business_date)
    # The pipelines' own service account is the principal that matters by
    # default: a 30-day project-wide read is dominated by cluster bootstrap noise.
    who = principal or getattr(settings, "support_service_account", "") or ""
    return _cache.remembered(("audit", _cache.day(business_date), who),
                       lambda: _logs.audit(datetime.fromisoformat(start), datetime.fromisoformat(end), principal=who))


def dataflow_job(job_id: str, region: str = "") -> dict[str, Any]:
    """What ONE Dataflow job did: state, timings, the job-level error messages,
    the worker error groups from its logs, and a `kind` decided by code
    (out_of_memory / quota / permission / schema / not_found / worker_lost /
    unknown). region empty = SUPPORT_DATAFLOW_REGION. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from release_agent.tools.support import dataflow_job as _df

    return _cache.remembered(("dataflow", job_id, region or ""), lambda: _df.inspect(job_id, region=region or ""))


def run_logs(run_id: str, written_at: str) -> dict[str, Any]:
    """Every log line, in any component and at any severity, that names ONE run
    by its run id (the control table's run id), from a few minutes before the
    run's row was written (`written_at`, the row's timestamp as ISO 8601 —
    take it from the triage or the investigation, never guess it) to just
    after: the run's own story, its first error and which components logged
    it. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from release_agent.tools.support import source_logs as _logs
    from release_agent.tools.support._common import _iso
    from release_agent.tools.support.dataflow_job import _when

    at = _when(written_at)
    if at is None:
        return {"ok": False, "error": f"{written_at!r} is not a timestamp (ISO 8601, e.g. 2026-10-03T03:34:16Z)."}
    return _cache.remembered(("runlogs", (run_id or "").strip(), _iso(at)),
                             lambda: _logs.for_run((run_id or "").strip(), at))


def release_lookup(name: str, days: int = 14, as_of: str = "") -> dict[str, Any]:
    """What is deployed where for a chart/image name (exact, else substring
    match), and its changes in the last `days` days up to `as_of`
    (YYYY-MM-DD, empty = today), from the release event log. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from release_agent.tools.support import release_lookup as _rl
    from release_agent.tools.support.report import _to_date

    return _rl.lookup(name, days=int(days or 14), as_of=_to_date(as_of) if as_of.strip() else None)


def what_changed(names: list[str], before: str, days: int = 7) -> dict[str, Any]:
    """Every release-log change to these chart/image names (a source, a
    report's chart) in the `days` before a business date (YYYY-MM-DD), newest
    first — a version that moved just before the first failure is the
    strongest cause of a process failure. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from datetime import date

    from release_agent.tools.support import release_lookup as _rl
    from release_agent.tools.support.report import _to_date

    when = _to_date(before.strip()) if before.strip() else None
    return _rl.what_changed(list(names or [])[:10], when or date.today(), days=int(days or 7))


def investigate_evidence(
    business_date: str = "", incident_id: str = "", source: str = "", process: str = "",
    job_ids: list[str] | None = None,
) -> dict[str, Any]:
    """ALL the evidence about ONE incident in a single call: the incident's
    triage facts, its source's metrics, logs and audit denials, its Dataflow
    jobs, and what was released just before — read in parallel, with a timeline,
    the leads code found, and every step it could not take (with what it
    needed). Give the incident_id and business_date (YYYY-MM-DD) from the
    triage. With no incident id, name what the person named: a report/process
    (e.g. REPORT-B) goes in `process`, a fetcher or feed service (e.g.
    sys-b-fetcher) in `source`, and job_ids if known — a call with none of
    these has nothing to look at, so find the incident with `support_triage`
    first. One call is the whole routine — answer from it; the single-source
    tools are for a targeted follow-up only. `runbook` and `priority_policy`
    are the team's own words: which known issue it is (or none) and how
    urgent — judge by them. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from release_agent.tools.support import evidence

    jobs = [str(j) for j in (job_ids or [])][:10]
    if not any(((incident_id or "").strip(), (source or "").strip(), (process or "").strip(), jobs)):
        # Nothing to look at: reading a whole day's logs for no one is noise.
        return {"ok": False, "error": "No incident, source, process or job was named.",
                "hint": "Call support_triage for the business date to find the incident, "
                        "then call investigate_evidence with its id."}
    bundle = evidence.collect(
        (business_date or "").strip(), (incident_id or "").strip(),
        source=(source or "").strip(), process=(process or "").strip(),
        job_ids=jobs or None,
    )
    out = evidence.for_model(bundle)
    if out.get("ok"):
        out.update(team_guidance())
    return out
