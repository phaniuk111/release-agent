"""What the portal relies on in google-adk 2.11, checked against the real
runtime — real Workflow, Runner, session service and tools; no model, no network.

These are the rules the 2.9 -> 2.11 upgrade touched (release notes: resume of
resolved interrupts, sibling outputs beside a failing node, tool confirmation,
ModelConsultTool budgets). If a later ADK changes one, the test names which
portal behaviour stands on it.
"""
import asyncio

import pytest
from google.adk.agents.invocation_context import InvocationContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.adk.tools.tool_confirmation import ToolConfirmation
from google.adk.tools.tool_context import ToolContext
from google.adk.workflow import FunctionNode, Workflow
from google.genai import types

from adk_release_agent import agent as agent_module
from adk_release_agent import approvals
from release_agent.config import settings


def _text(message: str) -> types.Content:
    return types.Content(role="user", parts=[types.Part.from_text(text=message)])


def _answer(interrupt_id: str, payload: dict) -> types.Content:
    return types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
        id=interrupt_id, name="adk_request_input", response=payload))])


def _runner(workflow) -> tuple[Runner, InMemorySessionService]:
    sessions = InMemorySessionService()
    app = App(name="adk211", root_agent=workflow, resumability_config=ResumabilityConfig(is_resumable=True))
    return Runner(app=app, session_service=sessions, auto_create_session=True), sessions


async def _run(runner: Runner, session_id: str, message: types.Content) -> list:
    return [e async for e in runner.run_async(user_id="u", session_id=session_id, new_message=message)]


def _paused_on(events) -> set[str]:
    return {i for e in events for i in (e.long_running_tool_ids or set())}


# --- Workflow: pause, resume, failure ------------------------------------------

def test_a_gate_with_rerun_on_resume_runs_again_and_reads_the_answer():
    """The deploy and commands gates are built on this: the node that paused is
    run AGAIN on resume and finds the person's answer under its interrupt id."""
    seen = []

    async def gate(ctx, node_input: str):
        seen.append(dict(ctx.resume_inputs or {}))
        if not ctx.resume_inputs:
            yield RequestInput(interrupt_id="CONFIRM-ABC123", message="ok?")
            return
        yield Event(output=ctx.resume_inputs["CONFIRM-ABC123"], route="confirmed")

    applied = []

    async def apply(node_input: dict):
        applied.append(node_input)
        yield Event(output={"ok": True})

    workflow = Workflow(name="w", edges=[("START", FunctionNode(func=gate, rerun_on_resume=True, name="gate"),
                                          {"confirmed": FunctionNode(func=apply, name="apply")})])
    runner, _ = _runner(workflow)

    async def scenario():
        first = await _run(runner, "s", _text("go"))
        assert _paused_on(first) == {"CONFIRM-ABC123"} and applied == []
        await _run(runner, "s", _answer("CONFIRM-ABC123", {"confirmed": True, "token": "CONFIRM-ABC123"}))

    asyncio.run(scenario())
    assert seen == [{}, {"CONFIRM-ABC123": {"confirmed": True, "token": "CONFIRM-ABC123"}}]
    assert applied == [{"confirmed": True, "token": "CONFIRM-ABC123"}]      # once


def test_a_resolved_interrupt_resumes_without_rerun_on_resume():
    """2.11: a node that paused WITHOUT rerun_on_resume is not run again — the
    answer becomes its output. Our gates must therefore keep rerun_on_resume=True:
    without it the gate's routing (confirmed / rejected) would never happen and
    the raw answer would flow to whatever node came next."""
    ran = {"ask": 0}
    received = []

    async def ask(ctx, node_input: str):
        ran["ask"] += 1
        yield RequestInput(interrupt_id="Q1", message="ok?")

    async def after(node_input: dict):
        received.append(node_input)
        yield Event(output={"done": True})

    ask_node = FunctionNode(func=ask, name="ask")
    workflow = Workflow(name="w", edges=[("START", ask_node), (ask_node, FunctionNode(func=after, name="after"))])
    runner, _ = _runner(workflow)

    async def scenario():
        assert _paused_on(await _run(runner, "s", _text("go"))) == {"Q1"}
        await _run(runner, "s", _answer("Q1", {"confirmed": False}))

    asyncio.run(scenario())
    assert ran["ask"] == 1
    assert received == [{"confirmed": False}]


