"""Deterministic checks for the two-repo model (BUILD_REPO + DEPLOY_REPO).

The agent was simplified from three repo concepts to two: the separate
`target_repo` was folded into `build_repo`. These tests pin that contract:
- `build_repo` is the canonical code+config+build repo (default phaniuk111/devops),
- the legacy `RELEASE_AGENT_TARGET_REPO` env spelling still resolves (backward compat),
- `target_repo` no longer exists, and the gh_tools module exposes BUILD_REPO not TARGET_REPO.

No network / GitHub access — pure config + import checks, so it is a fast,
deterministic regression oracle.
"""

import pytest

_REPO_ENV_VARS = (
    "BUILD_REPO",
    "RELEASE_BUILD_REPO",
    "RELEASE_AGENT_TARGET_REPO",
    "RELEASE_TARGET_REPO",
    "TARGET_REPO",
)


@pytest.fixture(autouse=True)
def _clean_repo_env(monkeypatch):
    # Avoid the gcloud subprocess in model_post_init and start from a clean slate.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "test-project")
    for var in _REPO_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _fresh_settings():
    from release_agent.config import Settings

    return Settings()


def test_build_repo_default_is_devops():
    assert _fresh_settings().build_repo == "phaniuk111/devops"


def test_build_repo_canonical_env(monkeypatch):
    monkeypatch.setenv("BUILD_REPO", "foo/bar")
    assert _fresh_settings().build_repo == "foo/bar"


def test_legacy_target_repo_alias_still_resolves(monkeypatch):
    # Existing deployments that set RELEASE_AGENT_TARGET_REPO must not silently
    # misroute — the legacy spelling maps onto build_repo.
    monkeypatch.setenv("RELEASE_AGENT_TARGET_REPO", "legacy/repo")
    assert _fresh_settings().build_repo == "legacy/repo"


def test_canonical_build_repo_wins_over_legacy_alias(monkeypatch):
    monkeypatch.setenv("BUILD_REPO", "canonical/repo")
    monkeypatch.setenv("RELEASE_AGENT_TARGET_REPO", "legacy/repo")
    assert _fresh_settings().build_repo == "canonical/repo"


def test_target_repo_field_is_gone():
    from release_agent.config import settings

    assert not hasattr(settings, "target_repo")


def test_gh_tools_exposes_build_repo_not_target_repo():
    import release_agent.tools.gh_tools as gh

    assert hasattr(gh, "BUILD_REPO")
    assert not hasattr(gh, "TARGET_REPO")


def test_deploy_repo_is_independent(monkeypatch):
    monkeypatch.setenv("DEPLOY_REPO", "org/deploy")
    monkeypatch.setenv("BUILD_REPO", "org/build")
    s = _fresh_settings()
    assert s.deploy_repo == "org/deploy"
    assert s.build_repo == "org/build"
