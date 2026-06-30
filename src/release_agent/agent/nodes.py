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
    _extract_change_fields,
    _extract_images_from_text,
    _is_chg_line,
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

    # A pasted JSON payload (multi-image + change_request) is the preferred input.
    payload = _try_parse_json_payload(last)
    if payload is not None:
        out["release_request"] = payload
        out["rerun_steps"] = None
        return out

    # Strip change-ticket lines first so their timestamps (e.g. 2026-07-01T18:00)
    # aren't mis-parsed as image:tag pairs.
    image_text = " ".join(ln for ln in last.splitlines() if not _is_chg_line(ln))
    pairs = _extract_images_from_text(image_text)
    # A message that names an image:tag but reads as a question (e.g. "find the
    # PR for payments-api:2.0.1") is a lookup, not a promote — send it to the LLM.
    if pairs and _is_query_not_promote(last):
        pairs = []
    out["release_request"] = (
        {
            "images": pairs,
            "environment": _detect_environment(last),
            "change_request": _extract_change_fields(last),
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
        env = (req.get("environment") or "prod").lower()
        args = {"environment": env, "image_tags": image_str}
        if env == "prod":
            args["change_request_json"] = json.dumps(req.get("change_request") or {})
        return {
            "name": "open_release_pr",
            "args": args,
            "id": f"call_{STEP_RELEASE_PR}_{uuid.uuid4().hex[:8]}",
        }
    raise ValueError(f"unknown step {step}")


def _prod_controls_summary(req: dict) -> tuple[str, bool, bool]:
    """Fetch each image:tag's build-pipeline release controls (RLFT/RFTL) and return
    (markdown summary, all_passed, all_located). Used to surface PASS/FAIL up front
    on a PRD release and block when a control failed."""
    from ..tools.gh_tools import (
        _get_github_client,
        _build_repo_full,
        _find_build_run,
        _controls_report,
    )

    images = req.get("images") or []
    repo_full = _build_repo_full()
    try:
        repo_obj = _get_github_client().get_repo(repo_full)
    except Exception as e:
        return (f"⚠️ Couldn't reach the build repo `{repo_full}`: {e}", False, False)

    lines, all_passed, all_located = [], True, True
    for im in images:
        name, tag = im.get("name"), im.get("tag")
        try:
            run, err = _find_build_run(repo_obj, name, tag)
            if run is None:
                all_located = False
                lines.append(
                    f"• **{name}:{tag}** — ⚠️ build run not found ({err}); share the run id "
                    "that generated this tag."
                )
                continue
            rep = _controls_report(repo_full, name, tag, run)
            ctrls = rep["controls"]
            if not ctrls:
                all_located = False
                lines.append(
                    f"• **{name}:{tag}** — ⚠️ no control steps in [this run]({rep['run']['url']})."
                )
                continue
            marks = []
            for c in ctrls:
                m = "✅" if c["passed"] else ("❌" if c["failed"] else "⏳")
                marks.append(f"{m} {c['control']}")
            ok = rep["all_controls_passed"]
            all_passed = all_passed and ok
            lines.append(
                f"• **{name}:{tag}** — controls {'PASS' if ok else 'FAIL'} "
                f"([run]({rep['run']['url']})): " + " · ".join(marks)
            )
        except Exception as e:
            all_located = False
            lines.append(f"• **{name}:{tag}** — ⚠️ controls check error: {e}")
    return ("**Build release controls (RLFT/RFTL):**\n" + "\n".join(lines), all_passed, all_located)


def propose(state: ReleaseState) -> Command[Literal["propose_tools", "respond"]]:
    """Craft an AIMessage with a real tool_call so ToolNode can run propose_update.

    Also mints the (stable) confirmation token here and persists it in state so
    the gate node sees the SAME token before and after the interrupt resume.
    """
    req = state.release_request
    if not req or not req.get("images"):
        return Command(
            goto="respond",
            update={
                "messages": [
                    AIMessage(
                        content=(
                            "I didn't find any image:tag pairs. Try: "
                            "`promote payments-api:2.0.33 and orders-api to v1.2.3`"
                        )
                    )
                ]
            },
        )

    # PROD promotions (SIT->UAT->PRD): controls gate, then honor the daily window.
    # Before the cutoff, images are STAGED on UAT (no change request needed). After
    # the cutoff, the single UAT->PRD release PR is raised (change request required).
    env = (req.get("environment") or "prod").lower()
    pre_msgs: list = []
    if env == "prod":
        summary, all_passed, all_located = _prod_controls_summary(req)
        # Fail-closed: a located control that FAILED blocks the promotion.
        if all_located and not all_passed:
            return Command(
                goto="respond",
                update={
                    "messages": [
                        AIMessage(
                            content=(
                                summary
                                + "\n\n❌ One or more build controls **FAILED** — I can't stage this for the "
                                "PRD release until they're resolved and the build is re-run."
                            )
                        )
                    ]
                },
            )

        from ..tools.gh_tools import get_release_status

        status = get_release_status()

        if status.get("locked"):
            p = status.get("prd_pr_today") or {}
            return Command(
                goto="respond",
                update={
                    "messages": [
                        AIMessage(
                            content=(
                                summary
                                + f"\n\n🔒 Today's UAT→PRD release **PR #{p.get('number')}** is already raised "
                                f"({p.get('url', '')}) — the day is locked, no more images can be added."
                            )
                        )
                    ]
                },
            )

        if status.get("cutoff_passed"):
            # Raise path — change request required.
            cr = req.get("change_request") or {}
            if not cr:
                note = summary + "\n\n"
                if not all_located:
                    note += (
                        "Some controls couldn't be auto-located — share the build **run id** that "
                        "generated the tag and I'll verify them.\n\n"
                    )
                return Command(
                    goto="respond",
                    update={
                        "messages": [
                            AIMessage(
                                content=(
                                    note
                                    + f"⏰ The {status.get('cutoff_utc')} UTC cutoff has passed. Raising today's "
                                    "**UAT → PRD** release PR requires a change request (it drives the CHG). Use the "
                                    "**Promote to PROD** action and paste the change-request JSON."
                                )
                            )
                        ]
                    },
                )
            # Production lead time: the change start_date must be tomorrow or later.
            from ..tools.gh_tools import _lead_time_ok

            lead_ok, lead_msg = _lead_time_ok(cr)
            if not lead_ok:
                return Command(
                    goto="respond",
                    update={
                        "messages": [
                            AIMessage(
                                content=(
                                    summary
                                    + f"\n\n📅 Can't raise the release — {lead_msg} Update the change "
                                    "request's `start_date` and resubmit."
                                )
                            )
                        ]
                    },
                )
            pre_msgs.append(
                AIMessage(
                    content=(
                        summary
                        + "\n\n⏰ Cutoff passed — I'll stage these on UAT and **raise today's UAT → PRD "
                        "release PR** (CHG/RMG auto-created). Confirm to proceed."
                    )
                )
            )
        else:
            # Staging path — no change request needed; release happens at the cutoff.
            pre_msgs.append(
                AIMessage(
                    content=(
                        summary
                        + f"\n\n🧺 I'll **stage** these on the **UAT** branch for today's release. The single "
                        f"UAT → PRD PR is raised after **{status.get('cutoff_utc')} UTC**, so more images can be "
                        "added until then. Confirm to stage."
                    )
                )
            )

    image_str = ",".join(f"{i['name']}:{i['tag']}" for i in req["images"])
    token = f"CONFIRM-{uuid.uuid4().hex[:6]}"
    tool_call = {
        "name": "propose_update",
        "args": {"image_tags": image_str},
        "id": f"call_propose_{uuid.uuid4().hex[:8]}",
    }
    ai = AIMessage(content="", tool_calls=[tool_call])  # type: ignore[arg-type]
    return Command(
        goto="propose_tools",
        update={"messages": pre_msgs + [ai], "confirmation_token": token},
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

    # Extract the proposal produced by propose_update (deterministic re-read).
    proposed = state.proposed or {}
    changes: list = []
    tmsg = _last_tool_message(state.messages)
    if tmsg is not None:
        raw = str(tmsg.content)
        if raw.startswith("ERROR"):
            return Command(
                goto="respond",
                update={
                    "messages": [
                        AIMessage(
                            content=(
                                f"⚠️ Could not build a proposal: {raw}\n\n"
                                "(If this is a GitHub error, make sure `GH_TOKEN` is set and "
                                "the repo/manifest path exist.)"
                            )
                        )
                    ]
                },
            )
        try:
            data = json.loads(raw)
            proposed = data.get("proposed", proposed)
            changes = data.get("changes", [])
        except (json.JSONDecodeError, TypeError):
            proposed = {"raw": raw[:500]}

    req = state.release_request or {}
    env = (req.get("environment") or "prod").lower()
    change = req.get("change_request") or {}
    if env == "uat":
        action = "stage these images on the **UAT** branch"
    elif env == "prod":
        # Stage on UAT (before cutoff) vs raise the day's UAT→PRD release PR (after).
        from ..tools.gh_tools import get_release_status

        status = get_release_status() or {}
        if status.get("cutoff_passed"):
            action = "**raise today's UAT → PRD release PR** (CHG/RMG auto-created from the change request)"
        else:
            action = f"**stage** these images on **UAT** (the UAT → PRD PR is raised after {status.get('cutoff_utc')} UTC)"
    else:
        action = "apply these changes and dispatch the workflow"

    user_reply = interrupt(
        {
            "type": "confirmation",
            "token": token,
            "proposed": proposed,
            "changes": changes,
            "environment": env,
            "change_request": change if env == "prod" else {},
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
            return (f"⏸ a promote PR is already open (#{data.get('pr_number')} {data.get('pr_url','')}) — "
                    "merge or close it first, then retry")
        if action == "staged_to_uat":
            imgs = data.get("uat_images") or {}
            base = f"staged on UAT ({len(imgs)} image(s) total)"
            dr = data.get("deploy_run")
            if dr:
                base += f" — UAT deploy run [#{dr['id']}]({dr['url']}) ({dr.get('status') or 'queued'})"
            else:
                base += " — UAT→PRD PR is raised after the cutoff"
            return base
        num, url = data.get("pr_number"), data.get("pr_url") or ""
        base = f"raised UAT→PRD release PR #{num} — {url}" if num else "release updated"
        if data.get("chg"):
            base += f" (CHG {data['chg']}, RMG {data.get('rmg')})"
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
