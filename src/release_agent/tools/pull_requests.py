"""Deployment-repo PR tracking + control-summary tools."""

from ._common import (
    tool,
    BaseModel,
    Field,
    json,
    itertools,
    _get_github_client,
    active_deploy_repo,
    ON_MERGE_WORKFLOW,
)


class FindPrsInput(BaseModel):
    search_term: str = Field(
        default="", description="Search term for PRs (e.g. image name, tag, or 'CHG')"
    )
    limit: int = Field(default=5, ge=1, le=20)


class PrNumberInput(BaseModel):
    pr_number: int = Field(..., description="Pull request number")


class PrCommentsInput(BaseModel):
    pr_number: int = Field(..., description="Pull request number")
    limit: int = Field(
        default=100,
        ge=1,
        le=300,
        description="Max comments to fetch (defaults high so the agent sees all PR comments, e.g. CHG/RMG tickets and RLFT gates)",
    )


class RetriggerDeploymentWorkflowInput(BaseModel):
    pr_number: int = Field(..., description="PR number in the deployment repo to simulate for")
    simulate_closed_controls: str = Field(
        default="",
        description="Comma-separated list of controls to mark as closed (e.g. 'RLFT approval gate,RLFT deploy control'). Use this to simulate external actions.",
    )


def _find_prs_for_images(image_tags: str, limit: int = 20) -> list[dict]:
    """Return deployment-repo PRs whose title/branch matches ALL tokens of the
    given image tags, newest first. Empty list on error or no match."""
    try:
        g = _get_github_client()
        repo = g.get_repo(active_deploy_repo())
        tokens = [t for t in image_tags.lower().replace(":", " ").replace(",", " ").split() if t]
        if not tokens:
            return []
        out: list[dict] = []
        for pr in itertools.islice(
            repo.get_pulls(state="all", sort="created", direction="desc"), 60
        ):
            hay = f"{pr.title} {pr.head.ref or ''}".lower()
            if all(tok in hay for tok in tokens):
                out.append(
                    {
                        "number": pr.number,
                        "url": pr.html_url,
                        "title": pr.title,
                        "state": pr.state,
                    }
                )
                if len(out) >= limit:
                    break
        return out
    except Exception:
        return []


def _find_pr_for_images(image_tags: str) -> dict | None:
    """Newest deployment-repo PR matching the image tags, or None."""
    prs = _find_prs_for_images(image_tags, limit=1)
    return prs[0] if prs else None


