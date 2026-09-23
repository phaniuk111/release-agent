"""Who asked for this — carried into everything the portal writes to GitHub.

The portal acts with a token: the person's own PAT when they connected one,
otherwise the server's. Either way GitHub's history shows the TOKEN's owner,
and with the server token the human who asked is lost exactly where an
auditor looks first. So every commit and PR the portal makes names the
verified caller twice:

  * a ``Requested-by: <email>`` trailer — the last paragraph of the commit
    message and the last line of the PR body (git's trailer convention, so
    ``git log --format='%(trailers:key=Requested-by)'`` finds it);
  * the commit AUTHOR (git's "who made the change") on commits the portal
    builds; the committer stays the token's owner, so GitHub shows
    "<person> authored, <bot> committed" — which is what happened.

Only a VERIFIED caller is named (identity.py). A typed email is a claim, not
an identity, and would let anyone sign someone else's name into history: with
identity off the portal writes nothing extra rather than something unverified.
"""
from __future__ import annotations

from typing import Any

from release_agent import identity

TRAILER_KEY = "Requested-by"


def requester_email() -> str:
    """The verified caller's email, or "" — never a typed one."""
    return identity.current_email()


def trailer() -> str:
    email = requester_email()
    return f"{TRAILER_KEY}: {email}" if email else ""


def with_trailer(text: str) -> str:
    """``text`` (a commit message or PR body) ending in the trailer paragraph —
    unchanged when nobody verified is asking."""
    line = trailer()
    if not line:
        return text
    return text.rstrip("\n") + "\n\n" + line + "\n"


def author() -> tuple[str, str] | None:
    """(name, email) to record as a commit's author, or None."""
    caller = identity.current()
    if caller is None or not caller.email:
        return None
    return (caller.name or caller.email.split("@")[0], caller.email)


def author_kwargs() -> dict[str, Any]:
    """``author=InputGitAuthor(...)`` for PyGithub's create_file / update_file /
    create_git_commit — or nothing, which leaves the token's owner as author."""
    who = author()
    if who is None:
        return {}
    from github import InputGitAuthor

    return {"author": InputGitAuthor(*who)}


def merge_kwargs() -> dict[str, Any]:
    """The merge commit's body: the trailer, so the merge itself says who asked.
    GitHub keeps its default title ("Merge pull request #N …")."""
    line = trailer()
    return {"commit_message": line} if line else {}
