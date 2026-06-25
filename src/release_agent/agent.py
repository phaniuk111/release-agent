"""
LangGraph Release Copilot agent.

Stateful graph:
- understands release requests from chat
- proposes a manifest diff (no mutation)
- interrupts for human confirmation (HITL)
- on confirmation, applies the JSON update + dispatches the workflow
- otherwise falls back to a normal ReAct LLM loop for free-form questions

The graph has THREE distinct, deterministic paths (no parallel fan-out):

    parse ─┬─(images found)─▶ propose ▶ propose_tools ▶ gate ─┬─▶ apply ▶ apply_tools ▶ finalize ▶ END
           │                                                  └─▶ respond ▶ END   (not confirmed)
           └─(no images)────▶ llm ⇄ llm_tools  (ReAct loop) ──▶ END

Every node has exactly one routing decision, so ToolNode always sees a fresh
AIMessage with tool_calls as its most recent AIMessage.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Annotated, Any, List, Literal, Optional

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt
from pydantic import BaseModel, Field

from .config import settings
from .tools.gh_tools import GH_TOOLS, TARGET_REPO, DEPLOY_REPO, _find_prs_for_images
from .budget import (
    get_budget_tracker,
    check_budget_before_call,
    BudgetInterrupt,
    confirm_budget_continue,
    get_budget_status,
)

DEFAULT_WORKFLOW = settings.default_workflow

# LLM - Vertex AI Gen AI SDK (via langchain-google-genai)
try:
    from langchain_google_genai import ChatGoogleGenerativeAI

    if settings.gcp_project:
        DEFAULT_MODEL = ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=0,
            vertexai=True,
            project=settings.gcp_project,
            location=settings.gcp_location,
        )
    else:
        DEFAULT_MODEL = None
except Exception:
    DEFAULT_MODEL = None


class ReleaseState(BaseModel):
    """Shared state for the release agent using Pydantic for structure and validation."""
    model_config = {"arbitrary_types_allowed": True}
    messages: Annotated[List[AnyMessage], add_messages] = Field(default_factory=list)
    release_request: Optional[dict] = None
    proposed: Optional[dict] = None
    confirmation_token: Optional[str] = None
    last_action: Optional[dict] = None
    # Per-step status of the last apply phase (for reporting + named re-run).
    steps: Optional[List[dict]] = None
    # Set by parse when the user asks to re-run step(s); routes to the rerun node.
    rerun_steps: Optional[List[str]] = None
    repo: str = Field(default=TARGET_REPO)
    deploy_repo: str = Field(default=DEPLOY_REPO)


# ---- Re-runnable apply-phase steps -----------------------------------------
STEP_APPLY = "apply_manifest"
STEP_DISPATCH = "dispatch_workflow"
ALL_STEPS = [STEP_APPLY, STEP_DISPATCH]

# Map the underlying tool name -> canonical step label (used to attribute each
# ToolMessage back to a step via its tool_call).
_STEP_BY_TOOL = {
    "apply_json_update": STEP_APPLY,
    "dispatch_workflow": STEP_DISPATCH,
}

# User-facing words that select a step in a "re-run ..." request.
_STEP_ALIASES = {
    STEP_APPLY: ["apply_manifest", "apply-manifest", "apply", "commit", "manifest"],
    STEP_DISPATCH: ["dispatch_workflow", "dispatch-workflow", "dispatch", "workflow", "trigger"],
}


SYSTEM_PROMPT = f"""You are Release Copilot, an expert release assistant for GitHub-based deployments.

Your job:
- Help users promote container images to production by updating JSON configs and triggering workflows.
- The triggered workflow typically creates a PR in the deployment repo (DEPLOY_REPO).
- You can track that PR, read its comments, and summarize:
  - CHG (change) and RMG (release management) ticket creation and approval status
  - Release control states (e.g. RLFT approval gate / deploy control — open or closed)
  - Overall PR readiness

