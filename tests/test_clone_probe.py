"""The diagnostics probe for how the deploy repo's files can be fetched."""
from types import SimpleNamespace

from release_agent.tools import clone_probe as P

TOKEN = "ghp_secretsecretsecret"


class _Resp:
    def __init__(self, status, ctype="", first=b""):
        self.status_code, self.headers, self._first = status, {"Content-Type": ctype}, first

    def iter_content(self, n):
        return iter([self._first])

    def close(self):
        pass


class _Session:
    """Answers per host, the way a proxy would allow or block each one."""

    def __init__(self, git=None, codeload=None, raise_on=()):
        self.git, self.codeload, self.raise_on, self.calls = git, codeload, raise_on, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        host = url.split("://")[-1].split("/")[0]
        if host in self.raise_on:
            raise ConnectionError(f"ProxyError: blocked {url} for {TOKEN}")
        return self.codeload if host.startswith("codeload") else self.git


class _Repo:
    size = 350 * 1024      # KB, as GitHub reports it

    def get_branch(self, name):
        return SimpleNamespace(commit=SimpleNamespace(sha="abc123"))

    def get_archive_link(self, kind, ref=None):
        return f"https://codeload.github.com/o/r/legacy.tar.gz/{ref}?token=SHORTLIVED"

    def get_git_tree(self, ref, recursive=False):
        return SimpleNamespace(tree=[SimpleNamespace(type="blob")] * 1200 + [SimpleNamespace(type="tree")],
                               raw_data={"truncated": False})


def _probe(session):
    return P.probe_clone_paths("o/r", session=session, gh=SimpleNamespace(get_repo=lambda r: _Repo()),
                               token=TOKEN)


GIT_OK = _Resp(200, "application/x-git-upload-pack-advertisement")
TAR_OK = _Resp(200, "application/x-gzip", b"\x1f\x8b\x08")


def test_both_paths_open_recommends_dulwich_and_reports_size():
    out = _probe(_Session(git=GIT_OK, codeload=TAR_OK))
    assert out["git_endpoint"]["ok"] and out["codeload"]["ok"]
    assert out["repo_size"] == {"size_mb": 350.0, "files": 1200, "file_list_truncated": False}
    v = out["verdict"]
    assert v["options"] == ["dulwich", "tarball", "api_sparse"]
    assert "Dulwich" in v["summary"] and "350.0 MB" in v["summary"], "a large repo is called out"


def test_git_endpoint_blocked_but_codeload_open_says_dulwich_would_fail():
    out = _probe(_Session(codeload=TAR_OK, raise_on=("github.com",)))
    assert not out["git_endpoint"]["ok"] and out["codeload"]["ok"]
    assert "Dulwich would fail here" in out["verdict"]["summary"]
    assert out["verdict"]["options"] == ["tarball", "api_sparse"]


def test_a_proxy_page_answering_200_is_not_mistaken_for_git_or_a_tarball():
    page = _Resp(200, "text/html", b"<html>Access denied")
    out = _probe(_Session(git=page, codeload=page))
    assert not out["git_endpoint"]["ok"] and "proxy" in out["git_endpoint"]["hint"]
    assert not out["codeload"]["ok"] and "block page" in out["codeload"]["hint"]
    assert out["verdict"]["options"] == ["api_sparse"]


def test_no_secret_ever_reaches_the_report():
    out = _probe(_Session(raise_on=("github.com", "codeload.github.com")))
    text = str(out)
    assert TOKEN not in text, "the GitHub token must never appear"
    assert "SHORTLIVED" not in text, "the tarball URL's own token must never appear"
    assert out["codeload"]["host"] == "codeload.github.com"


def test_the_probe_downloads_nothing():
    session = _Session(git=GIT_OK, codeload=TAR_OK)
    _probe(session)
    assert all(kw.get("stream") for _, kw in session.calls), "stream + first chunk only"


def test_release_ready_today_needs_both_the_binary_and_the_endpoint():
    ok = {"ok": True}
    assert P.verdict(True, ok, {}, {})["release_ready_today"] is True
    assert P.verdict(False, ok, {}, {})["release_ready_today"] is False
    assert P.verdict(True, {"ok": False}, {}, {})["release_ready_today"] is False
