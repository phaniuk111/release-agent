"""Deterministic promote-pipeline nodes: parse -> propose -> gate (HITL) -> apply
-> finalize -> track_pr (plus rerun + respond). The LLM is never on this path."""
from __future__ import annotations

import json
import time
import uuid
from typing import Any, Literal, Optional

from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END
from langgraph.types import Command, interrupt

from ..tools.gh_tools import _find_prs_for_images
from .parsing import (
    _detect_environment,
    _detect_rerun,
    _extract_images_from_text,
    _is_query_not_promote,
    _is_removal,
    _try_parse_json_payload,
)
from .state import (
    ALL_STEPS,
    ReleaseState,
    STEP_APPLY,
    STEP_DISPATCH,
    STEP_RELEASE_PR,
    _ENV_STEPS,
    _STEP_BY_TOOL,
)
from ..config import settings


DEFAULT_WORKFLOW = settings.default_workflow


def parse_intent(state: ReleaseState) -> dict:
    """First node: understand the ask and build release_request (or a re-run).

    Only emits a SystemMessage (added to history, never streamed to the UI) the
    first time it is needed.
    """
    messages = state.messages
    last = messages[-1].content if messages else ""
    if isinstance(last, list):
        last = " ".join(str(x) for x in last)
    last = str(last)

    # NOTE: do not inject a SystemMessage into state here. add_messages would APPEND
    # it after the user's HumanMessage, and Gemini only honors a *leading* system
    # instruction. The supervisor and each specialist supply their own system prompt
    # at call time (create_agent's `system_prompt=`), so state stays prompt-free.
    out: dict[str, Any] = {}

    # A re-run request reuses the prior release_request/steps — don't re-parse images.
    rerun = _detect_rerun(last, state.steps)
    if rerun is not None:
        out["rerun_steps"] = rerun
        return out

    # A remove/unstage request must NOT be parsed as a promote (a tagged
    # "remove orders-api:v1.1.0" would otherwise look like a stage). Route it to
    # the LLM, which calls remove_from_release.
    if _is_removal(last):
        out["release_request"] = None
        out["rerun_steps"] = None
        return out

    # A pasted JSON payload (the deploy form's entry) is the preferred structured input.
    payload = _try_parse_json_payload(last)
    if payload is not None:
        out["release_request"] = payload
        out["rerun_steps"] = None
        return out

    pairs = _extract_images_from_text(last)
    # A message that names a chart:version but reads as a question (e.g. "find the PR
    # for payments-api:2.0.1") is a lookup, not a deploy — send it to the LLM.
    if pairs and _is_query_not_promote(last):
        pairs = []
    out["release_request"] = (
        {
            "images": pairs,
            "environment": _detect_environment(last),
            "namespace": "",
            "raw": last[:300],
        }
        if pairs
        else None
    )
    out["rerun_steps"] = None
    return out


def _route_after_parse(state: ReleaseState) -> Literal["propose", "llm", "rerun"]:
    if state.rerun_steps is not None:
        return "rerun"
    req = state.release_request
    return "propose" if req and req.get("images") else "llm"


def _steps_for_request(req: dict) -> list[str]:
    """uat/prod promotes update the env config + open a PR; otherwise the legacy
    manifest-commit + workflow-dispatch path."""
    env = (req.get("environment") or "prod").lower()
    return list(_ENV_STEPS) if env in ("uat", "prod") else list(ALL_STEPS)


def _build_step_call(step: str, req: dict, token: str) -> dict:
    """Construct the tool_call for a single re-runnable step from the request."""
    image_str = ",".join(f"{i['name']}:{i['tag']}" for i in req.get("images", []))
    if step == STEP_APPLY:
        return {
            "name": "apply_json_update",
            "args": {
                "image_tags": image_str,
                "commit_message": f"chore(release): promote {image_str} via chat (token {token})",
            },
            "id": f"call_{STEP_APPLY}_{uuid.uuid4().hex[:8]}",
        }
    if step == STEP_DISPATCH:
        return {
            "name": "dispatch_workflow",
            "args": {"workflow": DEFAULT_WORKFLOW, "image_tags": image_str},
            "id": f"call_{STEP_DISPATCH}_{uuid.uuid4().hex[:8]}",
        }
    if step == STEP_RELEASE_PR:
        env = (req.get("environment") or "uat").lower()
        entries = req.get("entries") or []
        if entries:
            # Full editor entries -> override the whole file (multi-chart, per-entry fields).
            args = {"environment": env, "deployment_json": json.dumps({"include": entries})}
        else:
            args = {"environment": env, "image_tags": image_str}
            ns = (req.get("namespace") or "").strip()
            if ns:
                args["namespace"] = ns
            cd = (req.get("chart_dir") or "").strip()
            if cd:
                args["chart_dir"] = cd
            vf = (req.get("values_file") or "").strip()
            if vf:
                args["values_file"] = vf
        return {
            "name": "open_release_pr",
            "args": args,
            "id": f"call_{STEP_RELEASE_PR}_{uuid.uuid4().hex[:8]}",
        }
    raise ValueError(f"unknown step {step}")


