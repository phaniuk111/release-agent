"""The L1 runbook: a plain-English skill file, read by the portal AND the AI.

The team's runbook is `adk_release_agent/skills/support-runbook/SKILL.md`. It
ships in the image, so it is reviewed and versioned with the code. One file,
two readers:

    load()        -> (entries, problems)  the known issues' header lines
                     (match / category / when / action / owner / steps), so a
                     triage card names the known issue and its action with NO
                     model — first match wins, as before
    text()        -> the whole skill without its frontmatter: how to read the
                     control table, and each issue's "check / it is / it is
                     not", handed to the model by the chat tools and Investigate
    skill_body(p) -> any skill file's text without its frontmatter

Parsing is line by line — no regex. A known issue the portal cannot use (no
`match:`, an unknown action) is skipped and named in `problems`, which the
triage passes on as a note: the people editing the file see what they broke.
"""
from __future__ import annotations

import pathlib
from typing import Any

# src/release_agent/tools/support/runbook.py → the repo (or /app in the image),
# where adk_release_agent/ sits beside src/.
RUNBOOK_SKILL = (pathlib.Path(__file__).resolve().parents[4]
                 / "adk_release_agent" / "skills" / "support-runbook" / "SKILL.md")

ACTIONS = ("wait", "retrigger", "check", "escalate")
_CATEGORIES = ("upstream", "process", "unit", "single", "spread")
_KNOWN_ISSUES = "## known issues"


def skill_body(path: pathlib.Path | str) -> str:
    """The file's text after its `---` frontmatter; "" when it cannot be read."""
    try:
        lines = pathlib.Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    if lines and lines[0].strip() == "---":
        end = next((i for i, line in enumerate(lines[1:], 1) if line.strip() == "---"), None)
        if end is not None:
            lines = lines[end + 1:]
    return "\n".join(lines).strip()


def text(path: pathlib.Path | str | None = None) -> str:
    return skill_body(path or RUNBOOK_SKILL)


def _header(line: str) -> tuple[str, str] | None:
    """"match: quota, exceeded" (optionally a "- " bullet) → ("match", "quota, exceeded")."""
    s = line.strip()
    if s.startswith("- "):
        s = s[2:].strip()
    key, sep, value = s.partition(":")
    key = key.strip().lower()
    if not sep or key not in ("match", "category", "when", "action", "owner", "steps"):
        return None
    return key, value.strip()


def _step(line: str) -> str | None:
    """"1. Do this" / "- Do this" → "Do this"; anything else → None."""
    s = line.strip()
    if s.startswith(("- ", "* ")):
        return s[2:].strip() or None
    digits = len(s) - len(s.lstrip("0123456789"))
    if digits and s[digits:digits + 1] in (".", ")"):
        return s[digits + 1:].strip() or None
    return None


def _entry(title: str, lines: list[str]) -> tuple[dict[str, Any] | None, str | None]:
    """One `###` section → (entry, problem). Header lines come first; the
    steps list runs until the first line that is not a step."""
    matches: list[list[str]] = []
    fields: dict[str, str] = {}
    steps: list[str] = []
    in_steps = False
    for line in lines:
        if in_steps:
            step = _step(line)
            if step:
                steps.append(step)
                continue
            if not line.strip() and not steps:
                continue
            in_steps = False
        head = _header(line)
        if head is None:
            continue
        key, value = head
        if key == "match":
            words = [w.strip().lower() for w in value.split(",") if w.strip()]
            if words:
                matches.append(words)
        elif key == "steps":
            in_steps = True
            if value:
                steps.append(value)
        elif key not in fields:          # the first of each wins; the prose may say "action:" later
            fields[key] = value
    if not matches:
        return None, f"runbook issue {title!r} has no match: line — skipped"
    action = fields.get("action", "").lower()
    if action not in ACTIONS:
        return None, f"runbook issue {title!r}: action must be one of {', '.join(ACTIONS)} — skipped"
    category = fields.get("category", "").lower() or None
    if category and category not in _CATEGORIES:
        return None, f"runbook issue {title!r}: category must be one of {', '.join(_CATEGORIES)} — skipped"
    when: dict[str, str] = {}
    for pair in fields.get("when", "").split(","):
        role, eq, value = pair.partition("=")
        if eq and role.strip() and value.strip():
            when[role.strip()] = value.strip()
    return {"title": title, "match": matches, "action": action, "steps": steps,
            "escalate_to": fields.get("owner") or None, "category": category, "when": when}, None


def load(path: pathlib.Path | str | None = None) -> tuple[list[dict[str, Any]], list[str]]:
    """The known issues, in file order, and what was wrong with any skipped."""
    path = path or RUNBOOK_SKILL
    body = skill_body(path)
    if not body:
        return [], [f"The runbook skill could not be read ({pathlib.Path(path).name})."]
    entries: list[dict[str, Any]] = []
    problems: list[str] = []
    in_known = False
    title: str | None = None
    section: list[str] = []

    def flush() -> None:
        if title is None:
            return
        entry, problem = _entry(title, section)
        if entry:
            entries.append(entry)
        if problem:
            problems.append(problem)

    for line in body.splitlines():
        s = line.strip()
        if s.startswith("## ") and not s.startswith("### "):
            flush()
            title, section = None, []
            in_known = s.lower() == _KNOWN_ISSUES
            continue
        if in_known and s.startswith("### "):
            flush()
            title, section = s[4:].strip(), []
            continue
        if title is not None:
            section.append(line)
    flush()
    return entries, problems


def matches(entry: dict[str, Any], haystack: str) -> bool:
    """Any `match:` line whose words ALL appear in the (lowercased) error."""
    return any(all(w in haystack for w in words) for words in entry["match"])