def test_the_portals_gates_are_the_rerun_kind():
    from adk_release_agent.commands_workflow import build_commands_workflow
    from adk_release_agent.deploy_workflow import build_deploy_workflow

    for workflow, gate in ((build_deploy_workflow(), "deploy_gate"), (build_commands_workflow(), "command_gate")):
        nodes = {n.name: n for n in workflow.graph.nodes}
        assert nodes[gate].rerun_on_resume is True
        assert all(not n.rerun_on_resume for name, n in nodes.items() if name not in (gate, "START", "__START__")
                   and isinstance(n, FunctionNode))


def test_a_sibling_that_finished_beside_a_failing_node_keeps_its_output_and_state():
    """2.11 keeps what a sibling finished in the same tick as a failure. The
    failure itself still escapes run_async — which is why no portal node raises."""
    async def bad(node_input: str):
        raise RuntimeError("boom")
        yield  # pragma: no cover

    async def good(node_input: str):
        yield Event(output={"ok": 1}, state={"good_ran": True})

    workflow = Workflow(name="w", edges=[("START", FunctionNode(func=bad, name="bad")),
                                          ("START", FunctionNode(func=good, name="good"))])
    runner, sessions = _runner(workflow)
    outputs = []

    async def scenario():
        with pytest.raises(RuntimeError, match="boom"):
            async for event in runner.run_async(user_id="u", session_id="s", new_message=_text("go")):
                if event.output is not None:
                    outputs.append(event.output)
        return await sessions.get_session(app_name="adk211", user_id="u", session_id="s")

    session = asyncio.run(scenario())
    assert outputs == [{"ok": 1}]
    assert session.state.get("good_ran") is True


# --- FunctionTool confirmation (the yes/no on PRD/PRL1 promotions) ---------------

def _tool_context(session_id: str = "s", invocation_id: str = "inv-1", call_id: str = "call-1",
                  confirmation: ToolConfirmation | None = None, sessions=None, session=None) -> ToolContext:
    sessions = sessions or InMemorySessionService()
    if session is None:
        session = asyncio.run(sessions.create_session(app_name="adk211", user_id="u", session_id=session_id))
    ic = InvocationContext(session_service=sessions, invocation_id=invocation_id, session=session)
    return ToolContext(ic, function_call_id=call_id, tool_confirmation=confirmation)


def _promote_tool(monkeypatch) -> FunctionTool:
    monkeypatch.setattr(settings, "adk_confirm_prod_ops", True)
    monkeypatch.setattr(settings, "advisor_model", "")
    tools = {getattr(t, "name", getattr(t, "__name__", "")): t for t in agent_module._chat_additional_tools()}
    tool = tools["promote_release"]
    assert isinstance(tool, FunctionTool)
    return tool


def test_the_promotion_tool_asks_for_confirmation_only_on_a_terminal_target(monkeypatch):
    tool = _promote_tool(monkeypatch)
    ctx = _tool_context()

    async def ask(target):
        return await tool.check_require_confirmation({"target": target}, ctx)

    assert asyncio.run(ask("prd")) is True and asyncio.run(ask("PRL1")) is True
    assert asyncio.run(ask("uat")) is False
    assert approvals.needs_approval("promote_release", {"target": "prd"})      # the same rule, both lanes


def test_an_unconfirmed_promotion_pauses_and_never_runs(monkeypatch):
    ran = []
    from adk_release_agent import tools as release_tools

    monkeypatch.setattr(release_tools, "_invoke_tool", lambda name, args=None: ran.append(name) or {"ok": True})
    tool = _promote_tool(monkeypatch)

    ctx = _tool_context()
    result = asyncio.run(tool.run_async(args={"target": "prd"}, tool_context=ctx))
    assert "requires confirmation" in result["error"] and ran == []
    assert "call-1" in ctx.actions.requested_tool_confirmations
    # 2.11 propagates this: the pause is the turn's last word, not a model summary.
    assert ctx.actions.skip_summarization is True

    rejected = _tool_context(session_id="s2", confirmation=ToolConfirmation(confirmed=False))
    result = asyncio.run(tool.run_async(args={"target": "prd"}, tool_context=rejected))
    # adk_service._landed_changes reads a refusal as "did not run" by these keys.
    assert result == {"error": "This tool call is rejected."} and ran == []
    assert not ({"ok", "note", "result"} & set(result))