def propose(state: ReleaseState) -> Command[Literal["gate", "respond"]]:
    """Assemble the deployment.json entry(ies) the deploy will write, show them for
    confirmation, mint the (stable) token, and go to the human gate.

    No mutation and no LLM here — this is a deterministic preview of the exact JSON
    that the confirmed apply step will upsert. uat -> uat/deployment.json; prod ->
    BOTH uat/deployment.json and prd/deployment.json (each with its env values file
    + namespace)."""
    from ..tools.gh_tools import assemble_entry, plan_deploy, _normalize_entry

    req = state.release_request
    if not req or not req.get("images"):
        return Command(
            goto="respond",
            update={
                "messages": [
                    AIMessage(
                        content=(
                            "I didn't find any chart:version pairs. Try: "
                            "`deploy abc-client-api-svc:1.1.1230 to uat`"
                        )
                    )
                ]
            },
        )

    env = (req.get("environment") or "uat").lower()
    env = "prod" if env in ("prod", "prd", "production") else "uat"
    namespace = (req.get("namespace") or "").strip()
    chart_dir = (req.get("chart_dir") or "").strip()
    values_file = (req.get("values_file") or "").strip()
    pairs = req["images"]
    entries = req.get("entries") or []

    # Build the target-env entries: full entries from the UI editor (multi-chart,
    # per-entry fields), else assembled from name:version pairs. plan_deploy then maps
    # them to the OVERRIDE writes — so this preview equals exactly what apply will write.
    if entries:
        target_entries = [_normalize_entry(e, env) for e in entries]
    else:
        target_entries = [
            assemble_entry(i["name"], i["tag"], env, namespace, chart_dir, values_file) for i in pairs
        ]
    preview = plan_deploy(env, target_entries)  # {path: include[]}

    chart_str = ", ".join(f"{e['helm_chart_name']}:{e['helm_chart_version']}" for e in target_entries)
    token = f"CONFIRM-{uuid.uuid4().hex[:6]}"
    if env == "prod":
        action = (
            "will be **added to today's PRD release PR** (a day-long PR holding both the uat & prd "
            "entries below); after the cutoff, *release prod* promotes the staged charts through "
            "SIT→UAT→PRD"
        )
    else:
        action = f"will **OVERRIDE** `{' + '.join(preview.keys())}` with"
    msg = (
        f"**Deploy {chart_str} to {env.upper()}** — {action}:\n\n"
        "```json\n" + json.dumps(preview, indent=2) + "\n```\n\n"
        f"Reply `{token}` to confirm."
    )
    return Command(
        goto="gate",
        update={"proposed": preview, "confirmation_token": token, "messages": [AIMessage(content=msg)]},
    )


def _last_tool_message(messages: list) -> Optional[ToolMessage]:
    for m in reversed(messages):
        if isinstance(m, ToolMessage):
            return m
    return None


