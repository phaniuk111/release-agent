"""Which ways of getting the deploy repo's files actually work from here.

Creating a CARE/DF release needs the deploy repo's files on disk (the repo's own
updater script edits them), and there are three ways to get them — each over a
DIFFERENT host, which a corporate egress proxy may allow or block separately:

    git endpoint   https://<github>/<owner>/<repo>.git   git CLI, Dulwich
    codeload       https://codeload.github.com/…         tarball snapshot
    REST API       https://api.github.com/…              one call per file

A working REST API (which the rest of the portal proves) says nothing about the
other two. This probes each cheaply — the first response of the git handshake,
the first chunk of the tarball, no repository is downloaded — and reports the
repo's size, so the choice between them is measured rather than guessed.

Nothing sensitive is returned: the token never appears, and the tarball URL
(which carries a short-lived token of its own for private repos) is reported by
host only.
"""
from __future__ import annotations

import shutil
import time
from typing import Any

from ._common import settings

_TIMEOUT = (10, 30)          # connect, read — a blocked proxy should fail fast
_LARGE_REPO_MB = 200


def _git_host() -> str:
    """github.com, or the GitHub Enterprise host behind GITHUB_BASE_URL."""
    base = (settings.github_base_url or "").strip()
    return base.split("://")[-1].split("/")[0] if base else "github.com"


def _scrub(text: str, token: str) -> str:
    text = str(text)
    return text.replace(token, "***") if token else text


def _probe_git_endpoint(session, repo_full: str, token: str) -> dict[str, Any]:
    """The first request of a clone: the ref advertisement. 200 with git's own
    content type means a clone over this path can start."""
    host = _git_host()
    t0 = time.monotonic()
    try:
        r = session.get(
            f"https://{host}/{repo_full}.git/info/refs",
            params={"service": "git-upload-pack"},
            auth=("x-access-token", token) if token else None,
            timeout=_TIMEOUT, stream=True,
        )
        ms = int((time.monotonic() - t0) * 1000)
        ctype = r.headers.get("Content-Type", "")
        r.close()
        ok = r.status_code == 200 and ctype.startswith("application/x-git-upload-pack")
        out = {"ok": ok, "host": host, "status": r.status_code, "ms": ms}
        if not ok:
            out["hint"] = ("reached the host but it did not answer as a git server — "
                           "a proxy login page or a block page usually looks like this"
                           if r.status_code == 200 else "check the proxy allows this host, and SSO for the token")
        return out
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "host": host, "error": _scrub(f"{type(e).__name__}: {e}", token)[:300]}


def _probe_codeload(session, gh_repo, ref: str, token: str) -> dict[str, Any]:
    """The first chunk of a tarball snapshot — then hang up."""
    t0 = time.monotonic()
    host, url = "", ""
    try:
        url = gh_repo.get_archive_link("tarball", ref=ref)
        host = url.split("://")[-1].split("/")[0]
        r = session.get(url, timeout=_TIMEOUT, stream=True,
                        headers={"Authorization": f"token {token}"} if token else {})
        first = next(r.iter_content(8192), b"") if r.status_code == 200 else b""
        ms = int((time.monotonic() - t0) * 1000)
        r.close()
        # gzip magic: a real archive, not an HTML block page with a 200
        ok = r.status_code == 200 and first[:2] == b"\x1f\x8b"
        out = {"ok": ok, "host": host, "status": r.status_code, "ms": ms}
        if r.status_code == 200 and not ok:
            out["hint"] = "got a 200 that is not an archive — likely a proxy block page"
        return out
    except Exception as e:  # noqa: BLE001
        # Connection errors quote the URL they failed on, and this URL carries
        # a short-lived token of its own for private repos — drop it entirely.
        msg = _scrub(f"{type(e).__name__}: {e}", token)
        if url:
            msg = msg.replace(url, f"https://{host}/…")
        return {"ok": False, "host": host or "codeload", "error": msg[:300]}


def _repo_size(gh_repo, ref: str) -> dict[str, Any]:
    out: dict[str, Any] = {"size_mb": round((getattr(gh_repo, "size", 0) or 0) / 1024, 1)}
    try:
        tree = gh_repo.get_git_tree(ref, recursive=True)
        out["files"] = sum(1 for e in tree.tree if getattr(e, "type", "") == "blob")
        out["file_list_truncated"] = bool((getattr(tree, "raw_data", {}) or {}).get("truncated"))
    except Exception as e:  # noqa: BLE001
        out["files_error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def verdict(git_cli: bool, git_endpoint: dict, codeload: dict, size: dict) -> dict[str, Any]:
    """Which ways of getting the files are open, in plain words. Pure."""
    options = []
    if git_endpoint.get("ok"):
        options.append("dulwich")
    if codeload.get("ok"):
        options.append("tarball")
    options.append("api_sparse")          # api.github.com is proven by the github check
    large = (size.get("size_mb") or 0) >= _LARGE_REPO_MB
    if git_endpoint.get("ok"):
        text = ("The git endpoint is reachable — Dulwich (pure-Python git, no binary) "
                "can clone and push here.")
    elif codeload.get("ok"):
        text = ("The git endpoint is NOT reachable but tarball snapshots are — Dulwich "
                "would fail here; a snapshot download would work.")
    else:
        text = ("Neither the git endpoint nor tarball snapshots are reachable — only "
                "the REST API works, so fetch just the files the updater needs.")
    if large:
        text += (f" The repo is {size.get('size_mb')} MB: prefer fetching only the files "
                 "the updater script reads and writes over any full download.")
    return {
        "options": options,
        "summary": text,
        # What the CURRENT release flow needs: the git binary AND the endpoint.
        "release_ready_today": bool(git_cli and git_endpoint.get("ok")),
    }


def probe_clone_paths(repo_full: str, session=None, gh=None, token: str | None = None) -> dict[str, Any]:
    """Probe every way of getting ``repo_full``'s files. Never raises."""
    from ._common import _get_github_client, _resolve_github_token

    token = _resolve_github_token() if token is None else token
    if session is None:
        import requests  # honours HTTPS_PROXY / NO_PROXY / REQUESTS_CA_BUNDLE

        session = requests.Session()
    git_cli = shutil.which("git") is not None
    if not repo_full:
        return {"error": "DEPLOY_REPO is not configured", "git_cli_installed": git_cli}
    try:
        gh_repo = (gh or _get_github_client()).get_repo(repo_full)
        try:
            ref = gh_repo.get_branch(settings.sit_branch).commit.sha
        except Exception:
            ref = gh_repo.get_branch(gh_repo.default_branch).commit.sha
    except Exception as e:  # noqa: BLE001
        return {"error": _scrub(f"could not read {repo_full}: {e}", token or "")[:300],
                "git_cli_installed": git_cli}

    endpoint = _probe_git_endpoint(session, repo_full, token or "")
    codeload = _probe_codeload(session, gh_repo, ref, token or "")
    size = _repo_size(gh_repo, ref)
    return {
        "repo": repo_full,
        "git_cli_installed": git_cli,
        "git_endpoint": endpoint,
        "codeload": codeload,
        "repo_size": size,
        "verdict": verdict(git_cli, endpoint, codeload, size),
    }
