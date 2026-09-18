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

def test_uat_removal_uses_targeted_per_branch_edits(make_repo):
    repo = make_repo(_live_branches())

    res = json.loads(P.remove_from_release("targeted-svc", environment="uat"))

    assert res["action"] == "removed" and res["environment"] == "uat"
    # Targeted edits landed on SIT and UAT; PRD was never part of the chain.
    assert "targeted-svc" not in _include(repo, "SIT", UAT_PATH)
    assert "targeted-svc" not in _include(repo, "UAT", UAT_PATH)
    assert _include(repo, "PRD", PRD_PATH) == {"base-svc"}
    stages = [p["stage"] for p in res["prs"]]
    assert stages == ["→SIT", "→UAT"]  # per-branch working PRs, no SIT→UAT branch merge


def test_uat_is_the_default_environment(make_repo):
    repo = make_repo(_live_branches())

    res = json.loads(P.remove_from_release("targeted-svc"))

    assert res["environment"] == "uat" and res["action"] == "removed"
    assert "targeted-svc" not in _include(repo, "UAT", UAT_PATH)


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

def test_uat_deploy_overrides_both_branches_via_targeted_edits(monkeypatch):
    def entry(name, version, values="uat/values_uat.yaml"):
        return {
            "helm_chart_name": name, "helm_chart_version": version,
            "helm_chart_dir": "d", "helm_values_file_name": values, "gke_namespace": "ns",
        }

    # SIT and UAT deliberately start with DIFFERENT contents (diverged state):
    # a whole-branch merge could not reconcile these; targeted edits don't care.
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
    assert out["ok"] is True and out["action"] == "deployed"

    # Both branches end with EXACTLY the override — divergence self-healed.
    for br in ("SIT", "UAT"):
        inc = json.loads(repo.files[br][UAT_PATH])["include"]
        assert [(e["helm_chart_name"], e["helm_chart_version"]) for e in inc] == [("new-svc", "1.0")], br
    # PRD untouched by a UAT deploy.
    assert json.loads(repo.files["PRD"][UAT_PATH])["include"] == []
    # Two targeted PRs (one per branch), both merged — no SIT->UAT branch merge.
    stages = [(p.get("stage"), p.get("merged")) for p in out["prs"] if p.get("number")]
    assert stages == [("→SIT", True), ("→UAT", True)]


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
