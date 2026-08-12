"""CARE and DF releases are independent: separate repos, separate queues.

A Dataflow release is raised in its own GitOps repo, so it must not pick up CARE
artifacts, must not default to the CARE repo, and must not be blocked by (or
block) an in-flight CARE release.
"""
import pytest

from release_agent import app_fastapi as APP
from release_agent.config import settings
from release_agent.tools import release_fileset as RF


@pytest.fixture
def repos(monkeypatch):
    def _set(deploy_repo, df_release_repo):
        monkeypatch.setattr(settings, "deploy_repo", deploy_repo)
        monkeypatch.setattr(settings, "df_release_repo", df_release_repo)
        monkeypatch.setattr(APP.app_settings, "deploy_repo", deploy_repo, raising=False)
        monkeypatch.setattr(APP.app_settings, "df_release_repo", df_release_repo, raising=False)
        monkeypatch.setattr(APP, "active_deploy_repo", lambda: deploy_repo, raising=False)

    return _set


def _queue_ctx(monkeypatch, deploy_repo, df_release_repo):
    """The context the release forms render from."""
    monkeypatch.setattr(APP.app_settings, "deploy_repo", deploy_repo, raising=False)
    monkeypatch.setattr(APP.app_settings, "df_release_repo", df_release_repo, raising=False)
    monkeypatch.setattr(
        "release_agent.tools.release_queue.current_queue",
        lambda *a, **k: {"ok": True, "queue": []},
    )
    monkeypatch.setattr(APP, "_known_charts", lambda: [])
    return APP.release_queue_get()


def test_df_release_defaults_to_its_own_repo(monkeypatch):
    ctx = _queue_ctx(monkeypatch, "acme/care-deploy", "acme/df-deploy")
    assert ctx["default_repo"] == "acme/care-deploy"
    assert ctx["df_default_repo"] == "acme/df-deploy"


def test_single_repo_setups_still_work(monkeypatch):
    """DF_RELEASE_REPO unset = one repo for both, i.e. the previous behaviour."""
    ctx = _queue_ctx(monkeypatch, "acme/deploy", "")
    assert ctx["df_default_repo"] == ctx["default_repo"] == "acme/deploy"


def test_the_in_flight_guard_is_checked_against_the_target_repo(monkeypatch, repos):
    """An open CARE release must not block a DF release raised elsewhere — the
    guard reads the repo the release actually targets, not the configured one."""
    repos("acme/care-deploy", "acme/df-deploy")
    checked = []

    class _Repo:
        def __init__(self, full):
            self.full = full

    monkeypatch.setattr(
        RF, "_get_github_client",
        lambda: type("C", (), {"get_repo": staticmethod(lambda full: _Repo(full))})(),
    )

    def _blocker(repo, exclude_head=""):
        checked.append(repo.full)
        return None                      # no blocker in either repo

    monkeypatch.setattr(RF, "_open_prd_pr_blocker", _blocker)
    # stop right after the guard — the clone is not what this test is about
    monkeypatch.setattr(
        RF, "validate_release",
        lambda p: ({**p, "release_name": "R1", "change_initiator": "d@e.com"}, []),
    )
    monkeypatch.setattr(RF.tempfile, "mkdtemp", lambda **kw: (_ for _ in ()).throw(RuntimeError("stop")))

    for repo in ("acme/df-deploy", "acme/care-deploy"):
        with pytest.raises(RuntimeError, match="stop"):   # guard passed, clone begins
            RF.prepare_release_fileset({"deployment_repo": repo})

    assert checked == ["acme/df-deploy", "acme/care-deploy"]
