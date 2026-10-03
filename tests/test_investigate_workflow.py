"""The Investigate button: collect in code, then ONE capped model step.

Real ADK runtime (the Workflow, sessions, the chat and deploy lanes); only the
model is scripted and the evidence module is a fake (its real one is built
apart, against the contract in adk_release_agent/investigate_workflow.py).
What must hold (AGENTS.md invariants 1 and 6):

- an "Investigate incident ..." message is recognised without a model and
  reaches its own lane, never the classifier or the chat agent;
- it is read-only, so it runs while a CONFIRM token or a yes/no approval is
  pending and leaves both exactly as they were;
- the model sees the evidence once, and never more than the cap's worth of
  calls; LLM off, a failing model and a spent cap all still answer with the
  evidence — never an error after the evidence was gathered;
- the run is iterated to its natural end (no run finalised later by the GC).
"""
from __future__ import annotations

import asyncio
import gc
import json
import logging

import pytest
from google.adk import Agent
from google.adk.apps import App, ResumabilityConfig
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.tools import FunctionTool
from google.genai import types

from adk_release_agent import agent as agent_module
from adk_release_agent import approvals, deploy, investigate_workflow
from release_agent import adk_service, features
from release_agent.adk_service import AdkChatService
from release_agent.agent import parsing
from release_agent.config import settings

BUTTON = ("Investigate incident e-1a2b3c4d · COB 2026-10-03 · REPORT-B failed · source sys-b-fetcher "
          "· jobs j-1, j-2. Why did it fail, and what should L1 do?")

BUNDLE = {
    "ok": True, "business_date": "2026-10-03", "window": {"start": "a", "end": "b"},
    "incident": {"id": "e-1a2b3c4d", "title": "REPORT-B failed", "action": "Re-run after the fix",
                 "owner": "Team B", "note": "Fetch step failed"},
    "evidence": {"metrics": {}, "logs": {}, "audit": {}, "dataflow": [], "changes": {}},
    "unavailable": [{"step": "audit", "error": "no access", "hint": "needs the Logs Viewer role"}],
    "timeline": [{"time": "02:10", "kind": "log", "source": "sys-b-fetcher", "text": "403 on fetch"}],
    "checked": ["Read the source's logs: 3 error groups", "Read the Dataflow job: out of memory"],
    "signals": {"leads": ["Dataflow job ran out of memory"]}, "elapsed_ms": 12,
}


class _Finder(BaseLlm):
    """The finding step's model: writes ``reply``, fails, or keeps asking for a tool."""

    model: str = "scripted-finder"
    reply: str = "FINDING: the Dataflow job ran out of memory."
    fail: bool = False
    tool: str = ""
    calls: list = []

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.calls.append(llm_request)
        if self.fail:
            raise RuntimeError("503 model overloaded")
        if self.tool:
            part = types.Part(function_call=types.FunctionCall(name=self.tool, args={"question": "why?"}))
        else:
            part = types.Part(text=self.reply)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


class _Evidence:
    """A stand-in for release_agent.tools.investigation."""

    def __init__(self, bundle=None, boom: Exception | None = None):
        self.bundle = BUNDLE if bundle is None else bundle
        self.boom = boom
        self.collected: list = []

    def collect(self, business_date, incident_id="", **kw):
        self.collected.append((business_date, incident_id, kw))
        if self.boom:
            raise self.boom
        return self.bundle

    def for_model(self, bundle):
        return {"incident": bundle["incident"], "checked": bundle["checked"],
                "leads": bundle["signals"]["leads"], "timeline": bundle["timeline"]}

    def render_markdown(self, bundle):
        return "## Evidence summary\n" + "\n".join(f"- {c}" for c in bundle["checked"])


@pytest.fixture
def evidence(monkeypatch):
    fake = _Evidence()
    monkeypatch.setattr(investigate_workflow, "_inv", lambda: fake)
    monkeypatch.setattr(settings, "preview_users", "*")        # support-triage is a preview feature
    return fake


