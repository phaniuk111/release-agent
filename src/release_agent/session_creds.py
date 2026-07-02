"""Per-session repository + PAT token credentials.

The end user connects a session to *their* repository with *their* GitHub PAT,
so a single Release Copilot deployment can serve different repos/tokens per
chat thread — instead of relying only on the server-wide ``RELEASE_BUILD_REPO``
/ ``GH_TOKEN`` configuration.

State model (mirrors the existing in-memory ADK session/artifact/memory
services):

* a process-local ``thread_id -> SessionCredentials`` store, and
* a :class:`contextvars.ContextVar` holding the *active* credentials for the
  current request. ``contextvars`` propagate across ``await`` and
  ``asyncio.to_thread``, so the deeply-nested sync GitHub tools can read the
  override without threading a parameter through every call.

Security posture:

* tokens live only in memory — never written to disk, never persisted to the
  memory service;
* tokens are never logged and never returned to the client (only a masked
  preview like ``ghp_…abcd``);
* ``clear`` / a new thread drops the stored token.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass, field
from typing import Iterator


def _normalize_repo(value: str | None) -> str:
    """Coerce a repository URL or ``owner/repo`` string to ``owner/repo``.

    Accepts ``https://github.com/owner/repo(.git)``, ``git@github.com:owner/repo``,
    or a bare ``owner/repo``. Returns ``""`` for anything unrecognizable so a
    caller can validate.
    """
    if not value:
        return ""
    v = value.strip()
    if not v:
        return ""
    # git@github.com:owner/repo(.git)
    if v.startswith("git@"):
        _, _, path = v.partition(":")
        v = path
    else:
        for prefix in ("https://", "http://", "ssh://"):
            if v.startswith(prefix):
                v = v[len(prefix):]
                break
        # host/owner/repo -> owner/repo (drop a leading host segment if present)
        if "/" in v:
            head, _, rest = v.partition("/")
            if "." in head:  # looks like a hostname (github.com, ...)
                v = rest
    if v.endswith(".git"):
        v = v[: -len(".git")]
    v = v.strip("/")
    parts = [p for p in v.split("/") if p]
    if len(parts) < 2:
        return ""
    # Keep only owner/repo even if a URL carried extra path segments.
    return f"{parts[0]}/{parts[1]}"


def mask_token(token: str | None) -> str:
    """Return a non-secret preview of a token, e.g. ``ghp_…w1Z9``.

    Never reveals more than the last 4 characters and always hides the middle.
    """
    if not token:
        return ""
    t = token.strip()
    if len(t) <= 8:
        return "…" + t[-2:] if len(t) > 2 else "…"
    prefix = t[:4]
    return f"{prefix}…{t[-4:]}"


@dataclass
class SessionCredentials:
    """Per-session GitHub connection details supplied by the end user."""

    repo: str = ""
    branch: str = ""
    pat_token: str = ""
    project_name: str = ""
    # A single ``repo`` targets both build and deploy operations unless the
    # caller distinguishes them (kept separate so the two-repo model still works
    # if a future UI collects them independently).
    build_repo: str = field(default="")
    deploy_repo: str = field(default="")

    def __post_init__(self) -> None:
        self.repo = _normalize_repo(self.repo)
        self.build_repo = _normalize_repo(self.build_repo) or self.repo
        self.deploy_repo = _normalize_repo(self.deploy_repo) or self.repo
        self.branch = (self.branch or "").strip()
        self.pat_token = (self.pat_token or "").strip()
        self.project_name = (self.project_name or "").strip()

    def public_status(self) -> dict:
        """Client-safe view — the raw token is replaced by a masked preview."""
        return {
            "connected": bool(self.pat_token and self.repo),
            "repo": self.repo,
            "branch": self.branch,
            "project_name": self.project_name,
            "token_preview": mask_token(self.pat_token),
        }


# Active credentials for the current request. None => fall back to server config.
_active: contextvars.ContextVar[SessionCredentials | None] = contextvars.ContextVar(
    "release_agent_session_creds", default=None
)


class SessionCredentialStore:
    """In-memory ``thread_id -> SessionCredentials`` map."""

    def __init__(self) -> None:
        self._by_thread: dict[str, SessionCredentials] = {}

    def set(self, thread_id: str, creds: SessionCredentials) -> None:
        self._by_thread[thread_id] = creds

    def get(self, thread_id: str) -> SessionCredentials | None:
        return self._by_thread.get(thread_id)

    def clear(self, thread_id: str) -> None:
        self._by_thread.pop(thread_id, None)

    @contextlib.contextmanager
    def activate(self, thread_id: str) -> Iterator[SessionCredentials | None]:
        """Bind this thread's stored creds to the contextvar for a request.

        No-op (but still resets) when the thread has no stored credentials, so
        the agent transparently falls back to the server-wide config.
        """
        creds = self._by_thread.get(thread_id)
        token = _active.set(creds)
        try:
            yield creds
        finally:
            _active.reset(token)


_store = SessionCredentialStore()


def get_store() -> SessionCredentialStore:
    return _store


def active_credentials() -> SessionCredentials | None:
    """The credentials bound to the current request, if any."""
    return _active.get()
