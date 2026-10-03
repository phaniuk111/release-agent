"""ADK-friendly function tools over the existing Release Copilot GitHub tools.

The existing repo tool layer is still the source of truth for GitHub reads and
mutations. These wrappers give ADK plain Python functions with typed signatures
and dictionary returns, which ADK can expose as Function Tools.
"""
from __future__ import annotations

import asyncio
import functools
from typing import Any



# The dispatch + JSON coercion live in the tool facade itself (gh_tools), so the
# queue gate and these wrappers share one implementation. Kept under the old
# private names: deploy.py imports _invoke_tool, and tests rebind it.
from release_agent.tools.gh_tools import (  # noqa: E402
    invoke as _invoke_tool,
)


def off_event_loop(fn):
    """The same tool, run on a worker thread when the chat agent calls it.

    ADK runs a sync tool directly on the event loop (its thread-pool option only
    applies to live/audio mode), and the whole app is one event loop: while one
    person's tool waits on GitHub — a run poll can take ~12s — every other
    person's chat stream stalls. ``functools.wraps`` keeps the name, docstring
    and signature ADK builds the tool declaration from; the session PAT is a
    contextvar, which ``to_thread`` carries across.
    """
    @functools.wraps(fn)
    async def run(*args, **kwargs):
        return await asyncio.to_thread(fn, *args, **kwargs)
    return run


def check_release_window() -> dict[str, Any]:
    """Read live UAT/PRD deployment state and today's PRD release window."""
    return _invoke_tool("check_release_window")


def list_allowed_images() -> dict[str, Any]:
    """List the allowed image/chart catalog from the configured build repo."""
    return _invoke_tool("list_allowed_images")


def get_recent_runs(limit: int = 5) -> dict[str, Any]:
    """List recent release-related GitHub Actions runs."""
    return _invoke_tool("get_recent_runs", {"limit": limit})


def get_workflow_status(run_id: str) -> dict[str, Any]:
    """Get status and summary information for a GitHub Actions run id."""
    return _invoke_tool("get_workflow_status", {"run_id": run_id})


def find_prs(search_term: str = "", limit: int = 5) -> dict[str, Any]:
    """Find deployment PRs matching an image, tag, ticket, branch, or text query."""
    return _invoke_tool("find_prs", {"search_term": search_term, "limit": limit})


def get_pr_details(pr_number: int) -> dict[str, Any]:
    """Read details for a deployment PR number."""
    return _invoke_tool("get_pr_details", {"pr_number": pr_number})


def get_pr_comments(pr_number: int, limit: int = 30) -> dict[str, Any]:
    """Read recent comments from a deployment PR."""
    return _invoke_tool("get_pr_comments", {"pr_number": pr_number, "limit": limit})


def _build_repo_for(repo: str, dataflow: bool) -> str:
    """Explicit repo wins; else Dataflow images resolve to DF_BUILD_REPO (their
    builds live in a separate repo from the GKE services'); else config default."""
    if repo:
        return repo
    if dataflow:
        from release_agent.config import settings

        return settings.df_build_repo
    return ""


def get_build_report(
    image: str = "", tag: str = "", workflow_url: str = "", repo: str = "",
    run_id: int = 0, dataflow: bool = False,
) -> dict[str, Any]:
    """Full build diagnosis for an image:tag, a GitHub Actions run URL, or a bare
    run id: which STEPS failed, which controls (RCTLDEF…/RLFT) passed or
    failed (gate verdict), and whether the tag was built from the default
    branch. Use when a developer asks WHAT failed in their build/run and what
    to fix. A workflow_url carries its own repo; for image+tag or run_id
    lookups of Dataflow images set dataflow=true (they build in DF_BUILD_REPO)."""
    return _invoke_tool(
        "get_build_report",
        {"image": image, "tag": tag, "workflow_url": workflow_url,
         "repo": _build_repo_for(repo, dataflow), "run_id": run_id},
    )


