"""Per-session repo + PAT token credentials.

Pins the contract that lets the end user connect a chat thread to their own
repository + PAT: normalization, token masking, contextvar activation, and the
resolver precedence (a connected session's PAT/repo beats the server-wide env
config). Pure in-memory — no network.
"""

import pytest

from release_agent.session_creds import (
    SessionCredentials,
    SessionCredentialStore,
    active_credentials,
    mask_token,
    _normalize_repo,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("octocat/hello-world", "octocat/hello-world"),
        ("https://github.com/octocat/hello-world", "octocat/hello-world"),
        ("https://github.com/octocat/hello-world.git", "octocat/hello-world"),
        ("git@github.com:octocat/hello-world.git", "octocat/hello-world"),
        ("https://github.com/octocat/hello-world/tree/main", "octocat/hello-world"),
        ("  octocat/hello-world  ", "octocat/hello-world"),
        ("not-a-repo", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_repo(raw, expected):
    assert _normalize_repo(raw) == expected


def test_mask_token_hides_the_secret():
    masked = mask_token("ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345")
    assert masked.startswith("ghp_")
    assert masked.endswith("2345")
    assert "MNOP" not in masked  # the middle is never revealed
    assert mask_token("") == ""
    assert mask_token("short") == "…rt"


def test_single_repo_fans_out_to_build_and_deploy():
    creds = SessionCredentials(repo="octocat/hello-world", pat_token="ghp_x")
    assert creds.build_repo == "octocat/hello-world"
    assert creds.deploy_repo == "octocat/hello-world"


def test_public_status_never_leaks_the_token():
    creds = SessionCredentials(
        repo="octocat/hello-world", branch="main", pat_token="ghp_supersecrettoken1234", project_name="demo"
    )
    status = creds.public_status()
    assert status["connected"] is True
    assert status["repo"] == "octocat/hello-world"
    assert status["branch"] == "main"
    assert status["project_name"] == "demo"
    assert "supersecret" not in status["token_preview"]
    assert "pat_token" not in status


def test_token_only_credentials_are_connected():
    """Connection is token-based: no repo needed — GitHub calls run as the user
    against the server-configured repositories."""
    creds = SessionCredentials(pat_token="ghp_tokenonly1234567890")
    status = creds.public_status()
    assert status["connected"] is True
    assert status["repo"] == ""
    # And the repo resolvers fall back to server config (empty override).
    assert creds.build_repo == ""
    assert creds.deploy_repo == ""


def test_store_set_get_clear():
    store = SessionCredentialStore()
    creds = SessionCredentials(repo="a/b", pat_token="ghp_x")
    store.set("t1", creds)
    assert store.get("t1") is creds
    store.clear("t1")
    assert store.get("t1") is None


def test_activate_binds_and_resets_contextvar():
    store = SessionCredentialStore()
    store.set("t1", SessionCredentials(repo="a/b", pat_token="ghp_x"))
    assert active_credentials() is None  # nothing bound outside the context
    with store.activate("t1") as creds:
        assert creds is not None
        assert active_credentials().repo == "a/b"
    assert active_credentials() is None  # reset on exit


def test_activate_unknown_thread_is_a_noop():
    store = SessionCredentialStore()
    with store.activate("missing") as creds:
        assert creds is None
        assert active_credentials() is None


def test_token_resolver_prefers_session_pat(monkeypatch):
    from release_agent.tools import _common

    monkeypatch.setenv("GH_TOKEN", "env-token")
    store = SessionCredentialStore()
    store.set("t1", SessionCredentials(repo="a/b", pat_token="session-token"))

    assert _common._resolve_github_token() == "env-token"  # no session bound
    with store.activate("t1"):
        assert _common._resolve_github_token() == "session-token"
    assert _common._resolve_github_token() == "env-token"  # falls back after


def test_repo_resolvers_prefer_session_repo(monkeypatch):
    from release_agent.tools import _common

    monkeypatch.setattr(_common.settings, "build_repo", "server/build", raising=False)
    monkeypatch.setattr(_common.settings, "deploy_repo", "server/deploy", raising=False)
    store = SessionCredentialStore()
    store.set("t1", SessionCredentials(repo="user/repo", pat_token="ghp_x"))

    assert _common.active_build_repo() == "server/build"
    assert _common.active_deploy_repo() == "server/deploy"
    with store.activate("t1"):
        assert _common.active_build_repo() == "user/repo"
        assert _common.active_deploy_repo() == "user/repo"


# --- connecting checks the token before storing it -------------------------------
# Found live: POST /api/session/connect stored whatever was pasted, so a bogus
# PAT answered "connected: true" and every GitHub action afterwards failed
# somewhere else entirely, with an error that named neither the token nor the
# connect step.

def test_a_token_github_rejects_does_not_connect(monkeypatch):
    from release_agent import session_creds as SC

    class _Resp:
        status_code = 401

        def json(self):
            return {}

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    ok, why = SC.verify_token("ghp_" + "x" * 36)
    assert ok is False and "rejected" in why.lower()


def test_a_good_token_reports_its_login(monkeypatch):
    from release_agent import session_creds as SC

    class _Resp:
        status_code = 200

        def json(self):
            return {"login": "dev"}

    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp())
    assert SC.verify_token("ghp_good") == (True, "dev")


def test_github_being_unreachable_is_not_reported_as_a_bad_token(monkeypatch):
    """Behind the proxy this is the common failure, and blaming the token sends
    the person to regenerate a PAT that was fine."""
    from release_agent import session_creds as SC

    def _boom(*a, **k):
        raise RuntimeError("proxy timed out")

    monkeypatch.setattr("requests.get", _boom)
    ok, why = SC.verify_token("ghp_good")
    assert ok is False and "Could not reach GitHub" in why


def test_an_empty_token_is_refused_without_a_call(monkeypatch):
    from release_agent import session_creds as SC

    monkeypatch.setattr("requests.get", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")))
    assert SC.verify_token("  ")[0] is False
