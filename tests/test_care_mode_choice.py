"""The CARE release type chosen on the form: mono repo (one file, a PR a
person merges) or deployment repo (the file-set, landing on SIT).

The server's CARE_RELEASE_MODE is the default; the form offers only the modes
this deployment is set up for, and the choice travels with the release — the
defaults, the draft, the preview and the confirm all follow it.
"""
import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from adk_release_agent import chg_draft
from release_agent import app_fastapi as A
from release_agent.tools import _common
from release_agent.tools import care_release as CR
from release_agent.tools import chg_defaults as C
from release_agent.tools import git_snapshot as G
from release_agent.tools import release_fileset as RF
from release_agent.tools import release_queue as RQ

MONO = "example-org/mono-repo"
DEPLOY = "example-org/deploy-repo"
ART = "https://artifactory.example.com/docker/example-ds"


@pytest.fixture
def both(monkeypatch):
    """Set up for both types; the server's default is the deployment repo."""
    for k, v in {"care_release_mode": "fileset", "care_release_repo": MONO, "deploy_repo": DEPLOY,
                 "care_release_name_format": "<TEAM> CARE Release - %Y.%m.%d",
                 "care_previous_tags_file": ""}.items():
        monkeypatch.setattr(C.settings, k, v, raising=False)
    monkeypatch.setattr(_common, "active_deploy_repo", lambda: DEPLOY)
    return C.settings


def _payload(**over):
    base = {"release_name": "<TEAM> CARE Release - 2026.10.01", "start_date": "2026-10-01 10:00:00",
            "end_date": "2026-10-02 23:00:00", "change_initiator": "someone@example.com", "jira": "ABC-1",
            "change_summary": "<TEAM> CARE Release - 2026.10.01", "change_description": "d",
            "change_reason": "r", "associated_risk": "Low risk.", "consequence": "c",
            "user_service_impact": "No user impact is expected.", "prl1_only": [], "df_images": [],
            "artefact": [f"{ART}/svc-a:5.0.470"], "release_kind": "care"}
    base.update(over)
    return base


# --- what the form is offered ------------------------------------------------

def test_both_set_up_offers_both_the_default_first(both):
    assert C.available_care_modes() == ["fileset", "mono"]
    both.care_release_mode = "mono"
    assert C.available_care_modes() == ["mono", "fileset"]


def test_a_mode_that_is_not_set_up_is_not_offered(both, monkeypatch):
    both.care_release_repo = ""
    assert C.available_care_modes() == ["fileset"], "no CARE_RELEASE_REPO: no mono"
    both.care_release_repo, both.care_release_mode = MONO, "mono"
    both.deploy_repo = ""
    monkeypatch.setattr(_common, "active_deploy_repo", lambda: "")
    assert C.available_care_modes() == ["mono"], "no deployment repo: no file-set"


def test_the_queue_context_lists_the_modes_on_offer(both, monkeypatch):
    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": []})
    monkeypatch.setattr(A, "_known_charts", lambda: [])
    r = TestClient(A.app).get("/api/release-queue").json()
    assert r["care_release_modes"] == ["fileset", "mono"] and r["care_release_mode"] == "fileset"


# --- one choice, read everywhere ---------------------------------------------

def test_the_choice_holds_for_the_block_and_nothing_else(both):
    assert C.care_mono("care") is False
    with C.care_mode("mono"):
        assert C.care_mono("care") is True and C.current_care_mode() == "mono"
        assert C.care_mono("df") is False, "DF is never mono"
    with C.care_mode("anything-else"):
        assert C.current_care_mode() == "fileset", "an unknown choice leaves the default in charge"
    assert C.current_care_mode() == "fileset"


def test_the_defaults_follow_the_chosen_type(both):
    with C.care_mode("mono"):
        d = C.build_defaults([{"name": "svc-a", "version": "5.0.470"}], "care", dt.date(2026, 10, 1))
    assert d["release_name"] == "<TEAM> CARE Release - 2026.10.01", "mono naming, though the server is file-set"


