"""App-level safety guardrails for the ADK release copilot.

The free-form chat agent is only ever wired with read-only and tightly-scoped
ops tools. Release-*defining* mutations (opening a release PR, writing deployment
JSON, dispatching arbitrary workflows) must only happen through the deterministic,
confirmation-gated deploy Workflow — never as a side effect of a free-form chat
turn.

``MutationGuardPlugin`` enforces that boundary in code rather than trusting the
model to obey the system instruction. Registered on the ``App``, its
``before_tool_callback`` runs before every tool the chat agent invokes; if the
tool is a blocked release-defining mutation, it short-circuits the call by
returning an error dict (the tool never executes).
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("release_copilot")

try:
    from google.adk.plugins.base_plugin import BasePlugin
    from google.adk.tools.base_tool import BaseTool
    from google.adk.tools.tool_context import ToolContext

    _ADK_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised only without google-adk
    BasePlugin = object  # type: ignore[assignment,misc]
    BaseTool = Any  # type: ignore[assignment,misc]
    ToolContext = Any  # type: ignore[assignment,misc]
    _ADK_AVAILABLE = False


# Release-defining mutations that must never run from the free-form chat path.
# Kept in sync with ``adk_release_agent.tools.RELEASE_DEFINING_MUTATIONS`` plus the
# confirmed-apply entrypoint, which belongs to the deterministic deploy Workflow.
BLOCKED_FREEFORM_TOOLS = frozenset(
    {
        "open_release_pr",
        "apply_json_update",
        "dispatch_workflow",
        "apply_confirmed_deploy",
        "deploy_dataflow",
    }
)


def _blocked_response(tool_name: str) -> dict[str, Any]:
    return {
        "error": (
            f"Tool '{tool_name}' is blocked in free-form chat. Release-defining "
            "mutations only run through the deterministic, confirmation-gated "
            "deploy Workflow."
        ),
        "error_code": "MUTATION_BLOCKED",
        "blocked_tool": tool_name,
    }


class MutationGuardPlugin(BasePlugin):
    """Blocks release-defining mutation tools from the free-form chat path."""

    def __init__(self, name: str = "mutation_guard") -> None:
        super().__init__(name=name)

    async def before_tool_callback(  # type: ignore[override]
        self,
        *,
        tool: "BaseTool",
        tool_args: dict[str, Any],
        tool_context: "ToolContext",
    ) -> dict[str, Any] | None:
        """Return a block result (short-circuit) for blocked tools, else None."""
        tool_name = getattr(tool, "name", "") or ""
        if tool_name in BLOCKED_FREEFORM_TOOLS:
            return _blocked_response(tool_name)
        return None


# --- Off-topic screening -----------------------------------------------------
# This portal is reachable only behind the mesh's external auth, so the people
# typing in it are our own developers: off-topic use is a SPEND and hygiene
# problem, not an attack. It is screened here rather than left to the system
# instruction because an instruction is advice and this is a budget.
#
# Deliberately NOT an injection defence. It sees the user's typed message only —
# never tool output — so the PR-body / JIRA-text path is untouched by it (that is
# the data-boundary rule in ROOT_INSTRUCTION). Someone who sprinkles release
# words into an off-topic question gets past it, and that is fine: the cost of
# doing so is caring enough to try, and the outcome is still only an answer.

# Domain signal. Any of these and the message is ours — no model call, no cost.
_SCOPE_WORDS = frozenset(
    """release releases released deploy deploys deployed deployment deploying
    promote promoted promotion queue queued withdraw chart charts helm image
    images tag tags version versions build builds control controls rctld rlft
    rfrl rftl pr prs pull request jira chg rmg uat prd prl1 sit prod production
    staging environment env manifest artifact artifactory dataflow df composer
    dag dags rollback pipeline workflow run runs onboard onboarding api apis
    endpoint credentials auth cutoff window""".split()
)
# Environments and other bare tokens that carry meaning on their own.
_SCOPE_PREFIXES = ("confirm-",)


def _looks_in_scope(text: str) -> bool:
    """Deterministic first pass — free, and true for normal traffic.

    Also true for the short continuations a conversation is made of ("yes",
    "the second one", a CONFIRM token): screening those would strand somebody
    mid-deploy, which is a worse failure than answering one stray question.
    """
    raw = (text or "").strip()
    if not raw:
        return True
    low = raw.lower()
    if any(low.startswith(p) for p in _SCOPE_PREFIXES):
        return True
    # Short replies are continuations of an existing turn, not new topics.
    words = [w.strip(".,!?:;'\"()[]{}") for w in low.split()]
    if len(words) <= 4:
        return True
    if any(w in _SCOPE_WORDS for w in words):
        return True
    # "payments-api:2.0.4" style references count even without a keyword.
    return any(":" in w and not w.startswith("http") for w in words)


_CLASSIFY_PROMPT = """You screen ONE chat message for a software RELEASE portal.

