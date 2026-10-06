#!/usr/bin/env python3
"""Score the investigate agent offline on a fixed set of evidence bundles.

    PYTHONPATH=src .venv/bin/python scripts/eval_investigate.py             # live: the model writes each finding
    PYTHONPATH=src .venv/bin/python scripts/eval_investigate.py --model-free  # baseline: the no-model markdown

Each case in tests/eval/investigate_cases.json is an EVIDENCE BUNDLE (the shape
tools/support/evidence.collect returns) plus what a good finding must and must not
say. The cases are hand-written with neutral names; the point is a number that
moves when the prompt, the model or the tools change, so "did that help?" has
an answer before people are asked.

``score`` is PURE and deterministic — no model judges the model. It is a
heuristic over words, tuned on hand-written good and bad answers
(tests/test_eval_investigate.py), and it reads the FINDING, not the echo of the
evidence: an answer that only repeats "What I checked", the timeline or the
leads has said what the bundle said, not what it means, so those sections are
set aside before any check. Matching is by word, case-insensitive, with no
regular expressions:

* text is cut into lowercase runs of letters and digits (a hyphen or an
  apostrophe separates words: "re-run" is "re run", "don't" is "don t");
* a term matches where its words start a run of words — every word but the last
  must be equal, the last may continue ("wait" finds "waiting", "iam" does not
  find "williams");
* a term in must_not_mention counts only when it is not negated: a negator
  ("not", "no", "never", "without", ...) within the four words before it, or
  "will not" / "would fail" and the like right after it ("re-running will not
  help", "a retry would fail the same way") — so advice AGAINST a wrong step is not scored as advice FOR it;
* the action is the first action word found, un-negated, in the few lines that
  follow "L1 should" or "Do now": wait, retrigger (re-trigger, re-run, ...),
  check, escalate. "check" competes like the rest, so an answer that wants a
  different action should lead with it ("Do now: wait for the file");
* confidence is the first of low / medium / high within a few words of the
  word "confidence".
"""
from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
CASES_FILE = ROOT / "tests" / "eval" / "investigate_cases.json"

ACTIONS = ("wait", "retrigger", "check", "escalate")
LEVELS = ("low", "medium", "high")
CASE_KEYS = ("name", "bundle", "expect")
EXPECT_KEYS = ("must_mention_any", "must_not_mention", "action", "max_model_calls")

# The words each action goes by, in a finding. Hyphens are word breaks, so
# "re-trigger" is the term "re trigger".
ACTION_WORDS: dict[str, tuple[str, ...]] = {
    "wait": ("wait", "await", "hold off"),
    "retrigger": ("retrigger", "re trigger", "rerun", "re run", "trigger again", "trigger it again",
                  "run again", "run it again"),
    "check": ("check", "verify", "inspect", "look at"),
    "escalate": ("escalate", "raise with", "hand over", "hand off"),
}
_MARKERS = (("l1", "should"), ("do", "now"))
_MARKER_LINES = 4   # the marker's own line and the three after it

_NEGATORS = frozenset({"not", "no", "never", "without", "avoid", "nothing", "cannot", "don", "doesn", "didn",
                       "isn", "wasn", "won", "shouldn", "wouldn", "neither", "nor",
                       # a cause the answer sets aside is not a cause it invents:
                       # "rules out a permission problem", "ruling out the schema"
                       "rules", "ruled", "ruling", "excludes", "excluded", "unrelated"})
_AUX = frozenset({"will", "would", "won", "does", "doesn", "is", "isn", "can", "cannot", "should", "shouldn"})
# "a retry would fail the same way" denies the retry as firmly as "would not help".
_DOOMED = frozenset({"fail", "fails", "repeat", "repeats"})
# Six words back, within the clause: "no recent code changes or permission
# issues" denies the permission as much as the code change (found on a real
# answer that the four-word window marked as an invention).
_BEFORE = 6
_AFTER = 3

# Sections that repeat the evidence rather than say what it means. A title is
# one of these when it has any of these words.
_ECHO_WORDS = frozenset({"checked", "timeline", "leads"})
_PLAIN_TITLES = frozenset({"what i checked", "what was checked", "timeline", "leads", "finding", "cause",
                           "what l1 should do", "action", "ticket note", "follow ups", "next"})


