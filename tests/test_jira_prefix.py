"""Every commit and PR title an action creates starts with the JIRA typed on its
form — Deploy to CARE UAT, Deploy to DF UAT, CARE/DF Release — so commit
history reads "ABC-1234 <message>". Taken as typed (no lookup). A promotion of a
release has no form: it reuses the key the release was raised with. Chat actions
without a form (queue removals, requeue from history) carry none.
"""
import json
from types import SimpleNamespace

import pytest

from adk_release_agent import deploy
from adk_release_agent.deploy_workflow import _preview_text
from release_agent.agent.parsing import _try_parse_json_payload
from release_agent.config import settings
from release_agent.tools import attribution
from release_agent.tools import promotion as P
from release_agent.tools import release_fileset as RF
from tests.fakes import FakeRepo

KEY = "ABC-1234"


# --- the helpers -----------------------------------------------------------------

def test_titles_and_commit_messages_lead_with_the_key_once():
    with attribution.jira(KEY):
        assert attribution.titled("Deploy x:1 to uat") == f"{KEY} Deploy x:1 to uat"
        assert attribution.titled(f"{KEY} already led") == f"{KEY} already led", "never twice"
        assert attribution.commit_message("chore(release): update f").startswith(f"{KEY} chore(release)")
        pr = SimpleNamespace(title=f"{KEY} Release 31", number=7)
        assert attribution.merge_kwargs(pr)["commit_title"] == f"{KEY} Release 31 (#7)"


def test_without_a_key_nothing_changes():
    assert attribution.titled("Remove x from uat") == "Remove x from uat"
    assert "commit_title" not in attribution.merge_kwargs(SimpleNamespace(title="t", number=1))


def test_the_key_is_trimmed_and_scoped_to_one_action():
    with attribution.jira("  ABC-9 "):
        assert attribution.current_jira() == "ABC-9"
    assert attribution.current_jira() == ""


# --- a UAT deploy: every PR, commit and merge ------------------------------------

@pytest.fixture
def deploy_repo(monkeypatch):
    repo = FakeRepo({b: {"uat/deployment.json": {"include": []}} for b in ("SIT", "UAT")})
    monkeypatch.setattr(settings, "sit_branch", "SIT")
    monkeypatch.setattr(settings, "uat_branch", "UAT")
    monkeypatch.setattr(P, "_find_deploy_run", lambda *a, **k: None)
    monkeypatch.setattr(P, "active_deploy_repo", lambda: "o/deploy")
    monkeypatch.setattr(P, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda name: repo))
    return repo


def _uat_request(jira=""):
    entry = {"helm_chart_name": "orders-api", "helm_chart_version": "1.2.3", "gke_namespace": "ns"}
    return {"environment": "uat", "entries": [entry], "images": [{"name": "orders-api", "tag": "1.2.3"}],
            **({"jira": jira} if jira else {})}


def test_a_uat_deploy_leads_every_pr_commit_and_merge_with_the_forms_jira(deploy_repo):
    out = deploy._apply(_uat_request(KEY), "uat", "CONFIRM-A1B2C3")

    assert out["ok"] and out["action"] == "deployed"
    assert [p.base.ref for p in deploy_repo.prs] == ["SIT", "UAT"]
    assert all(p.title.startswith(f"{KEY} Deploy orders-api:1.2.3") for p in deploy_repo.prs)
    assert deploy_repo.writes and all(w["msg"].startswith(f"{KEY} ") for w in deploy_repo.writes)
    assert all(p.merge_title.startswith(f"{KEY} ") for p in deploy_repo.prs)
    assert attribution.current_jira() == "", "the key does not outlive the action"


def test_a_change_without_a_form_carries_no_key(deploy_repo):
    deploy._apply(_uat_request(), "uat", "CONFIRM-D4E5F6")

    assert not any(p.title.startswith(KEY) for p in deploy_repo.prs)
    assert not any(w["msg"].startswith(KEY) for w in deploy_repo.writes)


# --- a release: required, kept out of release_details.json, reused by promotions --

def test_a_release_without_a_jira_is_refused():
    _, errors = RF.validate_release({"release_name": "R", "start_date": "2026-10-01 10:00:00",
                                     "end_date": "2026-10-01 12:00:00", "change_initiator": "a@b.c",
                                     "change_summary": "R", "artefact": ["svc-a:1.0.0"]})
    assert any("'jira' is required" in e for e in errors)


def _release_repo(marker):
    files = ["artefact-provider/artefact.json"]
    repo = FakeRepo({"release/rel-31": {files[0]: '{"artefact": ["u1"]}'},
                     "SIT": {files[0]: '{"artefact": ["u1"]}'}, "UAT": {files[0]: '{"artefact": []}'}})
    repo.create_pull(title=f"{KEY} Release 31",
                     body=f"{RF._FILES_MARKER} {json.dumps(files)}\n" + marker,
                     head="release/rel-31", base="SIT")
    return repo


def test_a_promotion_reuses_the_jira_the_release_was_raised_with(monkeypatch):
    repo = _release_repo(f"{RF._JIRA_MARKER} {KEY}\n")
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))

    out = json.loads(RF.promote_release.invoke({"target": "uat"}))

    promo = repo.prs[-1]
    assert out["ok"] and promo.base.ref == "UAT"
    assert promo.title.startswith(f"{KEY} ") and promo.title.count(KEY) == 1
    assert all(w["msg"].startswith(f"{KEY} Promote release file") for w in repo.writes)
    assert attribution.current_jira() == ""


def test_a_release_raised_before_the_jira_field_promotes_without_one(monkeypatch):
    repo = _release_repo("")
    repo.prs[0].title = "Release 31"
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))

    json.loads(RF.promote_release.invoke({"target": "uat"}))

    assert not repo.prs[-1].title.startswith(KEY)


# --- from the form to the preview --------------------------------------------------

@pytest.mark.parametrize("payload", [
    {"environment": "uat", "include": [{"helm_chart_name": "a", "helm_chart_version": "1"}], "jira": " ABC-1234 "},
    {"deployment_type": "dataflow", "environment": "uat", "image": "a", "tag": "1", "jira": "ABC-1234"},
])
def test_the_forms_jira_is_kept_for_the_apply(payload):
    assert _try_parse_json_payload(json.dumps(payload))["jira"] == KEY


def test_the_preview_says_which_jira_the_commits_will_start_with():
    text = _preview_text({"uat/deployment.json": {}}, "CONFIRM-X", "uat", "a:1", jira=KEY)
    assert f"**JIRA:** `{KEY}`" in text
    assert "JIRA" not in _preview_text({}, "CONFIRM-X", "uat", "a:1")
