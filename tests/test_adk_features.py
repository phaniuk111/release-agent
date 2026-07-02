"""Tests for the ADK 2.x runtime features: context caching, event compaction,
memory tool, conditional prod-ops confirmation, and the deploy output_schema."""
import pytest

pytest.importorskip("google.adk")

from adk_release_agent import agent as agent_module  # noqa: E402
from adk_release_agent import deploy_workflow  # noqa: E402
from release_agent import adk_service  # noqa: E402
from release_agent.config import settings  # noqa: E402


# --- context caching / event compaction / resumability toggles ------------------

def test_app_enables_context_cache_and_compaction_by_default():
    app = agent_module.build_root_app()
    assert app.context_cache_config is not None
    assert app.context_cache_config.min_tokens == settings.adk_context_cache_min_tokens
    assert app.events_compaction_config is not None
    assert app.events_compaction_config.compaction_interval == settings.adk_compaction_interval
    assert app.events_compaction_config.overlap_size == settings.adk_compaction_overlap


def test_context_cache_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "adk_context_cache", False)
    monkeypatch.setattr(settings, "adk_event_compaction", False)
    app = agent_module.build_root_app()
    assert app.context_cache_config is None
    assert app.events_compaction_config is None


# --- memory tool ----------------------------------------------------------------

def test_memory_tool_present_when_enabled_and_absent_when_disabled(monkeypatch):
    from google.adk.tools.preload_memory_tool import PreloadMemoryTool

    enabled = agent_module.build_root_agent()
    assert any(isinstance(t, PreloadMemoryTool) for t in enabled.tools)

    monkeypatch.setattr(settings, "adk_memory_enabled", False)
    disabled = agent_module.build_root_agent()
    assert not any(isinstance(t, PreloadMemoryTool) for t in disabled.tools)


# --- conditional prod-ops confirmation ------------------------------------------

def test_remove_confirmation_predicate_only_fires_for_prod():
    assert agent_module._remove_needs_confirmation(environment="prod") is True
    assert agent_module._remove_needs_confirmation(environment="prd") is True
    assert agent_module._remove_needs_confirmation(environment="production") is True
    assert agent_module._remove_needs_confirmation(environment="uat") is False
    assert agent_module._remove_needs_confirmation() is False


def test_high_impact_ops_tools_are_confirmation_wrapped():
    from google.adk.tools import FunctionTool

    toolset = agent_module.build_root_agent().tools[0]
    provided = toolset._provided_tools_by_name

    merge = provided["merge_prod_release"]
    remove = provided["remove_from_release"]
    assert isinstance(merge, FunctionTool) and merge._require_confirmation is True
    # remove uses the prod-only predicate
    assert isinstance(remove, FunctionTool) and callable(remove._require_confirmation)

    # A read tool is not gated.
    check = provided["check_release_window"]
    assert getattr(check, "_require_confirmation", False) is False


def test_ops_tools_not_wrapped_when_confirmation_disabled(monkeypatch):
    monkeypatch.setattr(settings, "adk_confirm_prod_ops", False)
    tools = agent_module._chat_additional_tools()
    # Plain functions passed through untouched (no FunctionTool wrapping here).
    assert all(callable(t) and hasattr(t, "__name__") for t in tools)


# --- confirmation HITL plumbing in the chat service (no LLM needed) --------------

class _FakeFunctionCall:
    def __init__(self, id, name, args):
        self.id = id
        self.name = name
        self.args = args


class _FakeConfirmationEvent:
    invocation_id = "inv-1"

    def __init__(self):
        self.long_running_tool_ids = {"call-1"}

    def get_function_calls(self):
        return [
            _FakeFunctionCall(
                id="call-1",
                name=adk_service._REQUEST_CONFIRMATION,
                args={
                    "toolConfirmation": {"hint": "Confirm merge_prod_release?"},
                    "originalFunctionCall": {"name": "merge_prod_release", "args": {}},
                },
            )
        ]


def test_pending_call_and_interrupt_payload_from_confirmation_event():
    pending = adk_service._pending_call_from_event(_FakeConfirmationEvent())
    assert pending is not None
    assert pending.function_name == adk_service._REQUEST_CONFIRMATION
    assert pending.function_call_id == "call-1"
    assert pending.invocation_id == "inv-1"

    payload = adk_service._confirmation_interrupt_payload(pending)
    assert payload["type"] == "confirmation"
    # merge_prod_release gets the release-finality warning, overriding the hint.
    assert "no new charts can be added" in payload["message"]
    assert payload["function"] == "merge_prod_release"


def test_confirmation_reply_maps_yes_and_no_to_function_response():
    pending = adk_service._pending_call_from_event(_FakeConfirmationEvent())

    yes = adk_service._content_from_pending_reply("yes", pending)
    fr_yes = yes.parts[0].function_response
    assert fr_yes.id == "call-1"
    assert fr_yes.name == adk_service._REQUEST_CONFIRMATION
    assert fr_yes.response == {"confirmed": True}

    no = adk_service._content_from_pending_reply("nope", pending)
    assert no.parts[0].function_response.response == {"confirmed": False}


# --- deploy output_schema -------------------------------------------------------

def test_deploy_outcome_schema_is_workflow_output_schema_and_allows_extra():
    wf = deploy_workflow.build_deploy_workflow()
    assert wf.output_schema is deploy_workflow.DeployOutcome

    # extra="allow" preserves arbitrary open_release_pr fields (e.g. pr url/number).
    outcome = deploy_workflow.DeployOutcome(ok=True, note="done", pr_url="http://x", pr_number=7)
    dumped = outcome.model_dump()
    assert dumped["ok"] is True
    assert dumped["pr_url"] == "http://x"
    assert dumped["pr_number"] == 7