# ----- the scorer (pure) ----------------------------------------------------------

_BOUNDARY = "|"
_SOFT = ","       # a comma: ends a clause, but a LIST runs across it ("no A, B or C")
_BREAKS = frozenset(".;:!?()\n—–")
_LIST_WORDS = frozenset({"or", "nor", "and"})
# "not a schema issue BUT a permission problem" names the permission problem.
_CONTRAST = frozenset({"but", "however", "instead", "rather", "yet", "although", "while", "whereas"})
_LIST_BACK = 16


def words(text: str) -> list[str]:
    """Lowercase runs of letters and digits; any other character separates them.
    Sentence punctuation also leaves a "|" between runs, so a negation never
    reaches across a clause ("Do not re-run; escalate" denies the re-run, not the
    escalation) and a term never spans one."""
    out: list[str] = []
    run: list[str] = []
    for ch in str(text or "").lower():
        if ch.isalnum():
            run.append(ch)
            continue
        if run:
            out.append("".join(run))
            run = []
        if ch in _BREAKS and out and out[-1] != _BOUNDARY:
            if out[-1] == _SOFT:
                out.pop()
            out.append(_BOUNDARY)
        elif ch == "," and out and out[-1] not in (_BOUNDARY, _SOFT):
            out.append(_SOFT)
    if run:
        out.append("".join(run))
    return out


def _at(tokens: list[str], term: list[str], i: int) -> bool:
    if i + len(term) > len(tokens):
        return False
    *head, last = term
    return all(tokens[i + k] == w for k, w in enumerate(head)) and tokens[i + len(head)].startswith(last)


def find(tokens: list[str], term: str) -> list[int]:
    """Where ``term`` (words, matched as described in the module note) starts."""
    wanted = words(term)
    if not wanted:
        return []
    return [i for i in range(len(tokens)) if _at(tokens, wanted, i)]


def _clause(tokens: list[str]) -> list[str]:
    """The tokens up to the first clause boundary."""
    for i, t in enumerate(tokens):
        if t in (_BOUNDARY, _SOFT):
            return tokens[:i]
    return tokens


def _denied_in_a_list(tokens: list[str], start: int) -> bool:
    """"The evidence rules out code changes, security issues, or permission
    problems" denies the last item as much as the first: a negator earlier in
    the SAME sentence, a comma and a list word (or/nor/and) between it and the
    term, and no contrast word turning the sentence around. Without the list
    word a comma still ends the denial: "Do not re-run, escalate"."""
    back = tokens[max(0, start - _LIST_BACK):start]
    if _BOUNDARY in back:
        back = back[len(back) - back[::-1].index(_BOUNDARY):]
    at = next((i for i, t in enumerate(back) if t in _NEGATORS), None)
    if at is None:
        return False
    between = back[at + 1:]
    return (_SOFT in between and any(t in _LIST_WORDS for t in between)
            and not any(t in _CONTRAST for t in between))


def _negated(tokens: list[str], start: int, length: int) -> bool:
    before = tokens[max(0, start - _BEFORE):start]
    for cut in (_BOUNDARY, _SOFT):
        if cut in before:
            before = before[len(before) - before[::-1].index(cut):]
    if _denied_in_a_list(tokens, start):
        return True
    if any(t in _NEGATORS for t in before):
        return True
    after = _clause(tokens[start + length:start + length + _AFTER])
    if after and after[0] in _DOOMED:      # "a retry fails the same way"
        return True
    return len(after) >= 2 and after[0] in _AUX and any(t in _NEGATORS or t in _DOOMED for t in after[1:])


def _live(tokens: list[str], term: str) -> list[int]:
    """The places ``term`` is said and not denied."""
    n = len(words(term))
    return [i for i in find(tokens, term) if not _negated(tokens, i, n)]


