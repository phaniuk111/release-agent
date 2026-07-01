"""Google ADK app for Release Copilot.

Run with ADK after installing dependencies, for example:

    PYTHONPATH=src:. adk run adk_release_agent

This module intentionally exposes only read/query tools plus tightly scoped ops
tools to the ADK chat surface. The release-defining deploy path remains the
deterministic, confirmation-gated pipeline in the existing application.
"""
from __future__ import annotations

import pathlib
import sys


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from release_agent.config import settings  # noqa: E402

from . import deploy as deploy_tools  # noqa: E402
from . import tools as release_tools  # noqa: E402


ROOT_INSTRUCTION = """You are Release Copilot running on Google ADK.

Use the specialist sub-agents for release status, PR tracking, controls, and
scoped release operations. Facts must come from tools. Never invent PR numbers,
ticket numbers, build status, or control states.

Critical safety boundary:
- You may answer questions, summarize tool results, remove/unstage, retrigger a
  deployment workflow, or release today's staged PRD batch after cutoff.
- Deploy/add requests must be delegated to release_deploy_agent. That agent must
  call prepare_deploy_preview first, show the exact JSON, and only then call the
  confirmed apply tool with the user's CONFIRM token.
- You must not mutate deployment JSON, dispatch arbitrary workflows, or open
  release PRs from any other free-form chat path.
"""


def _model_name() -> str:
    return settings.gemini_model or "gemini-flash-latest"


def _skill_toolset():
    """Load filesystem ADK Skills when google-adk is installed."""
    from google.adk.skills import load_skill_from_dir
    from google.adk.tools import skill_toolset

    skills_dir = pathlib.Path(__file__).parent / "skills"
    skills = [
        load_skill_from_dir(path)
        for path in sorted(skills_dir.iterdir())
        if path.is_dir() and (path / "SKILL.md").exists()
    ]
    return skill_toolset.SkillToolset(skills=skills, additional_tools=release_tools.ADK_CHAT_TOOLS)


def _deploy_agent_tools():
    """Wrap the mutating deploy apply tool with ADK's native confirmation gate."""
    from google.adk.tools import FunctionTool

    return [
        deploy_tools.prepare_deploy_preview,
        FunctionTool(deploy_tools.apply_confirmed_deploy, require_confirmation=True),
    ]


def build_root_agent():
    """Build the ADK root agent and specialist team."""
    from google.adk import Agent

    model = _model_name()

    status_agent = Agent(
        name="release_status_agent",
        model=model,
        description="Read-only release status, release window, catalog, and run lookup.",
        instruction=(
            "Answer release status questions using tools only. For UAT/PRD state, "
            "always call check_release_window. You cannot mutate release state."
        ),
        tools=release_tools.STATUS_TOOLS,
    )
    pr_agent = Agent(
        name="release_pr_agent",
        model=model,
        description="Read-only PR lookup and CHG/RMG/RLFT comment summarization.",
        instruction=(
            "Find deployment PRs, read comments, and summarize exact CHG/RMG/RLFT "
            "states from tool output. Never fabricate missing values."
        ),
        tools=release_tools.PR_TOOLS,
    )
    controls_agent = Agent(
        name="release_controls_agent",
        model=model,
        description="Read-only image build and RLFT/RFTL control verification.",
        instruction=(
            "Verify image tags and build controls using tools. If a run cannot be "
            "located, ask for the run id rather than guessing."
        ),
        tools=release_tools.CONTROLS_TOOLS,
    )
    ops_agent = Agent(
        name="release_ops_agent",
        model=model,
        description="Scoped release ops: remove/unstage, retrigger, or release PRD batch.",
        instruction=(
            "You may only remove/unstage, retrigger a deployment workflow, or release "
            "today's staged PRD batch after cutoff. Do not deploy/add charts."
        ),
        tools=release_tools.OPS_TOOLS,
    )
    deploy_agent = Agent(
        name="release_deploy_agent",
        model=model,
        description="Deterministic deploy flow: preview exact JSON, then apply only after confirmation.",
        instruction=(
            "Handle deploy/add chart requests. Always call prepare_deploy_preview first. "
            "Show the proposed JSON and token. Only call apply_confirmed_deploy after "
            "the user supplies the exact CONFIRM token. Do not skip preview."
        ),
        tools=_deploy_agent_tools(),
    )

    return Agent(
        name="release_copilot_adk",
        model=model,
        description="ADK version of Release Copilot with Skills and scoped specialist agents.",
        instruction=ROOT_INSTRUCTION,
        tools=[_skill_toolset()],
        sub_agents=[status_agent, pr_agent, controls_agent, ops_agent, deploy_agent],
    )


try:
    root_agent = build_root_agent()
    ADK_IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    if exc.name and (exc.name == "google.adk" or exc.name.startswith("google.adk")):
        root_agent = None
        ADK_IMPORT_ERROR = exc
    else:
        raise