def confirmation_gate(
    state: ReleaseState, config: RunnableConfig
) -> Command[Literal["apply", "respond"]]:
    """Interrupt for human confirmation.

    NOTE: when resumed, LangGraph re-executes this node from the top, so every
    value used here must be deterministic. The token comes from state (set in
    `propose`) and the proposal is re-parsed from the persisted ToolMessage.
    """
    token = state.confirmation_token or f"CONFIRM-{uuid.uuid4().hex[:6]}"

    # The assembled entries come from `propose` (in state) — deterministic on resume.
    proposed = state.proposed or {}
    req = state.release_request or {}
    env = (req.get("environment") or "uat").lower()
    env = "prod" if env in ("prod", "prd", "production") else "uat"
    files = ", ".join(proposed.keys()) if isinstance(proposed, dict) and proposed else "the deployment config"
    action = f"deploy these chart(s) to **{env.upper()}** (upserts `{files}`)"

    user_reply = interrupt(
        {
            "type": "confirmation",
            "token": token,
            "proposed": proposed,
            "environment": env,
            "message": (
                f"Reply with exactly `{token}` (or `yes {token.split('-', 1)[-1]}`) to {action}."
            ),
            "repo": state.repo,
        }
    )

    text = str(user_reply).strip().lower() if user_reply is not None else ""
    expected = token.lower()
    suffix = expected.split("-", 1)[1] if "-" in expected else expected
    confirmed = expected in text or (text.startswith("yes") and suffix in text)

    if confirmed:
        return Command(goto="apply", update={"confirmation_token": token, "proposed": proposed})
    return Command(
        goto="respond",
        update={
            "messages": [
                AIMessage(
                    content=(
                        f"❌ Not confirmed (received: {user_reply!r}). No changes were applied.\n"
                        f"Send the exact token `{token}` to proceed, or start a new request."
                    )
                )
            ]
        },
    )


def build_apply_and_dispatch(state: ReleaseState) -> dict:
    """After confirmation, craft an AIMessage whose tool_calls perform the real
    mutation + workflow dispatch. ToolNode (apply_tools) executes every step.
    """
    req = state.release_request or {}
    image_str = ",".join(f"{i['name']}:{i['tag']}" for i in req.get("images", []))
    token = state.confirmation_token or "CONFIRM-xxx"
    env = (req.get("environment") or "prod").lower()

    steps = _steps_for_request(req)
    calls = [_build_step_call(s, req, token) for s in steps]
    content = (
        f"Opening a release PR to promote {image_str} to {env}…"
        if STEP_RELEASE_PR in steps
        else "Applying the manifest update and dispatching the workflow…"
    )
    ai = AIMessage(content=content, tool_calls=calls)  # type: ignore[arg-type]
    return {
        "messages": [ai],
        "last_action": {"phase": "confirmed-apply", "images": image_str, "env": env},
    }


def rerun(state: ReleaseState) -> Command[Literal["apply_tools", "respond"]]:
    """Re-run one or more previously-executed steps by name (no re-confirmation —
    the action was already confirmed in this thread)."""
    req = state.release_request or {}
    steps = state.rerun_steps or []

    if not req.get("images"):
        return Command(
            goto="respond",
            update={
                "rerun_steps": None,
                "messages": [
                    AIMessage(
                        content=(
                            "There's no prior release in this thread to re-run. "
                            "Start with a promote, e.g. `promote payments-api:2.0.33 to prod`."
                        )
                    )
                ],
            },
        )

    valid = _steps_for_request(req)
    if not steps:
        names = ", ".join(f"`{s}`" for s in valid)
        return Command(
            goto="respond",
            update={
                "rerun_steps": None,
                "messages": [
                    AIMessage(
                        content=(
                            f"Which step would you like to re-run? Available steps: {names}.\n"
                            f"Reply e.g. `re-run {valid[0]}`, `re-run all`, or `re-run failed`."
                        )
                    )
                ],
            },
        )

    token = state.confirmation_token or f"RERUN-{uuid.uuid4().hex[:4]}"
    calls = [_build_step_call(s, req, token) for s in steps if s in valid]
    if not calls:
        return Command(
            goto="respond",
            update={
                "rerun_steps": None,
                "messages": [
                    AIMessage(content=f"No matching step to re-run. Available: {', '.join(valid)}.")
                ],
            },
        )
    ai = AIMessage(content=f"Re-running step(s): {', '.join(steps)}…", tool_calls=calls)  # type: ignore[arg-type]
    return Command(goto="apply_tools", update={"messages": [ai], "rerun_steps": None})


