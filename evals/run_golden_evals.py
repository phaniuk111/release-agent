"""Golden-case evals for the LLM-judgment layer (routing, skills, narration).

The deterministic router is unit-tested; what regresses in practice is the
behavior that lives in prompts and the classifier — which lane a phrasing lands
in, which tool the chat agent reaches for, and how approvals/rejections are
narrated. This runner replays a fixed set of chat messages against a RUNNING
local server and asserts observable behavior, so a SKILL.md / ROOT_INSTRUCTION /
classifier-prompt edit can be regression-checked with one command.

Run:
    # server must be up (with the Vertex env) on http://127.0.0.1:8000
    .venv/bin/python evals/run_golden_evals.py

Safety: cases only ever mint preview tokens (immediately cancelled), reject
yes/no approvals, or hit read-only tools. Nothing is confirmed, so nothing
mutates GitHub. Each case runs in a fresh thread.

Exit code 0 = all cases pass. Non-zero = failures (printed per-case).
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import uuid
from dataclasses import dataclass, field

BASE = os.getenv("EVAL_BASE_URL", "http://127.0.0.1:8000")
# Vertex per-minute quotas are easy to hit when cases run back-to-back.
PACE_SECONDS = float(os.getenv("EVAL_PACE_SECONDS", "15"))
RETRY_BACKOFF_SECONDS = float(os.getenv("EVAL_RETRY_BACKOFF_SECONDS", "45"))


def chat(message: str, thread_id: str, timeout: int = 150) -> dict:
    """POST one chat turn; fold the SSE stream into {text, interrupt}."""
    req = urllib.request.Request(
        BASE + "/api/chat",
        data=json.dumps({"message": message, "thread_id": thread_id}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    text_parts: list[str] = []
    interrupt: dict | None = None
    errors: list[str] = []
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data: "):
                continue
            event = json.loads(line[len("data: "):])
            if event.get("type") == "token":
                text_parts.append(event.get("content") or "")
            elif event.get("type") == "interrupt":
                interrupt = event.get("data") or {}
            elif event.get("type") == "error":
                errors.append(event.get("content") or "error")
    return {"text": "\n".join(text_parts), "interrupt": interrupt, "errors": errors}


# --- assertion helpers -------------------------------------------------------

def expect_deploy_preview(turn: dict) -> str | None:
    intr = turn["interrupt"]
    if not intr or not (intr.get("token") or "").startswith("CONFIRM-"):
        return f"expected a deploy preview with a CONFIRM token, got interrupt={intr!r} text={turn['text'][:200]!r}"
    return None


def expect_release_approval(turn: dict) -> str | None:
    intr = turn["interrupt"]
    if not intr or intr.get("function") != "merge_prod_release":
        return f"expected a merge_prod_release yes/no approval, got interrupt={intr!r} text={turn['text'][:200]!r}"
    if "no new charts" not in (intr.get("message") or "").lower():
        return f"approval prompt lost the finality warning: {intr.get('message')!r}"
    return None


def expect_asks_for_chart_version(turn: dict) -> str | None:
    """Underspecified deploys should ask for chart:version (mentioning the
    CONFIRM-token deploy flow here is accurate, so it is allowed)."""
    if turn["interrupt"] is not None:
        return f"expected a clarifying question, got an interrupt: {turn['interrupt']!r}"
    if "version" not in turn["text"].lower():
        return f"expected it to ask for chart:version, got: {turn['text'][:300]!r}"
    return None


def expect_plain_answer(*must_not_contain: str):
    def check(turn: dict) -> str | None:
        if turn["interrupt"] is not None:
            return f"expected a plain answer, got an interrupt: {turn['interrupt']!r}"
        low = turn["text"].lower()
        for bad in must_not_contain:
            if bad.lower() in low:
                return f"answer must not mention {bad!r}: {turn['text'][:300]!r}"
        if not turn["text"].strip():
            return "empty answer"
        return None
    return check


@dataclass
class Case:
    name: str
    message: str
    check: object  # callable(turn) -> error | None
    followups: list[tuple[str, object]] = field(default_factory=list)  # (reply, check)
    cleanup_reply: str | None = None  # sent after checks to close any pause


CASES = [
    Case(
        name="deterministic deploy phrasing -> preview+token",
        message="deploy targeted-svc:9.9.1 to uat",
        check=expect_deploy_preview,
        cleanup_reply="CONFIRM-CANCEL",
    ),
    Case(
        name="free-form deploy phrasing -> preview+token (classifier)",
        message="can you get targeted-svc 9.9.1 into production please",
        check=expect_deploy_preview,
        cleanup_reply="CONFIRM-CANCEL",
    ),
    Case(
        name="status question stays a question",
        message="what is the deploy status of uat and prod?",
        check=expect_plain_answer("CONFIRM-"),
    ),
    Case(
        name="'release prod' -> merge_prod_release approval with finality warning",
        message="release prod",
        check=expect_release_approval,
        followups=[
            # Rejection narration: plain, no token/workflow lecture.
            ("no", expect_plain_answer("CONFIRM-", "token")),
        ],
    ),
    Case(
        name="underspecified deploy asks for chart:version",
        message="ship the latest orders build to uat",
        check=expect_asks_for_chart_version,
    ),
    Case(
        name="catalog question routes to chat tools",
        message="what images can I promote?",
        check=expect_plain_answer("CONFIRM-"),
    ),
]


def main() -> int:
    failures: list[str] = []
    for case in CASES:
        err = None
        for attempt in range(2):
            thread = f"eval-{uuid.uuid4().hex[:8]}"
            try:
                turn = chat(case.message, thread)
                if turn["errors"] or (not turn["text"].strip() and turn["interrupt"] is None):
                    # Server-side error (usually a Vertex 429): back off and retry once.
                    if attempt == 0:
                        print(f"  ... transient error on {case.name!r}; retrying in {RETRY_BACKOFF_SECONDS:.0f}s")
                        time.sleep(RETRY_BACKOFF_SECONDS)
                        continue
                    err = f"server error/empty turn: {turn['errors'] or 'no output'}"
                    break
                err = case.check(turn)  # type: ignore[operator]
                for reply, check in case.followups:
                    follow = chat(reply, thread)
                    err = err or check(follow)  # type: ignore[operator]
                if case.cleanup_reply and turn.get("interrupt"):
                    chat(case.cleanup_reply, thread)
            except Exception as e:  # noqa: BLE001 - report, don't crash the suite
                err = f"exception: {type(e).__name__}: {e}"
            break
        time.sleep(PACE_SECONDS)
        status = "PASS" if err is None else "FAIL"
        print(f"[{status}] {case.name}")
        if err:
            print(f"       {err}")
            failures.append(case.name)
    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