def promote_release(
    target: str, release_branch: str = "", deployment_repo: str = ""
) -> dict[str, Any]:
    """Promote the current CARE release's file-set to the next environment branch
    (uat, prd or prl1). Copies the release's changed files verbatim via a
    change-branch PR — use after a release has been created. deployment_repo
    (owner/repo) targets a non-default deployment repo — pass it only when the
    user names one. For the Dataflow (DF) release use promote_df_release."""
    return _invoke_tool(
        "promote_release",
        {"target": target, "release_branch": release_branch,
         "deployment_repo": deployment_repo, "kind": "care"},
    )


def promote_df_release(target: str, release_branch: str = "") -> dict[str, Any]:
    """Promote the current DATAFLOW (DF) release's file-set along the DF release
    repo's own branch chain (DF_RELEASE_BRANCHES, e.g. RELEASE_UAT -> RELEASE_PRD):
    a DF release lands on its UAT branch directly, so the target is normally prd.
    Use for 'promote the DF release to prd' / 'promote the dataflow release'.
    For the CARE release use promote_release."""
    # A tool of its own rather than a flag on promote_release: found live, the
    # model sent "promote the DF release" to the CARE release when the kind was
    # only an optional argument. A tool name is a signal it does not skip.
    return _invoke_tool(
        "promote_release",
        {"target": target, "release_branch": release_branch, "deployment_repo": "", "kind": "df"},
    )


def queue_release_intent(
    artifact: str,
    requested_by: str,
    prl1_only: bool = False,
    df_only: bool = False,
    note: str = "",
    jira_ticket: str = "",
    change_details: str = "",
    build_run_url: str = "",
    target_envs: str = "",
) -> dict[str, Any]:
    """Queue a chart:version for the next release, after checking it is eligible.

    Requires the GitHub Actions run URL that built exactly this chart:version, and
    a JIRA ticket. Refuses when a release control failed on that run. The rules
    live in release_agent.tools.queue_gate so the queue FORM applies exactly the
    same ones — this is the model's door to them, not a second implementation.
    """
    from release_agent.tools.queue_gate import queue_release_intent as _gate

    return _gate(
        artifact=artifact, requested_by=requested_by, prl1_only=prl1_only,
        df_only=df_only, note=note, jira_ticket=jira_ticket,
        change_details=change_details, build_run_url=build_run_url,
        target_envs=target_envs,
    )


def withdraw_release_intent(artifact_name: str, requested_by: str = "") -> dict[str, Any]:
    """Withdraw a chart from the next-release intake queue (e.g. 'remove my
    risk-fetcher from the queue'). Only touches the queue — never a live
    environment or an open release. The signed-in user, when known, is who
    the removal is recorded against."""
    from release_agent import identity
    from release_agent.tools import release_queue as _rq

    requested_by, refused = identity.actor(requested_by, identity.current())
    if refused:
        return {"ok": False, "error": refused}
    return _rq.withdraw_intent(artifact_name, requested_by)


def monitoring_checks() -> dict[str, Any]:
    """Run the monitoring checks NOW (PromQL expressions that return series
    only when something is wrong): Composer DAG runs, Dataflow jobs, GKE
    restarts, targets down, Google API 5xx, plus the team's own. Each comes back
    with state firing / ok / unknown / no_data (nothing to measure in this
    project — NOT healthy), the firing series (labels + value, capped),
    `watching` (how much an ok check measured) and, for unknown, the error and a
    likely fix. Read-only."""
    from release_agent import features, identity
    from release_agent.tools import monitoring as _mon

    if not features.allowed("monitoring", identity.current()):
        return {"ok": False, "error": features.refusal("monitoring")}
    return _mon.run_checks()


def query_metrics(promql: str) -> dict[str, Any]:
    """Run ONE read-only PromQL instant query against the team's Prometheus
    (Managed Service for Prometheus, which also exposes Cloud Monitoring's GCP
    metrics as e.g. serviceruntime_googleapis_com:api_request_count). Returns
    at most 20 series ({labels, value}) plus the true count and whether it was
    truncated. Use aggregations (sum by, topk, count) to keep answers small."""
    from release_agent import features, identity
    from release_agent.tools import monitoring as _mon

    if not features.allowed("monitoring", identity.current()):
        return {"ok": False, "error": features.refusal("monitoring")}
    return _mon.run_query(promql)


