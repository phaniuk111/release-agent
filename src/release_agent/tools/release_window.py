"""Deployment status: what's on UAT vs PRD (uat/deployment.json vs prd/deployment.json)."""

from ._common import (
    settings,
    tool,
    json,
    _get_github_client,
    _read_json_file,
)


def _charts(repo, env: str) -> dict:
    """Read {helm_chart_name: helm_chart_version} from an env's deployment.json include[]."""
    path = settings.deployment_path_pattern.format(env=env)
    doc = _read_json_file(repo, settings.uat_branch if env == "uat" else settings.prd_branch, path)
    include = doc.get("include") if isinstance(doc, dict) else None
    out = {}
    if isinstance(include, list):
        for e in include:
            if isinstance(e, dict) and e.get("helm_chart_name"):
                out[e["helm_chart_name"]] = e.get("helm_chart_version")
    return out


def get_release_status() -> dict:
    """Current deploy status: charts on UAT vs PRD, and what's pending (on UAT but not
    yet matching PRD). GitHub is the cross-session source of truth, so any session sees
    the same answer."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    base = {"date_utc": now.date().isoformat(), "now_utc": now.strftime("%H:%M")}
    try:
        repo = _get_github_client().get_repo(settings.deploy_repo)
        uat = _charts(repo, "uat")
        prd = _charts(repo, "prd")
    except Exception as e:
        return {**base, "error": str(e), "uat_charts": [], "prd_charts": [], "pending": [], "in_sync": True}

    # Pending = a chart whose UAT version differs from PRD (or isn't in PRD yet).
    pending = [
        {"helm_chart_name": n, "uat_version": v, "prd_version": prd.get(n)}
        for n, v in uat.items()
        if prd.get(n) != v
    ]
    in_sync = not pending
    if in_sync:
        reason = "UAT matches PRD — nothing pending." if uat else "No charts deployed yet."
    else:
        reason = f"{len(pending)} chart(s) on UAT not yet in PRD: " + ", ".join(
            f"{p['helm_chart_name']}:{p['uat_version']}" for p in pending
        )
    return {
        **base,
        "uat_charts": [{"helm_chart_name": n, "helm_chart_version": v} for n, v in uat.items()],
        "prd_charts": [{"helm_chart_name": n, "helm_chart_version": v} for n, v in prd.items()],
        "pending": pending,
        "in_sync": in_sync,
        "reason": reason,
    }


@tool
def check_release_window() -> str:
    """Report current deploy status (UTC): which Helm charts/versions are on UAT vs PRD
    and which are pending (on UAT but not yet in PRD). Shared across sessions via GitHub."""
    return json.dumps(get_release_status(), indent=2)
