"""Tests for remove_from_release: live removal from uat/prod deployment files.

PROD is reachable only through a release (queue -> CARE/DF release -> promote),
so there is no daily PRD staging PR to unstage from any more — remove_from_release
only ever touches a LIVE environment.
"""
import json
from types import SimpleNamespace

import pytest

from release_agent.tools import promotion as P
from tests.fakes import FakeRepo as _FakeRepo

UAT_PATH = P._deployment_path("uat")
PRD_PATH = P._deployment_path("prd")


def _entry(name, version, env="prd"):
    return P.assemble_entry(name, version, env)


def _include(repo, branch, path):
    return {e["helm_chart_name"] for e in P._read_include(repo, branch, path)}


@pytest.fixture
def make_repo(monkeypatch):
    """Build a _FakeRepo and wire it in as the GitHub client."""

    def _make(initial):
        repo = _FakeRepo(initial)
        gh = SimpleNamespace(get_repo=lambda name: repo)
        monkeypatch.setattr(P, "_get_github_client", lambda: gh)
        monkeypatch.setattr(P, "_find_deploy_run", lambda *a, **k: None)
        return repo

    return _make


def _live_branches(with_targeted_on_uat=True):
    """SIT/UAT carry targeted-svc (already released to UAT); PRD holds only base-svc."""
    uat_inc = [_entry("base-svc", "1.0.0", "uat")]
    if with_targeted_on_uat:
        uat_inc = uat_inc + [_entry("targeted-svc", "1.2.0", "uat")]
    return {
        b: {
            PRD_PATH: {"include": [_entry("base-svc", "1.0.0", "prd")]},
            UAT_PATH: {"include": list(uat_inc)},
        }
        for b in ("SIT", "UAT", "PRD")
    }


# --- explicit live removal --------------------------------------------------------

def test_uat_removal_goes_via_sit(make_repo):
    """UAT changes flow via SIT: the removal lands on SIT; UAT keeps the chart
    until the repository's SIT -> UAT PR is merged. PRD is never touched."""
    repo = make_repo(_live_branches())

    res = json.loads(P.remove_from_release("targeted-svc", environment="uat"))

    assert res["action"] == "removal_pending_review" and res["environment"] == "uat"
    assert "targeted-svc" not in _include(repo, "SIT", UAT_PATH)
    assert "targeted-svc" in _include(repo, "UAT", UAT_PATH), "UAT waits for the SIT -> UAT PR"
    assert _include(repo, "PRD", PRD_PATH) == {"base-svc"}
    assert [p["stage"] for p in res["prs"]] == ["→SIT"]


def test_uat_is_the_default_environment(make_repo):
    repo = make_repo(_live_branches())

    res = json.loads(P.remove_from_release("targeted-svc"))

    assert res["environment"] == "uat" and res["action"] == "removal_pending_review"
    assert "targeted-svc" not in _include(repo, "SIT", UAT_PATH)


def test_prod_removal_removes_from_both_live_files(make_repo):
    initial = _live_branches()
    for b in ("SIT", "UAT", "PRD"):
        initial[b][PRD_PATH]["include"].append(_entry("targeted-svc", "1.1.0", "prd"))
    repo = make_repo(initial)

    res = json.loads(P.remove_from_release("targeted-svc", environment="prod"))

    assert res["action"] == "removed" and res["environment"] == "prod"
    for b in ("SIT", "UAT", "PRD"):
        assert "targeted-svc" not in _include(repo, b, PRD_PATH)
        assert "targeted-svc" not in _include(repo, b, UAT_PATH)


def test_unsupported_environment_is_rejected(make_repo):
    make_repo(_live_branches())
    out = P.remove_from_release("targeted-svc", environment="qa")
    assert out.startswith("ERROR") and "uat or prod" in out


def test_input_schema_defaults_to_uat():
    assert P.RemoveFromReleaseInput(image_names="x").environment == "uat"


def test_removing_a_chart_not_deployed_is_a_no_op(make_repo):
    repo = make_repo(_live_branches(with_targeted_on_uat=False))

    res = json.loads(P.remove_from_release("targeted-svc", environment="uat"))

    assert res["ok"] is True and res["action"] == "no_change"
    assert repo.prs == []


# --- UAT deploy override via targeted per-branch edits ------------------------
# (open_release_pr env=uat used whole-branch working->SIT->UAT merges, which
# conflicted permanently once SIT/UAT histories diverged — deployment-repo PRs
# #93/#96/#103. It now applies the same override to each branch independently.)

def test_uat_deploy_overrides_sit_and_leaves_uat_to_the_sit_to_uat_pr(monkeypatch):
    def entry(name, version, values="uat/values_uat.yaml"):
        return {
            "helm_chart_name": name, "helm_chart_version": version,
            "helm_chart_dir": "d", "helm_values_file_name": values, "gke_namespace": "ns",
        }

    # SIT and UAT start with different contents: the override replaces SIT's
    # file exactly; UAT changes only when the SIT -> UAT PR is merged.
    initial = {
        "SIT": {UAT_PATH: {"include": [entry("old-svc", "0.1")]}},
        "UAT": {UAT_PATH: {"include": [entry("other-svc", "7.7"), entry("old-svc", "0.2")]}},
        "PRD": {UAT_PATH: {"include": []}, PRD_PATH: {"include": []}},
    }
    repo = _FakeRepo(initial)
    monkeypatch.setattr(P, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))

    out = json.loads(P.open_release_pr.invoke({
        "environment": "uat",
        "deployment_json": json.dumps({"include": [entry("new-svc", "1.0")]}),
    }))
    assert out["ok"] is True and out["action"] == "pending_review"

    sit = json.loads(repo.files["SIT"][UAT_PATH])["include"]
    assert [(e["helm_chart_name"], e["helm_chart_version"]) for e in sit] == [("new-svc", "1.0")]
    uat = json.loads(repo.files["UAT"][UAT_PATH])["include"]
    assert [e["helm_chart_name"] for e in uat] == ["other-svc", "old-svc"], "UAT untouched by the portal"
    # PRD untouched by a UAT deploy.
    assert json.loads(repo.files["PRD"][UAT_PATH])["include"] == []
    stages = [(p.get("stage"), p.get("merged")) for p in out["prs"] if p.get("number")]
    assert stages == [("→SIT", True)], "one PR, into SIT"


# --- PROD is not a single-chart deploy target any more ------------------------

def test_open_release_pr_refuses_prod(monkeypatch):
    """A CHART deploy whose environment resolves to prod is refused BEFORE any
    GitHub work — the release flow (queue -> CARE/DF release -> promote) is the
    only way to reach prod now."""
    calls = []
    monkeypatch.setattr(P, "_get_github_client",
                         lambda: (_ for _ in ()).throw(AssertionError("no GitHub call expected")))

    out = P.open_release_pr.invoke({"environment": "prod", "image_tags": "guarded-svc:2.0"})

    assert out.startswith("ERROR")
    assert "release" in out.lower()
    assert calls == []
