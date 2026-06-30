"""Release Copilot agent — a supervisor multi-agent over a deterministic promote
pipeline. Split out of the original agent.py; the public surface is unchanged and
re-exported here so `from release_agent.agent import get_compiled_graph` keeps working.
"""

from .state import ReleaseState
from .llm import DEFAULT_MODEL, _get_llm, message_text
from .parsing import _is_question, _is_removal, _is_retrigger, _last_human_text
from .nodes import (
    parse_intent,
    propose,
    confirmation_gate,
    build_apply_and_dispatch,
    rerun,
    finalize,
    track_pr,
    respond,
)
from .graph import build_graph, get_compiled_graph

__all__ = [
    "ReleaseState",
    "DEFAULT_MODEL",
    "_get_llm",
    "message_text",
    "_is_question",
    "_is_removal",
    "_is_retrigger",
    "_last_human_text",
    "parse_intent",
    "propose",
    "confirmation_gate",
    "build_apply_and_dispatch",
    "rerun",
    "finalize",
    "track_pr",
    "respond",
    "build_graph",
    "get_compiled_graph",
]
