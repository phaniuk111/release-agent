# ruff: noqa: F811  (fixtures reused from test_llm_off are redefined by name in each test signature)
"""LLM_ENABLED=false on google-adk 2.14, through the real ADK Workflows.

2.12-2.14 moved sync Workflow nodes to a tool thread pool (opt-in through
``RunConfig.tool_thread_pool_config``), changed how a confirmed tool and a
rejected child are resumed, and cancel parallel tool tasks differently. None of
that may change what the portal promises with no model behind it (AGENTS.md
invariants 1 and 6). Only GitHub is faked; the router, the commands Workflow,
the deploy Workflow, the Investigate Workflow, sessions and the pause/resume
are the real ones:

- a yes/no approval is consumed by its answer, exactly once;
- a CONFIRM token is single-use and a new request replaces a pending one;
- a reader that leaves mid-turn does not stop a Workflow;
- Investigate answers from the evidence alone.
"""
import asyncio
import time

from adk_release_agent import deploy
from adk_release_agent import tools as T
from release_agent import adk_service
from release_agent.adk_service import AdkChatService
from release_agent.config import settings
from adk_release_agent import agent as agent_module
from adk_release_agent import investigate_workflow
from tests.test_investigate_workflow import BUNDLE, BUTTON, _Evidence, _Finder
from tests.test_llm_off import _chat, _interrupt, _text, promoted, service  # noqa: F401


def _applier(monkeypatch):
    applied: list = []
    monkeypatch.setattr(deploy, "_invoke_tool",
                        lambda name, args: applied.append((name, args)) or {"ok": True, "note": "applied"})
    return applied


# --- yes / no, each answer consumed once ------------------------------------------

def test_a_yes_runs_the_promotion_once_and_is_then_spent(service, promoted):
    assert _interrupt(_chat(service, "promote the CARE release to prd"))["function"] == "promote_release"
    assert _chat(service, "yes")[-1] == {"type": "done", "mutated": True}
    assert promoted == [{"target": "prd"}]
    assert (adk_service._user_id(), "t-off") not in service._pending_adk_calls
    for _ in range(2):
        _chat(service, "yes")
    assert promoted == [{"target": "prd"}]


def test_a_no_runs_nothing_and_a_later_yes_has_nothing_to_answer(service, promoted):
    _chat(service, "promote the CARE release to prd")
    assert _chat(service, "no")[-1] == {"type": "done", "mutated": False}
    _chat(service, "yes")
    assert promoted == []
    assert (adk_service._user_id(), "t-off") not in service._pending_adk_calls


# --- CONFIRM token ------------------------------------------------------------------

def test_a_confirm_token_is_single_use(service, monkeypatch):
    applied = _applier(monkeypatch)
    token = _interrupt(_chat(service, "deploy abc-client-api-svc:1.1.1230 to uat"))["token"]
    assert token.startswith("CONFIRM-") and applied == []

    assert _chat(service, token)[-1] == {"type": "done", "mutated": True}
    again = _chat(service, token)
    assert len(applied) == 1
    assert not any(e.get("mutated") for e in again) and not any(e["type"] == "error" for e in again)


def test_a_new_request_replaces_the_pending_token(service, monkeypatch):
    applied = _applier(monkeypatch)
    first = _interrupt(_chat(service, "deploy abc-client-api-svc:1.1.1230 to uat"))["token"]
    second_events = _chat(service, "deploy abc-client-api-svc:1.1.1231 to uat")
    second = _interrupt(second_events)["token"]
    assert second != first

    _chat(service, first)                       # the replaced token is dead
    assert applied == []
    assert _chat(service, second)[-1]["mutated"] is True
    assert [args["image_tags"] for _, args in applied] == ["abc-client-api-svc:1.1.1231"]


def test_a_new_request_replaces_a_pending_yes_no(service, promoted, monkeypatch):
    applied = _applier(monkeypatch)
    _chat(service, "promote the CARE release to prd")
    token = _interrupt(_chat(service, "deploy abc-client-api-svc:1.1.1230 to uat"))["token"]
    _chat(service, "yes")                       # answers the token's question, not the dropped promotion
    assert promoted == [] and applied == []
    assert _chat(service, token)[-1]["mutated"] is True and len(applied) == 1


# --- the reader leaves ---------------------------------------------------------------

def test_a_reader_who_leaves_mid_turn_does_not_stop_a_commands_workflow(service, monkeypatch):
    ran: list = []

    def slow_promote(**kw):
        time.sleep(0.3)                          # GitHub work, on a worker thread
        ran.append(kw)
        return {"ok": True, "note": "Promoted via PR #9."}

    monkeypatch.setattr(T, "promote_release", slow_promote)
    _chat(service, "promote the CARE release to prd")
    abort = asyncio.Event()

    async def scenario():
        seen = []
        async for event in service.stream_chat("yes", "t-off", abort_signal=abort):
            seen.append(event)
            abort.set()                          # gone as soon as the first event is out
        return seen

    seen = asyncio.run(scenario())
    assert ran == [{"target": "prd"}]
    assert "Promoted via PR #9." in _text(seen) and seen[-1] == {"type": "done", "mutated": True}


def test_a_reader_who_leaves_mid_preview_still_leaves_a_usable_token(service, monkeypatch):
    applied = _applier(monkeypatch)
    real = deploy.prepare_deploy_preview

    def slow(**kwargs):
        time.sleep(0.3)
        return real(**kwargs)

    monkeypatch.setattr(deploy, "prepare_deploy_preview", slow)
    abort = asyncio.Event()

    async def scenario():
        asyncio.get_running_loop().call_later(0.1, abort.set)
        return [e async for e in service.stream_chat(
            "deploy abc-client-api-svc:1.1.1230 to uat", "t-gone", abort_signal=abort)]

    seen = asyncio.run(scenario())
    token = next(e["data"]["token"] for e in seen if e["type"] == "interrupt")
    assert _chat(service, token, "t-gone")[-1]["mutated"] is True
    assert len(applied) == 1


# --- Investigate, no model -------------------------------------------------------------

def test_investigate_answers_from_the_evidence_with_no_model(monkeypatch):
    fake = _Evidence()
    monkeypatch.setattr(investigate_workflow, "_inv", lambda: fake)
    monkeypatch.setattr(settings, "preview_users", "*")
    monkeypatch.setattr(settings, "llm_enabled", False)

    def no_model(*a, **k):
        raise AssertionError("no model may be reached with LLM_ENABLED=false")

    monkeypatch.setattr(adk_service.adk_intent, "deploy_payload_from_freeform", no_model)
    monkeypatch.setattr(AdkChatService, "_run_chat_agent", no_model)
    # The finding agent is still built (a Workflow node) but its model must never be asked.
    llm = _Finder(fail=True, calls=[])
    monkeypatch.setattr(agent_module, "_model", lambda name="": llm)
    monkeypatch.setattr(agent_module, "advisor_tool", lambda: None)

    events = _chat(AdkChatService(), BUTTON, "t-inv")
    assert "## Evidence summary" in _text(events) and BUNDLE["checked"][0] in _text(events)
    data = next(e["data"] for e in events if e["type"] == "investigation")
    assert llm.calls == [] and data["model_calls"] == 0 and data["model"] == ""
    assert events[-1] == {"type": "done", "mutated": False}
