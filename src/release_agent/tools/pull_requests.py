"""Deployment-repo PR tracking tools."""

import itertools
import json

from pydantic import BaseModel, Field

from ._common import tool, _get_github_client, active_deploy_repo


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