@pytest.fixture
def finder(monkeypatch):
    llm = _Finder(calls=[])
    monkeypatch.setattr(agent_module, "_model", lambda name="": llm)
    monkeypatch.setattr(agent_module, "advisor_tool", lambda: None)
    monkeypatch.setattr(settings, "llm_enabled", True)
    monkeypatch.setattr(settings, "investigate_max_model_calls", 4)
    monkeypatch.setattr(settings, "investigate_model", "scripted-finder")   # what the event names
    return llm


@pytest.fixture
def no_other_lane(monkeypatch):
    """A message for the investigate lane must not be classified or chatted."""
    def boom(*a, **k):
        raise AssertionError("the investigate lane must not reach the classifier or the chat agent")

    monkeypatch.setattr(adk_service.adk_intent, "deploy_payload_from_freeform", boom)
    monkeypatch.setattr(AdkChatService, "_run_chat_agent", boom)


def _chat(service, message, thread="t-inv"):
    async def run():
        return [e async for e in service.stream_chat(message, thread)]
    return asyncio.run(run())


def _text(events) -> str:
    return "".join(e.get("content") or "" for e in events if e.get("type") == "token")


def _types(events) -> list[str]:
    return [e["type"] for e in events]


# --- the parser -----------------------------------------------------------------

@pytest.mark.parametrize("message,expected", [
    (BUTTON, {"incident_id": "e-1a2b3c4d", "business_date": "2026-10-03"}),
    ("investigate INCIDENT e-9 · COB 2026-01-31 · x", {"incident_id": "e-9", "business_date": "2026-01-31"}),
    ("  Investigate incident abc-123 (2026-10-03).", {"incident_id": "abc-123", "business_date": "2026-10-03"}),
    ("Investigate incident e-1 · no date here", {"incident_id": "e-1", "business_date": ""}),
    ("Investigate incident e-1 · 2026-13-40 · then 2026-02-28", {"incident_id": "e-1", "business_date": "2026-02-28"}),
    ("Investigate incident e-1 · 20261003 · 2026-1-3", {"incident_id": "e-1", "business_date": ""}),
])
def test_the_button_message_is_recognised(message, expected):
    assert parsing.investigate_request(message) == expected


@pytest.mark.parametrize("message", [
    "why did REPORT-B fail",
    "Please investigate incident e-1 2026-10-03",         # must START with the prefix
    "Investigate incident",
    "Investigate incident ? · COB 2026-10-03",             # the button's "no id" placeholder
    "Investigate incident · COB 2026-10-03",
    "Investigate incidents e-1",
    "",
])
def test_anything_else_is_left_to_the_chat_lane(message):
    assert parsing.investigate_request(message) is None


# --- the lane -------------------------------------------------------------------

def test_the_button_runs_the_pipeline_and_the_model_sees_the_evidence_once(evidence, finder, no_other_lane):
    events = _chat(AdkChatService(), BUTTON)

    assert evidence.collected == [("2026-10-03", "e-1a2b3c4d", {})]
    kinds = _types(events)
    assert kinds[0] == "progress" and events[0]["content"] == "Collecting evidence"
    progress = [e["content"] for e in events if e["type"] == "progress"]
    assert progress[1:3] == BUNDLE["checked"]
    assert progress[-1] == "Writing the finding"
    assert _text(events) == finder.reply
    assert kinds[-2:] == ["investigation", "done"] and events[-1] == {"type": "done", "mutated": False}
    assert kinds.index("token") < kinds.index("investigation")

    assert len(finder.calls) == 1                                    # ONE model step
    sent = json.dumps([c.model_dump(mode="json") for r in finder.calls for c in r.contents])
    assert "Dataflow job ran out of memory" in sent and "403 on fetch" in sent
    assert "e-1a2b3c4d" in sent
    assert "no tool that reads anything" in str(finder.calls[0].config.system_instruction)


def test_the_investigation_event_carries_what_the_feedback_needs(evidence, finder, no_other_lane):
    events = _chat(AdkChatService(), BUTTON)
    data = next(e["data"] for e in events if e["type"] == "investigation")
    assert set(data) == {"business_date", "incident_id", "title", "model", "model_calls", "seconds"}
    assert data["business_date"] == "2026-10-03" and data["incident_id"] == "e-1a2b3c4d"
    assert data["title"] == "REPORT-B failed"
    assert data["model"] == "scripted-finder" and data["model_calls"] == 1
    assert isinstance(data["seconds"], float)