def test_the_defaults_endpoint_names_only_a_mono_choice(both, monkeypatch):
    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": []})
    ask = {"artifacts": [f"{ART}/svc-a:5.0.470"], "kind": "care", "repo": MONO, "date": "2026-10-01"}
    mono = TestClient(A.app).post("/api/release-defaults", json={**ask, "mode": "mono"}).json()
    assert mono["fields"]["release_name"] == "<TEAM> CARE Release - 2026.10.01", "the team's format"
    fileset = TestClient(A.app).post("/api/release-defaults", json={**ask, "mode": "fileset"}).json()
    assert fileset["fields"]["release_name"] == "", "the release manager names a file-set release"


def test_the_draft_follows_the_chosen_type(both, monkeypatch):
    monkeypatch.setattr(chg_draft, "_draft_mono", lambda items: {"ok": True, "via": "mono"})
    monkeypatch.setattr(chg_draft, "_draft_fileset", lambda items, kind: {"ok": True, "via": "fileset"})
    assert chg_draft.draft_change_request([], "care")["via"] == "fileset"
    with C.care_mode("mono"):
        assert chg_draft.draft_change_request([], "care")["via"] == "mono"
    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": [
        {"artifact_name": "svc-a", "artifact_version": "5.0.470"}]})
    r = TestClient(A.app).post("/api/release-draft", json={
        "artifacts": ["svc-a:5.0.470"], "kind": "care", "mode": "mono"}).json()
    assert r["via"] == "mono"


# --- the release follows it, from preview to confirm --------------------------

def test_choosing_mono_raises_the_mono_pr_though_the_default_is_file_set(both, monkeypatch):
    seen = []
    monkeypatch.setattr(CR, "prepare", lambda payload, details: seen.append(payload) or {"ok": True, "mode": "mono"})
    out = RF.prepare_release_fileset(_payload(care_release_mode="mono"))
    assert out == {"ok": True, "mode": "mono"} and len(seen) == 1


def test_choosing_the_deployment_repo_takes_the_file_set_though_the_default_is_mono(both, monkeypatch):
    both.care_release_mode = "mono"
    cloned = []
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: object()))
    monkeypatch.setattr(RF, "_resolve_github_token", lambda: "")
    monkeypatch.setattr(RF, "_open_prd_pr_blocker", lambda repo, **kw: None)
    monkeypatch.setattr(RF.settings, "sit_branch", "SIT", raising=False)

    def clone(url, branch, dest, token):
        cloned.append(branch)
        raise RuntimeError("clone reached")

    monkeypatch.setattr(G, "clone_branch", clone)
    monkeypatch.setattr(CR, "prepare", lambda *a, **k: pytest.fail("chose the deployment repo, not mono"))
    out = RF.prepare_release_fileset(_payload(care_release_mode="fileset", deployment_repo=DEPLOY))
    assert out == {"ok": False, "errors": ["clone reached"]} and cloned == ["SIT"]


def test_a_type_that_is_not_set_up_is_refused(both, monkeypatch):
    both.care_release_repo = ""
    monkeypatch.setattr(CR, "prepare", lambda *a, **k: pytest.fail("mono is not set up"))
    out = RF.prepare_release_fileset(_payload(care_release_mode="mono"))
    assert out["ok"] is False and "not set up here" in out["errors"][0]


def test_confirm_regenerates_in_the_previewed_type_not_the_servers(both, monkeypatch):
    both.care_release_mode = "mono"          # the default changed after the preview
    asked = []
    monkeypatch.setattr(RF, "prepare_release_fileset",
                        lambda payload, _keep_workdir=False: asked.append(payload) or {"ok": False, "errors": ["stop"]})
    prep = {"mode": "fileset", "kind": "care", "deployment_repo": DEPLOY, "jira": "ABC-1",
            "details": {"release_name": "x"}, "fileset_hash": "h", "branch": "release/x"}
    RF.apply_release_fileset(prep)
    assert asked and asked[0]["care_release_mode"] == "fileset"
