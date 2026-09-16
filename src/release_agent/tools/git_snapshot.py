"""The deploy repo's files on disk, and the release commit — without a git binary.

The release flow needs the deploy repo checked out (its own updater script
edits the files) and then one commit on a new branch. Enterprise images may
not ship ``git`` and a corporate proxy may block some GitHub hosts, so:

  read   Dulwich (pure-Python git) clones ONE commit of the base branch over
         the git endpoint — the path /api/diagnostics ``clone_paths`` probes.
  stage  Dulwich's ``add`` stages new, changed and deleted files and skips
         ``.gitignore``d ones, exactly like ``git add -A``.
  write  The commit and the branch go through GitHub's Git Data API on
         api.github.com — the connection every other tool already uses — so a
         push never depends on a second git path the proxy was never shown to
         allow, and a shallow clone never has to push.

Dulwich reads only the LOWERCASE ``https_proxy`` variable and its own CA list,
while clusters commonly set ``HTTPS_PROXY`` and a CA bundle for ``requests``.
The proxy and CA are therefore handed to it explicitly, resolved exactly the
way ``requests`` resolves them, so the clone takes the same route the
diagnostics probe (a ``requests`` call) proved.

The token travels as HTTP basic auth, never inside a URL, and is scrubbed from
every error.
"""
from __future__ import annotations

import base64
import io
import os
from typing import Any

from ._common import settings

_TIMEOUT_S = 120            # per request; a blocked proxy fails long before this


class SnapshotError(RuntimeError):
    """A clone or staging step failed; the message is safe to show."""


def git_host() -> str:
    """github.com, or the GitHub Enterprise host behind GITHUB_BASE_URL."""
    base = (settings.github_base_url or "").strip()
    return base.split("://")[-1].split("/")[0] if base else "github.com"


def repo_url(repo_full: str) -> str:
    """The clone URL — with no credentials in it."""
    return f"https://{git_host()}/{repo_full}.git"


def transport_config(url: str):
    """Dulwich config carrying the proxy and CA bundle ``requests`` would use."""
    import certifi
    from dulwich.config import ConfigDict
    from requests.utils import get_environ_proxies

    cfg = ConfigDict()
    if not url.startswith(("https://", "http://")):
        return cfg                  # a local path (tests): no network settings
    # get_environ_proxies reads HTTPS_PROXY / https_proxy and honours NO_PROXY.
    proxies = get_environ_proxies(url)
    proxy = proxies.get("https") or proxies.get("all")
    if proxy:
        cfg.set((b"http",), b"proxy", proxy.encode())
    ca = os.environ.get("REQUESTS_CA_BUNDLE") or os.environ.get("CURL_CA_BUNDLE") or certifi.where()
    cfg.set((b"http",), b"sslCAInfo", ca.encode())
    cfg.set((b"http",), b"timeout", str(_TIMEOUT_S).encode())
    return cfg


def _scrub(text: str, token: str) -> str:
    text = str(text)
    return text.replace(token, "***") if token else text


def clone_branch(url: str, branch: str, dest: str, token: str) -> str:
    """Check out the tip of ``branch`` (one commit, no history) into ``dest``.

    Returns the commit sha it checked out — the parent of the release commit.
    """
    from dulwich import porcelain

    auth: dict[str, Any] = {"username": "x-access-token", "password": token} if token else {}
    try:
        repo = porcelain.clone(
            url, dest, depth=1, branch=branch, checkout=True,
            config=transport_config(url), errstream=io.BytesIO(), **auth,
        )
        with repo:                  # closes pack files; the workdir is deleted later
            return repo.head().decode()
    except Exception as e:  # noqa: BLE001 — every failure becomes one readable line
        where = url.split("://")[-1].split("/")[0] if "://" in url else "local"
        raise SnapshotError(
            _scrub(f"could not fetch {branch} over {where}: {type(e).__name__}: {e}", token)[:500]
        ) from None


def branch_tip(url: str, branch: str, token: str) -> str | None:
    """``git ls-remote`` for one branch, over exactly the transport a clone uses.

    Diagnostics call this: it proves the proxy, CA and auth Dulwich will use
    without downloading anything. None when the branch does not exist.
    """
    from dulwich.client import get_transport_and_path

    auth: dict[str, Any] = {"username": "x-access-token", "password": token} if token else {}
    try:
        client, path = get_transport_and_path(url, config=transport_config(url), **auth)
        refs = client.get_refs(path, ref_prefix=[f"refs/heads/{branch}".encode()]).refs
    except Exception as e:  # noqa: BLE001
        raise SnapshotError(_scrub(f"{type(e).__name__}: {e}", token)[:300]) from None
    sha = refs.get(f"refs/heads/{branch}".encode())
    return sha.decode() if sha else None


def stage_all(repo_dir: str) -> dict[str, list[str]]:
    """Stage everything the updater changed; returns {'add','modify','delete'} paths."""
    from dulwich import porcelain

    try:
        porcelain.add(repo_dir)
        staged = porcelain.status(repo_dir).staged
    except Exception as e:  # noqa: BLE001
        raise SnapshotError(f"could not read the changed files: {type(e).__name__}: {e}"[:300]) from None
    return {kind: sorted(_s(p) for p in staged.get(kind, [])) for kind in ("add", "modify", "delete")}


def _s(p: str | bytes) -> str:
    return p.decode() if isinstance(p, bytes) else p


def changed_paths(staged: dict[str, list[str]]) -> list[str]:
    return sorted({p for paths in staged.values() for p in paths})


def base_commit(repo_dir: str) -> str:
    from dulwich.repo import Repo

    with Repo(repo_dir) as repo:
        return repo.head().decode()


def commit_via_api(gh_repo, repo_dir: str, staged: dict[str, list[str]], base_sha: str,
                   message: str, author_name: str, author_email: str) -> str:
    """One commit on top of ``base_sha`` holding exactly the staged changes.

    Contents and file modes come from the index Dulwich just wrote, so the
    commit is byte-for-byte what ``git commit`` would have recorded. Returns
    the new commit's sha; creates no branch.
    """
    from dulwich.repo import Repo
    from github import InputGitAuthor, InputGitTreeElement

    elements = []
    with Repo(repo_dir) as repo:
        index = repo.open_index()
        for path in staged.get("add", []) + staged.get("modify", []):
            entry = index[path.encode()]
            data = repo[entry.sha].data
            blob = gh_repo.create_git_blob(base64.b64encode(data).decode(), "base64")
            elements.append(InputGitTreeElement(path, f"{entry.mode:06o}", "blob", sha=blob.sha))
    for path in staged.get("delete", []):
        elements.append(InputGitTreeElement(path, "100644", "blob", sha=None))
    if not elements:
        raise SnapshotError("nothing to commit — the updater changed no files")

    parent = gh_repo.get_git_commit(base_sha)
    tree = gh_repo.create_git_tree(elements, base_tree=parent.tree)
    author = InputGitAuthor(author_name, author_email)
    commit = gh_repo.create_git_commit(message, tree, [parent], author=author)
    return commit.sha