def list_release_queue() -> dict[str, Any]:
    """List everything queued for the NEXT release: chart, version, requester,
    when, PRL1-only/DF flags, note, and whether the build was verified at queue
    time. This is what DevOps reviews before creating the release."""
    from release_agent.tools import release_queue as _rq

    return _rq.current_queue()


def release_stats(pattern: str = "", days: int = 90, event_type: str = "released") -> dict[str, Any]:
    """Stats over the release/deployment history event log. Answers questions
    like 'which images were released this month?' or 'how many acme-capability*
    images were released?'. pattern: empty = all charts; supports * globs
    (acme-capability*) or a plain substring (capability). event_type:
    'released' (shipped in a release), 'deployed' (UAT/PRD/PRL1/DF deploys and
    promotions), 'queued', 'all' — or 'state' for the CURRENT per-environment
    deployed picture ('how many capability images are on uat/prd/prl1?'):
    latest deployed/removed event per artifact per environment wins, returning
    each environment's distinct image count and list."""
    from release_agent.tools import release_queue as _rq

    return _rq.history_stats(pattern=pattern, days=days, event_type=event_type)


def bq_cost_scan(days: int = 0, top: int = 0) -> dict[str, Any]:
    """The ranked BigQuery cost report for the team's dedicated project: top
    query SHAPES — grouped, so 96 runs of one query are one row — ranked by
    slot-hours or bytes billed, plus storage findings (no expiry, unread, large
    unpartitioned tables) and write findings (unbatched writes, ingestion
    errors). Every number is MEASURED from INFORMATION_SCHEMA, never
    estimated, and the report states what running the scan itself cost.
    days/top: 0 uses the configured default (BQ_COST_DAYS / BQ_COST_TOP)."""
    from release_agent import features, identity

    if not features.allowed("bq-cost", identity.current()):
        return {"ok": False, "error": features.refusal("bq-cost")}
    from release_agent.tools import bq_cost

    return bq_cost.scan(days or None, top or None)


def bq_query_detail(qhash: str) -> dict[str, Any]:
    """One query shape in depth, from a bq_cost_scan row's qhash: the full
    sample SQL, run count, byte and slot-ms percentiles, execution stages,
    BigQuery's own performance insights, and the layout of every table it
    references. Call this before proposing a rewrite for a top-ranked shape."""
    from release_agent import features, identity

    if not features.allowed("bq-cost", identity.current()):
        return {"ok": False, "error": features.refusal("bq-cost")}
    from release_agent.tools import bq_cost

    return bq_cost.query_detail(qhash)


def bq_table_layout(table: str) -> dict[str, Any]:
    """A table's layout: partitioning, clustering, size, physical size, row
    count, expiry and last-modified time. Use to see whether a costly query's
    filter column is already the partition column, or whether the table is
    clustered."""
    from release_agent import features, identity

    if not features.allowed("bq-cost", identity.current()):
        return {"ok": False, "error": features.refusal("bq-cost")}
    from release_agent.tools import bq_cost

    return bq_cost.table_layout(table)


def bq_prune_estimate(table: str, column: str) -> dict[str, Any]:
    """ESTIMATED, never measured: what partitioning or clustering ``table`` on
    ``column`` would have pruned for the runs seen in the scan window. Table
    changes cannot be dry-run and Google's recommender is not available here,
    so this is inferred from INFORMATION_SCHEMA access patterns alone —
    always report it as an estimate, never as a measured saving."""
    from release_agent import features, identity

    if not features.allowed("bq-cost", identity.current()):
        return {"ok": False, "error": features.refusal("bq-cost")}
    from release_agent.tools import bq_cost

    return bq_cost.prune_estimate(table, column)


def bq_dry_run(sql: str) -> dict[str, Any]:
    """Dry-run ``sql`` — this NEVER executes the query. Returns the bytes it
    would process, the result schema, and the tables it references. Use to
    price a candidate rewrite, or to inspect any query with no shape yet."""
    from release_agent import features, identity

    if not features.allowed("bq-cost", identity.current()):
        return {"ok": False, "error": features.refusal("bq-cost")}
    from release_agent.tools import bq_cost

    return bq_cost.dry_run(sql)


