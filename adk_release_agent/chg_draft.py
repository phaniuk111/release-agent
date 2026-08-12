"""Draft the change-request prose from what the developers actually wrote.

DevOps fills the same five free-text fields every release, from information the
developers already supplied at queue time (chart:version, JIRA key + summary,
"what changed and why"). This turns that structured input into a first draft.

Two hard boundaries, because a change request is a REGULATED RECORD:

* **Nothing is invented.** The model is given only the queued items and is told
  to write "Not provided." for anything the inputs do not support. An LLM
  guessing "Associated risk: Low — no schema changes" when it has no idea
  whether there are schema changes is the exact failure an auditor finds.
* **Nothing is submitted.** This returns a draft into an editable form. The
  human reviews, edits and confirms; the release itself still runs through the
  deterministic preview → CONFIRM path, which never consults this module.

Facts stay deterministic: artifacts, target repo, dates and the release name are
computed, not drafted. Only prose comes from here.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("release_copilot")

_FIELDS = ("change_summary", "change_description", "change_reason",
           "associated_risk", "consequence", "user_impact")

_PROMPT = """You draft the free-text fields of a software change request for a
bank's regulated release process. A human reviews and edits everything you write
before it is submitted.

You are given the exact list of items shipping in this release. Each carries a
chart/image name and version, and MAY carry a JIRA ticket, the ticket summary,
the developer's own "what changed and why", and the requester.

Reply with ONLY a JSON object, no prose, no code fences:
{"change_summary": "<one line naming the release scope>",
 "change_description": "<what is shipping, one bullet per item>",
 "change_reason": "<why this is being released>",
 "associated_risk": "<risk this change carries>",
 "consequence": "<what happens if it is NOT released>",
 "user_impact": "<effect on users/services, including downtime>"}

RULES — these matter more than being helpful:
- Use ONLY the information given below. Never infer schema changes, downtime,
  rollback plans, testing, approvals, or anything else not stated.
- If the inputs do not support a field, write exactly: Not provided.
- Never state that risk is low/high, or that there is no user impact, unless a
  developer's own text says so. Absence of information is NOT evidence of safety.
- Name the concrete charts and JIRA keys you were given; do not invent keys.
- Keep each field under 60 words. Plain factual English, no marketing.

ITEMS IN THIS RELEASE:
"""


def _render_items(items: list[dict[str, Any]]) -> str:
    lines = []
    for item in items:
        name = f"{item.get('artifact_name', '')}:{item.get('artifact_version', '')}"
        parts = [f"- {name}"]
        if item.get("jira_ticket"):
            parts.append(f"JIRA {item['jira_ticket']}")
        if item.get("jira_summary"):
            parts.append(f"ticket summary: {item['jira_summary']}")
        if item.get("change_details"):
            parts.append(f"developer says: {item['change_details']}")
        if item.get("note"):
            parts.append(f"note to DevOps: {item['note']}")
        if item.get("requested_by"):
            parts.append(f"requested by {item['requested_by']}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


def draft_change_request(items: list[dict[str, Any]], kind: str = "care") -> dict[str, Any]:
    """One model call -> draft prose for the change-request fields.

    Returns {"ok": True, "draft": {...}, "grounded_on": N} or
    {"ok": False, "error": ...}. Never raises: a drafting failure must leave the
    form usable, exactly as if the button had not been pressed.
    """
    if not items:
        return {"ok": False, "error": "Nothing selected — tick the items to include first."}

    context = _PROMPT + _render_items(items)
    if kind == "df":
        context += ("\n\nNOTE: this is a Dataflow release. These are flex-template images "
                    "deployed by workflow dispatch, not Helm charts.")
    try:
        from google import genai

        client = genai.Client()
        response = client.models.generate_content(
            model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
            contents=context,
            # Deterministic: the same queue must not produce different change
            # records on a second press.
            config={"temperature": 0.0, "response_mime_type": "application/json"},
        )
        data = json.loads((response.text or "").strip())
    except Exception as e:
        logger.info("CHG drafting unavailable: %s", e)
        return {"ok": False, "error": f"Could not draft ({e}). Fill the fields manually."}

    if not isinstance(data, dict):
        return {"ok": False, "error": "Drafting returned an unexpected shape."}

    draft = {field: str(data.get(field) or "").strip() for field in _FIELDS}
    if not any(draft.values()):
        return {"ok": False, "error": "Drafting returned nothing usable."}
    return {"ok": True, "draft": draft, "grounded_on": len(items)}
