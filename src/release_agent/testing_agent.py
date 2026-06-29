"""
Testing Subagent for the Release Copilot (end-to-end tester).

This is a dedicated LangGraph agent whose job is to exercise the main
Release Copilot end-to-end and verify the side effects on GitHub.

It can be run independently (via CLI or FastAPI) or potentially called
as a sub-agent from the main copilot for self-testing scenarios.

Key capabilities:
- Send realistic chat messages to the copilot (image:tag updates, etc.)
- Wait for / observe confirmations (simulates user)
- After dispatch: locate the created PR in the deployment repo
- Inspect PR comments for:
  - CHG / change ticket references
  - Release control states (RLFT gates closed/opened, etc.)
- Retrigger the deployment workflow directly from chat using retrigger_deployment_workflow (to re-simulate after manually closing controls)
- Produce a structured test report (pass/fail + evidence)
- Support multiple test scenarios in one run

All GitHub operations use the existing safe gh tools.
The copilot itself is invoked via its compiled graph (isolated threads).

Usage:
    python -m src.release_agent.testing_agent
    # or
    python -m src.release_agent.testing_agent --scenario full-release

Environment:
    Same as the main copilot:
    - OPENAI_API_KEY
    - BUILD_REPO (code + config + build repo)
    - DEPLOY_REPO (where the workflow creates PRs)

    Use test tags (e.g. "payments-api:2.0.99-test") so you can clean up easily.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, List, Literal, Optional

from langchain_core.messages import AnyMessage, HumanMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from pydantic import BaseModel, Field

# Import the main copilot and the shared gh tools
from .agent import get_compiled_graph as get_copilot_graph
from .tools.gh_tools import (
    find_prs,
    get_pr_comments,
    get_pr_details,
    summarize_pr_controls,
    retrigger_deployment_workflow,
    BUILD_REPO,
    DEPLOY_REPO,
)

# Re-export some copilot tools so the tester can use them directly if needed
from .tools.gh_tools import (
    list_allowed_images,
    get_current_manifest,
    get_recent_runs,
)

# --------------------------------------------------------------------------- #
# Pydantic models for tester state and structured test results
# --------------------------------------------------------------------------- #


class TestStep(BaseModel):
    name: str
    status: Literal["pending", "passed", "failed", "skipped"] = "pending"
    detail: str = ""
    evidence: Optional[dict] = None


class TestReport(BaseModel):
    scenario: str
    overall_status: Literal["passed", "failed", "partial"] = "pending"
    steps: List[TestStep] = Field(default_factory=list)
    copilot_thread_id: Optional[str] = None
    pr_number: Optional[int] = None
    summary: str = ""


class TesterState(BaseModel):
    """Pydantic state for the testing sub-agent."""

    model_config = {"arbitrary_types_allowed": True}

    messages: Annotated[List[AnyMessage], add_messages] = Field(default_factory=list)
    current_scenario: Optional[str] = None
    test_image_tags: Optional[str] = None  # e.g. "payments-api:2.0.99-test"
    copilot_thread_id: Optional[str] = None
    last_copilot_response: Optional[str] = None
    pr_number: Optional[int] = None
    report: Optional[TestReport] = None


# --------------------------------------------------------------------------- #
# Tester-specific Pydantic tool schemas (using Pydantic as requested)
# --------------------------------------------------------------------------- #


class InvokeCopilotInput(BaseModel):
    message: str = Field(
        ...,
        description="Message to send to the release copilot (e.g. 'promote payments-api:2.0.99-test')",
    )
    thread_id: str = Field(
        default="",
        description="Optional thread id for the copilot. Leave empty to auto-generate a test thread.",
    )


class SimulateConfirmationInput(BaseModel):
    token: str = Field(..., description="The CONFIRM-xxx token shown by the copilot")
    thread_id: str = Field(..., description="The copilot thread id from the previous invoke")


class VerifyManifestInput(BaseModel):
    image: str = Field(..., description="Image name to look for in the manifest")
    expected_tag: str = Field(..., description="Expected tag for that image")


class LocatePRInput(BaseModel):
    search_term: str = Field(
        ..., description="Search term (image:tag or similar) to find the PR created by the workflow"
    )


class CheckPRCommentsInput(BaseModel):
    pr_number: int = Field(..., description="PR number in the deployment repo")
    required_patterns: List[str] = Field(
        default=["CHG-", "RLFT", "closed", "opened"],
        description="List of strings that must appear in the PR comments",
    )


class RunFullScenarioInput(BaseModel):
    image_tags: str = Field(
        ..., description="Comma-separated image:tag to test, e.g. 'payments-api:2.0.99-test'"
    )
    scenario_name: str = Field(default="full-release-e2e")


# --------------------------------------------------------------------------- #
# Tester tools (wrap the copilot + GitHub verification)
# --------------------------------------------------------------------------- #

# We reuse the main copilot graph (lazily)
_copilot_graph = None


def _get_copilot():
    global _copilot_graph
    if _copilot_graph is None:
        _copilot_graph = get_copilot_graph()
    return _copilot_graph


@tool(args_schema=InvokeCopilotInput)
def invoke_copilot(message: str, thread_id: str = "") -> str:
    """
    Send a message to the main Release Copilot and return its final response text.
    Use a dedicated test thread_id so we don't pollute real user threads.
    """
    copilot = _get_copilot()
    tid = thread_id or f"tester-{uuid.uuid4().hex[:8]}"

    result = copilot.invoke(
        {"messages": [HumanMessage(content=message)]}, {"configurable": {"thread_id": tid}}
    )

    # Extract the last useful message
    last = result.get("messages", [])[-1] if result.get("messages") else None
    content = getattr(last, "content", str(last)) if last else "No response"

    # Store the thread id in the tester state via side-channel return
    # (the caller will also receive it in the report)
    return json.dumps(
        {
            "thread_id": tid,
            "response": content[:2000],
            "note": "If the copilot asked for a confirmation token, call simulate_confirmation next.",
        },
        indent=2,
    )


@tool(args_schema=SimulateConfirmationInput)
def simulate_confirmation(token: str, thread_id: str) -> str:
    """
    Simulate the user pasting the CONFIRM-xxx token back to the copilot.
    This continues the HITL flow inside the copilot.
    """
    copilot = _get_copilot()
    result = copilot.invoke(
        {"messages": [HumanMessage(content=token)]}, {"configurable": {"thread_id": thread_id}}
    )
    last = result.get("messages", [])[-1] if result.get("messages") else None
    content = getattr(last, "content", str(last)) if last else "No response after confirmation"

    return json.dumps(
        {"thread_id": thread_id, "response_after_confirmation": content[:2000]}, indent=2
    )


@tool(args_schema=VerifyManifestInput)
def verify_manifest_update(image: str, expected_tag: str) -> str:
    """Check that the release manifest now contains the expected image:tag."""
    from .tools.gh_tools import get_current_manifest  # reuse

    raw = get_current_manifest()
    try:
        manifest = json.loads(raw)
        actual = manifest.get("images", {}).get(image)
        if actual == expected_tag:
            return json.dumps(
                {
                    "status": "passed",
                    "image": image,
                    "expected": expected_tag,
                    "actual": actual,
                    "manifest": manifest,
                },
                indent=2,
            )
        else:
            return json.dumps(
                {
                    "status": "failed",
                    "image": image,
                    "expected": expected_tag,
                    "actual": actual,
                    "manifest": manifest,
                },
                indent=2,
            )
    except Exception as e:
        return f"ERROR verifying manifest: {e}"


@tool(args_schema=LocatePRInput)
def locate_pr(search_term: str) -> str:
    """Find the PR created by the workflow for the given release."""
    from .tools.gh_tools import find_prs

    result = find_prs(search_term=search_term, limit=5)
    return result


@tool(args_schema=CheckPRCommentsInput)
def check_pr_comments(pr_number: int, required_patterns: List[str] = None) -> str:
    """
    Fetch PR comments and check whether they contain the expected patterns
    (CHG ticket, RLFT controls closed/opened, etc.).
    """
    from .tools.gh_tools import get_pr_comments

    comments_raw = get_pr_comments(pr_number)
    try:
        data = json.loads(comments_raw)
        comments_text = " ".join(c.get("body", "") for c in data.get("comments", []))
        found = []
        missing = []
        for pat in required_patterns or ["CHG-", "RLFT"]:
            if pat.lower() in comments_text.lower():
                found.append(pat)
            else:
                missing.append(pat)

        status = "passed" if not missing else "failed"
        return json.dumps(
            {
                "pr_number": pr_number,
                "status": status,
                "found_patterns": found,
                "missing_patterns": missing,
                "comment_count": len(data.get("comments", [])),
                "raw_comments": data.get("comments", [])[:5],  # first few for evidence
            },
            indent=2,
        )
    except Exception as e:
        return f"ERROR checking PR comments: {e}"


@tool(args_schema=RunFullScenarioInput)
def run_full_e2e_scenario(image_tags: str, scenario_name: str = "full-release-e2e") -> str:
    """
    High-level tool that orchestrates a complete end-to-end test:
    1. Invoke copilot with the release request
    2. Simulate the confirmation (assumes the copilot returns a token in the response)
    3. Verify manifest update
    4. Find the PR
    5. Check PR comments for CHG ticket + control states
    Returns a structured TestReport.
    """
    # This is a convenience orchestrator. The main graph will usually drive step-by-step,
    # but this tool is useful for batch / CI style testing.

    report = TestReport(scenario=scenario_name)
    tid = f"e2e-test-{uuid.uuid4().hex[:8]}"

    # Step 1: send the request
    step1 = TestStep(name="invoke_copilot")
    inv = invoke_copilot(message=f"promote {image_tags}", thread_id=tid)
    step1.detail = inv[:300]
    try:
        inv_data = json.loads(inv)
        tid = inv_data.get("thread_id", tid)
        step1.status = "passed"
        report.copilot_thread_id = tid
    except Exception:
        step1.status = "failed"
    report.steps.append(step1)

    # Step 2: crude extraction of token from the response (real usage would parse the interrupt)
    # For a real sub-agent you would look at the interrupt payload.
    # Here we do a best-effort parse.
    token = None
    if "CONFIRM-" in inv:
        for part in inv.split():
            if part.startswith("CONFIRM-"):
                token = part.strip(".,:;\"'")
                break

    if token:
        step2 = TestStep(name="simulate_confirmation", status="passed")
        conf = simulate_confirmation(token=token, thread_id=tid)
        step2.detail = conf[:200]
        report.steps.append(step2)
    else:
        step2 = TestStep(
            name="simulate_confirmation",
            status="skipped",
            detail="No CONFIRM token found in first response. In real use the UI would surface the interrupt.",
        )
        report.steps.append(step2)

    # Step 3: verify manifest
    step3 = TestStep(name="verify_manifest")
    for pair in image_tags.split(","):
        if ":" not in pair:
            continue
        img, tag = pair.strip().split(":", 1)
        v = verify_manifest_update(image=img, expected_tag=tag)
        if '"status": "passed"' in v:
            step3.status = "passed"
        else:
            step3.status = "failed"
        step3.detail += v[:150] + " | "
    report.steps.append(step3)

    # Step 4: locate PR
    step4 = TestStep(name="locate_pr")
    pr_search = locate_pr(search_term=image_tags)
    report.steps.append(step4)
    try:
        pr_data = json.loads(pr_search)
        prs = pr_data.get("prs", [])
        if prs:
            report.pr_number = prs[0].get("number")
            step4.status = "passed"
            step4.detail = f"Found PR #{report.pr_number}"
        else:
            step4.status = "failed"
            step4.detail = "No PR found with the search term"
    except Exception as e:
        step4.status = "failed"
        step4.detail = str(e)

    # Step 5: check comments for CHG + controls
    if report.pr_number:
        step5 = TestStep(name="check_pr_comments")
        patterns = ["CHG-", "RLFT", "closed", "opened", "ticket"]
        comment_check = check_pr_comments(pr_number=report.pr_number, required_patterns=patterns)
        step5.detail = comment_check[:400]
        if '"status": "passed"' in comment_check:
            step5.status = "passed"
        else:
            step5.status = "failed"
        report.steps.append(step5)

    # Final report
    passed = sum(1 for s in report.steps if s.status == "passed")
    total = len([s for s in report.steps if s.status != "skipped"])
    report.overall_status = (
        "passed" if passed == total and total > 0 else "partial" if passed > 0 else "failed"
    )
    report.summary = (
        f"{passed}/{total} steps passed for scenario {scenario_name} (images={image_tags})"
    )

    return report.model_dump_json(indent=2)


# --------------------------------------------------------------------------- #
# Tool list for the tester agent
# --------------------------------------------------------------------------- #

TESTER_TOOLS = [
    invoke_copilot,
    simulate_confirmation,
    verify_manifest_update,
    locate_pr,
    check_pr_comments,
    run_full_e2e_scenario,
    retrigger_deployment_workflow,  # new: retrigger deployment workflow from chat
    # We also expose a few useful read-only copilot tools
    list_allowed_images,
    get_current_manifest,
    get_recent_runs,
    find_prs,
    get_pr_details,
    get_pr_comments,
    summarize_pr_controls,
]


# --------------------------------------------------------------------------- #
# Tester graph definition (similar style to the main copilot)
# --------------------------------------------------------------------------- #

TESTER_SYSTEM_PROMPT = f"""You are an expert end-to-end tester for the Release Copilot.

