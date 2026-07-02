"""Deploy status: what's on UAT vs PRD, plus today's accumulating PRD release PR."""

import itertools

from ._common import (
    settings,
    tool,
    json,
    _get_github_client,
    _read_json_file,
)


def _charts_on_branch(repo, branch: str, env: str) -> dict:
    """{helm_chart_name: helm_chart_version} from an env's deployment.json include[],
    read on a specific branch."""
    path = settings.deployment_path_pattern.format(env=env)
    doc = _read_json_file(repo, branch, path)
    include = doc.get("include") if isinstance(doc, dict) else None
    out = {}
    if isinstance(include, list):
        for e in include:
            if isinstance(e, dict) and e.get("helm_chart_name"):
                out[e["helm_chart_name"]] = e.get("helm_chart_version")
    return out


def _charts(repo, env: str) -> dict:
    return _charts_on_branch(repo, settings.uat_branch if env == "uat" else settings.prd_branch, env)


def _prd_release_branch() -> str:
    """Deterministic per-day branch name so every prod deploy finds the same release PR."""
    from datetime import datetime, timezone

    return f"release/prd/{datetime.now(timezone.utc).date().isoformat()}"


def _today_prd_pr(repo):
    """Today's open PRD release PR (head = release/prd/<date>, base = PRD), or None."""
    branch = _prd_release_branch()
    try:
        for pr in itertools.islice(
            repo.get_pulls(state="open", base=settings.prd_branch, sort="created", direction="desc"), 30
        ):
            if pr.head.ref == branch:
                return pr
    except Exception:
        pass
    return None


def get_release_status() -> dict:
    """Current deploy status (UTC): charts live on UAT and PRD, today's accumulating PRD
    release PR (the charts staged for prod, merged at the cutoff), and the cutoff itself.
    GitHub is the cross-session source of truth, so every session sees the same answer."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    cutoff = settings.prd_cutoff_hour_utc
    cutoff_passed = now.hour >= cutoff
    base = {
        "date_utc": now.date().isoformat(),
        "now_utc": now.strftime("%H:%M"),
        "cutoff_utc": f"{cutoff:02d}:00",
        "cutoff_passed": cutoff_passed,
    }
    try:
        repo = _get_github_client().get_repo(settings.deploy_repo)
        uat = _charts(repo, "uat")
        prd = _charts(repo, "prd")
        pr = _today_prd_pr(repo)
    except Exception as e:
        return {
            **base, "error": str(e), "uat_charts": [], "prd_charts": [],
            "prd_release_pr": None, "pending_to_prod": [], "reason": f"status unavailable: {e}",
        }

    prd_release_pr = None
    pending_to_prod = []
    if pr is not None:
        staged = _charts_on_branch(repo, pr.head.ref, "prd")  # prd/deployment.json on the PR branch
        pending_to_prod = [
            {"helm_chart_name": n, "release_version": v, "prd_version": prd.get(n)}
            for n, v in staged.items()
            if prd.get(n) != v
        ]
        prd_release_pr = {
            "number": pr.number,
            "url": pr.html_url,
            "charts": [{"helm_chart_name": n, "helm_chart_version": v} for n, v in staged.items()],
            "can_merge_now": cutoff_passed,
        }

    if prd_release_pr:
        if cutoff_passed:
            reason = (
                f"PRD release PR #{prd_release_pr['number']} ({len(pending_to_prod)} change(s)) can be "
                f"released now — cutoff {cutoff:02d}:00 UTC has passed. Say 'release prod' to promote it "
                f"through {settings.sit_branch}→{settings.uat_branch}→{settings.prd_branch}."
            )
        else:
            reason = (
                f"PRD release PR #{prd_release_pr['number']} is collecting {len(pending_to_prod)} "
                f"change(s); after {cutoff:02d}:00 UTC it promotes to PRD through "
                f"{settings.sit_branch}→{settings.uat_branch}→{settings.prd_branch}."
            )
    else:
        reason = "No PRD release open today."

    return {
        **base,
        "uat_charts": [{"helm_chart_name": n, "helm_chart_version": v} for n, v in uat.items()],
        "prd_charts": [{"helm_chart_name": n, "helm_chart_version": v} for n, v in prd.items()],
        "prd_release_pr": prd_release_pr,
        "pending_to_prod": pending_to_prod,
        "reason": reason,
    }


@tool
def check_release_window() -> str:
    """Report current deploy status (UTC): charts live on UAT vs PRD, today's PRD release
    PR (charts staged for prod + whether it can be merged yet), and the cutoff. This is the
    source of truth for 'what's deployed' and 'what's pending to prod'."""
    return json.dumps(get_release_status(), indent=2)