def bq_verify_rewrite(
    before_sql: str, after_sql: str, declared_schema_change: bool = False
) -> dict[str, Any]:
    """The test a proposed rewrite MUST pass before it is shown as advice: two
    dry runs and a verdict. Accepted only if the rewrite is cheaper, references
    no table the original did not, and keeps an identical result schema —
    unless declared_schema_change=True for a deliberate, disclosed change (e.g.
    SELECT * to named columns). Never proves row-level equivalence: say so
    whenever reporting an accepted rewrite."""
    from release_agent import features, identity

    if not features.allowed("bq-cost", identity.current()):
        return {"ok": False, "error": features.refusal("bq-cost")}
    from release_agent.tools import bq_cost

    return bq_cost.verify_rewrite(
        before_sql, after_sql, declared_schema_change=declared_schema_change
    )


def bq_findings(qhash: str = "") -> dict[str, Any]:
    """What was suggested in earlier cost-scan runs and whether the cost
    actually fell afterwards: adopted (cost dropped and stayed down), still
    open (unchanged after repeated reports), or new. Empty qhash returns
    recent history across every shape."""
    from release_agent import features, identity

    if not features.allowed("bq-cost", identity.current()):
        return {"ok": False, "error": features.refusal("bq-cost")}
    from release_agent.tools import bq_cost

    return bq_cost.findings(qhash or None)

def support_triage(business_date: str = "") -> dict[str, Any]:
    """First-line triage of the workflow control table for one business date
    (YYYY-MM-DD; empty = the latest date in the table): how many runs failed,
    are stuck, or ran on the previous date but not this one; the distinct
    errors (count, category upstream/process/unit/single/spread, which values
    they share, how many are recurring vs new); patterns where one value carries
    most failures; per failure the dates failing in a row and the last success;
    retries; output volume that collapsed on a successful run. Read-only."""
    from release_agent import features, identity
    from release_agent.tools import support_triage as _st

    if not features.allowed("support-triage", identity.current()):
        return {"ok": False, "error": features.refusal("support-triage")}
    return _st.for_model(_st.triage(business_date))


# ----- support-investigate: read-only evidence about ONE incident -----------------
# Each tool names the access it lacks instead of raising, so the investigation
# routine (skills/support-investigate) can carry on with the steps it can take.

# Evidence reads are shared across people: ten engineers investigating the same
# incident should cost one read of the logs, one of the Dataflow job, not ten
# — and Cloud Logging allows about 60 reads a minute per project. Keyed by the
# tool's arguments; refreshed after _EVIDENCE_TTL seconds; bounded.
_log = __import__("logging").getLogger(__name__)
_EVIDENCE_TTL = 600.0
_EVIDENCE_MAX = 256
_evidence: dict[tuple, tuple[float, dict]] = {}
_evidence_lock = __import__("threading").Lock()


def _remembered(key: tuple, compute):
    import time

    with _evidence_lock:
        hit = _evidence.get(key)
        if hit and time.time() - hit[0] < _EVIDENCE_TTL:
            return hit[1]
    value = compute()
    if not value.get("ok"):
        # The model only sees the dict; the operator needs the reason in the log.
        _log.warning("Evidence read failed | %s | %s", "/".join(str(k) for k in key[:2]),
                     str(value.get("error") or "")[:300])
    if value.get("ok"):   # a refusal or an outage is retried, never remembered
        with _evidence_lock:
            if len(_evidence) >= _EVIDENCE_MAX:
                _evidence.clear()
            _evidence[key] = (time.time(), value)
    return value


def _support_gate():
    from release_agent import features, identity

    if not features.allowed("support-triage", identity.current()):
        return {"ok": False, "error": features.refusal("support-triage")}
    return None