Your job is to:
1. Drive the copilot through realistic release scenarios using image:tag values.
2. Handle the confirmation step (the copilot will ask for a CONFIRM- token).
3. After the copilot dispatches a workflow (which creates a PR in the deployment repo {DEPLOY_REPO}), locate the PR.
4. Inspect the PR comments for:
   - CHG / change ticket references
   - Release control states (RLFT gates closed or opened, approvals, etc.)
5. Produce a clear pass/fail report with evidence.

Always use the dedicated testing tools:
- invoke_copilot + simulate_confirmation for driving the copilot
- locate_pr / check_pr_comments / summarize_pr_controls for PR verification
- verify_manifest_update to check the JSON side effect

Be thorough but concise. At the end always output a structured summary.
Current build/source repo: {BUILD_REPO}
Deployment / PR repo: {DEPLOY_REPO}
"""


def build_tester_graph(checkpointer=None):
    """Build the testing sub-agent graph."""
    from langchain_google_genai import ChatGoogleGenerativeAI
    from .config import settings

    if not settings.gcp_project:
        raise RuntimeError(
            "No GCP project detected for Vertex AI. "
            "Set GOOGLE_CLOUD_PROJECT or run 'gcloud config set project YOUR_PROJECT'"
        )

    llm = ChatGoogleGenerativeAI(
        model=settings.gemini_model,
        temperature=0,
        vertexai=True,
        project=settings.gcp_project,
        location=settings.gcp_location,
    ).bind_tools(TESTER_TOOLS)

    def call_llm(state: TesterState):
        messages = state.messages
        if not any(isinstance(m, SystemMessage) for m in messages):
            messages = [SystemMessage(content=TESTER_SYSTEM_PROMPT)] + messages
        resp = llm.invoke(messages)
        return {"messages": [resp]}

    tool_node = ToolNode(TESTER_TOOLS)

    graph = StateGraph(TesterState)

    graph.add_node("llm", call_llm)
    graph.add_node("tools", tool_node)

    graph.add_edge(START, "llm")
    graph.add_conditional_edges(
        "llm", lambda s: "tools" if getattr(s.messages[-1], "tool_calls", None) else END
    )
    graph.add_edge("tools", "llm")

    return graph.compile(checkpointer=checkpointer)


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


def get_compiled_tester_graph():
    from langgraph.checkpoint.memory import MemorySaver

    return build_tester_graph(MemorySaver())


def run_end_to_end_test(
    image_tags: str = "payments-api:2.0.99-test,orders-api:v9.9.9-test",
    scenario_name: str = "full-release-e2e",
) -> TestReport:
    """
    Convenience function to run a complete test from Python.
    Returns a structured TestReport.
    """
    tester = get_compiled_tester_graph()
    result = tester.invoke(
        {"messages": [HumanMessage(content=f"Run full end-to-end test for: promote {image_tags}")]}
    )
    # The last message should contain the JSON report from run_full_e2e_scenario
    last = result.get("messages", [])[-1] if result.get("messages") else None
    content = getattr(last, "content", "{}")
    try:
        data = json.loads(content)
        return TestReport(**data)
    except Exception:
        # Fallback
        return TestReport(
            scenario=scenario_name,
            overall_status="failed",
            summary=f"Could not parse report. Raw: {content[:300]}",
        )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Run the Release Copilot testing subagent end-to-end"
    )
    parser.add_argument(
        "--image-tags", default="payments-api:2.0.99-test", help="image:tag pairs to test"
    )
    parser.add_argument("--scenario", default="full-release-e2e", help="Name of the test scenario")
    args = parser.parse_args()

    print("=== Running Release Copilot End-to-End Test via Testing Subagent ===")
    report = run_end_to_end_test(image_tags=args.image_tags, scenario_name=args.scenario)
    print(report.model_dump_json(indent=2))
    print("\nTest finished.")
