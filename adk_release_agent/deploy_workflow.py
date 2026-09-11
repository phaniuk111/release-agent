"""Deterministic deploy path expressed as an ADK Workflow graph.

The release-defining deploy flow is intentionally NOT LLM-driven. Previously it
lived as imperative branching inside the FastAPI chat adapter. Here it is a real
ADK ``Workflow`` graph (``google.adk.workflow``) so the preview -> confirm ->
apply sequence is declarative, testable, and enforced by the graph topology:

    START ─▶ deploy_gate ─┬─(confirmed)─▶ apply_deploy ──(await_run)─▶ await_df_run ──(green)─▶ bump_dags
                          └─(rejected)──▶ cancel_deploy

``apply_deploy`` ends the graph for everything except a Dataflow deploy that
names Composer DAGs. Those continue to ``await_df_run``: the DAG PR is raised
only once the flex-template run is green, because until then the version it
points at names a template that may never exist. A run still building when the
in-request wait runs out pauses the node on a ``CHECK-xxxxxx`` token — the same
resumable pattern as the CONFIRM gate, so it survives a restart or a move to
another replica — and re-polls when resumed.

Every node that touches GitHub runs its blocking work on a worker thread. A
``FunctionNode`` calls a sync function straight on the event loop, so a clone or
a burst of API calls there stalled every other request on the pod.

No node lets an exception escape. On ADK 2.9 a failed node re-runs when its
workflow resumes, so a node that raised after creating PRs would create them
again; each failure is returned as an honest outcome instead.

``deploy_gate`` is a human-in-the-loop node: on the first pass it builds the exact
deployment-JSON preview (minting a ``CONFIRM-xxxxxx`` token) and pauses on an ADK
``RequestInput`` interrupt. When the run is resumed with the user's confirmation,
the node re-runs (``rerun_on_resume=True``) and routes to ``apply_deploy`` or
``cancel_deploy``. All three nodes are plain Python functions that reuse the
existing :mod:`adk_release_agent.deploy` helpers, so the graph runs without an LLM
and is fully unit-testable through ``InMemoryRunner``.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict

from . import deploy

try:
    from google.adk.apps import App, ResumabilityConfig
    from google.adk.events.event import Event
    from google.adk.events.request_input import RequestInput
    from google.adk.workflow import FunctionNode, Workflow
    from google.genai import types

    _ADK_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - exercised only without google-adk
    _ADK_AVAILABLE = False


class DeployOutcome(BaseModel):
    """Typed terminal output of the deploy Workflow (used as its ``output_schema``).

    ``extra="allow"`` preserves the arbitrary fields the underlying
    ``open_release_pr`` tool returns (PR url, number, etc.) while giving the known
    fields real types for downstream formatting.
    """

    model_config = ConfigDict(extra="allow")

    ok: bool
    status: str = ""
    token: str = ""
    note: Optional[str] = None
    confirmed_token: Optional[str] = None
    error: Optional[str] = None


# State key used to carry the minted confirmation token from the first (preview)
# pass of the gate node to its resumed (decision) pass.
_TOKEN_STATE_KEY = "deploy_confirm_token"
# The preview payload the apply node needs on resume. Kept in ADK session state
# rather than a module dict so the confirmation can be applied by a process that
# never served the preview — another replica, or this one after a restart.
# FunctionNode binds a node parameter of this name straight from ctx.state
# (parameter_binding="state", the default), so nothing reads the key by hand.
_PENDING_STATE_KEY = "deploy_pending"

# A Dataflow deploy waiting on its run: dispatch context, the apply outcome and
# the DAG request. In state so a CHECK resume on any replica can pick it up.
_DF_WAIT_STATE_KEY = "df_wait"
# The CHECK-xxxxxx token the router matches to resume that wait.
_DF_CHECK_STATE_KEY = "df_check_token"

# App name for the standalone deploy Workflow runner.
DEPLOY_APP_NAME = "release_deploy_workflow"


def _change_request_preview(change_request: Any) -> str:
    """Compact change-request block for the prod deploy preview (empty if none)."""
    if not isinstance(change_request, dict) or not change_request:
        return ""
    cr = change_request
    summary = cr.get("chg_summary") or cr.get("summary") or ""
    description = cr.get("description") or cr.get("change_description") or ""
    start = cr.get("start_date") or cr.get("start_time") or "?"
    end = cr.get("end_date") or cr.get("end_time") or "?"
    return (
        "\n\n**Change request** (→ change-request.json):\n"
        f"- Summary: {summary or '(none)'}\n"
        f"- Window: {start} → {end}\n"
        f"- Description: {description or '(none)'}"
    )


def _preview_text(
    preview: dict[str, Any], token: str, env: str, image_tags: str, change_request: Any = None,
    deployment_repo: str = "",
) -> str:
    """Human-readable preview shown to the user before confirmation."""
    repo_line = f"\n\n**Deployment repo:** `{deployment_repo}`" if deployment_repo else ""
    return (
        f"**Deploy {image_tags} to {str(env).upper()}**\n\n"
        "```json\n" + json.dumps(preview, indent=2) + "\n```"
        + repo_line
        + _change_request_preview(change_request)
        + f"\n\nReply `{token}` to confirm."
    )


async def _deploy_gate(ctx: Any, node_input: str):
    """HITL gate: preview + request confirmation, then route on resume.

    First pass (no ``resume_inputs``): build the preview, emit the preview text,
    and pause on a ``RequestInput`` keyed by the confirmation token.
    Resumed pass: decide ``confirmed`` vs ``rejected`` from the reply payload.
    """
    if not ctx.resume_inputs:
        from release_agent.config import settings

        limit = float(settings.deploy_preview_timeout_seconds)
        try:
            # A release preview clones the deploy repo and runs its updater —
            # seconds of blocking work that must not sit on the event loop.
            result = await asyncio.wait_for(
                asyncio.to_thread(deploy.prepare_deploy_preview, message=node_input),
                timeout=limit,
            )
        except asyncio.TimeoutError:
            result = {"ok": False, "error": (
                f"Building the preview took longer than {int(limit)}s, so it was "
                "abandoned — nothing was changed. Try again; if it keeps happening, "
                "the deploy repo or GitHub is slow to answer.")}
        except Exception as e:  # noqa: BLE001
            result = {"ok": False, "error": f"Could not build the preview: {e}"}
        if not result.get("ok"):
            # Nothing previewable — route to cancel carrying the error.
            yield Event(
                output={"ok": False, "error": result.get("error"), "token": ""},
                route="rejected",
            )
            return

        token = result["token"]
        text = _preview_text(
            result["proposed"],
            token,
            result["environment"],
            result["image_tags"],
            result.get("change_request"),
            result.get("deployment_repo") or "",
        )
        yield Event(
            content=types.Content(role="model", parts=[types.Part.from_text(text=text)]),
            state={
                _TOKEN_STATE_KEY: token,
                _PENDING_STATE_KEY: result.get("pending") or {},
            },
        )
        yield RequestInput(
            interrupt_id=token,
            message=f"Reply with exactly {token} to apply this deploy.",
        )
        return

    # Resumed: recover the token (prefer state; fall back to the sole interrupt id).
    token = ctx.state.get(_TOKEN_STATE_KEY)
    if not token and ctx.resume_inputs:
        token = next(iter(ctx.resume_inputs))
    reply = ctx.resume_inputs.get(token) if token else None
    confirmed = bool(isinstance(reply, dict) and reply.get("confirmed"))
    yield Event(
        output={"token": token or "", "confirmed": confirmed},
        route="confirmed" if confirmed else "rejected",
    )


def _outcome(result: dict[str, Any]) -> dict[str, Any]:
    """A terminal output, validated against DeployOutcome (extra fields kept)."""
    return DeployOutcome(**result).model_dump()


async def _apply_deploy(node_input: dict[str, Any], deploy_pending: dict | None = None):
    """Apply the confirmed deploy via the existing token-gated apply helper.

    ``deploy_pending`` is bound from ctx.state by ADK — it is the preview the
    gate node persisted. Handing it to the apply helper is what removes the
    dependency on the process that served the preview still being alive.

    The token and the pending preview are cleared from session state BEFORE the
    apply runs. A CONFIRM token is single-use: were a later resume able to find
    them, the same change could be applied twice.
    """
    token = (node_input or {}).get("token") or ""
    yield Event(state={_TOKEN_STATE_KEY: None, _PENDING_STATE_KEY: None})
    result = await asyncio.to_thread(
        deploy.apply_confirmed_deploy, token, deploy_pending or None, True
    )
    dag_request = result.pop("dag_request", None)
    if dag_request and result.get("ok"):
        wait = {
            "outcome": result,
            "dag_request": dag_request,
            "run": result.get("run") or None,
            "dispatch": result.get("dispatch") or {},
        }
        yield Event(output=wait, state={_DF_WAIT_STATE_KEY: wait}, route="await_run")
        return
    yield Event(output=_outcome(result), route="done")


async def _cancel_deploy(node_input: dict[str, Any], deploy_pending: dict | None = None):
    """Discard the pending preview and report a non-mutating cancellation.

    A rejected release has a prepared workdir to clean up, and on a replica that
    only exists in the session copy — so the cleanup works from ``deploy_pending``
    as well as from the in-process dict.
    """
    payload = node_input or {}
    token = payload.get("token") or ""
    if token:
        deploy._PENDING_PREVIEWS.pop(token, None)
    prep = ((deploy_pending or {}).get("request") or {}).get("release_prep")
    if prep:
        from release_agent.tools import release_fileset as _rf

        try:
            await asyncio.to_thread(_rf.cleanup_prepared_release, prep)
        except Exception:  # noqa: BLE001 — tidiness only; the cancel still stands
            pass
    yield Event(
        output=_outcome({"ok": False, "status": "cancelled", "token": token,
                         "error": payload.get("error")}),
        state={_TOKEN_STATE_KEY: None, _PENDING_STATE_KEY: None},
    )


def _run_line(run: dict | None) -> str:
    if not run:
        return "the run"
    return f"[Run #{run.get('id')}]({run.get('url')})" if run.get("url") else f"run #{run.get('id')}"


async def _await_df_run(ctx: Any, node_input: Any):
    """Wait for the Dataflow run, then route: green → bump the DAGs.

    Polls for at most ``df_run_wait_seconds`` — the confirm turn must finish
    inside the gateway timeout. Still building after that: pause on a
    ``CHECK-xxxxxx`` token; the router resumes this node when it is sent back,
    and the node re-runs from the top with a fresh budget (``rerun_on_resume``).
    A finished-but-not-green run ends the deploy with the DAGs untouched.
    """
    from release_agent.config import settings
    from release_agent.tools import dataflow as _df

    wait = dict(ctx.state.get(_DF_WAIT_STATE_KEY) or {}) or (
        dict(node_input) if isinstance(node_input, dict) else {})
    if not wait.get("dag_request"):
        yield Event(output=_outcome({
            "ok": False, "status": "df_wait_lost",
            "error": "The Dataflow deploy this was waiting on is no longer recorded — "
                     "check the run in GitHub and bump the Composer DAGs by hand if it went green.",
        }), state={_DF_WAIT_STATE_KEY: None, _DF_CHECK_STATE_KEY: None})
        return

    repo_full = (wait.get("dispatch") or {}).get("repo") or ""
    run = wait.get("run")
    poll = max(1.0, float(settings.df_run_poll_seconds))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + float(settings.df_run_wait_seconds)
    status: dict | None = None
    while True:
        if not run:
            run = await asyncio.to_thread(_df.locate_dispatched_run, wait.get("dispatch") or {})
            if run:
                wait["run"] = run
        if run and repo_full:
            try:
                status = await asyncio.to_thread(_df.df_run_status, repo_full, run["id"])
            except Exception:  # noqa: BLE001 — a blip is "still waiting", not "failed"
                status = None
            if status and status["done"]:
                break
        if loop.time() + poll > deadline:
            break
        state_word = (status or {}).get("status") or "not registered yet"
        yield Event(custom_metadata={"progress": f"Waiting for {_run_line(run)} ({state_word})"})
        await asyncio.sleep(poll)

    outcome = dict(wait.get("outcome") or {})
    if status and status["done"] and status["green"]:
        yield Event(
            output={**wait, "run": {**(run or {}), "url": status.get("url")}},
            state={_DF_WAIT_STATE_KEY: None, _DF_CHECK_STATE_KEY: None},
            route="green",
        )
        return
    if status and status["done"]:
        outcome.update({
            "ok": False, "status": "df_run_failed", "run": status,
            "note": (
                f"{outcome.get('note', '').rstrip()}\n\n{_run_line(status)} finished "
                f"**{status.get('conclusion') or 'unsuccessfully'}**, so the Composer DAGs "
                "were NOT bumped — they still point at the previous version. Fix the build "
                "and deploy again."
            ).strip(),
        })
        yield Event(output=_outcome(outcome), route="done",
                    state={_DF_WAIT_STATE_KEY: None, _DF_CHECK_STATE_KEY: None})
        return

    token = f"CHECK-{secrets.token_hex(3).upper()}"
    seen = (f"still **{status.get('status')}**" if status
            else "not registered by GitHub yet")
    message = (
        f"{_run_line(run)} is {seen} after {int(settings.df_run_wait_seconds)}s. The "
        "Composer DAG PR is raised only once it is green, so the deploy is paused "
        f"here — reply `{token}` to check again."
    )
    yield Event(
        content=types.Content(role="model", parts=[types.Part.from_text(
            text=f"{outcome.get('note', '').rstrip()}\n\n{message}".strip())]),
        state={_DF_WAIT_STATE_KEY: wait, _DF_CHECK_STATE_KEY: token},
    )
    yield RequestInput(interrupt_id=token, message=message)


async def _bump_dags(node_input: dict[str, Any]):
    """Raise the Composer DAG PR now that the run is green. Idempotent."""
    wait = node_input or {}
    run_url = ((wait.get("run") or {}).get("url")) or ""
    bump = await asyncio.to_thread(deploy.apply_dag_request, wait.get("dag_request") or {}, run_url)
    outcome = dict(wait.get("outcome") or {})
    outcome["dag_bump"] = bump
    outcome["run_green"] = True
    yield Event(output=_outcome(outcome), route="done")


def build_deploy_workflow() -> "Workflow":
    """Build the deploy Workflow graph. Requires google-adk to be installed."""
    if not _ADK_AVAILABLE:
        raise RuntimeError("google-adk is not installed; cannot build deploy workflow")

    gate = FunctionNode(func=_deploy_gate, rerun_on_resume=True, name="deploy_gate")
    apply_node = FunctionNode(func=_apply_deploy, name="apply_deploy")
    cancel_node = FunctionNode(func=_cancel_deploy, name="cancel_deploy")
    await_node = FunctionNode(func=_await_df_run, rerun_on_resume=True, name="await_df_run")
    bump_node = FunctionNode(func=_bump_dags, name="bump_dags")

    return Workflow(
        name="release_deploy",
        edges=[
            ("START", gate, {"confirmed": apply_node, "rejected": cancel_node}),
            (apply_node, {"await_run": await_node}),
            (await_node, {"green": bump_node}),
        ],
        output_schema=DeployOutcome,
    )


def build_deploy_app() -> "App":
    """Wrap the deploy Workflow in a resumable ``App`` for the HITL interrupt."""
    if not _ADK_AVAILABLE:
        raise RuntimeError("google-adk is not installed; cannot build deploy app")

    return App(
        name=DEPLOY_APP_NAME,
        root_agent=build_deploy_workflow(),
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
