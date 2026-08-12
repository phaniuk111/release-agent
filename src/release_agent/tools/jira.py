"""JIRA lookups for the release intake.

Read-only. The developer types a ticket key when queueing; this resolves it so
the ticket can be VERIFIED (a typo'd key would otherwise ship straight into the
change record) and its summary reused in the CHG draft instead of asking the
developer to retype what JIRA already knows.

Credentials are a technical-account email + API token supplied as a Secret. They
are never logged and never returned to a client — only ``jira_configured()`` and
the resolved issue fields leave this module.

Degradation follows the same rule as every other integration here: unconfigured
means the feature is simply absent, and an outage must not block a release. The
two failure modes are deliberately distinguished by the caller — a key that does
not exist is a developer error worth refusing, an unreachable JIRA is not.
"""
from __future__ import annotations

from typing import Any

from ._common import settings


class JiraUnavailable(RuntimeError):
    """JIRA could not be reached / authenticated — infrastructure, not user error."""


class JiraIssueNotFound(LookupError):
    """The key resolved to nothing — a typo or a ticket the account cannot see."""


def jira_configured() -> bool:
    return bool(settings.jira_base_url and settings.jira_user_email and settings.jira_api_token)


def _adf_to_text(node: Any, out: list[str] | None = None) -> str:
    """Flatten Atlassian Document Format to plain text.

    The v3 API returns descriptions as a nested ADF tree, not a string; the CHG
    record wants prose. Unknown node types are walked rather than dropped so a
    macro or table still contributes its text.
    """
    out = [] if out is None else out
    if isinstance(node, dict):
        if node.get("type") == "text" and isinstance(node.get("text"), str):
            out.append(node["text"])
        for child in node.get("content") or []:
            _adf_to_text(child, out)
        if node.get("type") in ("paragraph", "heading", "listItem"):
            out.append("\n")
    elif isinstance(node, list):
        for child in node:
            _adf_to_text(child, out)
    return "".join(out).strip()


def get_issue(key: str) -> dict[str, Any]:
    """Resolve a ticket to {key, summary, status, assignee, url, description}.

    Raises JiraIssueNotFound for a 404 and JiraUnavailable for anything else, so
    the caller can refuse a bad key while tolerating an outage.
    """
    import requests

    key = (key or "").strip()
    if not key:
        raise JiraIssueNotFound("no issue key given")
    if not jira_configured():
        raise JiraUnavailable("JIRA is not configured (JIRA_BASE_URL/EMAIL/API_TOKEN)")

    base = settings.jira_base_url.rstrip("/")
    try:
        # requests honours HTTPS_PROXY / REQUESTS_CA_BUNDLE, so the corporate
        # proxy and CA set up for GitHub cover this call too.
        response = requests.get(
            f"{base}/rest/api/3/issue/{key}",
            auth=(settings.jira_user_email, settings.jira_api_token),
            headers={"Accept": "application/json"},
            params={"fields": "summary,status,assignee,description"},
            timeout=settings.jira_timeout_seconds,
        )
    except Exception as e:      # network, DNS, TLS, proxy
        raise JiraUnavailable(str(e)) from e

    if response.status_code == 404:
        raise JiraIssueNotFound(f"{key} does not exist (or this account cannot see it)")
    if response.status_code in (401, 403):
        # Not the developer's fault, and the token must not appear in the message.
        raise JiraUnavailable(
            f"JIRA rejected the service account ({response.status_code}) — check the token"
        )
    if response.status_code >= 400:
        raise JiraUnavailable(f"JIRA returned HTTP {response.status_code}")

    try:
        fields = (response.json() or {}).get("fields") or {}
    except ValueError as e:
        raise JiraUnavailable("JIRA returned a non-JSON response") from e

    status = (fields.get("status") or {}).get("name") or ""
    assignee = (fields.get("assignee") or {}).get("emailAddress") or ""
    return {
        "key": key,
        "summary": fields.get("summary") or "",
        "status": status,
        "assignee": assignee,
        "url": f"{base}/browse/{key}",
        "description": _adf_to_text(fields.get("description")),
    }
