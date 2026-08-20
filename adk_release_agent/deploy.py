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
    is_queue_intent,
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
        entry = _PENDING_PREVIEWS.pop(token, None)
        prep = ((entry or {}).get("request") or {}).get("release_prep")
        if prep:
            from release_agent.tools import release_fileset as _rf

            _rf.cleanup_prepared_release(prep)


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
    # "add X:1.2.3 to the NEXT release" is an intake-queue note, not a deploy.
    if pairs and message and (is_queue_intent(message) or _is_query_not_promote(message)):
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
    if req.get("deployment_type") == "dataflow":
        # DF deploy = workflow_dispatch; preview the exact dispatch inputs. The
        # input NAMES are configured per target workflow (DF_DISPATCH_INPUTS), so
        # the preview has to render the mapping — not our internal image/tag
        # wording — or the developer confirms something we do not send.
        from release_agent.config import settings
        from release_agent.tools.dataflow import _dispatch_inputs

        image = req["images"][0]
        preview = {
            f"workflow_dispatch ({settings.df_deploy_workflow})": [
                _dispatch_inputs(image["name"], image["tag"], env)
            ]
        }
        # A DF deploy is only half-done until the Composer DAGs point at the new
        # template, so the version bump is previewed in the SAME confirmation —
        # approving a deploy must mean approving both mutations, not just one.
        dags = req.get("dag_files") or []
        if dags:
            from release_agent.tools import composer as _composer

            bump = _composer.preview_dag_bump(
                dags, image["tag"], env, repo=req.get("composer_repo") or "")
            rows = [
                {"file": c["file"], "version": " → ".join([", ".join(c["from"]), c["to"]])
                 if not c["unchanged"] else f"{c['to']} (already)"}
                for c in bump.get("changes") or []
            ]
            rows += [{"file": p["file"], "version": f"PROBLEM: {p['error']}"}
                     for p in bump.get("problems") or []]
            preview[f"Composer DAG bump ({bump.get('repo')} @ {bump.get('branch')} → PR)"] = rows
        return preview
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
    if req.get("deployment_type") == "release":
        # Live release model: generate the file-set locally NOW (clone + updater
        # script, no push) so the preview shows the real diff, partition and RCTL
        # timeline. Apply then only pushes + opens the release PR.
        from release_agent.tools import release_fileset as _rf

        prep = _rf.prepare_release_fileset(req["release"])
        if not prep.get("ok"):
            return {"ok": False, "error": "; ".join(prep.get("errors", ["release preparation failed"]))}
        req["release_prep"] = prep
        token = f"CONFIRM-{uuid.uuid4().hex[:6].upper()}"
        pending = {"token": token, "request": req, "preview": prep["preview"],
                   "created_at": time.time()}
        _PENDING_PREVIEWS[token] = pending
        return {
            "ok": True,
            # Returned so the Workflow can persist it in ADK session state. The
            # module dict above stays as the in-process path (single pod, and the
            # non-Workflow fallback in adk_service); session state is what makes
            # a confirmation survive a restart or land on another replica.
            "pending": pending,
            "status": "awaiting_confirmation",
            "environment": env,
            "image_tags": prep["release_name"],
            "token": token,
            "proposed": prep["preview"],
            "deployment_repo": "",
            "message": f"Reply with exactly {token} to create this release.",
        }
    preview = _build_preview(req)
    token = f"CONFIRM-{uuid.uuid4().hex[:6].upper()}"
    pending = {"token": token, "request": req, "preview": preview, "created_at": time.time()}
    _PENDING_PREVIEWS[token] = pending
    return {
        "ok": True,
        "pending": pending,          # see the note above — persisted by the Workflow

        "status": "awaiting_confirmation",
        "environment": env,
        "image_tags": _image_tags(req),
        "token": token,
        "proposed": preview,
        "change_request": req.get("change_request"),
        "deployment_repo": req.get("deployment_repo") or "",
        "message": f"Reply with exactly {token} to apply this deploy.",
    }