def test_a_non_preview_user_is_refused_before_anything_is_read(evidence, finder, no_other_lane, monkeypatch):
    monkeypatch.setattr(settings, "preview_features", "support-triage")
    monkeypatch.setattr(settings, "preview_users", "tester@example.com")
    events = _chat(AdkChatService(), BUTTON)
    assert _text(events) == features.refusal("support-triage")
    assert _types(events) == ["token", "done"]
    assert evidence.collected == [] and finder.calls == []


def test_the_model_off_answers_with_the_evidence_and_never_calls_a_model(evidence, finder, no_other_lane,
                                                                        monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", False)
    events = _chat(AdkChatService(), BUTTON)
    assert "## Evidence summary" in _text(events) and BUNDLE["checked"][0] in _text(events)
    assert finder.calls == []
    data = next(e["data"] for e in events if e["type"] == "investigation")
    assert data["model_calls"] == 0 and data["model"] == ""
    assert "Writing the finding" not in [e.get("content") for e in events]
    assert events[-1] == {"type": "done", "mutated": False}


def test_a_failing_model_still_answers_with_the_evidence(evidence, finder, no_other_lane):
    finder.fail = True
    events = _chat(AdkChatService(), BUTTON)
    text = _text(events)
    assert "## Evidence summary" in text and "model step failed" in text
    assert "error" not in _types(events) and events[-1] == {"type": "done", "mutated": False}


def test_a_collect_that_raises_says_so_and_does_not_reach_the_model(evidence, finder, no_other_lane):
    evidence.boom = RuntimeError("logging API down")
    events = _chat(AdkChatService(), BUTTON)
    assert "Could not collect the evidence" in _text(events) and "logging API down" in _text(events)
    assert finder.calls == [] and "investigation" not in _types(events)
    assert events[-1]["type"] == "done"


def test_a_collect_that_says_not_ok_shows_its_error_and_hint(evidence, finder, no_other_lane):
    evidence.bundle = {"ok": False, "error": "No incident e-1a2b3c4d on 2026-10-03.",
                       "hint": "Open the triage card again."}
    events = _chat(AdkChatService(), BUTTON)
    text = _text(events)
    assert "No incident e-1a2b3c4d" in text and "Open the triage card again." in text
    assert finder.calls == [] and "investigation" not in _types(events)
    assert events[-1] == {"type": "done", "mutated": False}


# --- the cap --------------------------------------------------------------------

def test_the_cap_stops_the_model_and_falls_back_to_the_evidence(evidence, finder, no_other_lane, monkeypatch):
    """The model keeps asking the advisor; each consult is a model call too."""
    consulted: list = []

    def consult_advisor(question: str) -> dict:
        """Ask a stronger model."""
        consulted.append(question)
        return {"answer": "unsure"}

    monkeypatch.setattr(agent_module, "advisor_tool", lambda: FunctionTool(consult_advisor))
    monkeypatch.setattr(settings, "investigate_max_model_calls", 3)
    finder.tool = "consult_advisor"

    events = _chat(AdkChatService(), BUTTON)
    text = _text(events)
    assert "## Evidence summary" in text and "model-call limit (3) was reached" in text
    # model, advisor, model, advisor -> the 5th call would be a model call: refused at 3 spent
    assert len(finder.calls) + len(consulted) <= 3
    data = next(e["data"] for e in events if e["type"] == "investigation")
    assert data["model_calls"] <= 3
    assert "error" not in _types(events)


# --- pending approvals and tokens are untouched ------------------------------------

def test_it_runs_while_a_confirm_token_is_pending_and_leaves_it_usable(evidence, finder, monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    preview = _chat(service, "deploy abc-client-api-svc:1.1.1230 to uat", "t-tok")
    token = next(e["data"]["token"] for e in preview if e["type"] == "interrupt")
    assert service._pending_deploy[(adk_service._user_id(), "t-tok")] == token

    events = _chat(service, BUTTON, "t-tok")

    assert _text(events) == finder.reply and "investigation" in _types(events)
    assert "still waiting" not in _text(events) and "Replaced" not in _text(events)
    assert service._pending_deploy[(adk_service._user_id(), "t-tok")] == token       # not consumed
    assert token in deploy._PENDING_PREVIEWS                                         # not cancelled
    # ...and it is still answerable: "no" cancels exactly this token.
    cancelled = _chat(service, "no", "t-tok")
    assert f"Cancelled `{token}`" in _text(cancelled)


class _Promote(BaseLlm):
    model: str = "scripted-chat"
    calls: list = []

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.calls.append(1)
        answered = any(p.function_response for c in llm_request.contents for p in (c.parts or []))
        part = (types.Part(text="Reported.") if answered
                else types.Part(function_call=types.FunctionCall(name="promote_release", args={"target": "prd"})))
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def test_it_runs_while_a_yes_no_approval_is_pending_and_leaves_it_answerable(evidence, finder, monkeypatch):
    monkeypatch.setattr(adk_service.adk_intent, "deploy_payload_from_freeform", lambda message: None)
    ran: list = []

    def promote_release(target: str) -> dict:
        """Promote the current release to ``target``."""
        ran.append(target)
        return {"ok": True, "note": f"Promoted to {target.upper()}"}

    service = AdkChatService()
    app = App(name="inv_chat", root_agent=Agent(name="release_copilot_adk", model=_Promote(calls=[]),
                                                instruction="test", tools=[
        FunctionTool(promote_release, require_confirmation=approvals.RULES["promote_release"])]),
              resumability_config=ResumabilityConfig(is_resumable=True))
    service.chat_runner = Runner(app=app, session_service=service.session_service,
                                 artifact_service=service.artifact_service,
                                 memory_service=service.memory_service, auto_create_session=True)

    asked = _chat(service, "promote the release to prd", "t-appr")
    assert any(e["type"] == "interrupt" for e in asked)
    key = (adk_service._user_id(), "t-appr")
    assert key in service._pending_adk_calls

    events = _chat(service, BUTTON, "t-appr")

    assert _text(events) == finder.reply and "still waiting" not in _text(events)
    assert key in service._pending_adk_calls and ran == []          # neither consumed nor run
    approved = _chat(service, "yes", "t-appr")
    assert approved[-1] == {"type": "done", "mutated": True} and ran == ["prd"]


# --- the run is finished, not abandoned ---------------------------------------------

def test_the_run_is_iterated_to_its_end_and_leaves_nothing_to_be_finalised_later(evidence, finder, caplog):
    service = AdkChatService()
    with caplog.at_level(logging.DEBUG):
        _chat(service, BUTTON, "t-close")
        gc.collect()
        finder.fail = True
        _chat(service, BUTTON, "t-close2")
        gc.collect()
    noisy = [r for r in caplog.records
             if "Failed to detach context" in r.getMessage() or "leftover tasks" in r.getMessage()]
    assert not noisy, [f"{r.name}: {r.getMessage()[:80]}" for r in noisy]
    assert investigate_workflow._RUNS == {}                      # no run record is left behind


# --- follow-ups in the chat lane -----------------------------------------------------

class _Echo(BaseLlm):
    model: str = "scripted-chat"
    seen: list = []

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.seen.append(str(llm_request.config.system_instruction))
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="ok")]))


