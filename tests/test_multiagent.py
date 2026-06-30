"""Guardrail tests for the supervisor multi-agent lane.

These assert the security-critical invariant of the design — the free-form chat
lane can never reach the four release-defining mutations — plus the supervisor's
deterministic routing. All dependency-free (no Vertex, no GitHub).
"""

from release_agent import multiagent as M
from release_agent.agent import _is_question, _is_removal, _is_retrigger, _last_human_text

from langchain_core.messages import AIMessage, HumanMessage

# The four mutations that define/realize a release. They live ONLY in the
# deterministic, human-confirmed promote pipeline — never in a chat specialist.
RELEASE_MUTATIONS = {
    "apply_json_update",
    "dispatch_workflow",
    "open_release_pr",
}

SPECIALIST_TOOLSETS = {
    "status": M.STATUS_TOOLS,
    "pr": M.PR_TOOLS,
    "controls": M.CONTROLS_TOOLS,
    "ops": M.OPS_TOOLS,
    "general": M.GENERAL_TOOLS,
}


def _names(tools):
    return {t.name for t in tools}


def test_no_release_mutation_reachable_from_any_specialist():
    for label, tools in SPECIALIST_TOOLSETS.items():
        leaked = RELEASE_MUTATIONS & _names(tools)
        assert not leaked, f"{label} specialist exposes release mutation(s): {leaked}"


def test_scoped_mutations_are_ops_only():
    for mut in ("remove_from_release", "retrigger_deployment_workflow"):
        holders = [lbl for lbl, tools in SPECIALIST_TOOLSETS.items() if mut in _names(tools)]
        assert holders == ["ops"], f"{mut} must be ops-only, found in {holders}"


def test_read_only_specialists_have_no_mutations_at_all():
    mutating = RELEASE_MUTATIONS | {"remove_from_release", "retrigger_deployment_workflow"}
    for label in ("status", "pr", "controls", "general"):
        assert not (mutating & _names(SPECIALIST_TOOLSETS[label])), f"{label} should be read-only"


def test_route_map_matches_route_schema():
    # Every Route literal has a node target, and vice-versa.
    schema_routes = set(M.Route.model_fields["route"].annotation.__args__)
    assert set(M.ROUTE_TO_NODE) == schema_routes
    assert set(M.ROUTE_TO_NODE.values()) == {f"{r}_agent" for r in schema_routes}


def test_fast_path_fires_only_for_remove_and_retrigger():
    ops_now = [
        "remove orders-api from today's release",
        "unstage payments-api",
        "retrigger the deployment workflow for PR 42",
        "rerun deployment workflow",
    ]
    reads = [
        "what's the release status today?",
        "find the PR for payments-api:2.0.1",
        "verify payments-api:2.0.33 was built",
        "summarize the controls on PR 42",
    ]
    # the supervisor takes the fast-path only for a mutating COMMAND, not a question
    def fast_paths(t):
        return (_is_removal(t) or _is_retrigger(t)) and not _is_question(t)

    for t in ops_now:
        assert fast_paths(t), f"expected ops fast-path for: {t}"
    for t in reads:
        assert not fast_paths(t), f"read query wrongly fast-pathed: {t}"


def test_question_form_remove_retrigger_does_not_fast_path():
    # phrasings the security review flagged: they mention remove/retrigger but READ
    # as questions, so they must NOT deterministically hit the mutating ops agent.
    questions = [
        "explain how to retrigger a deployment",
        "can you re run the deployment workflow?",
        "did anyone exclude orders-api?",
        "how do I unstage an image?",
        "what does retrigger do?",
    ]
    for t in questions:
        fast = (_is_removal(t) or _is_retrigger(t)) and not _is_question(t)
        assert not fast, f"question wrongly fast-pathed to ops: {t}"


def test_last_human_text_picks_latest_human_turn():
    msgs = [HumanMessage(content="hi"), AIMessage(content="hello"), HumanMessage(content="status?")]
    assert _last_human_text(msgs) == "status?"
    assert _last_human_text([]) == ""


def test_build_specialists_returns_all_five():
    """Specialists compile from a model object alone (no network)."""
    from release_agent.agent import _get_llm

    specs = M.build_specialists(_get_llm())
    assert set(specs) == {"status_agent", "pr_agent", "controls_agent", "ops_agent", "general_agent"}
