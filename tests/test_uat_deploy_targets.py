"""Where a single-chart deploy may go: UAT only, via SIT (the overwrite lands on
SIT; UAT changes when the repository's SIT -> UAT PR is merged). PROD is
reachable only through a release (queue -> CARE/DF release -> promote). The
portal has no removals: a chart leaves the next release by being withdrawn from
the release queue.
"""
import json
from types import SimpleNamespace


from release_agent.tools import promotion as P
from tests.fakes import FakeRepo as _FakeRepo

UAT_PATH = P._deployment_path("uat")
PRD_PATH = P._deployment_path("prd")


def _entry(name, version, env="prd"):
    return P.assemble_entry(name, version, env)


def _include(repo, branch, path):
    return {e["helm_chart_name"] for e in P._read_include(repo, branch, path)}


# --- UAT deploy override lands on SIT ------------------------------------------

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
