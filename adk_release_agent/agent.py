"""Google ADK app for Release Copilot.

Run with ADK after installing dependencies, for example:

    PYTHONPATH=src:. adk run adk_release_agent

This module builds a single skills-routed chat ``Agent`` wrapped in an ``App`` with
the mutation-guard safety plugin. Skills declare their tools via
``adk_additional_tools`` frontmatter, so read/query and tightly scoped ops tools
surface only when the matching skill activates. The release-defining deploy path is
NOT here — it runs through the deterministic ADK ``Workflow`` graph in
:mod:`adk_release_agent.deploy_workflow`.
"""
from __future__ import annotations

import pathlib
import sys


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from release_agent.config import settings  # noqa: E402

from . import tools as release_tools  # noqa: E402


ROOT_INSTRUCTION = """You are Release Copilot running on Google ADK.

You have specialized Skills for release status, PR tracking, build controls, and
scoped release operations. When a request matches a skill, load it with the skill
tools and follow its instructions; the skill unlocks exactly the tools it needs.
Facts must come from tools. Never invent PR numbers, ticket numbers, build
status, or control states.

Critical safety boundary:
- You may answer questions, summarize tool results, remove/unstage, retrigger a
  deployment workflow, or release today's staged PRD batch after cutoff.
- Deploy/add/promote/stage requests are handled by a deterministic, confirmation
  -gated deploy Workflow — NOT by you. Use the release-deploy skill only to
  explain that the request will be previewed and require the exact CONFIRM token.
- You must not mutate deployment JSON, dispatch arbitrary workflows, or open
  release PRs from any free-form chat path. A safety plugin enforces this.
"""

# App name follows the ADK convention of matching the agent package directory so
# the `adk` CLI and any eval harness resolve sessions correctly.
ROOT_APP_NAME = "adk_release_agent"


def _model_name() -> str:
    return settings.gemini_model or "gemini-flash-latest"


# Environment words that mark a high-impact PRODUCTION scope.
_PROD_ENV_WORDS = {"prod", "prd", "production"}


def _remove_needs_confirmation(environment: str = "staging", **kwargs) -> bool:
    """Confirm ``remove_from_release`` only when it targets live PROD."""
    return str(environment).lower() in _PROD_ENV_WORDS


def _chat_additional_tools():
    """Read/ops tools surfaced via skill activation.

    When ``adk_confirm_prod_ops`` is on, the high-impact ops mutations are wrapped
    with ADK tool confirmation: ``merge_prod_release`` always confirms; a prod
    ``remove_from_release`` confirms while UAT passes straight through.
    """
    if not settings.adk_confirm_prod_ops:
        return release_tools.ADK_CHAT_TOOLS

    from google.adk.tools import FunctionTool

    wrapped = {
        "merge_prod_release": FunctionTool(
            release_tools.merge_prod_release, require_confirmation=True
        ),
        "remove_from_release": FunctionTool(
            release_tools.remove_from_release, require_confirmation=_remove_needs_confirmation
        ),
    }
    return [wrapped.get(tool.__name__, tool) for tool in release_tools.ADK_CHAT_TOOLS]


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
    return skill_toolset.SkillToolset(
        skills=skills, additional_tools=_chat_additional_tools()
    )


def build_root_agent():
    """Build the single skills-routed ADK chat agent.

    Skills are the router: each SKILL.md declares its ``adk_additional_tools`` in
    frontmatter, so a domain's tools are surfaced to the model only after that
    skill is activated. The release-defining deploy path is intentionally absent
    from this toolset — it runs through the deterministic deploy Workflow
    (:mod:`adk_release_agent.deploy_workflow`). When memory is enabled, the
    ``preload_memory`` tool injects relevant recalled context at the start of each
    turn.
    """
    from google.adk import Agent

    tools = [_skill_toolset()]
    if settings.adk_memory_enabled:
        from google.adk.tools import preload_memory

        tools.append(preload_memory)

    return Agent(
        name="release_copilot_adk",
        model=_model_name(),
        description="ADK Release Copilot: Skills route to scoped tools; deploys run a deterministic Workflow.",
        instruction=ROOT_INSTRUCTION,
        tools=tools,
    )


def build_root_app():
    """Wrap the chat agent in an ``App`` with the safety plugin and ADK 2.x runtime
    features: context caching, event compaction, and (for prod-ops confirmation)
    resumability."""
    from google.adk.agents.context_cache_config import ContextCacheConfig
    from google.adk.apps import App
    from google.adk.apps.app import EventsCompactionConfig

    from .safety import MutationGuardPlugin

    kwargs: dict = {
        "name": ROOT_APP_NAME,
        "root_agent": build_root_agent(),
        "plugins": [MutationGuardPlugin()],
    }
    if settings.adk_confirm_prod_ops:
        from google.adk.apps import ResumabilityConfig

        kwargs["resumability_config"] = ResumabilityConfig(is_resumable=True)
    if settings.adk_context_cache:
        kwargs["context_cache_config"] = ContextCacheConfig(
            min_tokens=settings.adk_context_cache_min_tokens,
            ttl_seconds=settings.adk_context_cache_ttl_seconds,
        )
    if settings.adk_event_compaction:
        kwargs["events_compaction_config"] = EventsCompactionConfig(
            compaction_interval=settings.adk_compaction_interval,
            overlap_size=settings.adk_compaction_overlap,
        )
    return App(**kwargs)


try:
    app = build_root_app()
    root_agent = app.root_agent
    ADK_IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    if exc.name and (exc.name == "google.adk" or exc.name.startswith("google.adk")):
        app = None
        root_agent = None
        ADK_IMPORT_ERROR = exc
    else:
        raise
