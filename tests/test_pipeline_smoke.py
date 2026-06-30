"""End-to-end smoke of the deterministic promote pipeline.

Drives a real compiled graph through parse -> propose -> confirmation gate (HITL
interrupt) -> resume -> apply -> finalize, with the GitHub tools stubbed and no
LLM configured. No Vertex, no network — proves the pipeline wiring + interrupt /
resume survive the gh_tools / agent-package refactors.
"""
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import release_agent.agent.graph as graph


@tool
def propose_update(image_tags: str) -> str:
    """Stub: return a canned proposal diff the gate node parses."""
    return json.dumps(
        {
            "proposed": {"images": {"payments-api": "2.0.33"}},
            "changes": [{"image": "payments-api", "from": "2.0.32", "to": "2.0.33"}],
        }
    )


@tool
def open_release_pr(environment: str, image_tags: str, change_request_json: str = "") -> str:
    """Stub: simulate staging onto UAT with a captured deploy run."""
    return json.dumps(
        {
            "action": "staged_to_uat",
            "uat_images": {"payments-api": "2.0.33"},
            "deploy_run": {"id": 4242, "url": "https://example/runs/4242", "status": "queued"},
        }
    )


FAKE_TOOLS = [propose_update, open_release_pr]


def _build():
    # force model=None (no Vertex) and swap in the stub tools for the deterministic lane
    return patch.object(graph, "_get_llm", side_effect=RuntimeError("no model in test")), patch.object(
        graph, "GH_TOOLS", FAKE_TOOLS
    )


def test_uat_promote_interrupts_then_applies_on_confirmation():
    p1, p2 = _build()
    with p1, p2:
        g = graph.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "smoke-uat"}}

        # 1) promote request -> pipeline pauses at the confirmation gate
        g.invoke({"messages": [HumanMessage(content="promote payments-api:2.0.33 to uat")]}, cfg)
        snap = g.get_state(cfg)
        assert snap.interrupts, "expected a HITL confirmation interrupt"
        payload = snap.interrupts[0].value
        assert payload["environment"] == "uat"
        token = payload["token"]
        assert token.startswith("CONFIRM-")

        # 2) resume with the exact token -> apply -> finalize
        g.invoke(Command(resume=token), cfg)
        final = g.get_state(cfg).values
        report = [m.content for m in final["messages"] if isinstance(m, AIMessage) and m.content][-1]
        assert "staged on UAT" in report, report
        steps = final.get("steps") or []
        assert steps and all(s["status"] == "ok" for s in steps), steps


def test_promote_not_applied_without_correct_token():
    p1, p2 = _build()
    with p1, p2:
        g = graph.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "smoke-noconfirm"}}
        g.invoke({"messages": [HumanMessage(content="promote payments-api:2.0.33 to uat")]}, cfg)
        # wrong token -> not confirmed, nothing applied
        g.invoke(Command(resume="nope"), cfg)
        final = g.get_state(cfg).values
        texts = [m.content for m in final["messages"] if isinstance(m, AIMessage) and m.content]
        assert any("Not confirmed" in t for t in texts), texts
        # no successful release step was recorded
        assert not (final.get("steps") or [])


def test_free_form_falls_back_to_responder_when_no_model():
    """With no LLM, a non-promote message degrades gracefully (no crash)."""
    p1, p2 = _build()
    with p1, p2:
        g = graph.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "smoke-noform"}}
        g.invoke({"messages": [HumanMessage(content="what's the release status today?")]}, cfg)
        final = g.get_state(cfg).values
        assert any(isinstance(m, AIMessage) and m.content for m in final["messages"])
