"""UAT changes always flow via SIT: a CARE UAT deploy
raises and merges its PR into SIT only. The deployment repository's OWN
workflow raises SIT -> UAT on a change to uat/deployment.json; the portal finds
that PR and shows it for the developer to merge — it never raises or merges it.
"""
import json
from types import SimpleNamespace

import pytest

from adk_release_agent import deploy
from adk_release_agent.deploy_workflow import _preview_text
from release_agent.config import settings
from release_agent.tools import promotion as P
from tests.fakes import FakePR, FakeRepo

UAT_FILE = "uat/deployment.json"


class _RepoWithSitToUatWorkflow(FakeRepo):
    """What the real repo's workflow does: a merge into SIT raises SIT -> UAT."""

    def __init__(self, initial, workflow=True):
        super().__init__(initial)
        self.workflow = workflow
        self.full_name, self.html_url = "o/deploy", "https://github.example/o/deploy"

    def create_pull(self, title, body, head, base):
        pr = super().create_pull(title, body, head, base)
        if base == "SIT" and self.workflow:
            merge = pr.merge

            def merge_then_raise(*a, **k):
                merge(*a, **k)
                if not self.get_pulls(state="open", base="UAT", head="o:SIT"):
                    FakeRepo.create_pull(self, "Promote SIT to UAT", "auto", "SIT", "UAT")
            pr.merge = merge_then_raise
        return pr


@pytest.fixture
def via_sit(monkeypatch):
    monkeypatch.setattr(settings, "uat_pr_wait_seconds", 0)
    monkeypatch.setattr(settings, "sit_branch", "SIT")
    monkeypatch.setattr(settings, "uat_branch", "UAT")
    monkeypatch.setattr(P, "_find_deploy_run", lambda *a, **k: None)
    monkeypatch.setattr(P, "active_deploy_repo", lambda: "o/deploy")

    def make(workflow=True, uat=()):
        repo = _RepoWithSitToUatWorkflow(
            {"SIT": {UAT_FILE: {"include": list(uat)}}, "UAT": {UAT_FILE: {"include": list(uat)}}},
            workflow=workflow)
        monkeypatch.setattr(P, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda name: repo))
        return repo
    return make


def _chart(name, version):
    return {"helm_chart_name": name, "helm_chart_version": version, "helm_chart_dir": "d",
            "helm_values_file_name": "uat/values_uat.yaml", "gke_namespace": "ns"}


def _deploy(chart="orders-api", version="1.2.3"):
    return json.loads(P.open_release_pr("uat", deployment_json=json.dumps({"include": [_chart(chart, version)]})))


def _names(repo, branch):
    return [e["helm_chart_name"] + ":" + e["helm_chart_version"]
            for e in json.loads(repo.files[branch][UAT_FILE])["include"]]


def test_a_deploy_merges_into_sit_and_hands_over_the_sit_to_uat_pr(via_sit):
    repo = via_sit()

    out = _deploy()

    mine = [p for p in repo.prs if p.head.ref.startswith("change/")]
    assert [p.base.ref for p in mine] == ["SIT"], "the portal raises only the PR into SIT"
    sit_to_uat = next(p for p in repo.prs if p.head.ref == "SIT" and p.base.ref == "UAT")
    assert sit_to_uat.state == "open", "never merged by the portal"
    assert out["action"] == "pending_review"
    assert out["pending_prs"] == [{"number": sit_to_uat.number, "url": sit_to_uat.html_url, "stage": "→UAT",
                                   "reason": "SIT → UAT, raised by the repository's workflow", "final": True}]
    assert f"merge [PR #{sit_to_uat.number}]" in out["note"] and "UAT is unchanged" in out["note"]
    assert _names(repo, "SIT") == ["orders-api:1.2.3"] and _names(repo, "UAT") == []


def test_when_the_workflows_pr_has_not_appeared_the_developer_is_told_where_to_look(via_sit):
    repo = via_sit(workflow=False)

    out = _deploy()

    assert out["action"] == "pending_review" and out["pending_prs"] == []
    assert "has not appeared yet" in out["note"] and "https://github.example/o/deploy/pulls" in out["note"]
    assert _names(repo, "UAT") == []


def test_the_next_deploy_waits_for_the_sit_to_uat_pr(via_sit):
    via_sit()
    first = _deploy()
    number = first["pending_prs"][0]["number"]

    second = _deploy("payments-api", "2.0.0")

    assert second["ok"] is False and second["action"] == "blocked"
    assert f"PR #{number}" in second["error"] and "Merge it" in second["error"]


