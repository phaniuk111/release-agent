"""Route-distribution summary for the router — the 'revisit with data' one-liner.

The deterministic parser (agent/parsing.py) is a performance optimization, not a
safety feature: both it and the classifier fallback feed the SAME preview + CONFIRM
+ mutation-guard gate. So the only question that justifies its ~200 lines is:

    how often does the fast path actually save a model call?

That answer lives in the traces. Every chat turn logs a router_decision event with
one of three routes (see adk_release_agent/tracing.py::log_router_decision):

    deploy_workflow:deterministic   the parser recognized the deploy -> £0, ~0ms
    deploy_workflow:classifier      the parser MISSED; one LLM call rescued it
    chat                            not a deploy -> chat lane

This reads that stream and reports the ratio, so the keep-or-prune decision can be
made from evidence instead of vibes. Read-only; it never deletes or mutates anything.

Run:
    .venv/bin/python evals/route_stats.py               # default traces/agent-traces.jsonl
    .venv/bin/python evals/route_stats.py path/to.jsonl # or an explicit file
    TRACE_LOG_PATH=... .venv/bin/python evals/route_stats.py
"""
from __future__ import annotations

import collections
import json
import os
import sys
from datetime import datetime, timezone

DETERMINISTIC = "deploy_workflow:deterministic"
CLASSIFIER = "deploy_workflow:classifier"
CHAT = "chat"

# Below this many deploy-intent turns, any ratio is noise — don't let it drive a decision.
MIN_SAMPLE_FOR_DECISION = 200


def _trace_path() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1]
    return os.getenv("TRACE_LOG_PATH", os.path.join("traces", "agent-traces.jsonl"))


def _load_router_decisions(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # tracing is best-effort; a torn line shouldn't break the report
            if event.get("type") == "router_decision":
                out.append(event)
    return out


def _fmt_ts(ts: float | None) -> str:
    if not ts:
        return "?"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _pct(n: int, total: int) -> str:
    return f"{(100 * n / total):.0f}%" if total else "—"


def _examples(decisions: list[dict], route: str, limit: int = 3) -> list[str]:
    seen = []
    for d in decisions:
        if d.get("route") == route:
            msg = (d.get("message_preview") or "").replace("\n", " ").strip()
            if msg and msg not in seen:
                seen.append(msg)
        if len(seen) >= limit:
            break
    return seen


def main() -> int:
    path = _trace_path()
    if not os.path.exists(path):
        print(f"No trace file at {path!r}.")
        print("Run the app/evals with tracing on (TRACE_LOG_PATH set or default), then retry.")
        return 1

    decisions = _load_router_decisions(path)
    if not decisions:
        print(f"{path}: no router_decision events yet.")
        return 1

    counts = collections.Counter(d.get("route") for d in decisions)
    det, clf, chat = counts[DETERMINISTIC], counts[CLASSIFIER], counts[CHAT]
    deploy_turns = det + clf          # turns the router treated as a deploy
    total = len(decisions)
    ts = [d.get("ts") for d in decisions if d.get("ts")]

    print("=" * 64)
    print("  ROUTER DECISION DISTRIBUTION")
    print("=" * 64)
    print(f"  source : {path}")
    print(f"  window : {_fmt_ts(min(ts) if ts else None)}  ->  {_fmt_ts(max(ts) if ts else None)}")
    print(f"  turns  : {total} routed  ({deploy_turns} deploy-intent, {chat} chat)")
    print()
    print(f"  {DETERMINISTIC:32} {det:4}  ({_pct(det, total)} of all turns)")
    print(f"  {CLASSIFIER:32} {clf:4}  ({_pct(clf, total)} of all turns)")
    print(f"  {CHAT:32} {chat:4}  ({_pct(chat, total)} of all turns)")
    print()

    # --- the metric that actually decides keep-vs-prune -----------------------
    print("-" * 64)
    print("  THE DECISION METRIC — of deploy-intent turns:")
    if deploy_turns:
        print(f"    fast-path coverage : {_pct(det, deploy_turns)}  "
              f"(caught by the parser, £0 / ~0ms)")
        print(f"    classifier rescue  : {_pct(clf, deploy_turns)}  "
              f"(parser MISSED, needed one LLM call)")
    else:
        print("    no deploy-intent turns recorded yet")
    print("-" * 64)
    print()

    # --- honest verdict framing (states the rule; does NOT auto-delete) --------
    print("  READ:")
    if deploy_turns < MIN_SAMPLE_FOR_DECISION:
        print(f"    ⚠ only {deploy_turns} deploy-intent turns — below the "
              f"{MIN_SAMPLE_FOR_DECISION} threshold for a decision.")
        print("    Likely still dominated by eval/synthetic traffic (clean chart:version),")
        print("    which flatters the parser. Let real usage accumulate; do NOT prune yet.")
    else:
        cov = det / deploy_turns
        if cov >= 0.80:
            print(f"    Parser handles {_pct(det, deploy_turns)} of deploys for free — it earns its")
            print("    keep. Pruning would push those through a ~500ms model call.")
        elif cov <= 0.40:
            print(f"    Parser only catches {_pct(det, deploy_turns)}; the classifier is doing the")
            print("    real work. Strong case to delete the NL-guessing and let intent.py own it")
            print("    (keep _try_parse_json_payload — the UI form still needs it).")
        else:
            print(f"    Mixed: parser catches {_pct(det, deploy_turns)}. Consider keeping ONLY the")
            print("    bare chart:version fast path and deleting the rest of the NL heuristics.")
    print()

    # --- a few real messages per route, for eyeballing the labels -------------
    for label, route in (("deterministic", DETERMINISTIC),
                         ("classifier", CLASSIFIER),
                         ("chat", CHAT)):
        ex = _examples(decisions, route)
        if ex:
            print(f"  e.g. {label}:")
            for m in ex:
                print(f"       - {m[:80]}")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
