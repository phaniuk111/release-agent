"""Tests for the mutation-guard safety plugin and the skills-as-router wiring."""
import asyncio
import pathlib

import pytest

from adk_release_agent import tools

pytest.importorskip("google.adk")

from adk_release_agent import agent as agent_module  # noqa: E402
from adk_release_agent.safety import BLOCKED_FREEFORM_TOOLS, MutationGuardPlugin  # noqa: E402
from google.adk.skills import load_skill_from_dir  # noqa: E402


class _FakeTool:
    def __init__(self, name):
        self.name = name


def _before_tool(plugin, tool_name):
    return asyncio.run(
        plugin.before_tool_callback(tool=_FakeTool(tool_name), tool_args={}, tool_context=None)
    )


def test_mutation_guard_blocks_release_defining_mutations():
    plugin = MutationGuardPlugin()
    for name in ("open_release_pr", "apply_json_update", "dispatch_workflow", "apply_confirmed_deploy"):
        result = _before_tool(plugin, name)
        assert result is not None
        assert result["error_code"] == "MUTATION_BLOCKED"
        assert result["blocked_tool"] == name


def test_mutation_guard_allows_read_and_scoped_ops_tools():
    plugin = MutationGuardPlugin()
    # Read tools and the allowed scoped-ops mutations must pass through untouched.
    for name in ("check_release_window", "find_prs", "remove_from_release", "merge_prod_release"):
        assert _before_tool(plugin, name) is None


def test_blocked_set_matches_release_defining_mutations():
    # The plugin's blocked set must cover every release-defining mutation plus the
    # confirmed-apply entrypoint (which belongs to the deploy Workflow).
    assert tools.RELEASE_DEFINING_MUTATIONS <= BLOCKED_FREEFORM_TOOLS
    assert "apply_confirmed_deploy" in BLOCKED_FREEFORM_TOOLS


def test_chat_app_registers_mutation_guard_plugin():
    assert agent_module.app is not None
    assert "mutation_guard" in {p.name for p in agent_module.app.plugins}


def test_skill_additional_tools_reference_real_chat_tools():
    """Every adk_additional_tools name in a SKILL.md must resolve to a chat tool."""
    chat_tool_names = {tool.__name__ for tool in tools.ADK_CHAT_TOOLS}
    skills_dir = pathlib.Path(agent_module.__file__).parent / "skills"

    declared_any = False
    for path in sorted(skills_dir.iterdir()):
        if not path.is_dir():
            continue
        skill = load_skill_from_dir(path)
        names = skill.frontmatter.metadata.get("adk_additional_tools") or []
        for name in names:
            declared_any = True
            assert name in chat_tool_names, f"{skill.name} references unknown tool {name!r}"
        if skill.name == "release-deploy":
            # The deploy skill is tool-less: deploys run the deterministic Workflow.
            assert not names
    assert declared_any


def test_free_form_toolset_excludes_release_defining_mutations():
    toolset = agent_module.root_agent.tools[0]
    provided = set(toolset._provided_tools_by_name)
    assert not (BLOCKED_FREEFORM_TOOLS & provided)
    assert "prepare_deploy_preview" not in provided