@tool(args_schema=FindPrsInput)
def find_prs(search_term: str = "", limit: int = 5) -> str:
    """
    Find recent PRs in the deployment repo.
    Use search_term like image name, tag, or 'CHG' to filter.
    Example: search_term="payments-api:2.0.33" or "CHG-12345"
    """

    def _pr_dict(pr):
        return {
            "number": pr.number,
            "title": pr.title,
            "url": pr.html_url,
            "state": pr.state,
            "createdAt": str(pr.created_at),
            "author": pr.user.login if pr.user else None,
        }

    try:
        g = _get_github_client()
        repo = g.get_repo(active_deploy_repo())

        if not search_term:
            pulls = list(
                itertools.islice(
                    repo.get_pulls(state="all", sort="created", direction="desc"), limit
                )
            )
            return json.dumps(
                {"repo": active_deploy_repo(), "search_term": "recent", "prs": [_pr_dict(p) for p in pulls]},
                indent=2,
            )

        # Token-based scan of recent PRs (reliable; no search-index delay and
        # tolerant of ':' vs ' ' between image and tag). A PR matches if every
        # token of the search term appears in its title or head branch.
        tokens = [t for t in search_term.lower().replace(":", " ").replace(",", " ").split() if t]
        results: dict[int, dict] = {}
        for pr in itertools.islice(
            repo.get_pulls(state="all", sort="created", direction="desc"), 80
        ):
            hay = f"{pr.title} {pr.head.ref or ''}".lower()
            if tokens and all(tok in hay for tok in tokens):
                results[pr.number] = _pr_dict(pr)
                if len(results) >= limit:
                    break

        # Supplement with GitHub search (catches matches in body/comments, e.g. a
        # CHG/RMG number) — best-effort, since the search index can lag.
        if len(results) < limit:
            try:
                query = f"{search_term} repo:{active_deploy_repo()} is:pr"
                for issue in itertools.islice(g.search_issues(query), limit):
                    if issue.pull_request and issue.number not in results:
                        results[issue.number] = {
                            "number": issue.number,
                            "title": issue.title,
                            "url": issue.html_url,
                            "state": issue.state,
                            "createdAt": str(issue.created_at),
                            "author": issue.user.login if issue.user else None,
                        }
            except Exception:
                pass

        return json.dumps(
            {
                "repo": active_deploy_repo(),
                "search_term": search_term,
                "prs": list(results.values())[:limit],
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR finding PRs: {e}"


def _fetch_pr_details(pr_number: int) -> str:
    """Plain helper reusable without tool-wrapper invocation."""
    try:
        g = _get_github_client()
        repo = g.get_repo(active_deploy_repo())
        pr = repo.get_pull(pr_number)
        return json.dumps(
            {
                "number": pr.number,
                "title": pr.title,
                "url": pr.html_url,
                "state": pr.state,
                "headRefName": pr.head.ref,
                "baseRefName": pr.base.ref,
                "author": pr.user.login if pr.user else None,
                "createdAt": str(pr.created_at),
                "updatedAt": str(pr.updated_at),
                "mergedAt": str(pr.merged_at) if pr.merged_at else None,
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR getting PR #{pr_number}: {e}"


@tool(args_schema=PrNumberInput)
def get_pr_details(pr_number: int) -> str:
    """Get basic details of a PR (title, state, URL, branch, etc.)."""
    return _fetch_pr_details(pr_number)


def _fetch_pr_comments(pr_number: int, limit: int = 100) -> str:
    """Plain helper reusable without tool-wrapper invocation."""
    try:
        g = _get_github_client()
        repo = g.get_repo(active_deploy_repo())
        pr = repo.get_pull(pr_number)
        comments = list(itertools.islice(pr.get_issue_comments(), limit))

        simplified = []
        for c in comments:
            simplified.append(
                {
                    "id": c.id,
                    "user": c.user.login if c.user else None,
                    "created_at": str(c.created_at),
                    "body": c.body[:2000] if c.body else "",
                }
            )
        return json.dumps(
            {
                "repo": active_deploy_repo(),
                "pr": pr_number,
                "comment_count": len(simplified),
                "comments": simplified,
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR getting comments for PR #{pr_number}: {e}"


@tool(args_schema=PrCommentsInput)
def get_pr_comments(pr_number: int, limit: int = 30) -> str:
    """
    Get recent comments on a PR.
    These usually contain CHG ticket references and control status
    (e.g. "CHG-12345 created", "RLFT approval gate closed", "controls opened").
    """
    return _fetch_pr_comments(pr_number, limit)


@tool(args_schema=PrNumberInput)
def summarize_pr_controls(pr_number: int) -> str:
    """
    Fetch the PR + its comments and provide a summary focused on:
    - CHG/change ticket references
    - Release control states (RLFT gates, closed/opened)
    - Overall readiness
    """
    try:
        details = json.loads(_fetch_pr_details(pr_number))
        comments_data = json.loads(_fetch_pr_comments(pr_number, limit=100))

        return json.dumps(
            {
                "pr_details": details,
                "comments": comments_data.get("comments", []),
                "note": "Look for CHG and RMG tickets, 'RLFT', 'closed', 'opened', 'approved', 'gate' in the comments.",
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR summarizing PR #{pr_number}: {e}"


@tool(args_schema=RetriggerDeploymentWorkflowInput)
def retrigger_deployment_workflow(pr_number: int, simulate_closed_controls: str = "") -> str:
    """
    Retrigger the deployment simulation workflow in the active_deploy_repo().
    This is useful when you have closed some controls manually (outside the automation)
    and want the deployment comments / status to be re-generated with the updated control state.
    """
    try:
        g = _get_github_client()
        repo = g.get_repo(active_deploy_repo())
        workflow = repo.get_workflow(ON_MERGE_WORKFLOW)

        inputs = {"pr_number": str(pr_number)}
        if simulate_closed_controls:
            inputs["simulate_closed_controls"] = simulate_closed_controls

        workflow.create_dispatch(ref=repo.default_branch, inputs=inputs)

        return json.dumps(
            {
                "triggered": True,
                "repo": active_deploy_repo(),
                "pr_number": pr_number,
                "simulate_closed_controls": simulate_closed_controls,
                "note": "Workflow retriggered. Use summarize_pr_controls or get_pr_comments to see the updated status.",
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR retriggering deployment workflow for PR #{pr_number}: {e}"


# ============ Image-tag build verification (PyGithub refactor of gh-image-tag-steps.sh) ============

