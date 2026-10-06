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

The release flow checks out with Dulwich (tools/git_snapshot), whose transport
is urllib3 with its own proxy and CA settings — not ``requests``. So besides the
raw git endpoint, ``dulwich`` lists the base branch through that exact
transport: it is what decides ``release_ready_today``.

Nothing sensitive is returned: the token never appears, and the tarball URL
(which carries a short-lived token of its own for private repos) is reported by
host only.
"""
from __future__ import annotations

import time
from typing import Any

from ._common import settings
from . import git_snapshot

_TIMEOUT = (10, 30)          # connect, read — a blocked proxy should fail fast
_LARGE_REPO_MB = 200

# git_snapshot already computes the GitHub host and scrubs a token from error
# text for the exact same reasons (its Dulwich transport hits the same host,
# over the same corporate proxy) — reuse rather than duplicate.
_git_host = git_snapshot.git_host
_scrub = git_snapshot._scrub


def _mask_token_params(text: str) -> str:
    """Blank every ``token=…`` query value, wherever a URL is quoted."""
    out, rest = [], text
    while "token=" in rest:
        head, tail = rest.split("token=", 1)
        out.append(head + "token=***")
        end = 0
        while end < len(tail) and tail[end] not in " '\"&)>,;":
            end += 1
        rest = tail[end:]
    out.append(rest)
    return "".join(out)


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
        # urllib3 quotes only the path and query ("…with url: /o/r/…?token=…"),
        # so the whole URL, its path, and any token= value are each removed.
        msg = _scrub(f"{type(e).__name__}: {e}", token)
        if url:
            msg = msg.replace(url, f"https://{host}/…")
            path = url.split(host, 1)[-1] if host else ""
            if path:
                msg = msg.replace(path, "/…")
        return {"ok": False, "host": host or "codeload", "error": _mask_token_params(msg)[:300]}


def _probe_dulwich(repo_full: str, branch: str, token: str) -> dict[str, Any]:
    """List the base branch through the transport the release checkout uses."""
    t0 = time.monotonic()
    try:
        sha = git_snapshot.branch_tip(git_snapshot.repo_url(repo_full), branch, token)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "branch": branch, "error": _mask_token_params(_scrub(str(e), token))[:300]}
    ms = int((time.monotonic() - t0) * 1000)
    if sha is None:
        return {"ok": False, "branch": branch, "ms": ms, "error": f"branch {branch} not found"}
    return {"ok": True, "branch": branch, "ms": ms}


def _repo_size(gh_repo, ref: str) -> dict[str, Any]:
    out: dict[str, Any] = {"size_mb": round((getattr(gh_repo, "size", 0) or 0) / 1024, 1)}
    try:
        tree = gh_repo.get_git_tree(ref, recursive=True)
        out["files"] = sum(1 for e in tree.tree if getattr(e, "type", "") == "blob")
        out["file_list_truncated"] = bool((getattr(tree, "raw_data", {}) or {}).get("truncated"))
    except Exception as e:  # noqa: BLE001
        out["files_error"] = f"{type(e).__name__}: {e}"[:200]
    return out


def verdict(git_endpoint: dict, codeload: dict, size: dict, dulwich: dict | None = None) -> dict[str, Any]:
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
                "can check out the repo here; the release commit goes through the REST API.")
    elif codeload.get("ok"):
        text = ("The git endpoint is NOT reachable but tarball snapshots are — Dulwich "
                "would fail here; a snapshot download would work.")
    else:
        text = ("Neither the git endpoint nor tarball snapshots are reachable — only "
                "the REST API works, so fetch just the files the updater needs.")
    if large:
        text += (f" The repo is {size.get('size_mb')} MB: prefer fetching only the files "
                 "the updater script reads and writes over any full download.")
    dulwich = dulwich or {}
    if git_endpoint.get("ok") and not dulwich.get("ok"):
        text += (" But Dulwich's own transport could not list the branch "
                 f"({dulwich.get('error') or 'not probed'}) — check the proxy and CA settings.")
    return {
        "options": options,
        "summary": text,
        # What the release flow needs: Dulwich's checkout (no git binary) —
        # its commit goes through the REST API the github check proves.
        "release_ready_today": bool(dulwich.get("ok")),
    }


def probe_clone_paths(repo_full: str, session=None, gh=None, token: str | None = None,
                      dulwich_probe=None) -> dict[str, Any]:
    """Probe every way of getting ``repo_full``'s files. Never raises."""
    from ._common import _get_github_client, _resolve_github_token

    token = _resolve_github_token() if token is None else token
    if session is None:
        import requests  # honours HTTPS_PROXY / NO_PROXY / REQUESTS_CA_BUNDLE

        session = requests.Session()
    if not repo_full:
        return {"error": "DEPLOY_REPO is not configured"}
    try:
        gh_repo = (gh or _get_github_client()).get_repo(repo_full)
        try:
            ref = gh_repo.get_branch(settings.sit_branch).commit.sha
        except Exception:
            ref = gh_repo.get_branch(gh_repo.default_branch).commit.sha
    except Exception as e:  # noqa: BLE001
        return {"error": _scrub(f"could not read {repo_full}: {e}", token or "")[:300]}

    endpoint = _probe_git_endpoint(session, repo_full, token or "")
    codeload = _probe_codeload(session, gh_repo, ref, token or "")
    dulwich = (dulwich_probe or _probe_dulwich)(repo_full, settings.sit_branch, token or "")
    size = _repo_size(gh_repo, ref)
    return {
        "repo": repo_full,
        "git_endpoint": endpoint,
        "dulwich": dulwich,
        "codeload": codeload,
        "repo_size": size,
        "verdict": verdict(endpoint, codeload, size, dulwich),
    }
