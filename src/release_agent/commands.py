"""The portal without a model (LLM_ENABLED=false): a fixed set of chat commands.

With the AI assistant switched off, the chat still answers the pills that send
text — and the same phrases typed by hand — by calling the very tools the chat
agent would have called, and writing their results out plainly. Nothing here
interprets free text beyond recognising those commands: a message that is not
one of them is told what is available, never guessed at.

This module only RECOGNISES a command and WRITES UP its result. Running it —
and pausing a promotion to PRD/PRL1 or a removal from production for a yes/no —
is the ADK commands workflow's job (adk_release_agent/commands_workflow.py),
using the same approval rules as the chat agent (adk_release_agent/approvals.py)
and the same tools (adk_release_agent/tools.py). Deploys and releases never
come here: they take the deterministic preview → CONFIRM-token workflow, which
needs no model either.

No regex (house rule): words come from parsing._norm_words, image:tag pairs
from parsing._extract_images_from_text.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .agent.parsing import _extract_images_from_text, _norm_words

ONBOARDING_OFF = ("Enable LLM access for this — API onboarding guidance needs the AI assistant, "
                  "which is switched off in this portal (LLM_ENABLED=false).")
AI_ONLY_OFF = ("Enable LLM access for this — it needs the AI assistant, which is switched off in "
               "this portal (LLM_ENABLED=false).")

HELP = """The AI assistant is switched off in this portal (LLM_ENABLED=false), so the chat
answers these commands only — the pills above send them for you:

- `deploy status` — what is deployed in UAT and PRD, and the PRD release window
- `promote the CARE release to prd` / `promote the DF release to prd` (also `uat`, `prl1`)
- `what images can I promote?` — the allowed image catalogue
- `show me the 5 most recent workflow runs`
- `verify <image>:<tag>` — the build report for that tag (`… in <owner/repo>` for another build repo)
- `check build controls for <image>:<tag>`
- `find the deployment PR for <image>:<tag>`, or `PR #123`
- `remove <chart>[, <chart>] from the release` (add `from prod` for production)

Deploys and releases work as always through their forms."""


@dataclass(frozen=True)
class Command:
    name: str                        # the tool it calls, or "onboarding"
    args: dict[str, Any] = field(default_factory=dict)
    label: str = ""                  # what it does, in words — replies and approvals


def _target(words: set[str]) -> str:
    if "prl1" in words:
        return "prl1"
    if words & {"prd", "prod", "production"}:
        return "prd"
    if "uat" in words:
        return "uat"
    return ""


def _first_number(words: list[str], default: int, most: int) -> int:
    for w in words:
        if w.isdigit():
            return max(1, min(int(w), most))
    return default


def _pr_number(message: str, words: list[str]) -> int:
    """'#123', 'PR 123', 'pull request 123' -> 123; 0 when none."""
    for token in message.replace(",", " ").split():
        if token.startswith("#") and token[1:].isdigit():
            return int(token[1:])
    for i, w in enumerate(words[:-1]):
        if w in ("pr", "request") and words[i + 1].isdigit():
            return int(words[i + 1])
    return 0


def _repo(message: str) -> str:
    """An owner/repo token ('in org/build-repo'), never a URL or a path."""
    for token in message.replace(",", " ").split():
        t = token.strip(".;:()[]`'\"")
        if t.count("/") == 1 and "://" not in t and all(t.split("/")) and ":" not in t:
            return t
    return ""


def _removal_names(message: str) -> list[str]:
    """The chart names between 'remove' and 'from': 'remove a, b and c from …'."""
    low = message.lower()
    start = low.find("remove")
    end = low.find(" from ", start)
    if start < 0 or end < 0:
        return []
    chunk = message[start + len("remove"):end].replace(" and ", ",")
    return [n.strip(" .") for n in chunk.split(",") if n.strip(" .")]


def parse(message: str) -> Command | None:
    """The command a message is, or None. Order matters only where phrases share
    words: an image:tag lookup is read before 'promote' because 'what images can
    I promote?' names no target and must not become a promotion."""
    text = message or ""
    words = _norm_words(text)
    ws = set(words)
    images = _extract_images_from_text(text)
    image = images[0] if images else None

    if any(w.startswith("onboard") for w in ws):
        return Command("onboarding", label="API onboarding")

    # A PR lookup first: "find the deployment PR for x:1 and summarize its …
    # controls" names controls too, and is still a PR lookup.
    if ws & {"pr", "prs", "pull"}:
        number = _pr_number(text, words)
        if number:
            return Command("get_pr_details", {"pr_number": number}, f"PR #{number}")
        if image:
            term = f"{image['name']}:{image['tag']}"
            return Command("find_prs", {"search_term": term}, f"Deployment PRs for {term}")

    if image and (ws & {"verify", "verified", "built", "build"} or ("controls" in ws or "control" in ws)):
        args = {"image": image["name"], "tag": image["tag"]}
        if repo := _repo(text):
            args["repo"] = repo
        what = "Build controls" if ("controls" in ws or "control" in ws) else "Build report"
        return Command("get_build_report", args, f"{what} for {image['name']}:{image['tag']}")

    if "images" in ws and (ws & {"allowed", "promote", "catalog", "catalogue", "list"}):
        return Command("list_allowed_images", label="Allowed images")

    if any(w.startswith("promot") for w in ws) and (target := _target(ws)):
        df = bool(ws & {"df", "dataflow"})
        name = "promote_df_release" if df else "promote_release"
        which = "DF" if df else "CARE"
        return Command(name, {"target": target}, f"Promote the {which} release to {target.upper()}")

    if "remove" in ws and (names := _removal_names(text)):
        env = "prod" if ws & {"prod", "prd", "production"} else ("uat" if "uat" in ws else "staging")
        where = {"prod": "production", "uat": "UAT", "staging": "today's PRD release"}[env]
        return Command("remove_from_release", {"image_names": ",".join(names), "environment": env},
                       f"Remove {', '.join(names)} from {where}")

    if "status" in ws and ws & {"deploy", "deployment", "release", "uat", "prd"}:
        return Command("check_release_window", label="Deploy status")

    if "runs" in ws or ("workflow" in ws and "run" in ws):
        limit = _first_number(words, 5, 20)
        return Command("get_recent_runs", {"limit": limit}, f"The {limit} most recent workflow runs")

    return None


