"""LLM fallback for free-form deploy intent.

The deterministic router (:func:`adk_release_agent.deploy._request_from_inputs`)
only recognizes promote-verb + ``chart:version`` phrasings, so politely-phrased
commands like "can you get payments-api:1.2.3 to prod" fall through to the chat
lane, which cannot deploy. This module recovers those: when the deterministic
parse misses but the message *sounds* deploy-ish, one cheap model call
classifies the intent and — only when a concrete chart AND version are named —
produces the canonical JSON payload the deploy Workflow already accepts.

Safety: this is routing only. The payload still goes through the SAME
deterministic Workflow — preview, CONFIRM-token pause, mutation-guard plugin.
A misclassification can at worst show a preview nobody confirms; it can never
mutate anything. On any model error/ambiguity we return None and the message
falls back to the chat lane exactly as before.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger("release_copilot")

# Words that make a free-form message worth one classifier call. Deliberately
# broad — a false positive costs one flash call, a false negative costs the
# user a dead end. The classifier is the decider, not this filter.
_ENV_WORDS = {"uat", "prod", "prd", "production"}
_ACTION_WORDS = {
    "deploy", "deployment", "promote", "release", "ship", "push", "send",
    "get", "put", "move", "bump", "install", "upgrade", "rollout", "roll",
    "stage", "add",
}

_CLASSIFY_PROMPT = """You classify ONE chat message for a Helm-chart release copilot.

Decide whether the message is a COMMAND to deploy a specific chart VERSION to an
environment (uat or prod). It is NOT a deploy command if it is a question, a
status/lookup request, a request to release/ship today's already-staged release,
a removal/unstage request, or if no concrete version is given.

Reply with ONLY a JSON object, no prose, no code fences:
{"is_deploy": bool,
 "chart": "<chart/image name or empty>",
 "version": "<version/tag or empty>",
 "environment": "uat" | "prod",
 "deployment_repo": "<owner/repo if the user names a target repo, else empty>"}

Rules:
- is_deploy=true ONLY when both a chart name and a concrete version appear.
- environment defaults to "uat" unless the message clearly targets production.
- Copy chart/version verbatim (e.g. "payments-api", "1.2.3" from "payments-api:1.2.3"
  or "payments-api 1.2.3").
- deployment_repo only when an owner/repo (or GitHub URL) is explicitly named.

Message: """


def _norm_words(text: str) -> set[str]:
    words, cur = set(), ""
    for ch in text.lower():
        if ch.isalnum():
            cur += ch
        else:
            if cur:
                words.add(cur)
            cur = ""
    if cur:
        words.add(cur)
    return words


def _sounds_deployish(message: str) -> bool:
    """Cheap pre-filter so ordinary chat never pays for a classifier call."""
    words = _norm_words(message)
    return bool(words & _ENV_WORDS) and bool(words & _ACTION_WORDS)


def _classify(message: str) -> dict | None:
    """One model call -> intent dict, or None on any failure."""
    from google import genai

    model = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
    client = genai.Client()
    response = client.models.generate_content(
        model=model,
        contents=_CLASSIFY_PROMPT + message.strip(),
        config={"temperature": 0.0, "response_mime_type": "application/json"},
    )
    text = (response.text or "").strip()
    data = json.loads(text)
    return data if isinstance(data, dict) else None


def deploy_payload_from_freeform(message: str) -> str | None:
    """Free-form English -> canonical deploy-Workflow JSON payload, or None.

    Returns the ``{"environment", "images", "deployment_repo"?}`` JSON string the
    deterministic parser already understands, so the caller can route it into the
    deploy Workflow unchanged. None means "not a deploy — use the chat lane"
    (also on ANY classifier error: fail toward the safe, existing behavior).
    """
    if not _sounds_deployish(message):
        return None
    try:
        intent = _classify(message)
    except Exception:
        logger.info("deploy-intent classifier unavailable; falling back to chat lane")
        return None
    if not intent or not intent.get("is_deploy"):
        return None
    chart = str(intent.get("chart") or "").strip()
    version = str(intent.get("version") or "").strip()
    if not chart or not version:
        return None  # underspecified — let the chat lane ask for chart:version
    env = "prod" if str(intent.get("environment", "")).lower() in ("prod", "prd", "production") else "uat"
    payload: dict = {"environment": env, "images": {chart: version}}
    repo = str(intent.get("deployment_repo") or "").strip()
    if repo:
        payload["deployment_repo"] = repo
    return json.dumps(payload)
