"""Graph assembly: the deterministic promote pipeline + the supervisor multi-agent
free-form lane, compiled into one StateGraph."""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.errors import GraphRecursionError
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt, RetryPolicy

from ..config import settings
from ..tools.gh_tools import GH_TOOLS
from ..multiagent import build_specialists, ROUTE_TO_NODE, Route, SUPERVISOR_PROMPT
from ..budget import (
    get_budget_tracker,
    check_budget_before_call,
    BudgetInterrupt,
    confirm_budget_continue,
    get_budget_status,
)
from .llm import _get_llm
from .parsing import _is_question, _is_removal, _is_retrigger, _last_human_text
from .state import ReleaseState
from .nodes import (
    parse_intent,
    propose,
    confirmation_gate,
    build_apply_and_dispatch,
    rerun,
    finalize,
    track_pr,
    respond,
    _route_after_parse,
    _route_after_finalize,
)


def build_graph(checkpointer=None):
    """Build and compile the supervisor multi-agent graph.

    The deterministic promote pipeline and the specialist sub-agents share one
    ReleaseState graph. The model is resolved once here (network-free when
    DEFAULT_MODEL is set) because each specialist is a compiled graph node.
    """
    # Resolve the LLM once. If none is configured the graph still compiles and the
    # free-form lane degrades to the deterministic "promote ..." responder.
    try:
        model = _get_llm()
    except Exception:
        model = None

    # Retry only TRANSIENT failures (network blips, 5xx, rate limits) — never
    # deterministic ones (404/422/auth), which won't fix on retry. Catches Vertex/LLM
    # hiccups in the supervisor + any tool exception that escapes ToolNode's own error
    # handling. (GitHub API blips are also retried lower down, at the HTTP layer in
    # _get_github_client.)
    def _is_transient_error(exc: Exception) -> bool:
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True
        if type(exc).__name__ in {
            "ServiceUnavailable",
            "DeadlineExceeded",
            "ResourceExhausted",
            "InternalServerError",
            "TooManyRequests",
            "Aborted",
            "GatewayTimeout",
        }:
            return True
        try:
            import requests

            if isinstance(exc, requests.exceptions.RequestException):
                return True
        except Exception:
            pass
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        return isinstance(status, int) and (status == 429 or status >= 500)

    retry = RetryPolicy(
        max_attempts=3,
        initial_interval=0.5,
        backoff_factor=2.0,
        jitter=True,
        retry_on=_is_transient_error,
    )

    # One ToolNode for the deterministic mutation lane, registered under two names
    # (propose_tools / apply_tools) so each node has exactly one outgoing edge.
    tool_node = ToolNode(GH_TOOLS)

    graph = StateGraph(ReleaseState)

    graph.add_node("parse", parse_intent)
    graph.add_node("propose", propose)
    graph.add_node("propose_tools", tool_node, retry_policy=retry)
    graph.add_node("gate", confirmation_gate)
    graph.add_node("apply", build_apply_and_dispatch)
    graph.add_node("rerun", rerun)
    graph.add_node("apply_tools", tool_node, retry_policy=retry)
    graph.add_node("finalize", finalize)
    graph.add_node("track_pr", track_pr, retry_policy=retry)
    graph.add_node("respond", respond)

    # --- Free-form lane: supervisor → scoped specialist sub-agents -------------
    # Each specialist is a compiled create_agent (see multiagent.py). It runs
    # as a graph node via a thin wrapper that (a) bounds its ReAct loop, (b) returns
    # only its final answer to keep parent history clean, and (c) degrades
    # gracefully if it can't converge. Falls back to `respond` when no LLM exists.
    free_form_entry = "respond"
    if model is not None:
        specialists = build_specialists(model)
        router = model.with_structured_output(Route, include_raw=True)
        # Bound each specialist's loop: ~2 graph steps (model + tools) per tool turn.
        specialist_recursion = max(8, settings.react_max_tool_turns * 2 + 3)

        def supervisor(state: ReleaseState) -> Command:
            """Classify the free-form turn and delegate to ONE specialist.

            Budget-gated once per turn (covers the routing call + the specialist's
            own loop). The unambiguous mutating asks (remove / retrigger) take a
            deterministic fast-path to the ops specialist; everything else is
            classified by the router LLM, defaulting to the read-only general agent.
            """
            last = _last_human_text(state.messages)

            # Budget protection (Vertex AI via ADC) — one gate per free-form turn.
            try:
                check_budget_before_call(
                    estimated_input_tokens=3000, estimated_output_tokens=1500
                )
            except BudgetInterrupt as be:
                user_response = interrupt(
                    {
                        "type": "budget_confirmation",
                        "message": str(be) + f"\n\nCurrent budget status: {get_budget_status()}",
                        "action": "Continue with this LLM call? (yes/no)",
                    }
                )
                if not confirm_budget_continue(str(user_response)):
                    return Command(
                        goto="respond",
                        update={
                            "messages": [
                                AIMessage(
                                    content=(
                                        "🛑 Stopped to protect the budget — no LLM call was made. "
                                        f"{get_budget_status()}"
                                    )
                                )
                            ]
                        },
                    )

            # Deterministic fast-path for the clear mutating COMMANDS (no routing call).
            # Question-form phrasings ("how do I retrigger?", "did anyone exclude X?")
            # fall through to the LLM router so a pure question lands on a read-only
            # specialist rather than the mutating ops agent.
            if (_is_removal(last) or _is_retrigger(last)) and not _is_question(last):
                return Command(goto="ops_agent")

            # LLM routing — default to the read-only general agent on any failure.
            route = "general"
            try:
                out = router.invoke(
                    [SystemMessage(content=SUPERVISOR_PROMPT), HumanMessage(content=last)]
                )
                raw = out.get("raw") if isinstance(out, dict) else None
                parsed = out.get("parsed") if isinstance(out, dict) else out
                usage = getattr(raw, "usage_metadata", None) or {}
                input_t = usage.get("input_tokens", 0) or 0
                output_t = usage.get("output_tokens", 0) or 0
                if input_t or output_t:
                    get_budget_tracker().add_usage(input_t, output_t)
                if parsed is not None:
                    route = parsed.route
            except Exception:
                route = "general"
            return Command(goto=ROUTE_TO_NODE.get(route, "general_agent"))

        def _make_specialist_node(sub):
            def specialist_node(state: ReleaseState) -> dict:
                prior = len(state.messages)
                try:
                    result = sub.invoke(
                        {"messages": state.messages},
                        {"recursion_limit": specialist_recursion},
                    )
                except GraphRecursionError:
                    return {
                        "messages": [
                            AIMessage(
                                content=(
                                    "I ran several lookups without converging on an answer. "
                                    "Could you narrow the request — e.g. a specific `image:tag` "
                                    "or PR number — and I'll dig in directly?"
                                )
                            )
                        ]
                    }
                msgs = result.get("messages", []) if isinstance(result, dict) else []
                # Persist only the specialist's final answer: the parent history stays
                # clean and the app streams one assistant message, not the whole turn.
                if msgs and len(msgs) > prior:
                    return {"messages": [msgs[-1]]}
                return {}

            return specialist_node

        graph.add_node("supervisor", supervisor)
        for node_name, sub in specialists.items():
            graph.add_node(node_name, _make_specialist_node(sub))
            graph.add_edge(node_name, END)
        free_form_entry = "supervisor"

    # Entry + branch: re-run request, concrete image:tag pairs, or free-form chat.
    graph.add_edge(START, "parse")
    graph.add_conditional_edges(
        "parse",
        _route_after_parse,
        {"propose": "propose", "llm": free_form_entry, "rerun": "rerun"},
    )

    # Propose → confirm → apply path (propose & gate route via Command).
    graph.add_edge("propose_tools", "gate")
    graph.add_edge("apply", "apply_tools")
    graph.add_edge("apply_tools", "finalize")
    graph.add_conditional_edges(
        "finalize",
        _route_after_finalize,
        {"track_pr": "track_pr", END: END},
    )
    graph.add_edge("track_pr", END)
    graph.add_edge("respond", END)

    return graph.compile(checkpointer=checkpointer)


# Convenience for CLI / apps
def get_compiled_graph():
    from langgraph.checkpoint.memory import MemorySaver

    checkpointer = MemorySaver()
    return build_graph(checkpointer=checkpointer)
