"""PURE: how urgent an incident is — an ordered list of rules the team owns.

The rules are configuration (SUPPORT_PRIORITY, written as the chart's
`supportPriority:` block), not code: the people working the queue tune the
policy without a release. The FIRST rule whose conditions all hold decides the
level and the reason the card shows. With nothing configured, the built-in list
below applies — the same policy the code held before it moved here.

    parse_rules(raw, high_count=, medium_count=) -> (rules, problems)
    decide(rules, facts)                         -> (level, reason)

A rule is {"level", "when", "reason"}:

    level   high | medium | low
    when    every condition must hold (none = always):
              kind        error | stuck | missing | volume (or a list)
              category    upstream | process | unit | single | spread | stuck | missing | volume
              min_runs    at least this many runs      max_runs  at most this many
              min_streak  failing at least this many dates in a row
              recurring   true: some runs failed on earlier dates too; false: all new today
              critical    true: a value listed in SUPPORT_CRITICAL is affected
              values      {role: value or [values]} — any listed value is affected
              runbook     a known-issue title (or list), or true / false for any / none
    reason  the text on the card, with {runs} {count} {streak} {kind} {category}
            {cause} {critical} {runbook} filled in

Nothing is evaluated: conditions are a fixed vocabulary and a reason's
placeholders are a fixed set, so a rule cannot run code. A rule set with any
problem is NOT used — the built-in rules apply and the problem is reported (the
triage notes and /api/support/config-check), never a half-applied policy.
"""
from __future__ import annotations

import json
from typing import Any

from .config import DIMENSIONS

LEVELS = ("high", "medium", "low")
KINDS = ("error", "stuck", "missing", "volume")
CATEGORIES = ("upstream", "process", "unit", "single", "spread", "stuck", "missing", "volume")
PLACEHOLDERS = ("runs", "count", "streak", "kind", "category", "cause", "critical", "runbook")
_INT_CONDITIONS = ("min_runs", "max_runs", "min_streak")
_BOOL_CONDITIONS = ("recurring", "critical")
CONDITIONS = ("kind", "category", "values", "runbook") + _INT_CONDITIONS + _BOOL_CONDITIONS
_MAX_RULES = 50
_MAX_REASON = 200

_CAUSE = {"upstream": "one upstream system", "process": "one process",
          "spread": "the same error everywhere (platform)"}


def default_rules(high_count: int = 10, medium_count: int = 3) -> list[dict[str, Any]]:
    """The built-in policy (and the shape to copy when writing your own)."""
    return [
        {"level": "high", "when": {"critical": True}, "reason": "critical: {critical}"},
        {"level": "high", "when": {"kind": "volume"}, "reason": "output collapsed on a run that succeeded"},
        {"level": "high", "when": {"min_runs": high_count}, "reason": f"{{runs}} (≥ {high_count})"},
        {"level": "high", "when": {"kind": "error", "category": ["process", "spread", "upstream"],
                                   "min_runs": medium_count},
         "reason": "{runs}, {cause}"},
        {"level": "medium", "when": {"kind": ["stuck", "missing"]}, "reason": "{kind} work will not finish on its own"},
        {"level": "medium", "when": {"min_runs": medium_count}, "reason": f"{{runs}} (≥ {medium_count})"},
        {"level": "medium", "when": {"min_streak": 2}, "reason": "failing {streak} dates in a row"},
        {"level": "low", "when": {"max_runs": 1}, "reason": "one run, first time"},
        {"level": "low", "reason": "{runs}"},
    ]


# ----- parsing ---------------------------------------------------------------------

def _texts(value: Any) -> list[str] | None:
    """A string or a list of strings → the list; anything else → None."""
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not items or not all(isinstance(v, str) and v.strip() for v in items):
        return None
    return [v.strip() for v in items]


def _placeholders(text: str) -> tuple[list[str], bool]:
    """({names} in the text, every brace balanced) — read character by character."""
    names, cur, inside = [], "", False
    for ch in text:
        if ch == "{":
            if inside:
                return names, False
            inside, cur = True, ""
        elif ch == "}":
            if not inside:
                return names, False
            names.append(cur)
            inside = False
        elif inside:
            cur += ch
    return names, not inside


