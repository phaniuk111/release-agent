"""Per-session GitHub PAT credentials.

The end user connects a session with *their* GitHub PAT, so every GitHub
operation in that chat thread runs as that user — against the server-configured
repositories — instead of relying only on the server-wide ``GH_TOKEN``
configuration. A per-session repo override is still supported at the dataclass
level (``repo``/``build_repo``/``deploy_repo``) but the UI collects the token
only; connection is token-based, not repository-based.

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
    # The verified signed-in user who connected it (identity on), else None.
    owner: str | None = None

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
            "connected": bool(self.pat_token),
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

    def get(self, thread_id: str, owner: str | None = None) -> SessionCredentials | None:
        """The thread's creds — withheld from anyone but their owner.

        ``owner`` None means identity is off: no check, as before. A thread id is
        not a secret (it sits in the browser's storage), so with identity on it
        must not be enough to run as someone else's GitHub token.
        """
        creds = self._by_thread.get(thread_id)
        if creds is not None and owner is not None and creds.owner is not None \
                and creds.owner != owner:
            return None
        return creds

    def clear(self, thread_id: str) -> None:
        self._by_thread.pop(thread_id, None)

    @contextlib.contextmanager
    def activate(self, thread_id: str, owner: str | None = None) -> Iterator[SessionCredentials | None]:
        """Bind this thread's stored creds to the contextvar for a request.

        No-op (but still resets) when the thread has no stored credentials — or
        they belong to someone else — so the agent falls back to server config.
        """
        creds = self.get(thread_id, owner)
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


def verify_token(token: str) -> tuple[bool, str]:
    """Is this PAT usable? Returns (ok, login-or-reason). Never raises.

    Connecting used to store whatever was pasted, so a typo'd or expired token
    reported "Connected" and every GitHub action afterwards failed with a
    confusing error somewhere else entirely. One call to /user turns that into
    an immediate, accurate answer at the point the person can fix it.
    """
    token = str(token or "").strip()
    if not token:
        return False, "A PAT token is required to connect."
    try:
        import requests

        from .config import settings

        api = (settings.github_base_url or "https://api.github.com").rstrip("/")
        r = requests.get(
            f"{api}/user",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            timeout=15,
        )
    except Exception as e:                      # network/proxy trouble, not a bad token
        return False, f"Could not reach GitHub to check the token: {e}"[:200]
    if r.status_code == 200:
        try:
            return True, str((r.json() or {}).get("login") or "")
        except Exception:
            return True, ""
    if r.status_code in (401, 403):
        return False, ("GitHub rejected that token (401/403). Check it has not expired and "
                       "that it carries the scopes this portal needs.")
    return False, f"GitHub answered {r.status_code} when checking the token."