Deployment governance glossary (these appear in deployment-repo PR comments):
- CHG = a Change ticket that authorizes the change (id looks like CHG-<yyyymm>-<digits>).
- RMG = a Release Management ticket/approval for the release (id looks like RMG-<yyyymm>-<digits>).
- RLFT = release control gates ("RLFT approval gate", "RLFT deploy control") that must be
  closed/approved before a production deployment proceeds.

CRITICAL — never fabricate values. Actual CHG/RMG ticket numbers, PR numbers, and RLFT gate states
live ONLY in the PR comments; they are NOT in these instructions (the formats above are templates,
not real values). Whenever a user asks about a specific release/PR's CHG ticket, RMG ticket, RLFT
gates, approvals, controls, or readiness, you MUST call the tools — find_prs to locate the PR (if you
don't already have its number), then get_pr_comments or summarize_pr_controls — and report EXACTLY
the ticket numbers and statuses found in the comments. Never guess a PR number or ticket id. If you
cannot find the PR or comments, say so plainly. Only give a generic definition when no specific
release/PR is in context.

Always work against manifest repo: {TARGET_REPO}
PRs and controls are usually in the deployment repo: {DEPLOY_REPO} (use the PR tools)

Valid images come from image-workflows.json (use the list_allowed_images tool).

Before promoting a real (non-test) image:tag, you can VERIFY it was actually built correctly with
verify_image_tag_build(image, tag): it resolves the git tag to its commit, finds the image's build
workflow run, confirms the tag-generation step succeeded and the job log contains the TAG_GENERATED
marker, and reports the RLFT release-control steps. Offer this check and report verified true/false
plus the RLFT controls; warn the user if a promote is requested for an unverified tag.

You MUST propose changes first.
You MUST get an explicit confirmation token from the user before calling apply_json_update or dispatch_workflow.

After dispatch, the workflow opens a PR in the deployment repo asynchronously; proactively find and
track it using find_prs / summarize_pr_controls.

NEVER ask the user for a PR number. If they ask about a PR, its comments, CHG/RMG tickets, or RLFT
gates without giving a number, derive the image:tag from the most recent promote in this conversation
and call find_prs(search_term="<image:tag>") to locate the PR yourself, then read its comments. Only
ask for clarification if no release has been discussed in this thread at all.

If find_prs returns MULTIPLE matching PRs, default to the most recent one (highest PR number /
newest createdAt) for the summary, but tell the user the others exist (list their numbers) so they
can pick a different one. If the user names a specific PR number, use exactly that one.

From the chat (UI or CLI) you can ask to retrigger/re-run the deployment workflow in the DEPLOY_REPO using retrigger_deployment_workflow (e.g. "retrigger deployment workflow for PR 42 after closing controls"). This will re-execute it and post fresh comments reflecting the current control state.

Be concise, precise, and always show the GitHub URLs you produce.
After any mutation or PR update, tell the user the exact outcome and links.

Safety rules (never break):
1. Never call apply_json_update or dispatch_workflow without a matching confirmation token shown to the user in this thread.
2. Only operate on allowed images.
3. When the user gives image names and tags, parse them, propose, then wait for confirmation.

Confirmation flow:
- Call propose_update first.
- Show the user a unique token like CONFIRM-abc123
- Only after they reply with that exact token, proceed to apply + dispatch.

Current manifest repo: {TARGET_REPO}
Deployment / PR repo: {DEPLOY_REPO}
"""


def message_text(msg) -> str:
    """Extract human-readable text from a message.

    Newer Gemini models (2.5+) return `content` as a list of content blocks
    (text + thinking/signature metadata) rather than a plain string. Concatenate
    only the text blocks so callers never render the raw repr / thinking signatures.
    """
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str) \
                    and block.get("type") in (None, "text"):
                parts.append(block["text"])
        return "".join(parts)
    return str(content) if content else ""


def _get_llm():
    """Returns the configured LLM using Vertex AI Gen AI SDK.
    Project is resolved from env or gcloud (no hardcoding).
    """
    if DEFAULT_MODEL is None:
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not settings.gcp_project:
            raise RuntimeError(
                "No GCP project found. Set GOOGLE_CLOUD_PROJECT env var or ensure "
                "'gcloud config set project ...' and run 'gcloud auth application-default login'"
            )

        return ChatGoogleGenerativeAI(
            model=settings.gemini_model,
            temperature=0,
            vertexai=True,
            project=settings.gcp_project,
            location=settings.gcp_location,
        )
    return DEFAULT_MODEL


# Words that are never image names and never tags (deployment environments + filler).
_ENV_WORDS = {
    "prod", "production", "stage", "staging", "dev", "development",
    "qa", "uat", "test", "testing", "sandbox", "preprod", "perf", "canary",
}
_STOP_WORDS = {
    "to", "and", "the", "for", "update", "promote", "set", "as", "tag",
    "in", "on", "env", "environment", "deploy", "release", "please", "with",
    "a", "an", "image", "version", "bump", "rollout", "roll", "out",
} | _ENV_WORDS

# A tag must look like a version / sha / 'latest' — NOT an environment word.
_TAG_RE = re.compile(r"^(?:v?\d[\w.\-]*|latest|[0-9a-f]{7,40})$", re.I)


def _is_image_name(name: str) -> bool:
    name = name.lower()
    return (
        len(name) >= 2
        and not name.isdigit()
        and bool(re.search(r"[a-z]", name))
        and name not in _STOP_WORDS
    )


def _is_tag(tag: str) -> bool:
    return tag.lower() not in _STOP_WORDS and bool(_TAG_RE.match(tag.strip()))


def _extract_images_from_text(text: str) -> list[dict]:
    """Find image:tag pairs.

    Handles:
      - explicit "name:tag" / "name=tag"      (e.g. payments-api:2.0.33)
      - "name to/as/tag <version>"            (e.g. orders-api to v1.2.3)

    Crucially it does NOT treat environment words ('prod', 'staging', ...) as
    tags, and never lets a trailing version fragment like the '3' in '1.2.3'
    become an image name (image names must start with a letter).
    """
    pairs: list[dict] = []

    def add(name: str, tag: str) -> None:
        name = name.lower()
        if not _is_image_name(name) or tag.lower() in _STOP_WORDS:
            return
        if not any(p["name"] == name for p in pairs):
            pairs.append({"name": name, "tag": tag})

    # primary: name:tag or name=tag  (name must start with a letter)
    for m in re.finditer(r"([A-Za-z][\w-]*)[:=](v?[\w][\w.\-]*)", text):
        add(m.group(1), m.group(2))

    # secondary: "name to|as|tag <version>"  (value must look like a tag, not an env)
    for m in re.finditer(r"([A-Za-z][\w-]*)\s+(?:to|as|tag)\s+([\w][\w.\-]*)", text, re.I):
        name, tag = m.group(1), m.group(2)
        if _is_tag(tag):
            add(name, tag)

    return pairs


_PROMOTE_VERBS = re.compile(r"\b(promote|deploy|releas|ship|roll\s?out|bump|cut|set)\b", re.I)
_QUERY_HINTS = re.compile(
    r"\b(find|show|list|get|summari[sz]e|track|status|comment|ticket|chg|rmg|prs?|"
    r"pull\s?request|which|what|where|when|how|why|check|view|read|tell|is|are|did|does)\b|\?",
    re.I,
)


def _is_query_not_promote(text: str) -> bool:
    """True when the message mentions an image:tag but reads as a question/lookup
    (e.g. 'find the PR for payments-api:2.0.1') rather than a promote command, so
    it should go to the LLM/ReAct path instead of the confirmation gate."""
    return bool(_QUERY_HINTS.search(text)) and not _PROMOTE_VERBS.search(text)


def _detect_environment(text: str) -> str:
    low = text.lower()
    if "stag" in low:
        return "staging"
    if re.search(r"\b(dev|development)\b", low):
        return "dev"
    if re.search(r"\b(qa|uat|test|sandbox|preprod)\b", low):
        return "non-prod"
    return "prod"


def _detect_rerun(text: str, current_steps: Optional[list]) -> Optional[list[str]]:
    """Detect a 're-run <step>' request and return the selected step labels.

    Returns None when the message isn't a re-run request, or a (possibly empty)
    list of step labels when it is. An empty list means "re-run intent but the
    step is unspecified" — the rerun node will then list the available steps.
    """
    low = text.lower()
    if not re.search(r"\b(re-?run|re-?try|re-?execute|redo|run again|try again)\b", low):
        return None

    if re.search(r"\b(all|both|everything|entire|whole)\b", low):
        return list(ALL_STEPS)

    if "fail" in low and current_steps:
        failed = [s["name"] for s in current_steps if s.get("status") == "error"]
        if failed:
            return failed

    requested: list[str] = []
    # "step 1" / "step 2"
    for canon, idx in zip(ALL_STEPS, ("1", "2")):
        if re.search(rf"\bstep\s*{idx}\b", low):
            requested.append(canon)
    # name aliases (whole-word match)
    for canon, aliases in _STEP_ALIASES.items():
        if canon in requested:
            continue
        if any(re.search(rf"\b{re.escape(a)}\b", low) for a in aliases):
            requested.append(canon)

    seen, ordered = set(), []
    for s in requested:
        if s not in seen:
            seen.add(s)
            ordered.append(s)
    return ordered


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

    # NOTE: do not inject the SystemMessage here. add_messages would APPEND it
    # after the user's HumanMessage, and Gemini only honors a *leading* system
    # instruction — so the prompt would be ignored. call_llm prepends it instead.
    out: dict[str, Any] = {}

    # A re-run request reuses the prior release_request/steps — don't re-parse images.
    rerun = _detect_rerun(last, state.steps)
    if rerun is not None:
        out["rerun_steps"] = rerun
        return out

    pairs = _extract_images_from_text(last)
    # A message that names an image:tag but reads as a question (e.g. "find the
    # PR for payments-api:2.0.1") is a lookup, not a promote — send it to the LLM.
    if pairs and _is_query_not_promote(last):
        pairs = []
    out["release_request"] = {
        "images": pairs,
        "environment": _detect_environment(last),
        "raw": last[:300],
    } if pairs else None
    out["rerun_steps"] = None
    return out


def _route_after_parse(state: ReleaseState) -> Literal["propose", "llm", "rerun"]:
    if state.rerun_steps is not None:
        return "rerun"
    req = state.release_request
    return "propose" if req and req.get("images") else "llm"


def _build_step_call(step: str, image_str: str, token: str) -> dict:
    """Construct the tool_call for a single re-runnable step."""
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
    raise ValueError(f"unknown step {step}")


def propose(state: ReleaseState) -> Command[Literal["propose_tools", "respond"]]:
    """Craft an AIMessage with a real tool_call so ToolNode can run propose_update.

    Also mints the (stable) confirmation token here and persists it in state so
    the gate node sees the SAME token before and after the interrupt resume.
    """
    req = state.release_request
    if not req or not req.get("images"):
        return Command(
            goto="respond",
            update={"messages": [AIMessage(content=(
                "I didn't find any image:tag pairs. Try: "
                "`promote payments-api:2.0.33 and orders-api to v1.2.3`"
            ))]},
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
        update={"messages": [ai], "confirmation_token": token},
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
                update={"messages": [AIMessage(content=(
                    f"⚠️ Could not build a proposal: {raw}\n\n"
                    "(If this is a GitHub error, make sure `GH_TOKEN` is set and "
                    "the repo/manifest path exist.)"
                ))]},
            )
        try:
            data = json.loads(raw)
            proposed = data.get("proposed", proposed)
            changes = data.get("changes", [])
        except (json.JSONDecodeError, TypeError):
            proposed = {"raw": raw[:500]}

    user_reply = interrupt({
        "type": "confirmation",
        "token": token,
        "proposed": proposed,
        "changes": changes,
        "message": (
            f"Reply with exactly `{token}` (or `yes {token.split('-', 1)[-1]}`) "
            "to apply these changes and dispatch the workflow."
        ),
        "repo": state.repo,
    })

    text = str(user_reply).strip().lower() if user_reply is not None else ""
    expected = token.lower()
    suffix = expected.split("-", 1)[1] if "-" in expected else expected
    confirmed = expected in text or (text.startswith("yes") and suffix in text)

    if confirmed:
        return Command(goto="apply", update={"confirmation_token": token, "proposed": proposed})
    return Command(
        goto="respond",
        update={"messages": [AIMessage(content=(
            f"❌ Not confirmed (received: {user_reply!r}). No changes were applied.\n"
            f"Send the exact token `{token}` to proceed, or start a new request."
        ))]},
    )


def build_apply_and_dispatch(state: ReleaseState) -> dict:
    """After confirmation, craft an AIMessage whose tool_calls perform the real
    mutation + workflow dispatch. ToolNode (apply_tools) executes every step.
    """
    req = state.release_request or {}
    image_str = ",".join(f"{i['name']}:{i['tag']}" for i in req.get("images", []))
    token = state.confirmation_token or "CONFIRM-xxx"

    calls = [_build_step_call(s, image_str, token) for s in ALL_STEPS]
    ai = AIMessage(
        content="Applying the manifest update and dispatching the workflow…",
        tool_calls=calls,  # type: ignore[arg-type]
    )
    return {
        "messages": [ai],
        "last_action": {"phase": "confirmed-apply-dispatch", "images": image_str},
    }


def rerun(state: ReleaseState) -> Command[Literal["apply_tools", "respond"]]:
    """Re-run one or more previously-executed steps by name (no re-confirmation —
    the action was already confirmed in this thread)."""
    req = state.release_request or {}
    steps = state.rerun_steps or []

    if not req.get("images"):
        return Command(goto="respond", update={
            "rerun_steps": None,
            "messages": [AIMessage(content=(
                "There's no prior release in this thread to re-run. "
                "Start with a promote, e.g. `promote payments-api:2.0.33 to prod`."
            ))],
        })

    if not steps:
        names = ", ".join(f"`{s}`" for s in ALL_STEPS)
        return Command(goto="respond", update={
            "rerun_steps": None,
            "messages": [AIMessage(content=(
                f"Which step would you like to re-run? Available steps: {names}.\n"
                "Reply e.g. `re-run dispatch_workflow`, `re-run all`, or `re-run failed`."
            ))],
        })

    image_str = ",".join(f"{i['name']}:{i['tag']}" for i in req["images"])
    token = state.confirmation_token or f"RERUN-{uuid.uuid4().hex[:4]}"
    calls = [_build_step_call(s, image_str, token) for s in steps if s in ALL_STEPS]
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

    # Merge this batch's results into the persisted per-step status.
    steps = {s["name"]: dict(s) for s in (state.steps or [])}
    for name in ALL_STEPS:
        steps.setdefault(name, {"name": name, "status": "pending", "detail": "not run yet"})
    for name, content in batch.items():
        if content.startswith("ERROR"):
            steps[name] = {"name": name, "status": "error", "detail": content[:280]}
        else:
            steps[name] = {"name": name, "status": "ok", "detail": _summarize_step_result(name, content)}
    steps_list = [steps[n] for n in ALL_STEPS]

    icon = {"ok": "✅", "error": "❌", "pending": "⏳"}
    lines = ["**Release steps:**"]
    for s in steps_list:
        lines.append(f"{icon.get(s['status'], '•')} `{s['name']}` — {s['detail']}")

    failed = [s["name"] for s in steps_list if s["status"] == "error"]
    lines.append("")
    if failed:
        lines.append(
            f"⚠️ Failed: {', '.join(f'`{f}`' for f in failed)}. "
            f"Once the cause is resolved, reply `re-run {failed[0]}` "
            "(or `re-run failed` / `re-run all`) to retry just that step."
        )
    else:
        lines.append(
            "All steps succeeded. You can re-run any step by name, e.g. "
            "`re-run dispatch_workflow`, or `re-run all`."
        )
    lines.append("\nAnything else?")

    if not failed:
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
        new_prs = [p for p in _find_prs_for_images(image_str, limit=20) if p["number"] not in seen_before]
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
    return {"messages": [AIMessage(content=(
        "Tell me the image:tag pairs you'd like to promote, e.g. "
        "`promote payments-api:2.0.33 to prod`."
    ))]}


def build_graph(checkpointer=None):
    """Build and return the compiled graph.
    LLM is created lazily to avoid import-time credential requirements.
    """
    _llm = None

    def get_llm():
        nonlocal _llm
        if _llm is None:
            _llm = _get_llm().bind_tools(GH_TOOLS)
        return _llm

    def call_llm(state: ReleaseState):
        # Always place exactly one system prompt FIRST. State may hold none, a
        # mis-ordered one, or duplicates — Gemini only honors a leading system
        # instruction, so rebuild the list deterministically.
        non_system = [m for m in state.messages if not isinstance(m, SystemMessage)]
        messages = [SystemMessage(content=SYSTEM_PROMPT)] + non_system

        # === Budget protection (Vertex AI via ADC) ===
        try:
            check_budget_before_call(
                estimated_input_tokens=2500,
                estimated_output_tokens=800,
            )
        except BudgetInterrupt as be:
            user_response = interrupt({
                "type": "budget_confirmation",
                "message": str(be) + f"\n\nCurrent budget status: {get_budget_status()}",
                "action": "Continue with this LLM call? (yes/no)",
            })
            if not confirm_budget_continue(str(user_response)):
                # User declined → stop this turn gracefully (no hard process kill,
                # which would take down a shared server worker).
                return {"messages": [AIMessage(content=(
                    "🛑 Stopped to protect the budget — no LLM call was made. "
                    f"{get_budget_status()}"
                ))]}

        resp = get_llm().invoke(messages)

        # Record actual usage if available from the response.
        try:
            usage = getattr(resp, "usage_metadata", None) or {}
            input_t = usage.get("input_tokens", 0) or 0
            output_t = usage.get("output_tokens", 0) or 0
            if input_t or output_t:
                get_budget_tracker().add_usage(input_t, output_t)
        except Exception:
            pass

        return {"messages": [resp]}

    def route_after_llm(state: ReleaseState) -> Literal["llm_tools", "__end__"]:
        last = state.messages[-1] if state.messages else None
        return "llm_tools" if getattr(last, "tool_calls", None) else END

    # One ToolNode implementation, registered under three deterministic names so
    # each graph node has exactly one outgoing edge.
    tool_node = ToolNode(GH_TOOLS)

    graph = StateGraph(ReleaseState)

    graph.add_node("parse", parse_intent)
    graph.add_node("propose", propose)
    graph.add_node("propose_tools", tool_node)
    graph.add_node("gate", confirmation_gate)
    graph.add_node("apply", build_apply_and_dispatch)
    graph.add_node("rerun", rerun)
    graph.add_node("apply_tools", tool_node)
    graph.add_node("finalize", finalize)
    graph.add_node("track_pr", track_pr)
    graph.add_node("respond", respond)
    graph.add_node("llm", call_llm)
    graph.add_node("llm_tools", tool_node)

    # Entry + branch: re-run request, concrete image:tag pairs, or free-form chat.
    graph.add_edge(START, "parse")
    graph.add_conditional_edges(
        "parse", _route_after_parse,
        {"propose": "propose", "llm": "llm", "rerun": "rerun"},
    )

    # Propose → confirm → apply path (propose & gate route via Command).
    graph.add_edge("propose_tools", "gate")
    graph.add_edge("apply", "apply_tools")
    graph.add_edge("apply_tools", "finalize")
    graph.add_conditional_edges(
        "finalize", _route_after_finalize,
        {"track_pr": "track_pr", END: END},
    )
    graph.add_edge("track_pr", END)
    graph.add_edge("respond", END)

    # Free-form ReAct path.
    graph.add_conditional_edges(
        "llm", route_after_llm,
        {"llm_tools": "llm_tools", END: END},
    )
    graph.add_edge("llm_tools", "llm")

    return graph.compile(checkpointer=checkpointer)


# Convenience for CLI / apps
def get_compiled_graph():
    from langgraph.checkpoint.memory import MemorySaver
    checkpointer = MemorySaver()
    return build_graph(checkpointer=checkpointer)
