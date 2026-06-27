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
from langgraph.types import Command, interrupt, RetryPolicy
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
    # Tool-call turns taken in the free-form ReAct lane this user turn (loop guard).
    llm_tool_turns: int = 0
    repo: str = Field(default=TARGET_REPO)
    deploy_repo: str = Field(default=DEPLOY_REPO)


# ---- Re-runnable apply-phase steps -----------------------------------------
STEP_APPLY = "apply_manifest"
STEP_DISPATCH = "dispatch_workflow"
STEP_RELEASE_PR = "release_pr"     # env promote: update env config JSON + open a PR
ALL_STEPS = [STEP_APPLY, STEP_DISPATCH]

# Steps used for an environment (uat/prod) promote vs. the legacy dispatch path.
_ENV_STEPS = [STEP_RELEASE_PR]

# Map the underlying tool name -> canonical step label (used to attribute each
# ToolMessage back to a step via its tool_call).
_STEP_BY_TOOL = {
    "apply_json_update": STEP_APPLY,
    "dispatch_workflow": STEP_DISPATCH,
    "open_release_pr": STEP_RELEASE_PR,
}

# User-facing words that select a step in a "re-run ..." request.
_STEP_ALIASES = {
    STEP_APPLY: ["apply_manifest", "apply-manifest", "apply", "commit", "manifest"],
    STEP_DISPATCH: ["dispatch_workflow", "dispatch-workflow", "dispatch", "workflow", "trigger"],
    STEP_RELEASE_PR: ["release_pr", "release-pr", "pr", "open_pr", "open-pr", "raise_pr"],
}

# Change-ticket fields required when promoting to prod.
_CHG_FIELDS = ("chg_name", "chg_summary", "start_date", "end_date")


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

PROMOTION MODEL — SIT → UAT → PRD (important):
- The env branches (SIT, UAT, PRD) are PROTECTED — never edited directly; every change is a PR. A
  "promote to prod / add image" request opens a PR chain (working branch → SIT → UAT) so the image
  lands on UAT, where the day's release accumulates. The single UAT → PRD release PR is raised ONLY
  after the daily cutoff (raising it LOCKS the day, so it must not happen earlier or no more images
  could be added).
- Before the cutoff: a prod request stages onto UAT (no change request needed yet).
- After the cutoff: the prod request (or raise_prod_release) raises the one UAT → PRD PR with the full
  UAT set; this requires a change request (drives the CHG/RMG) and locks the day.
- LEAD TIME: the change request's start_date must be at least one day out (tomorrow or later) — a prod
  release can't be raised for a same-day start. If start_date is today/past, refuse and ask for a later date.
- NOTHING TO RELEASE: if UAT has no changes vs PRD, do not raise a PR — say there is nothing to release.
- REMOVE / UNSTAGE: to pull an image back out of the release, call
  remove_from_release(image_names="<name>[,<name>...]"). Like add, it goes through the protected-branch
  PR chain — a PR from a working branch into SIT dropping the image, then a PR promoting SIT → UAT,
  both merged so the removal reaches UAT. Each image is reverted to PRD's current tag (or dropped if
  new). Branches are never edited directly. Report the PR links.