# --- ModelConsultTool (consult_advisor) -----------------------------------------

class _Advisor(BaseLlm):
    """An advisor that answers without a network; counts how often it is asked."""

    model: str = "fake-advisor"
    fail: bool = False
    asked: list = []

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.asked.append(llm_request)
        if self.fail:
            raise RuntimeError("advisor down")
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="Check the source logs first.")]))


def _advisor_tool(monkeypatch, advisor: _Advisor, per_turn: int = 1, per_session: int = 2):
    monkeypatch.setattr(settings, "advisor_model", "gemini-2.5-pro")
    monkeypatch.setattr(settings, "advisor_max_uses", per_turn)
    monkeypatch.setattr(settings, "advisor_session_max_uses", per_session)
    tool = agent_module.advisor_tool()
    assert tool.name == "consult_advisor" and tool.thinking_level is None
    tool.advisor_model = advisor        # same tool, same budgets — no Vertex
    return tool


def test_the_advisor_is_capped_per_turn_and_per_session_and_says_so_without_raising(monkeypatch):
    advisor = _Advisor(asked=[])
    tool = _advisor_tool(monkeypatch, advisor)
    sessions = InMemorySessionService()
    session = asyncio.run(sessions.create_session(app_name="adk211", user_id="u", session_id="s"))

    def consult(invocation_id, call_id):
        ctx = _tool_context(invocation_id=invocation_id, call_id=call_id, sessions=sessions, session=session)
        return asyncio.run(tool.run_async(args={"question": "why did it fail?"}, tool_context=ctx))

    first = consult("turn-1", "c1")
    assert first["status"] == "ok" and first["guidance"] == "Check the source logs first."
    assert first["consults"] == {"used_this_turn": 1, "max_uses": 1, "used_this_session": 1,
                                 "session_max_uses": 2, "remaining": 0}

    second = consult("turn-1", "c2")                      # the same turn: over the per-turn cap
    assert second["status"] == "limit_reached"
    assert "budget for this turn is exhausted (1 of 1 used)" in second["message"]

    third = consult("turn-2", "c3")                       # a new turn: the per-turn cap resets
    assert third["status"] == "ok" and third["consults"]["used_this_session"] == 2

    fourth = consult("turn-3", "c4")                      # the session cap holds across turns
    assert fourth["status"] == "limit_reached"
    assert "budget for this session is exhausted (2 of 2 used)" in fourth["message"]

    assert len(advisor.asked) == 2                         # the model was reached exactly twice
    # The advisor is called tool-less: it reasons over what the executor read, nothing more.
    assert all(not (req.config.tools or []) for req in advisor.asked)


def test_an_advisor_that_fails_is_an_answer_not_an_exception_and_costs_no_budget(monkeypatch):
    advisor = _Advisor(asked=[], fail=True)
    tool = _advisor_tool(monkeypatch, advisor)
    sessions = InMemorySessionService()
    session = asyncio.run(sessions.create_session(app_name="adk211", user_id="u", session_id="s"))
    ctx = _tool_context(sessions=sessions, session=session)

    result = asyncio.run(tool.run_async(args={"question": "why?"}, tool_context=ctx))

    assert result["status"] == "error" and "could not be reached" in result["message"]
    assert result["consults"]["used_this_turn"] == 0 and result["consults"]["used_this_session"] == 0


def test_a_blank_question_is_refused_before_the_advisor_is_called(monkeypatch):
    advisor = _Advisor(asked=[])
    tool = _advisor_tool(monkeypatch, advisor)
    result = asyncio.run(tool.run_async(args={"question": "  "}, tool_context=_tool_context()))
    assert result["status"] == "invalid_request" and advisor.asked == []