The portal covers: releases, deploys and promotions between environments; the
next-release intake queue; build verification and release controls; deployment
pull requests; release history and what is deployed where; and guiding API
CONSUMERS through onboarding. It also covers BACKGROUND questions about any of
that ("what is a helm chart?", "how does the queue work?", "what can you do?").

Crucially, it also covers SHORT or VAGUE questions from someone already working
in the portal — "why did my thing fail?", "what do I need to do next?", "who
added that and when?", "is it safe to ship today?". These are in scope: the
missing detail is context the portal has, not evidence of a different topic.

OUT of scope means the message is plainly about something else entirely:
general-purpose programming help, homework, general knowledge, translation,
creative writing, personal advice, current events, casual chat.

Reply with ONLY a JSON object, no prose, no code fences:
{"in_scope": true or false}

When genuinely unsure, answer true.

Message: """


def _classify_in_scope(message: str) -> bool | None:
    """One cheap call -> in/out, or None on ANY failure (caller allows)."""
    import json

    from ._genai import classifier_client

    from release_agent.config import settings

    client = classifier_client()
    response = client.models.generate_content(
        model=settings.gemini_model or "gemini-2.5-flash",
        contents=_CLASSIFY_PROMPT + message.strip(),
        config={"temperature": 0.0, "response_mime_type": "application/json"},
    )
    data = json.loads((response.text or "").strip())
    value = data.get("in_scope") if isinstance(data, dict) else None
    return bool(value) if isinstance(value, bool) else None


class ScopeGuardPlugin(BasePlugin):
    """Screens free-form chat for questions this portal is not for.

    Registered on the ``App``, its ``before_model_callback`` runs before the LLM
    request: returning an ``LlmResponse`` short-circuits the turn, so a refused
    message never reaches the expensive call.

    Fails OPEN by design. This protects spend, not correctness — a classifier
    timeout must not take the portal down, so anything uncertain is allowed
    through.
    """

    #: Kept as an attribute so tests can assert on it without matching prose.
    REFUSAL = (
        "That's outside what this portal does. I cover releases and deploys, the "
        "next-release queue, build controls, deployment PRs, what's deployed where "
        "— and onboarding consumers to our APIs. Ask me one of those and I'm useful."
    )

    def __init__(self, name: str = "scope_guard", mode: str | None = None) -> None:
        super().__init__(name=name)
        self._mode = mode

    @property
    def mode(self) -> str:
        """off | log | enforce — read late so tests and env changes take effect."""
        if self._mode is not None:
            return self._mode
        from release_agent.config import settings

        return (settings.scope_guard or "log").strip().lower()

    @staticmethod
    def _classify(text: str) -> bool | None:
        """Seam for tests, and the place a classifier failure is swallowed."""
        try:
            return _classify_in_scope(text)
        except Exception:
            logger.warning("scope_guard: classifier unavailable — allowing", exc_info=True)
            return None

    @staticmethod
    def _latest_user_text(llm_request: Any) -> str:
        for content in reversed(getattr(llm_request, "contents", None) or []):
            if getattr(content, "role", None) != "user":
                continue
            for part in getattr(content, "parts", None) or []:
                if getattr(part, "text", None):
                    return part.text
        return ""

    async def before_model_callback(  # type: ignore[override]
        self,
        *,
        callback_context: Any,
        llm_request: Any,
    ) -> Any:
        mode = self.mode
        if mode == "off":
            return None
        try:
            text = self._latest_user_text(llm_request)
            # Stage 1 is an ALLOW path only. On its own it refuses far too much
            # — "why did my thing fail yesterday?" carries no domain word but is
            # exactly what this portal is for — so a miss here decides nothing.
            if _looks_in_scope(text):
                return None
            # Stage 2 decides. Anything other than a confident false is allowed.
            if self._classify(text) is not False:
                return None
        except Exception:
            logger.exception("scope_guard: could not screen the message — allowing it")
            return None

        if mode != "enforce":
            logger.info("scope_guard[log]: would refuse as off-topic: %r", text[:200])
            return None

        logger.info("scope_guard: refused as off-topic: %r", text[:200])
        from google.adk.models.llm_response import LlmResponse
        from google.genai import types

        return LlmResponse(
            content=types.Content(role="model", parts=[types.Part(text=self.REFUSAL)])
        )
