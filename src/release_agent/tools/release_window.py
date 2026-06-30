"""Daily PRD release-window status (SIT->UAT->PRD)."""

from ._common import (
    settings,
    tool,
    json,
    itertools,
    _get_github_client,
    _read_json_file,
)


def _todays_prd_prs() -> list:
    """Today's (UTC) UAT->PRD release PRs that LOCK the day — open or merged. A
    closed-unmerged PR is abandoned and does not lock. GitHub is the cross-session
    source of truth, so any session sees the same answer."""
    from datetime import datetime, timezone

    today = datetime.now(timezone.utc).date()
    g = _get_github_client()
    repo = g.get_repo(settings.deploy_repo)
    out = []
    for pr in itertools.islice(
        repo.get_pulls(state="all", base=settings.prd_branch, sort="created", direction="desc"), 40
    ):
        d = pr.created_at.astimezone(timezone.utc).date()
        if d < today:
            break
        if d == today and (pr.state == "open" or pr.merged_at is not None):
            out.append(pr)
    return out


def get_release_status() -> dict:
    """Today's PRD release status under the SIT->UAT->PRD model.

    Images accumulate on UAT through the day; the single UAT->PRD release PR is
    raised ONLY after the cutoff (and raising it locks the day). Reports the UAT
    accumulation, whether the cutoff has passed, whether the day is locked, and
    whether images can still be added / the release can be raised."""
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
        g = _get_github_client()
        repo = g.get_repo(settings.deploy_repo)
        uat_cfg = _read_json_file(repo, settings.uat_branch, settings.env_config_path)
        uat_images = uat_cfg.get("images", {}) if isinstance(uat_cfg, dict) else {}
        prd_cfg = _read_json_file(repo, settings.prd_branch, settings.env_config_path)
        prd_images = prd_cfg.get("images", {}) if isinstance(prd_cfg, dict) else {}
        prs = _todays_prd_prs()
    except Exception as e:
        return {
            **base,
            "error": str(e),
            "can_add": True,
            "can_raise_prod": False,
            "locked": False,
            "uat_images": {},
            "pending_changes": {},
            "prd_pr_today": None,
        }

    # Pending = images on UAT whose tag differs from PRD (what an UAT->PRD PR would
    # actually promote). UAT config persists across days, so "has any images" is NOT
    # the same as "has something to release today" — compare against PRD.
    pending = {i: t for i, t in uat_images.items() if prd_images.get(i) != t}

    today_pr = prs[0] if prs else None  # today's UAT->PRD release PR (post-cutoff)
    locked = today_pr is not None  # release raised -> no more adds today
    can_add = not locked  # adds land on UAT until the PR is raised
    can_raise_prod = cutoff_passed and not locked and bool(pending)

    if locked:
        reason = f"Today's UAT→PRD release PR #{today_pr.number} is raised — the day is locked."
    elif not pending:
        reason = "No changes on UAT vs PRD — nothing to release" + (
            " (cutoff passed)." if cutoff_passed else f"; UAT→PRD opens after {cutoff:02d}:00 UTC."
        )
    elif cutoff_passed:
        reason = (
            f"Cutoff passed — raise the UAT→PRD release PR ({len(pending)} image(s) to promote)."
        )
    else:
        reason = (
            f"Collecting on UAT ({len(pending)} image(s) pending vs PRD); the UAT→PRD PR opens "
            f"after {cutoff:02d}:00 UTC."
        )
    return {
        **base,
        "uat_images": uat_images,
        "pending_changes": pending,
        "locked": locked,
        "can_add": can_add,
        "can_raise_prod": can_raise_prod,
        "reason": reason,
        "prd_pr_today": (
            {
                "number": today_pr.number,
                "url": today_pr.html_url,
                "title": today_pr.title,
                "state": today_pr.state,
                "author": today_pr.user.login if today_pr.user else None,
                "created_at": str(today_pr.created_at),
            }
            if today_pr
            else None
        ),
    }


@tool
def check_release_window() -> str:
    """Report today's PRD release status (UTC): whether a PRD release PR already
    exists today, whether the daily cutoff has passed, and whether a new PRD
    release can still be created. Shared across all sessions/developers via GitHub."""
    return json.dumps(get_release_status(), indent=2)