def _summarize_step_result(step: str, content: str) -> str:
    """Turn a step's raw tool output into a short human-readable detail line."""
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return content[:140]
    if step == STEP_APPLY:
        url = data.get("url") or ""
        note = data.get("note")
        base = f"manifest committed — {url}".rstrip(" —") if url else "manifest committed"
        return f"{base} ({note})" if note else base
    if step == STEP_DISPATCH:
        wf = data.get("workflow", DEFAULT_WORKFLOW)
        inp = json.dumps(data["inputs"]) if data.get("inputs") else ""
        return f"dispatched `{wf}`" + (f" with {inp}" if inp else "")
    if step == STEP_RELEASE_PR:
        action = data.get("action")
        if action == "pr_already_open":
            return (f"⏸ a deploy PR is already open (#{data.get('pr_number')} {data.get('pr_url','')}) — "
                    "merge or close it first, then retry")
        if action == "no_change":
            return data.get("note", "no change — already deployed with that version")
        if action == "staged_to_prd_pr":
            n = len(data.get("charts_in_release") or [])
            return (
                f"added to today's PRD release PR [#{data.get('pr_number')}]({data.get('pr_url','')}) — "
                f"{n} chart(s) staged; after {data.get('cutoff_utc')} UTC *release prod* promotes them "
                f"through SIT→UAT→PRD"
            )
        if action in ("prod_released", "release_pending_prd_merge", "nothing_to_release", "removed") and data.get("note"):
            return data["note"]
        env = data.get("environment", "uat")
        files = ", ".join(data.get("files_updated") or [])
        base = f"deployed to {env.upper()} ({files})" if files else f"deployed to {env.upper()}"
        runs = []
        if data.get("deploy_run"):
            dr = data["deploy_run"]
            runs.append(f"UAT run [#{dr['id']}]({dr['url']}) ({dr.get('status') or 'queued'})")
        if data.get("deploy_run_prd"):
            dr = data["deploy_run_prd"]
            runs.append(f"PRD run [#{dr['id']}]({dr['url']}) ({dr.get('status') or 'queued'})")
        if runs:
            base += " — " + "; ".join(runs)
        return base
    return "ok"


def finalize(state: ReleaseState) -> dict:
    """Attribute each tool result to its step, update per-step status, and emit a
    step list the user can selectively re-run by name."""
    msgs = state.messages

    # The most recent AIMessage with tool_calls is the batch we just executed
    # (full apply OR a partial re-run). Map each tool_call id -> step label.
    last_ai = next(
        (m for m in reversed(msgs) if isinstance(m, AIMessage) and getattr(m, "tool_calls", None)),
        None,
    )
    id_to_step: dict[str, str] = {}
    if last_ai:
        for tc in last_ai.tool_calls:
            id_to_step[tc.get("id")] = _STEP_BY_TOOL.get(tc.get("name"), tc.get("name"))

    batch: dict[str, str] = {}
    for m in msgs:
        if isinstance(m, ToolMessage) and m.tool_call_id in id_to_step:
            batch[id_to_step[m.tool_call_id]] = str(m.content)

    # Steps to report: this batch's steps (in order) merged with any persisted.
    order: list[str] = list(dict.fromkeys(id_to_step.values()))
    for s in state.steps or []:
        if s["name"] not in order:
            order.append(s["name"])
    if not order:
        order = list(_steps_for_request(state.release_request or {}))

    steps = {s["name"]: dict(s) for s in (state.steps or [])}
    for name in order:
        steps.setdefault(name, {"name": name, "status": "pending", "detail": "not run yet"})
    for name, content in batch.items():
        if content.startswith("ERROR"):
            steps[name] = {"name": name, "status": "error", "detail": content[:280]}
        else:
            status = "ok"
            try:
                d = json.loads(content)
                if isinstance(d, dict) and (d.get("action") == "pr_already_open" or d.get("ok") is False):
                    status = "blocked"
            except (json.JSONDecodeError, TypeError):
                pass
            steps[name] = {
                "name": name,
                "status": status,
                "detail": _summarize_step_result(name, content),
            }
    steps_list = [steps[n] for n in order]

    icon = {"ok": "✅", "error": "❌", "pending": "⏳", "blocked": "⏸"}
    lines = ["**Release steps:**"]
    for s in steps_list:
        lines.append(f"{icon.get(s['status'], '•')} `{s['name']}` — {s['detail']}")

    failed = [s["name"] for s in steps_list if s["status"] == "error"]
    blocked = [s["name"] for s in steps_list if s["status"] == "blocked"]
    example = steps_list[0]["name"] if steps_list else "all"
    lines.append("")
    if failed:
        lines.append(
            f"⚠️ Failed: {', '.join(f'`{f}`' for f in failed)}. "
            f"Once the cause is resolved, reply `re-run {failed[0]}` "
            "(or `re-run failed` / `re-run all`) to retry just that step."
        )
    elif blocked:
        lines.append(
            "⏸ Nothing was applied — a promote PR is already open on the images config. "
            f"Once it merges or is closed, reply `re-run {blocked[0]}` to retry."
        )
    else:
        lines.append(
            f"All steps succeeded. You can re-run any step by name, e.g. `re-run {example}`, or `re-run all`."
        )
    lines.append("\nAnything else?")

    # The legacy dispatch path opens the PR asynchronously (track_pr finds it);
    # the env release_pr step already returns the PR in its result above.
    if not failed and any(s["name"] == STEP_DISPATCH for s in steps_list):
        lines.append("🔎 Locating the deployment PR the workflow is opening…")

    # Clear the one-shot confirmation token (a fresh one is minted per promote).
    return {
        "messages": [AIMessage(content="\n".join(lines))],
        "steps": steps_list,
        "confirmation_token": None,
        "rerun_steps": None,
    }