def test_the_finding_reaches_the_chat_lane_for_follow_ups(evidence, finder, monkeypatch):
    monkeypatch.setattr(adk_service.adk_intent, "deploy_payload_from_freeform", lambda message: None)
    service = AdkChatService()
    echo = _Echo(seen=[])
    service.chat_runner = Runner(
        app=App(name="inv_chat2", root_agent=Agent(name="release_copilot_adk", model=echo,
                                                    instruction=agent_module._root_instruction, tools=[])),
        session_service=service.session_service, artifact_service=service.artifact_service,
        memory_service=service.memory_service, auto_create_session=True)

    _chat(service, "show me the worker log lines", "t-follow")           # no investigation yet
    _chat(service, BUTTON, "t-follow")
    _chat(service, "show me the worker log lines", "t-follow")

    assert "FINDING" not in echo.seen[0]
    assert "FINDING: the Dataflow job ran out of memory." in echo.seen[1]
    assert "e-1a2b3c4d" in echo.seen[1] and "2026-10-03" in echo.seen[1]
    assert "DATA" in echo.seen[1]                                         # quoted, never obeyed


# --- the scripts' entry points ------------------------------------------------------

def test_answer_from_bundle_runs_only_the_finding_step(evidence, finder):
    result = asyncio.run(investigate_workflow.answer_from_bundle(BUNDLE, thread_id="eval-1"))
    assert result["text"] == finder.reply and result["fallback"] is False
    assert result["model"] == "scripted-finder" and result["model_calls"] == 1
    assert evidence.collected == []                                      # no collect
    assert set(result) >= {"text", "model", "model_calls", "seconds", "fallback"}