# --- results in plain words ---------------------------------------------------------

_SKIP = {"ok", "note"}


def _cell(value: Any) -> str:
    if isinstance(value, str) and value.startswith(("http://", "https://")):
        return f"[link]({value})"
    text = "" if value is None else str(value)
    return text.replace("|", "\\|").replace("\n", " ")[:120]


def _table(rows: list[dict[str, Any]], limit: int = 20) -> list[str]:
    columns: list[str] = []
    for row in rows[:limit]:
        for key, value in row.items():
            if key not in columns and not isinstance(value, (dict, list)) and len(columns) < 7:
                columns.append(key)
    if not columns:
        return []
    out = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    out += ["| " + " | ".join(_cell(row.get(c)) for c in columns) + " |" for row in rows[:limit]]
    if len(rows) > limit:
        out.append(f"_…and {len(rows) - limit} more._")
    return out


def _lines(data: dict[str, Any], depth: int = 0) -> list[str]:
    pad = "  " * depth
    out: list[str] = []
    for key, value in data.items():
        if key in _SKIP or value in (None, "", [], {}):
            continue
        label = key.replace("_", " ")
        if isinstance(value, list) and all(isinstance(v, dict) for v in value):
            out.append(f"{pad}**{label}**")
            out.extend(_table(value))
        elif isinstance(value, list):
            shown = ", ".join(_cell(v) for v in value[:30])
            out.append(f"{pad}- {label}: {shown}" + (f" (+{len(value) - 30} more)" if len(value) > 30 else ""))
        elif isinstance(value, dict):
            # Deeper than one level is configuration, not an answer: its flat
            # values on one line, and a label with nothing under it is dropped.
            if depth >= 1:
                flat = ", ".join(f"{k}={_cell(v)}" for k, v in value.items() if not isinstance(v, (dict, list)))
                if flat:
                    out.append(f"{pad}- {label}: {flat[:300]}")
            elif inner := _lines(value, depth + 1):
                out.append(f"{pad}- **{label}**")
                out.extend(inner)
        else:
            out.append(f"{pad}- {label}: {_cell(value)}")
    return out


def render(command: Command, result: Any) -> str:
    """A tool result as plain markdown — no model writes it up."""
    if isinstance(result, str):
        return f"**{command.label}**\n\n{result}"
    if not isinstance(result, dict):
        return f"**{command.label}**\n\n{result}"
    if result.get("ok") is False or (result.get("error") and not result.get("ok")):
        why = result.get("error") or result.get("note") or "it did not succeed"
        return f"**{command.label}** — not done: {why}"
    parts = [f"**{command.label}**"]
    if result.get("note"):
        parts.append(str(result["note"]))
    body = _lines(result)
    if body:
        parts.append("\n".join(body))
    return "\n\n".join(parts)


def run(command: Command) -> str:
    """Call the command's tool (blocking — the caller runs this off the event loop)."""
    if command.name == "onboarding":
        return ONBOARDING_OFF
    from adk_release_agent import tools as T

    tool = getattr(T, command.name)
    try:
        result = tool(**command.args)
    except Exception as e:  # noqa: BLE001 — a failed lookup is an answer, not a crash
        return f"**{command.label}** — not done: {e}"
    return render(command, result)


def approval_payload(name: str, label: str) -> dict[str, Any]:
    """The same interrupt shape a paused agent approval sends, so the page shows
    its Approve/Reject box."""
    return {
        "type": "confirmation",
        "function": name,
        "message": f"**{label}?** This changes a live environment.",
        "action": 'Reply "yes" to approve, or "no" to reject.',
    }