def _title(line: str) -> str | None:
    """The title when ``line`` is a section heading — a markdown heading, a line
    that is only bold text, or a bare known title — else None."""
    s = line.strip()
    if not s:
        return None
    t = " ".join(w for w in words(s) if w not in (_BOUNDARY, _SOFT))
    if s.startswith("#"):
        return t
    if t in _PLAIN_TITLES and len(s) < 40:
        return t
    if s.startswith("**") and (s.endswith("**") or s.endswith("**:")) and len(t) < 50:
        return t
    return None


def _listish(line: str) -> bool:
    """A bullet, a numbered item or a table row — what an evidence section is made of."""
    s = line.strip()
    if s[:1] in ("-", "•", "|") or s[:2] in ("* ", "+ "):
        return True
    digits = s.lstrip("0123456789")
    return len(digits) < len(s) and digits[:1] in (".", ")")


def finding_text(answer: str) -> str:
    """The answer without the sections that only echo the evidence. Such a
    section runs from its title to the next title — or, once its list has
    started, to the first line of prose after it ("What I checked" is a list,
    then the finding follows)."""
    keep: list[str] = []
    skipping = False
    listed = False
    for line in str(answer or "").splitlines():
        title = _title(line)
        if title is not None:
            skipping, listed = bool(_ECHO_WORDS & set(title.split())), False
        elif skipping and line.strip():
            if _listish(line):
                listed = True
            elif listed:
                skipping = False
        if not skipping:
            keep.append(line)
    return "\n".join(keep)


def stated_action(answer: str) -> str | None:
    """The action an answer gives L1 (see the module note), or None."""
    lines = str(answer or "").splitlines()
    for i, line in enumerate(lines):
        tokens = words(line)
        if not any(_at(tokens, list(m), j) for m in _MARKERS for j in range(len(tokens))):
            continue
        window = words("\n".join(lines[i:i + _MARKER_LINES]))
        hits = [(pos, action) for action, terms in ACTION_WORDS.items() for term in terms
                for pos in _live(window, term)]
        if hits:
            return min(hits)[1]
    return None


def stated_confidence(answer: str) -> str | None:
    """low / medium / high, whichever stands beside the first "confidence"."""
    tokens = words(answer)
    for i, tok in enumerate(tokens):
        if tok.startswith("confiden"):
            near = tokens[i + 1:i + 6] + list(reversed(tokens[max(0, i - 3):i]))
            for t in near:
                if t in LEVELS:
                    return t
    return None


def asserted_text(answer_text: str) -> str:
    """The lines that state a cause or an action outright: "Cause: …" (the
    ticket note) and "Do now: …". "" when the answer has no "Cause:" line."""
    kept: list[str] = []
    has_cause = False
    for raw in str(answer_text or "").splitlines():
        line = raw.strip().lstrip("-*> ").strip().strip("*_`").strip()
        low = line.lower()
        if low.startswith("cause:"):
            has_cause = True
            kept.append(line)
        elif low.startswith("do now"):
            kept.append(line)
    return "\n".join(kept) if has_cause else ""


