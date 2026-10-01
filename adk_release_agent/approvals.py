"""Which operations pause for a person's yes/no — one definition for both paths.

The chat agent (LLM on) wraps these tools with ADK tool confirmation; the
commands workflow (LLM_ENABLED=false) pauses on an ADK ``RequestInput``. Both
ask the same question of the same arguments, here, so turning the model on or
off can never change what needs approval.
"""
from __future__ import annotations

# Environment words that mark a high-impact PRODUCTION scope.
PROD_ENV_WORDS = {"prod", "prd", "production"}


def promote_needs_confirmation(target: str = "", **kwargs) -> bool:
    """Confirm release promotion only for terminal environments (PRD / PRL1)."""
    return str(target).lower() in (PROD_ENV_WORDS | {"prl1"})


# Tool name -> its rule. A tool not listed never pauses.
RULES = {
    "promote_release": promote_needs_confirmation,
    "promote_df_release": promote_needs_confirmation,
}


def needs_approval(tool_name: str, args: dict) -> bool:
    rule = RULES.get(tool_name)
    return bool(rule and rule(**(args or {})))
