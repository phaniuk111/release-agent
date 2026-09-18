"""Deploy status: what's on UAT vs PRD, plus today's accumulating PRD release PR."""

import itertools
import json

from ._common import (
    settings,
    tool,
    _get_github_client,
    _read_json_file,
    active_deploy_repo,
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


def _release_guard_branches() -> list[str]:
    """Branches that count as 'a release in flight' when an open PR targets them —
    CARE's own guard-branch logic lives in release_chain; this stays as a thin
    wrapper because promotion.py imports this name directly."""
    from .release_chain import guard_branches

    return guard_branches("care")


def _open_prd_pr_blocker(repo, exclude_head: str = "", branches: list[str] | None = None):
    """First OPEN PR into any release-guard branch that is NOT today's staging PR.

    A PR already targeting PRD/PRL1/... (e.g. a manually raised UAT -> PRD promotion)
    means a release is in flight — staging MORE charts on top would create two
    competing releases, so adds are blocked until it's merged or closed.
    ``exclude_head`` skips today's own release/prd/<date> staging PR."""
    for base in (branches or _release_guard_branches()):
        try:
            prs = repo.get_pulls(state="open", base=base, sort="created", direction="desc")
            for pr in itertools.islice(prs, 30):
                if exclude_head and pr.head.ref == exclude_head:
                    continue
                return pr
        except Exception:
            continue
    return None


def get_release_status(deployment_repo: str = "", kind: str = "care") -> dict:
    """Current deploy status (UTC): charts live on UAT and PRD, and whether a release
    (CARE or DF) is currently in flight (an open PR into a guard branch). GitHub is
    the cross-session source of truth, so every session sees the same answer.

    PROD is reached only through a release — queue a chart, raise the CARE or DF
    release, then promote it — so there is no daily PRD staging PR any more; what
    ships is whatever the current release's file-set carries when it is promoted.

    ``deployment_repo`` selects which release is being reported: CARE and DF are
    raised in different repos, so each has its own PRs, branches and guard.
    Empty = the configured CARE repo. ``kind`` "df" watches the DF chain
    (DF_RELEASE_BRANCHES)."""
    from .release_chain import guard_branches
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    base = {
        "date_utc": now.date().isoformat(),
        "now_utc": now.strftime("%H:%M"),
    }
    try:
        repo = _get_github_client().get_repo(deployment_repo or active_deploy_repo())
        uat = _charts(repo, "uat")
        prd = _charts(repo, "prd")
        blocker = _open_prd_pr_blocker(repo, branches=guard_branches(kind))
    except Exception as e:
        return {
            **base, "error": str(e), "uat_charts": [], "prd_charts": [],
            "blocking_pr": None, "reason": f"status unavailable: {e}",
        }

    reason = "No release currently in flight."
    blocking_pr = None
    if blocker is not None:
        blocking_pr = {
            "number": blocker.number,
            "url": blocker.html_url,
            "head": blocker.head.ref,
            "base": blocker.base.ref,
        }
        reason = (
            f"A release is in flight: PR #{blocker.number} "
            f"({blocker.head.ref} → {blocker.base.ref}) is open — one release at a time."
        )

    return {
        **base,
        "uat_charts": [{"helm_chart_name": n, "helm_chart_version": v} for n, v in uat.items()],
        "prd_charts": [{"helm_chart_name": n, "helm_chart_version": v} for n, v in prd.items()],
        "blocking_pr": blocking_pr,
        "reason": reason,
    }


@tool
def check_release_window() -> str:
    """Report current deploy status (UTC): charts live on UAT vs PRD, and whether a
    release (CARE or DF) is currently in flight. This is the source of truth for
    'what's deployed' and 'is a release in progress'."""
    return json.dumps(get_release_status(), indent=2)