def test_the_portal_never_edits_uat_itself(via_sit):
    """No setting brings back a direct UAT edit: UAT changes only when someone
    merges the repository's SIT -> UAT PR."""
    repo = via_sit()

    _deploy()

    assert not [p for p in repo.prs if p.base.ref == "UAT" and p.head.ref != "SIT"]
    assert not any(w["branch"] == "UAT" for w in repo.writes)
    assert _names(repo, "UAT") == []


def test_the_preview_says_how_the_deploy_finishes(via_sit):
    note = deploy._uat_flow_note({"entries": [_chart("a", "1")]})
    assert "merging that PR deploys to UAT" in note
    assert note in _preview_text({}, "CONFIRM-X", "uat", "a:1", flow=note)
    assert deploy._uat_flow_note({"deployment_type": "dataflow"}) == "", "DF deploys are not helm UAT deploys"


def test_the_fake_reports_the_files_a_pr_changes():
    repo = FakeRepo({"SIT": {UAT_FILE: {"include": [1]}}, "UAT": {UAT_FILE: {"include": []}}})
    assert [f.filename for f in FakePR(repo, "SIT", "UAT").get_files()] == [UAT_FILE]


# --- an open PR first, and several developers at once -----------------------------

def test_an_open_sit_to_uat_pr_must_be_merged_before_the_next_deploy(via_sit):
    """Asked live: if a PR is already open, the developer is told to merge it
    first — and once it is merged, they can deploy."""
    repo = via_sit()
    first = _deploy()
    held = next(p for p in repo.prs if p.head.ref == "SIT" and p.base.ref == "UAT")

    refused = _deploy("payments-api", "2.0.0")
    assert refused["action"] == "blocked" and f"PR #{held.number}" in refused["error"]
    assert _names(repo, "SIT") == ["orders-api:1.2.3"], "the refused deploy changed nothing"

    held.merge()                                    # the developer merges it: orders-api reaches UAT
    assert _names(repo, "UAT") == ["orders-api:1.2.3"]

    second = _deploy("payments-api", "2.0.0")
    assert second["action"] == "pending_review" and second["pending_prs"][0]["final"]
    assert first["pending_prs"][0]["number"] != second["pending_prs"][0]["number"]


def test_two_developers_at_once_one_deploys_and_the_other_is_told_to_wait(via_sit):
    """The running marker is held until the SIT -> UAT PR is there to show, so a
    second change cannot land on SIT in between and ride along unseen."""
    import contextvars
    import threading

    from release_agent import identity

    repo = via_sit()
    inside, release = threading.Event(), threading.Event()
    real = P._sit_to_uat_pr

    def slow_workflow(*a, **k):                     # the repo's workflow taking its time
        inside.set()
        release.wait(5)
        return real(*a, **k)
    P._sit_to_uat_pr = slow_workflow
    try:
        with identity.activate(identity.Caller(email="alice@example.com")):
            ctx = contextvars.copy_context()
        results = {}
        alice = threading.Thread(target=lambda: ctx.run(lambda: results.setdefault("alice", _deploy())))
        alice.start()
        assert inside.wait(5)
        results["bob"] = _deploy("payments-api", "2.0.0")
        release.set()
        alice.join(5)
    finally:
        P._sit_to_uat_pr = real

    assert results["bob"]["action"] == "blocked"
    assert "alice@example.com is deploying to UAT right now" in results["bob"]["error"]
    assert results["alice"]["action"] == "pending_review"
    assert _names(repo, "SIT") == ["orders-api:1.2.3"], "only alice's change is on SIT"


def test_an_open_sit_to_uat_pr_for_other_files_is_the_one_handed_over(via_sit):
    """GitHub allows one open SIT -> UAT PR: if one is open for other files, the
    deploy's change joins it, and that PR is the one shown."""
    repo = via_sit(workflow=False)
    repo.files["SIT"]["scripts/other.sh"] = "echo other\n"
    other = FakeRepo.create_pull(repo, "Promote SIT to UAT", "other work", "SIT", "UAT")

    out = _deploy()

    assert out["action"] == "pending_review" and out["pending_prs"][0]["number"] == other.number


def test_via_sit_the_deploy_form_opens_on_sits_file(via_sit, monkeypatch):
    """SIT holds a change still waiting in its SIT -> UAT PR; the overwrite
    replaces SIT's file, so the developer must see SIT's, not UAT's."""
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi as A
    from release_agent.tools import _common

    repo = via_sit(workflow=False)
    repo.files["SIT"][UAT_FILE] = json.dumps({"include": [_chart("waiting-api", "3.0.0")]})
    monkeypatch.setattr(_common, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda name: repo))

    body = TestClient(A.app).get("/api/deploy-template", params={"env": "uat"}).json()

    assert body["branch"] == "SIT"
    assert [e["helm_chart_name"] for e in body["deployment"]["include"]] == ["waiting-api"]
