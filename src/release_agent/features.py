"""Preview features: shipped in the image, shown only to the people testing them.

Two switches, both server-side:

  PREVIEW_GROUPS    pill groups (palette.js GROUPS) only preview users see —
                    the rest of the page never renders them, not even briefly.
  PREVIEW_FEATURES  server features only preview users may USE: the API refuses
                    and the chat's tools refuse. Hiding a pill alone would leave
                    the same capability one chat question away.

PREVIEW_USERS says who that is: verified gateway emails (release_agent.identity)
— never a URL parameter or a typed field, which anyone could set. "*" means
everyone, for a separate testers-only deployment (a second Helm release at its
own path) where the identity gateway is not in play.
"""
from __future__ import annotations

from typing import Any

from .config import settings


def _names(raw: str) -> list[str]:
    return [p.strip() for p in (raw or "").split(",") if p.strip()]


def is_preview_user(caller) -> bool:
    users = {u.lower() for u in _names(settings.preview_users)}
    if "*" in users:
        return True
    return bool(caller and caller.email.lower() in users)


def hidden_groups(caller) -> list[str]:
    return [] if is_preview_user(caller) else _names(settings.preview_groups)


def allowed(feature: str, caller) -> bool:
    return feature not in _names(settings.preview_features) or is_preview_user(caller)


def refusal(feature: str) -> str:
    return f"{feature.capitalize()} is not available yet — it is a preview feature."


def ui_config(caller) -> dict[str, Any]:
    """What the page needs to render this caller's view — no secrets, no emails."""
    preview = is_preview_user(caller)
    return {
        "hiddenGroups": hidden_groups(caller),
        # Shown to testers as a "preview" tag, so nobody mistakes it for released.
        "previewGroups": _names(settings.preview_groups) if preview else [],
        "preview": preview,
    }
