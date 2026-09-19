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


def remove_from_release(
    image_names: str, environment: str = "staging", deployment_repo: str = ""
) -> dict[str, Any]:
    """Unstage chart names from today's PRD release PR (environment='staging', the
    default) or remove them from a live environment ('uat' or 'prod' — only when the
    user explicitly names it). deployment_repo (owner/repo) targets a non-default
    deployment repo — pass it only when the user names one."""
    return _invoke_tool(
        "remove_from_release",
        {"image_names": image_names, "environment": environment, "deployment_repo": deployment_repo},
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
    remove_from_release,
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
]