def score(case: dict[str, Any], answer_text: str, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check one answer against a case's expectations.

    Returns {"passed": bool, "checks": {name: {"passed": bool, "detail": str}}}.
    The checks: answered, mentions (each group of synonyms has a hit),
    not_invented (none of must_not_mention is said un-negated), action, and —
    when the case sets them or the run reports them — confidence, model_calls
    and model_answered (meta["fallback"]: the model failed and the portal's own
    summary was used, which never counts as the model's pass)."""
    expect = case.get("expect") or {}
    meta = meta or {}
    finding = finding_text(answer_text)
    tokens = words(finding)
    checks: dict[str, dict[str, Any]] = {}

    checks["answered"] = {"passed": bool(tokens), "detail": "" if tokens else "the answer has no finding text"}

    groups = expect.get("must_mention_any") or []
    missing = [g for g in groups if not any(find(tokens, term) for term in g)]
    checks["mentions"] = {"passed": not missing,
                          "detail": "" if not missing else "never says any of: " + " | ".join(
                              "/".join(g) for g in missing)}

    # Judged where a cause or an action is ASSERTED — the note's "Cause:" line
    # and the "Do now" line — not on every sentence of the reasoning: the
    # finding is asked to say what the evidence rules out, and "no sign of a
    # permission problem" in some new phrasing is not an invention (three live
    # runs failed on exactly that, differently each time). An answer with no
    # "Cause:" line is judged on its whole finding, so it cannot hide by
    # leaving the line out.
    asserted = words(asserted_text(answer_text)) or tokens
    said = [term for term in expect.get("must_not_mention") or [] if _live(asserted, term)]
    checks["not_invented"] = {"passed": not said, "detail": "" if not said else "says: " + ", ".join(said)}

    want = expect.get("action")
    if want:
        got = stated_action(answer_text)
        checks["action"] = {"passed": got == want,
                            "detail": "" if got == want else f"wants {want}, the answer says {got or 'none'}"}

    cap = expect.get("confidence_at_most")
    if cap:
        got = stated_confidence(finding)
        ok = got is not None and LEVELS.index(got) <= LEVELS.index(cap)
        checks["confidence"] = {"passed": ok, "detail": "" if ok else
                                f"wants at most {cap}, the answer says {got or 'nothing'}"}

    if meta.get("fallback"):
        # The model did not write this: its failure is the result, not a pass on the portal's own summary.
        checks["model_answered"] = {"passed": False, "detail": "the model did not answer (the fallback summary was used)"}

    limit = expect.get("max_model_calls")
    calls = meta.get("model_calls")
    if limit is not None and calls is not None:
        ok = int(calls) <= int(limit)
        checks["model_calls"] = {"passed": ok, "detail": "" if ok else f"{calls} model calls, at most {limit}"}

    return {"passed": all(c["passed"] for c in checks.values()), "checks": checks}


# ----- the cases file ---------------------------------------------------------------

def load_cases(path: Path | str = CASES_FILE) -> list[dict[str, Any]]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def case_problems(case: dict[str, Any]) -> list[str]:
    """Why a case is malformed (empty = fine) — a typo here would quietly score nothing."""
    out = [f"missing {k}" for k in CASE_KEYS if k not in case]
    expect = case.get("expect") or {}
    out += [f"expect is missing {k}" for k in EXPECT_KEYS if k not in expect]
    if expect.get("action") not in ACTIONS:
        out.append(f"expect.action must be one of {', '.join(ACTIONS)}")
    if expect.get("confidence_at_most", "low") not in LEVELS:
        out.append(f"expect.confidence_at_most must be one of {', '.join(LEVELS)}")
    groups = expect.get("must_mention_any")
    if not (isinstance(groups, list) and groups and all(isinstance(g, list) and g for g in groups)):
        out.append("expect.must_mention_any must be a non-empty list of non-empty synonym lists")
    return out


# ----- running ----------------------------------------------------------------------

AnswerFn = Callable[[dict[str, Any]], dict[str, Any]]


def _normal(answer: Any) -> dict[str, Any]:
    if isinstance(answer, str):
        return {"text": answer}
    return dict(answer) if isinstance(answer, dict) else {"text": ""}


def run(cases: list[dict[str, Any]], answer_fn: AnswerFn, out: Callable[[str], None] = print) -> dict[str, Any]:
    """Answer and score every case; print a table and the totals. A case whose
    answer_fn raises is a failed case with the error, never a crashed run."""
    results: list[dict[str, Any]] = []
    for case in cases:
        started = time.monotonic()
        try:
            answer = _normal(answer_fn(case["bundle"]))
            error = ""
        except Exception as e:  # noqa: BLE001 — one case failing must not hide the others
            answer, error = {"text": ""}, f"{type(e).__name__}: {str(e)[:200]}"
        meta = {"model_calls": answer.get("model_calls"), "seconds": answer.get("seconds"),
                "model": answer.get("model", ""), "fallback": bool(answer.get("fallback"))}
        if meta["seconds"] is None:
            meta["seconds"] = round(time.monotonic() - started, 1)
        res = score(case, str(answer.get("text") or ""), meta)
        if error:
            res = {"passed": False, "checks": {"answer_fn": {"passed": False, "detail": error}}}
        results.append({"name": case.get("name", "?"), "passed": res["passed"], "checks": res["checks"],
                        "model_calls": meta["model_calls"], "seconds": meta["seconds"], "model": meta["model"]})
    _print_table(results, out)
    return {"passed": sum(r["passed"] for r in results), "total": len(results), "results": results}


def _failed(result: dict[str, Any]) -> str:
    return "; ".join(f"{name}: {c['detail']}" if c["detail"] else name
                     for name, c in result["checks"].items() if not c["passed"])


def _print_table(results: list[dict[str, Any]], out: Callable[[str], None]) -> None:
    rows = [("case", "result", "calls", "seconds", "failed checks")]
    for r in results:
        rows.append((r["name"], "pass" if r["passed"] else "FAIL",
                     "-" if r["model_calls"] is None else str(r["model_calls"]),
                     "-" if r["seconds"] is None else f"{r['seconds']:g}", _failed(r)))
    widths = [max(len(row[i]) for row in rows) for i in range(4)]
    for row in rows:
        out("  ".join(row[i].ljust(widths[i]) for i in range(4)) + "  " + row[4])
    calls = [r["model_calls"] for r in results if r["model_calls"] is not None]
    secs = [r["seconds"] for r in results if r["seconds"] is not None]
    out("")
    out(f"passed {sum(r['passed'] for r in results)} of {len(results)}"
        + (f" · {sum(calls)} model calls ({sum(calls) / len(calls):.1f} a case)" if calls else "")
        + (f" · {sum(secs):.1f}s in all" if secs else ""))


# ----- where the answers come from ---------------------------------------------------

def _paths() -> None:
    for p in (ROOT / "src", ROOT):
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))


