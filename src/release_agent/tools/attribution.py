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

import contextlib
import contextvars
from typing import Any, Iterator

from release_agent import identity

TRAILER_KEY = "Requested-by"

# The JIRA the developer typed on the form (Deploy to CARE/DF UAT, CARE/DF
# Release): every commit and PR title of that action starts with it, so commit
# history reads "ABC-1234 <message>". Taken as typed — no lookup — and set for
# one action at a time, like the caller's identity.
_JIRA: contextvars.ContextVar[str] = contextvars.ContextVar("jira", default="")


@contextlib.contextmanager
def jira(key: str) -> Iterator[None]:
    token = _JIRA.set((key or "").strip())
    try:
        yield
    finally:
        _JIRA.reset(token)


def current_jira() -> str:
    return _JIRA.get()


def use_jira(key: str) -> None:
    """Fill the JIRA of the ``jira()`` scope already open around this call — for
    an action that learns its key part-way (a promotion reads it from the release
    PR). Outside such a scope the value would leak to the rest of the context."""
    _JIRA.set((key or "").strip())


def titled(text: str) -> str:
    """A PR title or commit subject, led by this action's JIRA (once)."""
    key = _JIRA.get()
    if not key or text == key or text.startswith(key + " "):
        return text
    return f"{key} {text}"


def commit_message(text: str) -> str:
    """A commit message: the JIRA-led subject, then the Requested-by trailer."""
    return with_trailer(titled(text))


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


def merge_kwargs(pr: Any = None) -> dict[str, Any]:
    """The merge commit's body: the trailer, so the merge itself says who asked.
    With a JIRA, its title too — GitHub's default for a merge ("Merge pull
    request #N from …") would not start with it."""
    out: dict[str, Any] = {}
    line = trailer()
    if line:
        out["commit_message"] = line
    if _JIRA.get() and pr is not None:
        out["commit_title"] = f"{titled(pr.title)} (#{pr.number})"
    return out