def _route_after_finalize(state: ReleaseState) -> Literal["track_pr", "__end__"]:
    steps = state.steps or []
    dispatched_ok = any(s.get("name") == STEP_DISPATCH and s.get("status") == "ok" for s in steps)
    has_images = bool((state.release_request or {}).get("images"))
    return "track_pr" if (dispatched_ok and has_images) else END


def track_pr(state: ReleaseState) -> dict:
    """After a successful dispatch, poll the deployment repo for the PR the
    workflow opens (it's created asynchronously) and report its number/URL so the
    user never has to find or supply it."""
    req = state.release_request or {}
    image_str = ",".join(f"{i['name']}:{i['tag']}" for i in req.get("images", []))

    # Snapshot PRs that already match BEFORE the new one is created, so that
    # re-promoting the same tag picks the freshly-opened PR — not an old one.
    seen_before = {p["number"] for p in _find_prs_for_images(image_str, limit=20)}

    pr = None
    # ~36s budget: the dispatched workflow needs to queue, run, and open the PR.
    for _ in range(12):
        new_prs = [
            p for p in _find_prs_for_images(image_str, limit=20) if p["number"] not in seen_before
        ]
        if new_prs:
            pr = max(new_prs, key=lambda p: p["number"])  # the just-opened one
            break
        time.sleep(3)

    if pr:
        msg = (
            f"🔗 **Deployment PR opened:** [#{pr['number']}]({pr['url']}) — “{pr['title']}” "
            f"({pr['state']}).\n"
            f"It includes the **CHG** / **RMG** tickets and **RLFT** control gates as a comment. "
            f"Say *summarize PR #{pr['number']}* (or *show its comments*) and I'll report them."
        )
    else:
        # No NEW PR yet. If an existing match is present, point at the newest one;
        # otherwise the workflow is still running.
        existing = _find_prs_for_images(image_str, limit=1)
        if existing:
            p = existing[0]
            msg = (
                f"The workflow is still opening the new PR. The most recent existing PR for "
                f"`{image_str}` is [#{p['number']}]({p['url']}) ({p['state']}). Ask me to "
                f"*summarize PR #{p['number']}* or *find the PR for {image_str}* in a moment for the latest."
            )
        else:
            msg = (
                f"The deployment PR in `{state.deploy_repo}` is still being created by the workflow "
                f"(it runs asynchronously). Ask me to *find the PR for {image_str}* in a few seconds and "
                "I'll pull it up — you won't need the PR number."
            )
    return {"messages": [AIMessage(content=msg)], "last_action": {"phase": "tracked-pr", "pr": pr}}


def respond(state: ReleaseState) -> dict:
    """Terminal responder for the 'not confirmed' / 'nothing actionable' paths.

    The meaningful message was already added by the caller (propose/gate); this
    node just guarantees the turn ends with an assistant message.
    """
    last = state.messages[-1] if state.messages else None
    if isinstance(last, AIMessage) and last.content:
        return {}
    return {
        "messages": [
            AIMessage(
                content=(
                    "Tell me the image:tag pairs you'd like to promote, e.g. "
                    "`promote payments-api:2.0.33 to prod`."
                )
            )
        ]
    }