- Once the day is locked (today's UAT → PRD PR exists), refuse further adds and point to that PR.

PRD RELEASE CONTROL GATE (mandatory): when a developer wants a PRD/prod release and gives an
image:tag, you MUST first call get_build_controls(image, tag) to fetch the release CONTROLS
(RLFT/RFTL gates) recorded in that tag's build pipeline run, and report each one as PASSED or
FAILED (e.g. "RFTL0001: FAILED, RFTL0002: PASSED"). Rules:
- The tool finds the build run from the tag automatically. If it returns need_run_id (the run can't
  be located from image+tag), ASK the developer for the GitHub Actions run id that generated the
  tag, then call get_build_controls(run_id=<that id>).
- If ANY control FAILED (gate != PASS), do NOT stage it for the PRD release — tell the developer which
  controls failed and that they must be resolved/re-run first.
- Only when ALL controls PASSED should you continue (open_release_pr stages onto UAT, and after the
  cutoff raises the UAT → PRD PR). open_release_pr enforces this gate server-side too.

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

def _looks_like_tag(tag: str) -> bool:
    """A tag looks like a version / sha / 'latest' — NOT an environment word."""
    t = tag.strip().lower()
    if not t:
        return False
    if t == "latest":
        return True
    if t[0] == "v" and len(t) > 1 and t[1].isdigit():                  # v1.2.3
        return True
    if t[0].isdigit():                                                 # 2.0.0
        return True
    if 7 <= len(t) <= 40 and all(c in "0123456789abcdef" for c in t):  # git sha
        return True
    return False


def _is_image_name(name: str) -> bool:
    name = name.lower()
    return (
        len(name) >= 2
        and not name.isdigit()
        and any(c.isalpha() for c in name)
        and name not in _STOP_WORDS
    )


def _is_tag(tag: str) -> bool:
    return tag.lower() not in _STOP_WORDS and _looks_like_tag(tag)


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
        tag = tag.strip().strip(".,;:)")
        if not tag or not _is_image_name(name) or tag.lower() in _STOP_WORDS:
            return
        if not any(p["name"] == name for p in pairs):
            pairs.append({"name": name, "tag": tag})

    tokens = text.replace(",", " ").split()

    # primary: a token of the form name:tag or name=tag (name starts with a letter)
    for tok in tokens:
        sep = ":" if ":" in tok else ("=" if "=" in tok else None)
        if not sep:
            continue
        name, _, tag = tok.partition(sep)
        if name and name[0].isalpha():
            add(name, tag)

    # secondary: "name to|as|tag <version>"  (value must look like a tag, not an env)
    for i in range(len(tokens) - 2):
        if tokens[i + 1].lower() in ("to", "as", "tag"):
            name, tag = tokens[i], tokens[i + 2]
            if name and name[0].isalpha() and _is_tag(tag):
                add(name, tag)

    return pairs


# Whole-word sets for intent classification (no regex).
_PROMOTE_PREFIXES = ("promote", "deploy", "releas", "ship", "rollout", "bump", "cut")
_QUERY_WORDS = {
    "find", "show", "list", "get", "summarize", "summarise", "track", "status",
    "comment", "ticket", "chg", "rmg", "pr", "prs", "pull", "request", "which",
    "what", "where", "when", "how", "why", "check", "view", "read", "tell",
    "is", "are", "did", "does",
    # verification / lookups (route to the LLM, not the promote gate)
    "verify", "verified", "validate", "build", "built", "builds",
}


def _norm_words(text: str) -> list[str]:
    """Lowercased word tokens with non-alphanumerics treated as separators."""
    return "".join(c if c.isalnum() else " " for c in text.lower()).split()


def _has_promote_verb(words: list[str]) -> bool:
    return "set" in words or any(w.startswith(_PROMOTE_PREFIXES) for w in words)


def _is_query_not_promote(text: str) -> bool:
    """True when the message names an image:tag but reads as a question/lookup
    (e.g. 'find the PR for payments-api:2.0.1') rather than a promote command."""
    words = _norm_words(text)
    is_query = "?" in text or any(w in _QUERY_WORDS for w in words)
    return is_query and not _has_promote_verb(words)


_REMOVAL_WORDS = {"remove", "unstage", "drop", "exclude", "withdraw", "deselect", "unselect"}


def _is_removal(text: str) -> bool:
    """True when the user wants to remove/unstage an image from today's release."""
    words = set(_norm_words(text))
    if words & _REMOVAL_WORDS:
        return True
    low = text.lower()
    return "back out" in low or "take out" in low


def _detect_environment(text: str) -> str:
    low = text.lower()
    words = set(_norm_words(text))
    if "uat" in words or "user acceptance" in low:
        return "uat"
    if "stag" in low:
        return "staging"
    if "dev" in words or "development" in words:
        return "dev"
    if words & {"prod", "production", "prd"}:
        return "prod"
    return "prod"


def _is_chg_line(line: str) -> bool:
    sep = ":" if ":" in line else ("=" if "=" in line else None)
    if not sep:
        return False
    return line.partition(sep)[0].strip().lower() in _CHG_FIELDS


def _extract_change_fields(text: str) -> dict:
    """Pull change-ticket fields from lines like 'chg_name: CHG0012345' (no regex)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if not _is_chg_line(line):
            continue
        sep = ":" if ":" in line else "="
        key, _, val = line.partition(sep)
        if val.strip():
            out[key.strip().lower()] = val.strip()
    return out


def _try_parse_json_payload(text: str) -> Optional[dict]:
    """If the message contains a JSON object with an 'images' field, parse it into a
    release_request. Supports images as a {name: tag} map or a [{image, tag}] list,
    plus an optional 'change_request' block (the data the CHG is created from)."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    images = data.get("images")
    pairs: list[dict] = []
    if isinstance(images, dict):
        pairs = [{"name": str(k), "tag": str(v)} for k, v in images.items()]
    elif isinstance(images, list):
        for it in images:
            if isinstance(it, dict):
                name, tag = it.get("image") or it.get("name"), it.get("tag")
                if name and tag:
                    pairs.append({"name": str(name), "tag": str(tag)})
    if not pairs:
        return None
    env = str(data.get("environment") or "prod").lower()
    if env in ("prd", "production"):
        env = "prod"
    return {
        "images": pairs,
        "environment": env,
        "change_request": data.get("change_request") or {},
        "raw": "json-paste",
    }


def _detect_rerun(text: str, current_steps: Optional[list]) -> Optional[list[str]]:
    """Detect a 're-run <step>' request and return the selected step labels.

    Returns None when the message isn't a re-run request, or a (possibly empty)
    list of step labels when it is. An empty list means "re-run intent but the
    step is unspecified" — the rerun node will then list the available steps.
    """
    # space-padded normalized words, so " word " is a whole-word check (no regex)
    norm = " " + " ".join(_norm_words(text)) + " "
    rerun_phrases = (" rerun ", " re run ", " retry ", " re try ", " reexecute ",
                     " re execute ", " redo ", " run again ", " try again ")
    if not any(p in norm for p in rerun_phrases):
        return None

    if any(w in norm for w in (" all ", " both ", " everything ", " entire ", " whole ")):
        return [s["name"] for s in current_steps] if current_steps else list(ALL_STEPS)

    if "fail" in text.lower() and current_steps:
        failed = [s["name"] for s in current_steps if s.get("status") == "error"]
        if failed:
            return failed

    requested: list[str] = []
    # "step 1" / "step 2"
    for canon, idx in zip(ALL_STEPS, ("1", "2")):
        if f" step {idx} " in norm:
            requested.append(canon)
    # name aliases (whole-word match; aliases normalized the same way)
    for canon, aliases in _STEP_ALIASES.items():
        if canon in requested:
            continue
        if any(f" {' '.join(_norm_words(a))} " in norm for a in aliases):
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
    out: dict[str, Any] = {"llm_tool_turns": 0}  # reset the ReAct loop guard each turn

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
    out["release_request"] = {
        "images": pairs,
        "environment": _detect_environment(last),
        "change_request": _extract_change_fields(last),
        "raw": last[:300],
    } if pairs else None
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
        return {"name": "open_release_pr", "args": args,
                "id": f"call_{STEP_RELEASE_PR}_{uuid.uuid4().hex[:8]}"}
    raise ValueError(f"unknown step {step}")


def _prod_controls_summary(req: dict) -> tuple[str, bool, bool]:
    """Fetch each image:tag's build-pipeline release controls (RLFT/RFTL) and return
    (markdown summary, all_passed, all_located). Used to surface PASS/FAIL up front
    on a PRD release and block when a control failed."""
    from .tools.gh_tools import (
        _get_github_client, _build_repo_full, _find_build_run, _controls_report,
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
                lines.append(f"• **{name}:{tag}** — ⚠️ build run not found ({err}); share the run id "
                             "that generated this tag.")
                continue
            rep = _controls_report(repo_full, name, tag, run)
            ctrls = rep["controls"]
            if not ctrls:
                all_located = False
                lines.append(f"• **{name}:{tag}** — ⚠️ no control steps in [this run]({rep['run']['url']}).")
                continue
            marks = []
            for c in ctrls:
                m = "✅" if c["passed"] else ("❌" if c["failed"] else "⏳")
                marks.append(f"{m} {c['control']}")
            ok = rep["all_controls_passed"]
            all_passed = all_passed and ok
            lines.append(f"• **{name}:{tag}** — controls {'PASS' if ok else 'FAIL'} "
                         f"([run]({rep['run']['url']})): " + " · ".join(marks))
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
            update={"messages": [AIMessage(content=(
                "I didn't find any image:tag pairs. Try: "
                "`promote payments-api:2.0.33 and orders-api to v1.2.3`"
            ))]},
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
                update={"messages": [AIMessage(content=(
                    summary + "\n\n❌ One or more build controls **FAILED** — I can't stage this for the "
                    "PRD release until they're resolved and the build is re-run."
                ))]},
            )

        from .tools.gh_tools import get_release_status
        status = get_release_status()

        if status.get("locked"):
            p = status.get("prd_pr_today") or {}
            return Command(
                goto="respond",
                update={"messages": [AIMessage(content=(
                    summary + f"\n\n🔒 Today's UAT→PRD release **PR #{p.get('number')}** is already raised "
                    f"({p.get('url','')}) — the day is locked, no more images can be added."
                ))]},
            )

        if status.get("cutoff_passed"):
            # Raise path — change request required.
            cr = req.get("change_request") or {}
            if not cr:
                note = summary + "\n\n"
                if not all_located:
                    note += ("Some controls couldn't be auto-located — share the build **run id** that "
                             "generated the tag and I'll verify them.\n\n")
                return Command(
                    goto="respond",
                    update={"messages": [AIMessage(content=(
                        note + f"⏰ The {status.get('cutoff_utc')} UTC cutoff has passed. Raising today's "
                        "**UAT → PRD** release PR requires a change request (it drives the CHG). Use the "
                        "**Promote to PROD** action and paste the change-request JSON."
                    ))]},
                )
            # Production lead time: the change start_date must be tomorrow or later.
            from .tools.gh_tools import _lead_time_ok
            lead_ok, lead_msg = _lead_time_ok(cr)
            if not lead_ok:
                return Command(
                    goto="respond",
                    update={"messages": [AIMessage(content=(
                        summary + f"\n\n📅 Can't raise the release — {lead_msg} Update the change "
                        "request's `start_date` and resubmit."
                    ))]},
                )
            pre_msgs.append(AIMessage(content=(
                summary + "\n\n⏰ Cutoff passed — I'll stage these on UAT and **raise today's UAT → PRD "
                "release PR** (CHG/RMG auto-created). Confirm to proceed."
            )))
        else:
            # Staging path — no change request needed; release happens at the cutoff.
            pre_msgs.append(AIMessage(content=(
                summary + f"\n\n🧺 I'll **stage** these on the **UAT** branch for today's release. The single "
                f"UAT → PRD PR is raised after **{status.get('cutoff_utc')} UTC**, so more images can be "
                "added until then. Confirm to stage."
            )))

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

    req = state.release_request or {}
    env = (req.get("environment") or "prod").lower()
    change = req.get("change_request") or {}
    if env == "uat":
        action = "stage these images on the **UAT** branch"
    elif env == "prod":
        # Stage on UAT (before cutoff) vs raise the day's UAT→PRD release PR (after).
        from .tools.gh_tools import get_release_status
        status = get_release_status() or {}
        if status.get("cutoff_passed"):
            action = "**raise today's UAT → PRD release PR** (CHG/RMG auto-created from the change request)"
        else:
            action = f"**stage** these images on **UAT** (the UAT → PRD PR is raised after {status.get('cutoff_utc')} UTC)"
    else:
        action = "apply these changes and dispatch the workflow"

    user_reply = interrupt({
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
    env = (req.get("environment") or "prod").lower()

    steps = _steps_for_request(req)
    calls = [_build_step_call(s, req, token) for s in steps]
    content = (f"Opening a release PR to promote {image_str} to {env}…"
               if STEP_RELEASE_PR in steps
               else "Applying the manifest update and dispatching the workflow…")
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
        return Command(goto="respond", update={
            "rerun_steps": None,
            "messages": [AIMessage(content=(
                "There's no prior release in this thread to re-run. "
                "Start with a promote, e.g. `promote payments-api:2.0.33 to prod`."
            ))],
        })

    valid = _steps_for_request(req)
    if not steps:
        names = ", ".join(f"`{s}`" for s in valid)
        return Command(goto="respond", update={
            "rerun_steps": None,
            "messages": [AIMessage(content=(
                f"Which step would you like to re-run? Available steps: {names}.\n"
                f"Reply e.g. `re-run {valid[0]}`, `re-run all`, or `re-run failed`."
            ))],
        })

    token = state.confirmation_token or f"RERUN-{uuid.uuid4().hex[:4]}"
    calls = [_build_step_call(s, req, token) for s in steps if s in valid]
    if not calls:
        return Command(goto="respond", update={
            "rerun_steps": None,
            "messages": [AIMessage(content=f"No matching step to re-run. Available: {', '.join(valid)}.")],
        })
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
        if action == "staged_to_uat":
            imgs = data.get("uat_images") or {}
            return f"staged on UAT ({len(imgs)} image(s) total) — UAT→PRD PR is raised after the cutoff"
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
    for s in (state.steps or []):
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
            steps[name] = {"name": name, "status": "ok", "detail": _summarize_step_result(name, content)}
    steps_list = [steps[n] for n in order]

    icon = {"ok": "✅", "error": "❌", "pending": "⏳"}
    lines = ["**Release steps:**"]
    for s in steps_list:
        lines.append(f"{icon.get(s['status'], '•')} `{s['name']}` — {s['detail']}")

    failed = [s["name"] for s in steps_list if s["status"] == "error"]
    example = steps_list[0]["name"] if steps_list else "all"
    lines.append("")
    if failed:
        lines.append(
            f"⚠️ Failed: {', '.join(f'`{f}`' for f in failed)}. "
            f"Once the cause is resolved, reply `re-run {failed[0]}` "
            "(or `re-run failed` / `re-run all`) to retry just that step."
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


def react_giveup(state: ReleaseState) -> dict:
    """Stop the ReAct lane gracefully when it hits the tool-call cap. Answers the
    last (unexecuted) tool_calls so the message history stays valid, then returns a
    helpful message instead of looping to the recursion limit and crashing."""
    last = state.messages[-1] if state.messages else None
    tool_msgs = []
    for tc in (getattr(last, "tool_calls", None) or []):
        tcid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", None)
        if tcid:
            tool_msgs.append(ToolMessage(
                content="Skipped: reached the tool-call limit for this request.",
                tool_call_id=tcid,
            ))
    msg = AIMessage(content=(
        "I've run several lookups without converging on an answer. Could you narrow "
        "the request — e.g. a specific `image:tag` or PR number — and I'll dig in directly?"
    ))
    return {"messages": tool_msgs + [msg]}


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

        update = {"messages": [resp]}
        # Count a ReAct tool turn each time the model asks to call tools.
        if getattr(resp, "tool_calls", None):
            update["llm_tool_turns"] = (state.llm_tool_turns or 0) + 1
        return update

    def route_after_llm(state: ReleaseState) -> Literal["llm_tools", "giveup", "__end__"]:
        last = state.messages[-1] if state.messages else None
        if getattr(last, "tool_calls", None):
            if (state.llm_tool_turns or 0) >= settings.react_max_tool_turns:
                return "giveup"   # cap reached → stop gracefully
            return "llm_tools"
        return END

    # Retry only TRANSIENT failures (network blips, 5xx, rate limits) — never
    # deterministic ones (404/422/auth), which won't fix on retry. Mainly catches
    # Vertex/LLM hiccups in the `llm` node and any tool exception that escapes
    # ToolNode's own error handling. (GitHub API blips are also retried lower down,
    # at the HTTP layer in _get_github_client.)
    def _is_transient_error(exc: Exception) -> bool:
        if isinstance(exc, (ConnectionError, TimeoutError)):
            return True
        if type(exc).__name__ in {
            "ServiceUnavailable", "DeadlineExceeded", "ResourceExhausted",
            "InternalServerError", "TooManyRequests", "Aborted", "GatewayTimeout",
        }:
            return True
        try:
            import requests
            if isinstance(exc, requests.exceptions.RequestException):
                return True
        except Exception:
            pass
        status = getattr(exc, "status", None) or getattr(exc, "status_code", None)
        return isinstance(status, int) and (status == 429 or status >= 500)

    retry = RetryPolicy(max_attempts=3, initial_interval=0.5, backoff_factor=2.0,
                        jitter=True, retry_on=_is_transient_error)

    # One ToolNode implementation, registered under three deterministic names so
    # each graph node has exactly one outgoing edge.
    tool_node = ToolNode(GH_TOOLS)

    graph = StateGraph(ReleaseState)

    graph.add_node("parse", parse_intent)
    graph.add_node("propose", propose)
    graph.add_node("propose_tools", tool_node, retry_policy=retry)
    graph.add_node("gate", confirmation_gate)
    graph.add_node("apply", build_apply_and_dispatch)
    graph.add_node("rerun", rerun)
    graph.add_node("apply_tools", tool_node, retry_policy=retry)
    graph.add_node("finalize", finalize)
    graph.add_node("track_pr", track_pr, retry_policy=retry)
    graph.add_node("respond", respond)
    graph.add_node("llm", call_llm, retry_policy=retry)
    graph.add_node("llm_tools", tool_node, retry_policy=retry)
    graph.add_node("react_giveup", react_giveup)

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

    # Free-form ReAct path (with a tool-call cap that exits via react_giveup).
    graph.add_conditional_edges(
        "llm", route_after_llm,
        {"llm_tools": "llm_tools", "giveup": "react_giveup", END: END},
    )
    graph.add_edge("llm_tools", "llm")
    graph.add_edge("react_giveup", END)

    return graph.compile(checkpointer=checkpointer)


# Convenience for CLI / apps
def get_compiled_graph():
    from langgraph.checkpoint.memory import MemorySaver
    checkpointer = MemorySaver()
    return build_graph(checkpointer=checkpointer)
