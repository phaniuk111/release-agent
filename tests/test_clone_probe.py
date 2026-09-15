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


DULWICH_OK = {"ok": True, "branch": "sit", "ms": 5}


def _probe(session, dulwich=DULWICH_OK):
    return P.probe_clone_paths("o/r", session=session, gh=SimpleNamespace(get_repo=lambda r: _Repo()),
                               token=TOKEN, dulwich_probe=lambda repo, branch, token: dulwich)


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


def test_urllib3s_path_only_quote_does_not_leak_the_tarball_token():
    """The cluster's real error: urllib3 quotes only "/path?token=…", never the
    full URL — replacing the full URL alone let the token through."""

    class _Urllib3Style(_Session):
        def get(self, url, **kw):
            host = url.split("://")[-1].split("/")[0]
            if host.startswith("codeload"):
                path = url.split(host, 1)[1]
                raise ConnectionError(
                    f"HTTPSConnectionPool(host='{host}', port=443): Max retries exceeded "
                    f"with url: {path} (Caused by ProxyError('Unable to connect to proxy', "
                    "OSError('Tunnel connection failed: 403 Forbidden')))")
            return GIT_OK

    out = _probe(_Urllib3Style())
    assert "SHORTLIVED" not in str(out)
    assert "403 Forbidden" in out["codeload"]["error"], "the real cause still fits in the message"


def test_a_token_param_is_masked_wherever_it_appears():
    assert P._mask_token_params("url: /x?token=AB12&a=1 (y)") == "url: /x?token=***&a=1 (y)"
    assert P._mask_token_params("a token=Z b token=Q") == "a token=*** b token=***"
    assert P._mask_token_params("no secrets") == "no secrets"


def test_the_probe_downloads_nothing():
    session = _Session(git=GIT_OK, codeload=TAR_OK)
    _probe(session)
    assert all(kw.get("stream") for _, kw in session.calls), "stream + first chunk only"


def test_release_ready_today_is_dulwich_listing_the_branch_no_git_binary():
    ok = {"ok": True}
    assert P.verdict(ok, {}, {}, ok)["release_ready_today"] is True
    assert P.verdict(ok, {}, {}, {"ok": False, "error": "SSLError"})["release_ready_today"] is False
    assert P.verdict(ok, {}, {})["release_ready_today"] is False, "not probed is not ready"


def test_endpoint_open_but_dulwich_failing_points_at_proxy_and_ca():
    v = P.verdict({"ok": True}, {}, {}, {"ok": False, "error": "SSLError: certificate verify failed"})
    assert "proxy and CA" in v["summary"] and "certificate verify failed" in v["summary"]


def test_the_cluster_shape_codeload_blocked_endpoint_open_is_ready():
    out = _probe(_Session(git=GIT_OK, raise_on=("codeload.github.com",)))
    assert out["verdict"]["release_ready_today"] is True
    assert out["verdict"]["options"] == ["dulwich", "api_sparse"]
    assert "git_cli_installed" not in out, "the release flow no longer needs a git binary"