def apply_confirmed_deploy(
    confirmation_text: str, pending: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Apply a previously prepared deploy after exact token confirmation.

    This mutates GitHub by calling the existing open_release_pr tool. In the ADK
    agent this function is wrapped with ADK FunctionTool(require_confirmation=True)
    so the runtime also asks for human approval before executing it.

    ``pending`` is the preview payload recovered from ADK session state by the
    Workflow. Passing it is what lets a confirmation be applied by a process
    that never served the preview — a replica, or the same pod after a restart.
    Omitted, we fall back to the in-process dict.
    """
    _cleanup_expired_previews()
    token = _extract_confirmation_token(confirmation_text)
    if pending is not None:
        # A payload recovered from session state is keyed by THREAD, so it must
        # still be checked against the token the human typed — otherwise passing
        # a payload would be a way around the confirmation gate entirely.
        if not token or str(pending.get("token") or "") != token:
            pending = None
    else:
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
    if req.get("deployment_type") == "release":
        from release_agent.tools import release_fileset as _rf

        result = _rf.apply_release_fileset(req.get("release_prep") or {})
        _PENDING_PREVIEWS.pop(token, None)
        result.setdefault("ok", True)
        result["confirmed_token"] = token
        return result
    if req.get("deployment_type") == "dataflow":
        image = req["images"][0]
        args = {"environment": env, "image": image["name"], "tag": image["tag"]}
        if req.get("deployment_repo"):
            args["deployment_repo"] = req["deployment_repo"]
        result = _invoke_tool("deploy_dataflow", args)
        _PENDING_PREVIEWS.pop(token, None)
        result.setdefault("ok", True)
        result["confirmed_token"] = token
        # DISPATCH FIRST, then bump. The other order would leave the DAGs
        # pointing at a template that does not exist yet if the build fails;
        # this order leaves a template nothing uses, which is recoverable.
        dags = req.get("dag_files") or []
        if dags and result.get("ok"):
            from release_agent.tools import composer as _composer

            result["dag_bump"] = _composer.apply_dag_bump(
                dags, image["tag"], env, image=image["name"],
                run_url=((result.get("run") or {}) or {}).get("url") or "",
                repo=req.get("composer_repo") or "",
            )
        _record_deploy_event(req, f"dataflow-{env}", result)
        return result
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
    # Deploy form: target deployment repo for this deploy (part of the JSON payload).
    if req.get("deployment_repo"):
        args["deployment_repo"] = req["deployment_repo"]

    result = _invoke_tool("open_release_pr", args)
    _PENDING_PREVIEWS.pop(token, None)
    result.setdefault("ok", True)
    result["confirmed_token"] = token
    _record_deploy_event(req, env, result)
    return result


def _record_deploy_event(req: dict[str, Any], environment: str, result: dict[str, Any]) -> None:
    """Capture a confirmed deploy in the BQ event log (deployment history with
    the target GitHub repo). Best-effort telemetry — never fails the deploy."""
    if not result.get("ok") or result.get("action") in ("no_change", "blocked_prd_pr_open"):
        return
    try:
        from release_agent.tools import release_queue as _rq
        from release_agent.tools._common import active_deploy_repo

        repo = req.get("deployment_repo") or ""
        if not repo:
            try:
                repo = active_deploy_repo()
            except Exception:
                repo = ""
        # PRD staging returns pr_number; the UAT promote chain returns a prs list —
        # record the terminal (last-merged) PR of the chain.
        pr_number = result.get("pr_number")
        if not pr_number:
            prs = [p for p in (result.get("prs") or []) if p.get("number")]
            if prs:
                pr_number = prs[-1].get("number")
        _rq.record_deployment(
            environment=environment,
            artifacts=req.get("images") or [],
            deployment_repo=repo,
            pr_number=int(pr_number) if pr_number else None,
            note=str(result.get("action") or ""),
        )
    except Exception:
        pass
