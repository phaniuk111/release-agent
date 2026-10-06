"""LLM_ENABLED=false: the portal with no model behind it.

The chat answers the pills' commands through an ADK Workflow (commands_workflow)
— the same tools, the same yes/no rules (approvals.py) and the same pause and
resume as with the model on — and never reaches the chat agent or the
classifier. Deploys and releases keep their preview → CONFIRM token.
"""
import asyncio
from pathlib import Path

import pytest

from adk_release_agent import approvals, deploy
from adk_release_agent import tools as T
from release_agent import commands, identity
from release_agent.adk_service import AdkChatService
from release_agent.config import settings


@pytest.fixture
def service(monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", False)
    deploy._PENDING_PREVIEWS.clear()
    svc = AdkChatService()

    def no_model(*a, **k):
        raise AssertionError("no model may be reached with LLM_ENABLED=false")

    monkeypatch.setattr(svc, "_run_chat_agent", no_model)
    import release_agent.adk_service as S
    monkeypatch.setattr(S.adk_intent, "deploy_payload_from_freeform", no_model)
    return svc


@pytest.fixture
def promoted(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "promote_release",
                        lambda **kw: calls.append(kw) or {"ok": True, "note": "Promoted via PR #9."})
    return calls


def _chat(svc, message, thread="t-off"):
    async def run():
        return [e async for e in svc.stream_chat(message, thread)]

    return [e for e in asyncio.run(run()) if e.get("type") != "progress"]


def _text(events):
    return "".join(e.get("content") or "" for e in events if e.get("type") == "token")


def _interrupt(events):
    return next((e["data"] for e in events if e.get("type") == "interrupt"), None)


# --- recognising the commands ----------------------------------------------------

@pytest.mark.parametrize("message,name,args", [
    ("Show the current deploy status", "check_release_window", {}),
    ("promote the CARE release to prd", "promote_release", {"target": "prd"}),
    ("Promote the DF release to PRD", "promote_df_release", {"target": "prd"}),
    ("promote the care release to prl1", "promote_release", {"target": "prl1"}),
    ("what images can I promote?", "list_allowed_images", {}),
    ("show me the 10 most recent workflow runs", "get_recent_runs", {"limit": 10}),
    ("verify payments-api:1.2.3", "get_build_report", {"image": "payments-api", "tag": "1.2.3"}),
    ("verify payments-api:1.2.3 in acme/builds", "get_build_report",
     {"image": "payments-api", "tag": "1.2.3", "repo": "acme/builds"}),
    ("check build controls for payments-api:1.2.3", "get_build_report", {"image": "payments-api", "tag": "1.2.3"}),
    ("find the deployment PR for payments-api:1.2.3", "find_prs", {"search_term": "payments-api:1.2.3"}),
    ("track PR #123", "get_pr_details", {"pr_number": 123}),
    ("remove svc-a, svc-b from the release", "no_removal", {}),
    ("remove svc-a from uat", "no_removal", {}),
    ("remove svc-a from prod", "no_removal", {}),
])
def test_each_pill_phrase_is_the_tool_the_agent_would_call(message, name, args):
    command = commands.parse(message)
    assert command is not None and (command.name, command.args) == (name, args)


def _pill_texts():
    """label -> text of every pill that puts text in the chat, read from the page
    itself: a reworded pill must still be a command with the model off."""
    palette = (Path(__file__).parents[1] / "src/release_agent/static/palette.js").read_text()
    out = {}
    for line in palette.splitlines():
        if "label:'" in line and "text:'" in line:
            label = line.split("label:'", 1)[1].split("'", 1)[0]
            out[label] = line.split("text:'", 1)[1].rsplit("'", 1)[0]
    return out


@pytest.mark.parametrize("label,name,args", [
    ("Promote CARE release to PRD", "promote_release", {"target": "prd"}),
    ("Promote DF release to PRD", "promote_df_release", {"target": "prd"}),
    ("Deploy status", "check_release_window", {}),
    ("Verify a build", "get_build_report", {"image": "payments-api", "tag": "1.2.3", "repo": "acme/builds"}),
    ("Check PRD controls", "get_build_report", {"image": "payments-api", "tag": "1.2.3"}),
    ("Track a PR", "find_prs", {"search_term": "payments-api:1.2.3"}),
    ("List allowed images", "list_allowed_images", {}),
    ("Recent workflow runs", "get_recent_runs", {"limit": 5}),
    ("Consumer onboarding", "onboarding", {}),
])
def test_every_pill_as_the_page_sends_it_is_a_command(label, name, args):
    text = (_pill_texts()[label].replace("<image>:<tag>", "payments-api:1.2.3")
            .replace("<owner/repo>", "acme/builds"))
    command = commands.parse(text)
    assert command is not None and (command.name, command.args) == (name, args)


def test_every_text_pill_is_covered():
    covered = {"Promote CARE release to PRD", "Promote DF release to PRD", "Deploy status", "Verify a build",
               "Check PRD controls", "Track a PR", "List allowed images", "Recent workflow runs",
               "Consumer onboarding"}
    assert set(_pill_texts()) == covered, "a new text pill needs a command (or a line here)"


@pytest.mark.parametrize("message", ["hello", "what images can I promote?", "promote something"])
def test_nothing_is_guessed_a_phrase_without_a_target_never_promotes(message):
    command = commands.parse(message)
    assert command is None or not command.name.startswith("promote")


@pytest.mark.parametrize("message,needs", [
    ("promote the CARE release to prd", True),
    ("promote the DF release to prl1", True),
    ("promote the CARE release to uat", False),
    ("remove svc-a from the release", False),
])
def test_what_needs_a_yes_is_the_chat_agents_own_rule(message, needs):
    command = commands.parse(message)
    assert approvals.needs_approval(command.name, command.args) is needs


# --- the chat, with no model -------------------------------------------------------

def test_a_command_is_answered_by_its_tool_with_no_model(service, monkeypatch):
    monkeypatch.setattr(T, "check_release_window",
                        lambda: {"ok": True, "uat_charts": [{"name": "svc-a", "version": "1.2.3"}]})
    # The pill's own text through the real runner: the gate must be handed the
    # message as text (found live: typed Any it got the Content object, whose
    # repr "parts=[Part(" read as an image tag and became a PR search).
    events = _chat(service, _pill_texts()["Deploy status"])
    assert _text(events).startswith("**Deploy status**")
    assert "svc-a" in _text(events) and "1.2.3" in _text(events)
    assert events[-1] == {"type": "done", "mutated": False}


def test_free_text_is_told_what_the_chat_can_do(service):
    assert "LLM_ENABLED=false" in _text(_chat(service, "why did the payments deploy fail yesterday?"))


def test_onboarding_says_to_enable_llm_access(service):
    assert _text(_chat(service, "Help me onboard a new API consumer")) == commands.ONBOARDING_OFF


def test_a_prd_promotion_pauses_and_runs_once_on_yes(service, promoted):
    asked = _interrupt(_chat(service, "promote the CARE release to prd"))
    assert asked["function"] == "promote_release" and not asked.get("token"), "Approve/Reject, not a token"
    assert promoted == []

    reminder = _text(_chat(service, "what is in the release?"))
    assert "still waiting" in reminder and promoted == []

    done = _chat(service, "yes")
    assert "Promoted via PR #9." in _text(done) and done[-1]["mutated"] is True
    assert promoted == [{"target": "prd"}]

    _chat(service, "yes")                       # single use: nothing is waiting any more
    assert promoted == [{"target": "prd"}]


def test_no_rejects_and_nothing_runs(service, promoted):
    _chat(service, "promote the CARE release to prd")
    events = _chat(service, "no")
    assert "rejected" in _text(events) and events[-1]["mutated"] is False
    assert promoted == []


def test_a_uat_promotion_needs_no_yes(service, promoted):
    events = _chat(service, "promote the CARE release to uat")
    assert _interrupt(events) is None and promoted == [{"target": "uat"}]


def test_one_persons_yes_cannot_answer_another_persons_approval(service, promoted):
    alice, bob = identity.Caller(email="alice@example.com"), identity.Caller(email="bob@example.com")
    with identity.activate(alice):
        _chat(service, "promote the CARE release to prd", "t-shared")
    with identity.activate(bob):
        _chat(service, "yes", "t-shared")
    assert promoted == []
    with identity.activate(alice):
        _chat(service, "yes", "t-shared")
    assert promoted == [{"target": "prd"}]


def test_a_new_deploy_request_drops_the_pending_approval_and_is_previewed(service, promoted):
    _chat(service, "promote the CARE release to prd")
    events = _chat(service, "deploy abc-client-api-svc:1.1.1230 to uat")
    assert "Dropped the pending approval" in _text(events)
    assert _interrupt(events)["token"].startswith("CONFIRM-")
    _chat(service, "yes")
    assert promoted == [], "the dropped approval can no longer be answered"


def test_deploys_keep_their_preview_and_token(service, monkeypatch):
    token = _interrupt(_chat(service, "deploy abc-client-api-svc:1.1.1230 to uat"))["token"]
    applied = []
    monkeypatch.setattr(deploy, "_invoke_tool", lambda name, args: applied.append(name) or {"ok": True, "note": "ok"})
    events = _chat(service, token)
    assert applied == ["open_release_pr"] and events[-1]["mutated"] is True


# --- everything else a model did ---------------------------------------------------

def test_drafting_is_refused_without_a_model_call(monkeypatch):
    from fastapi.testclient import TestClient

    import google.genai as genai
    from release_agent import app_fastapi as A

    monkeypatch.setattr(settings, "llm_enabled", False)
    monkeypatch.setattr(genai, "Client", lambda *a, **k: pytest.fail("no model call"))
    r = TestClient(A.app).post("/api/release-draft", json={"kind": "df", "artifacts": ["x:1"]}).json()
    assert r["ok"] is False and r["disabled"] is True


def test_the_page_is_told_there_is_no_model(monkeypatch):
    from release_agent import features

    monkeypatch.setattr(settings, "llm_enabled", False)
    assert features.ui_config(None)["llm"] is False