def _rule_problems(i: int, rule: Any) -> list[str]:
    where = f"priority rule {i + 1}"
    if not isinstance(rule, dict):
        return [f"{where} is not a mapping"]
    out = [f"{where}: unknown key {k!r} (use level, when, reason)" for k in rule if k not in ("level", "when", "reason")]
    if rule.get("level") not in LEVELS:
        out.append(f"{where}: level must be one of {', '.join(LEVELS)}")
    reason = rule.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > _MAX_REASON:
        out.append(f"{where}: reason must be a text of 1–{_MAX_REASON} characters")
    else:
        names, balanced = _placeholders(reason)
        if not balanced:
            out.append(f"{where}: reason has an unmatched {{ or }}")
        out += [f"{where}: reason uses {{{n}}}, not one of {{{'}, {'.join(PLACEHOLDERS)}}}"
                for n in names if n not in PLACEHOLDERS]
    when = rule.get("when", {})
    if not isinstance(when, dict):
        return out + [f"{where}: when must be a mapping of conditions"]
    for key, value in when.items():
        if key not in CONDITIONS:
            out.append(f"{where}: unknown condition {key!r} (use {', '.join(CONDITIONS)})")
        elif key in _INT_CONDITIONS:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                out.append(f"{where}: {key} must be a whole number ≥ 0")
        elif key in _BOOL_CONDITIONS:
            if not isinstance(value, bool):
                out.append(f"{where}: {key} must be true or false")
        elif key in ("kind", "category"):
            allowed = KINDS if key == "kind" else CATEGORIES
            items = _texts(value)
            if items is None or any(v not in allowed for v in items):
                out.append(f"{where}: {key} must be one of {', '.join(allowed)} (or a list of them)")
        elif key == "runbook":
            if not isinstance(value, bool) and _texts(value) is None:
                out.append(f"{where}: runbook must be a known-issue title, a list of them, or true / false")
        elif key == "values":
            if not isinstance(value, dict) or not value:
                out.append(f"{where}: values must be a mapping of role → value(s)")
            else:
                for role, wanted in value.items():
                    if role not in DIMENSIONS:
                        out.append(f"{where}: values names {role!r}, not one of {', '.join(DIMENSIONS)}")
                    elif _texts(wanted) is None:
                        out.append(f"{where}: values.{role} must be a value or a list of values")
    return out


def parse_rules(raw: str, *, high_count: int = 10, medium_count: int = 3) -> tuple[list[dict[str, Any]], list[str]]:
    """SUPPORT_PRIORITY (JSON: a list of rules, or {"rules": [...]}) → (rules,
    problems). Empty → the built-in rules. Any problem → the built-in rules and
    the problems, so a typo never silently re-orders the queue."""
    builtin = default_rules(max(1, int(high_count)), max(1, int(medium_count)))
    text = (raw or "").strip()
    if not text:
        return builtin, []
    try:
        data = json.loads(text)
    except ValueError as e:
        return builtin, [f"SUPPORT_PRIORITY is not valid JSON ({e.msg} at character {e.pos})"]
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list) or not rules:
        return builtin, ["SUPPORT_PRIORITY must be a non-empty list of rules"]
    if len(rules) > _MAX_RULES:
        return builtin, [f"SUPPORT_PRIORITY has {len(rules)} rules; at most {_MAX_RULES}"]
    problems = [p for i, rule in enumerate(rules) for p in _rule_problems(i, rule)]
    if problems:
        return builtin, problems
    return [{"level": r["level"], "when": dict(r.get("when") or {}), "reason": r["reason"].strip()} for r in rules], []


# ----- deciding ----------------------------------------------------------------------

def _plural(n: int) -> str:
    return f"{n} run" if n == 1 else f"{n} runs"


def _holds(key: str, wanted: Any, f: dict[str, Any]) -> bool:
    if key == "kind":
        return f["kind"] in _texts(wanted)
    if key == "category":
        return f["category"] in _texts(wanted)
    if key == "min_runs":
        return f["count"] >= wanted
    if key == "max_runs":
        return f["count"] <= wanted
    if key == "min_streak":
        return f["streak"] >= wanted
    if key == "recurring":
        return f["recurring"] == wanted
    if key == "critical":
        return bool(f["critical"]) == wanted
    if key == "runbook":
        if isinstance(wanted, bool):
            return bool(f["runbook"]) == wanted
        return (f["runbook"] or "").lower() in {w.lower() for w in _texts(wanted)}
    if key == "values":
        for role, items in wanted.items():
            have = f["values"].get(role)
            have = set(have) if isinstance(have, (list, set, tuple)) else ({have} if have else set())
            if have & set(_texts(items)):
                return True
        return False
    return False


def _fill(reason: str, f: dict[str, Any]) -> str:
    filled = {"runs": _plural(f["count"]), "count": str(f["count"]), "streak": str(f["streak"]),
              "kind": f["kind"], "category": f["category"], "cause": _CAUSE.get(f["category"], f["category"]),
              "critical": ", ".join(f["critical"]), "runbook": f["runbook"] or "no runbook match"}
    for name, value in filled.items():
        reason = reason.replace("{" + name + "}", value)
    return reason


def decide(rules: list[dict[str, Any]], facts: dict[str, Any]) -> tuple[str, str]:
    """(level, reason) of the first rule that holds. `facts`: kind, category,
    count, streak, recurring, critical (the matched "role value" texts),
    values ({role: values}), runbook (the known issue's title or None)."""
    f = {"kind": "error", "category": "", "count": 0, "streak": 0, "recurring": False, "critical": [],
         "values": {}, "runbook": None, **facts}
    for rule in rules:
        if all(_holds(k, v, f) for k, v in (rule.get("when") or {}).items()):
            return rule["level"], _fill(rule["reason"], f)
    return "low", "no priority rule matched"