def model_free_answer_fn() -> AnswerFn:
    """The baseline: the markdown the portal writes with no model at all
    (tools/support/evidence.render_markdown). It lists leads, it does not conclude —
    so it should miss the cause checks and say nothing it cannot support."""
    _paths()
    from release_agent.tools.support import evidence as investigation

    render = getattr(investigation, "render_markdown", None)
    if render is None:
        raise SystemExit("release_agent.tools.support.evidence.render_markdown does not exist yet.")
    return lambda bundle: {"text": render(bundle), "model_calls": 0, "seconds": 0.0, "model": ""}


def live_answer_fn() -> AnswerFn:
    """The real agent: adk_release_agent.investigate_workflow.answer_from_bundle(bundle)
    writes the finding from a GIVEN bundle (no evidence is collected), returning
    {"text", "model_calls", "seconds", "model"}. Needs model credentials."""
    _paths()
    try:
        from adk_release_agent import investigate_workflow
    except ImportError as e:
        raise SystemExit(f"adk_release_agent.investigate_workflow is not available yet ({e}).") from e
    fn = getattr(investigate_workflow, "answer_from_bundle", None)
    if fn is None:
        raise SystemExit("adk_release_agent.investigate_workflow has no answer_from_bundle(bundle) yet.")
    if inspect.iscoroutinefunction(fn):
        # ONE loop for every case: the model client keeps its connections on
        # the loop that first used it, and a loop per case leaves them to be
        # closed after their loop is gone ("Event loop is closed" at exit).
        loop = asyncio.new_event_loop()
        return lambda bundle: loop.run_until_complete(fn(bundle))
    return fn


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--model-free", action="store_true",
                    help="baseline: score investigation.render_markdown instead of the agent")
    ap.add_argument("--cases", default=str(CASES_FILE), help="the cases file")
    ap.add_argument("--case", action="append", default=[], help="run only the case with this name (repeatable)")
    ap.add_argument("--json", action="store_true", help="print the results as JSON instead of the table")
    args = ap.parse_args(argv)

    cases = load_cases(args.cases)
    if args.case:
        cases = [c for c in cases if c.get("name") in args.case]
        if not cases:
            raise SystemExit("no case has that name")
    problems = [f"{c.get('name', '?')}: {p}" for c in cases for p in case_problems(c)]
    if problems:
        raise SystemExit("bad cases file:\n  " + "\n  ".join(problems))
    answer_fn = model_free_answer_fn() if args.model_free else live_answer_fn()
    summary = run(cases, answer_fn, out=(lambda _line: None) if args.json else print)
    if args.json:
        print(json.dumps(summary, indent=1))
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