def test_answer_from_bundle_with_the_model_off_is_the_summary(evidence, finder, monkeypatch):
    monkeypatch.setattr(settings, "llm_enabled", False)
    result = asyncio.run(investigate_workflow.answer_from_bundle(BUNDLE))
    assert "## Evidence summary" in result["text"]
    assert result["model_calls"] == 0 and result["fallback"] is True and finder.calls == []


def test_run_once_is_collect_then_the_same_finding(evidence, finder):
    result = asyncio.run(investigate_workflow.run_once("2026-10-03", "e-1a2b3c4d"))
    assert evidence.collected == [("2026-10-03", "e-1a2b3c4d", {})]
    assert result["text"] == finder.reply and result["bundle"] == BUNDLE and result["model_calls"] == 1


# --- the chat lane's one-call tool ------------------------------------------------------

def test_the_chat_tool_returns_the_pruned_bundle_and_is_gated(monkeypatch):
    from adk_release_agent import tools

    fake = _Evidence()
    monkeypatch.setattr(settings, "preview_users", "*")
    from release_agent.tools import investigation

    monkeypatch.setattr(investigation, "collect", fake.collect)
    monkeypatch.setattr(investigation, "for_model", fake.for_model)
    got = tools.investigate_evidence("2026-10-03", "e-1a2b3c4d", job_ids=["j-1"])
    assert got["leads"] == ["Dataflow job ran out of memory"]
    assert fake.collected == [("2026-10-03", "e-1a2b3c4d",
                               {"source": "", "process": "", "job_ids": ["j-1"]})]

    monkeypatch.setattr(settings, "preview_features", "support-triage")
    monkeypatch.setattr(settings, "preview_users", "tester@example.com")
    refused = tools.investigate_evidence("2026-10-03", "e-1a2b3c4d")
    assert refused["ok"] is False and "preview feature" in refused["error"]
    assert len(fake.collected) == 1


def test_the_skill_unlocks_the_one_call_tool_first():
    import pathlib

    from google.adk.skills import load_skill_from_dir

    skill = load_skill_from_dir(pathlib.Path(agent_module.__file__).parent / "skills" / "support-investigate")
    names = skill.frontmatter.metadata["adk_additional_tools"]
    assert names[0] == "investigate_evidence" and "source_logs" in names


def test_the_chat_tool_refuses_a_call_that_names_nothing(monkeypatch):
    from adk_release_agent import tools
    from release_agent.tools import investigation

    monkeypatch.setattr(settings, "preview_users", "*")
    monkeypatch.setattr(investigation, "collect",
                        lambda *a, **k: pytest.fail("nothing named: no evidence may be read"))
    out = tools.investigate_evidence("2026-10-03")
    assert out["ok"] is False and "support_triage" in out["hint"]


# --- with the real evidence module: the shapes the collector really returns ---------------

def _real_inv(monkeypatch, bundle):
    """The real for_model / render_markdown on a bundle the fake collect returns."""
    from release_agent.tools import investigation

    monkeypatch.setattr(investigation, "collect", lambda *a, **k: bundle)
    monkeypatch.setattr(investigate_workflow, "_inv", lambda: investigation)
    monkeypatch.setattr(settings, "preview_users", "*")


