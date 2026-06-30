"""End-to-end smoke of the deterministic deploy pipeline.

Drives a real compiled graph through parse -> propose (assembles the deployment.json
entry) -> confirmation gate (HITL interrupt) -> resume -> apply -> finalize, with the
GitHub tools stubbed and no LLM configured. No Vertex, no network — proves the pipeline
wiring + interrupt/resume survive the Helm-chart deployment.json refactor.
"""
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import release_agent.agent.graph as graph


@tool
def open_release_pr(environment: str, image_tags: str, namespace: str = "") -> str:
    """Stub: simulate a deploy, echoing which files were written + a deploy run."""
    files = ["uat/deployment.json"] + (["prd/deployment.json"] if environment == "prod" else [])
    return json.dumps(
        {
            "ok": True,
            "action": "deployed",
            "environment": environment,
            "image_tags": image_tags,
            "files_updated": files,
            "uat_charts": [{"helm_chart_name": "abc-client-api-svc", "helm_chart_version": "1.1.1230"}],
            "deploy_run": {"id": 4242, "url": "https://example/runs/4242", "status": "queued"},
            "note": f"Deployed {image_tags} to {environment}.",
        }
    )


FAKE_TOOLS = [open_release_pr]


def _build():
    # force model=None (no Vertex) and swap in the stub tool for the deterministic lane
    return (
        patch.object(graph, "_get_llm", side_effect=RuntimeError("no model in test")),
        patch.object(graph, "GH_TOOLS", FAKE_TOOLS),
    )


def test_uat_deploy_interrupts_then_applies_on_confirmation():
    p1, p2 = _build()
    with p1, p2:
        g = graph.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "smoke-uat"}}

        # 1) deploy request -> pipeline pauses at the confirmation gate
        g.invoke({"messages": [HumanMessage(content="deploy abc-client-api-svc:1.1.1230 to uat")]}, cfg)
        snap = g.get_state(cfg)
        assert snap.interrupts, "expected a HITL confirmation interrupt"
        payload = snap.interrupts[0].value
        assert payload["environment"] == "uat"
        # the gate previews the assembled entry for the uat file only
        assert "uat/deployment.json" in (payload.get("proposed") or {})
        token = payload["token"]
        assert token.startswith("CONFIRM-")

        # 2) resume with the exact token -> apply -> finalize
        g.invoke(Command(resume=token), cfg)
        final = g.get_state(cfg).values
        report = [m.content for m in final["messages"] if isinstance(m, AIMessage) and m.content][-1]
        assert "deployed to UAT" in report, report
        steps = final.get("steps") or []
        assert steps and all(s["status"] == "ok" for s in steps), steps


def test_prod_deploy_previews_both_files():
    p1, p2 = _build()
    with p1, p2:
        g = graph.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "smoke-prod"}}
        g.invoke({"messages": [HumanMessage(content="deploy abc-client-api-svc:1.1.1230 to prod")]}, cfg)
        payload = g.get_state(cfg).interrupts[0].value
        assert payload["environment"] == "prod"
        proposed = payload.get("proposed") or {}
        # a prod deploy writes BOTH files; the gate previews both
        assert "uat/deployment.json" in proposed and "prd/deployment.json" in proposed
        token = payload["token"]
        g.invoke(Command(resume=token), cfg)
        final = g.get_state(cfg).values
        report = [m.content for m in final["messages"] if isinstance(m, AIMessage) and m.content][-1]
        assert "deployed to PROD" in report, report


def test_deploy_not_applied_without_correct_token():
    p1, p2 = _build()
    with p1, p2:
        g = graph.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "smoke-noconfirm"}}
        g.invoke({"messages": [HumanMessage(content="deploy abc-client-api-svc:1.1.1230 to uat")]}, cfg)
        g.invoke(Command(resume="nope"), cfg)  # wrong token
        final = g.get_state(cfg).values
        texts = [m.content for m in final["messages"] if isinstance(m, AIMessage) and m.content]
        assert any("Not confirmed" in t for t in texts), texts
        assert not (final.get("steps") or [])


def test_free_form_falls_back_to_responder_when_no_model():
    """With no LLM, a non-deploy message degrades gracefully (no crash)."""
    p1, p2 = _build()
    with p1, p2:
        g = graph.build_graph(checkpointer=MemorySaver())
        cfg = {"configurable": {"thread_id": "smoke-noform"}}
        g.invoke({"messages": [HumanMessage(content="what's deployed to prod right now?")]}, cfg)
        final = g.get_state(cfg).values
        assert any(isinstance(m, AIMessage) and m.content for m in final["messages"])
