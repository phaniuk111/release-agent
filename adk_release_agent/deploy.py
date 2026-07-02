"""Deterministic deploy workflow for the ADK migration.

This module is intentionally regular Python, not prompt logic. ADK can expose
these functions as tools, but the deploy path remains:

    parse request -> preview exact deployment JSON -> confirmation token -> apply

The apply step is separated from free-form chat tools and is also wrapped with
ADK tool confirmation when google-adk is installed.
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Any

from release_agent.agent.parsing import (
    _detect_environment,
    _extract_images_from_text,
    _is_query_not_promote,
    _try_parse_json_payload,
)
from release_agent.tools.gh_tools import assemble_entry, plan_deploy, _normalize_entry

from .tools import _invoke_tool


_PENDING_PREVIEWS: dict[str, dict[str, Any]] = {}
_PREVIEW_TTL_SECONDS = 30 * 60


def _cleanup_expired_previews(now: float | None = None) -> None:
    now = time.time() if now is None else now
    expired = [
        token
        for token, payload in _PENDING_PREVIEWS.items()
        if now - float(payload.get("created_at", 0)) > _PREVIEW_TTL_SECONDS
    ]
    for token in expired:
        _PENDING_PREVIEWS.pop(token, None)


def _image_pairs_from_tags(image_tags: str) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    for raw in image_tags.replace(",", " ").split():
        name, sep, tag = raw.partition(":")
        if sep and name and tag:
            pairs.append({"name": name.strip(), "tag": tag.strip()})
    return pairs


def _request_from_inputs(
    message: str = "",
    image_tags: str = "",
    environment: str = "",
    deployment_json: str = "",
    namespace: str = "",
    chart_dir: str = "",
    values_file: str = "",
) -> dict[str, Any] | None:
    source = deployment_json or message
    payload = _try_parse_json_payload(source) if source else None
    if payload is not None:
        if environment:
            env = str(environment).lower()
            payload["environment"] = "prod" if env in ("prod", "prd", "production") else "uat"
        return payload

    pairs = _image_pairs_from_tags(image_tags) if image_tags else _extract_images_from_text(message)
    if pairs and message and _is_query_not_promote(message):
        return None
    if not pairs:
        return None
    env = (environment or _detect_environment(message)).lower()
    env = "prod" if env in ("prod", "prd", "production") else "uat"
    return {
        "images": pairs,
        "entries": [],
        "environment": env,
        "namespace": namespace,
        "chart_dir": chart_dir,
        "values_file": values_file,
        "raw": message[:300] if message else "adk-deploy-tool",
    }


def _build_preview(req: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    env = (req.get("environment") or "uat").lower()
    env = "prod" if env in ("prod", "prd", "production") else "uat"
    entries = req.get("entries") or []
    if entries:
        target_entries = [_normalize_entry(entry, env) for entry in entries]
    else:
        target_entries = [
            assemble_entry(
                image["name"],
                image["tag"],
                env,
                str(req.get("namespace") or ""),
                str(req.get("chart_dir") or ""),
                str(req.get("values_file") or ""),
            )
            for image in req.get("images", [])
        ]
    return plan_deploy(env, target_entries)


def _image_tags(req: dict[str, Any]) -> str:
    return ",".join(f"{image['name']}:{image['tag']}" for image in req.get("images", []))


def _extract_confirmation_token(text: str) -> str:
    for token in str(text).replace("`", " ").replace(",", " ").split():
        cleaned = token.strip().strip(".;:!?)(")
        if cleaned.upper().startswith("CONFIRM-"):
            return cleaned.upper()
    return ""


def prepare_deploy_preview(
    message: str = "",
    image_tags: str = "",
    environment: str = "",
    deployment_json: str = "",
    namespace: str = "",
    chart_dir: str = "",
    values_file: str = "",
) -> dict[str, Any]:
    """Prepare a deploy preview and mint a confirmation token without mutating GitHub.

    Provide either a natural-language message, comma-separated image_tags, or a
    deployment_json payload. The response contains the exact deployment JSON plan
    and a CONFIRM token that must be supplied to apply_confirmed_deploy.
    """
    _cleanup_expired_previews()
    req = _request_from_inputs(
        message=message,
        image_tags=image_tags,
        environment=environment,
        deployment_json=deployment_json,
        namespace=namespace,
        chart_dir=chart_dir,
        values_file=values_file,
    )
    if not req or not req.get("images"):
        return {
            "ok": False,
            "error": "No chart:version pairs found. Try image_tags='abc-client-api-svc:1.1.1230'.",
        }

    env = (req.get("environment") or "uat").lower()
    env = "prod" if env in ("prod", "prd", "production") else "uat"
    req["environment"] = env
    preview = _build_preview(req)
    token = f"CONFIRM-{uuid.uuid4().hex[:6].upper()}"
    _PENDING_PREVIEWS[token] = {
        "request": req,
        "preview": preview,
        "created_at": time.time(),
    }
    return {
        "ok": True,
        "status": "awaiting_confirmation",
        "environment": env,
        "image_tags": _image_tags(req),
        "token": token,
        "proposed": preview,
        "change_request": req.get("change_request"),
        "message": f"Reply with exactly {token} to apply this deploy.",
    }


def apply_confirmed_deploy(confirmation_text: str) -> dict[str, Any]:
    """Apply a previously prepared deploy after exact token confirmation.

    This mutates GitHub by calling the existing open_release_pr tool. In the ADK
    agent this function is wrapped with ADK FunctionTool(require_confirmation=True)
    so the runtime also asks for human approval before executing it.
    """
    _cleanup_expired_previews()
    token = _extract_confirmation_token(confirmation_text)
    pending = _PENDING_PREVIEWS.get(token)
    if not token or pending is None:
        return {
            "ok": False,
            "status": "not_confirmed",
            "error": "No matching pending deploy preview. Run prepare_deploy_preview first.",
        }

    req = pending["request"]
    env = (req.get("environment") or "uat").lower()
    args: dict[str, Any]
    if req.get("entries"):
        args = {"environment": env, "deployment_json": json.dumps({"include": req["entries"]})}
    else:
        args = {"environment": env, "image_tags": _image_tags(req)}
        for key in ("namespace", "chart_dir", "values_file"):
            if req.get(key):
                args[key] = req[key]

    # PROD deploy form: carry change-request details into open_release_pr.
    if req.get("change_request"):
        args["change_request"] = req["change_request"]

    result = _invoke_tool("open_release_pr", args)
    _PENDING_PREVIEWS.pop(token, None)
    result.setdefault("ok", True)
    result["confirmed_token"] = token
    return result


ADK_DEPLOY_TOOLS = [prepare_deploy_preview, apply_confirmed_deploy]