def test_a_stuck_incident_has_no_shared_or_error_index_and_still_investigates(finder, no_other_lane,
                                                                              monkeypatch):
    stuck = {"ok": True, "business_date": "2026-10-03", "window": {"start": "a", "end": "b"},
             "incident": {"id": "stuck", "kind": "stuck", "title": "1 stuck", "action": "wait",
                          "owner": "Platform L2", "note": "One run is stuck"},
             "evidence": {}, "unavailable": [], "timeline": [], "checked": ["Control table: 1 stuck"],
             "signals": {"leads": []}, "elapsed_ms": 3}
    _real_inv(monkeypatch, stuck)
    events = _chat(AdkChatService(), "Investigate incident stuck · COB 2026-10-03 · 1 stuck")
    assert _text(events) == finder.reply and len(finder.calls) == 1
    assert next(e["data"] for e in events if e["type"] == "investigation")["incident_id"] == "stuck"

    finder.calls.clear()
    monkeypatch.setattr(settings, "llm_enabled", False)
    off = _chat(AdkChatService(), "Investigate incident stuck · COB 2026-10-03 · 1 stuck", "t-stuck-off")
    assert "Control table: 1 stuck" in _text(off) and finder.calls == []


def test_a_failed_collect_in_the_collectors_own_shape_is_explained(finder, no_other_lane, monkeypatch):
    _real_inv(monkeypatch, {"ok": False, "error": "No incident e-1a2b3c4d on 2026-10-03.",
                            "hint": "Open the triage card again.", "business_date": "2026-10-03"})
    events = _chat(AdkChatService(), BUTTON, "t-failed")
    assert "No incident e-1a2b3c4d" in _text(events) and "Open the triage card again." in _text(events)
    assert finder.calls == [] and "investigation" not in _types(events)


def test_the_advisor_is_for_doubt_not_for_an_open_and_shut_case():
    """Seen in the eval: two consults on a plain out-of-memory failure."""
    from adk_release_agent import investigate_workflow as iw

    clear = {"signals": {"job_kinds": ["out_of_memory"], "source_healthy": True, "security_in_logs": 0}}
    doubt = {"signals": {"job_kinds": ["unknown"], "source_healthy": True, "security_in_logs": 0}}
    clash = {"signals": {"job_kinds": ["not_found"], "source_healthy": True, "security_in_logs": 20}}
    two = {"signals": {"job_kinds": ["quota", "schema"]}}
    assert iw._evidence_conflicts(clear) is False and iw._evidence_conflicts(doubt) is False
    assert iw._evidence_conflicts(clash) is True and iw._evidence_conflicts(two) is True


def test_ten_people_on_one_incident_cost_one_model_call_and_see_one_answer(evidence, finder, no_other_lane):
    """Load test: ten separate model calls, throttled by the sandbox, slowest 59 s."""
    service = AdkChatService()

    async def crowd():
        async def one(i):
            return [e async for e in service.stream_chat(BUTTON, f"t-crowd-{i}")]
        return await asyncio.gather(*(one(i) for i in range(10)))

    runs = asyncio.run(crowd())

    assert len(finder.calls) == 1 and len(evidence.collected) == 1
    assert {_text(events) for events in runs} == {finder.reply}
    shared = [next(e["data"] for e in events if e["type"] == "investigation").get("shared") for events in runs]
    assert shared.count(True) == 9 and shared.count(None) == 1
    waited = [any("already investigating" in (e.get("content") or "") for e in events) for events in runs]
    assert any(waited)
    assert all(events[-1] == {"type": "done", "mutated": False} for events in runs)


def test_a_fallback_answer_is_not_shared_so_the_next_person_gets_a_real_one(evidence, finder, no_other_lane):
    service = AdkChatService()
    finder.fail = True
    first = _chat(service, BUTTON, "t-fb-1")
    finder.fail = False
    second = _chat(service, BUTTON, "t-fb-2")
    assert _text(second) == finder.reply and _text(first) != finder.reply
    assert next(e["data"] for e in second if e["type"] == "investigation").get("shared") is None