def _window(business_date: str):
    """The business date's overnight window as datetimes (investigation.cob_window
    — one definition for these tools and the collector); no date = the last 36
    hours."""
    from datetime import datetime, timedelta, timezone

    from release_agent.tools import investigation
    from release_agent.tools.support_triage import _to_date

    d = _to_date(business_date.strip()) if business_date.strip() else None
    if d is not None:
        return investigation.cob_window(d)
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
    from release_agent.tools import investigation
    from release_agent.tools import monitoring as _mon

    source = (source or "").strip()
    if not source or not all(c.isalnum() or c in "-_." for c in source):
        return {"ok": False, "error": "source must be a service name (letters, digits, '-', '_', '.')."}
    start, end = _window(business_date)
    at = end.isoformat()
    try:
        checks = investigation.metric_checks(source)
    except ValueError as e:
        return {"ok": False, "error": str(e)}
    results = {name: _remembered(("promql", expr, at), lambda e=expr: _mon.run_query(e, at=at))
               for name, expr in checks.items()}
    return investigation.assemble_metrics(source, start, end, results)


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

    from release_agent.tools import source_logs as _logs

    start, end = _cob_window(business_date)
    return _remembered(("logs", source, start, end, min_severity or "ERROR"),
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
    from release_agent.tools import source_logs as _logs

    start, end = _cob_window(business_date)
    # The pipelines' own service account is the principal that matters by
    # default: a 30-day project-wide read is dominated by cluster bootstrap noise.
    who = principal or getattr(settings, "support_service_account", "") or ""
    return _remembered(("audit", start, end, who),
                       lambda: _logs.audit(datetime.fromisoformat(start), datetime.fromisoformat(end), principal=who))


def dataflow_job(job_id: str, region: str = "") -> dict[str, Any]:
    """What ONE Dataflow job did: state, timings, the job-level error messages,
    the worker error groups from its logs, and a `kind` decided by code
    (out_of_memory / quota / permission / schema / not_found / worker_lost /
    unknown). region empty = SUPPORT_DATAFLOW_REGION. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from release_agent.tools import dataflow_job as _df

    return _remembered(("dataflow", job_id, region or ""), lambda: _df.inspect(job_id, region=region or ""))


def release_lookup(name: str, days: int = 14, as_of: str = "") -> dict[str, Any]:
    """What is deployed where for a chart/image name (exact, else substring
    match), and its changes in the last `days` days up to `as_of`
    (YYYY-MM-DD, empty = today), from the release event log. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from release_agent.tools import release_lookup as _rl
    from release_agent.tools.support_triage import _to_date

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

    from release_agent.tools import release_lookup as _rl
    from release_agent.tools.support_triage import _to_date

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
    tools are for a targeted follow-up only. Read-only."""
    refused = _support_gate()
    if refused:
        return refused
    from release_agent.tools import investigation

    jobs = [str(j) for j in (job_ids or [])][:10]
    if not any(((incident_id or "").strip(), (source or "").strip(), (process or "").strip(), jobs)):
        # Nothing to look at: reading a whole day's logs for no one is noise.
        return {"ok": False, "error": "No incident, source, process or job was named.",
                "hint": "Call support_triage for the business date to find the incident, "
                        "then call investigate_evidence with its id."}
    bundle = investigation.collect(
        (business_date or "").strip(), (incident_id or "").strip(),
        source=(source or "").strip(), process=(process or "").strip(),
        job_ids=jobs or None,
    )
    return investigation.for_model(bundle)


# Every tool the free-form chat agent can call. The per-domain grouping a skill
# actually surfaces (status/PR/controls/ops/queue/monitoring) is declared in
# that skill's own SKILL.md frontmatter (adk_additional_tools) — this flat list
# is only the full universe those frontmatter lists are checked against.
ADK_CHAT_TOOLS = [
    check_release_window,
    list_allowed_images,
    get_recent_runs,
    get_workflow_status,
    find_prs,
    get_pr_details,
    get_pr_comments,
    get_build_report,
    promote_release,
    promote_df_release,
    queue_release_intent,
    withdraw_release_intent,
    list_release_queue,
    release_stats,
    monitoring_checks,
    query_metrics,
    bq_cost_scan,
    bq_query_detail,
    bq_table_layout,
    bq_prune_estimate,
    bq_dry_run,
    bq_verify_rewrite,
    bq_findings,
    support_triage,
    investigate_evidence,
    source_metrics,
    source_logs,
    source_audit,
    dataflow_job,
    release_lookup,
    what_changed,
]

