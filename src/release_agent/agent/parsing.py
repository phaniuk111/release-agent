"""Pure natural-language intent parsing — image:tag extraction, environment and
change-ticket detection, and re-run / removal / retrigger / question classification.
Every function here is a deterministic text helper with no graph or LLM state."""
from __future__ import annotations

import json
from typing import Any, Optional


STEP_APPLY = "apply_manifest"
STEP_DISPATCH = "dispatch_workflow"
STEP_RELEASE_PR = "release_pr"
ALL_STEPS = [STEP_APPLY, STEP_DISPATCH]
_STEP_ALIASES = {
    STEP_APPLY: ["apply_manifest", "apply-manifest", "apply", "commit", "manifest"],
    STEP_DISPATCH: ["dispatch_workflow", "dispatch-workflow", "dispatch", "workflow", "trigger"],
    STEP_RELEASE_PR: ["release_pr", "release-pr", "pr", "open_pr", "open-pr", "raise_pr"],
}


# Change-ticket fields required when promoting to prod.
_CHG_FIELDS = ("chg_name", "chg_summary", "start_date", "end_date")


# Words that are never image names and never tags (deployment environments + filler).
_ENV_WORDS = {
    "prod",
    "production",
    "stage",
    "staging",
    "dev",
    "development",
    "qa",
    "uat",
    "test",
    "testing",
    "sandbox",
    "preprod",
    "perf",
    "canary",
}
_STOP_WORDS = {
    "to",
    "and",
    "the",
    "for",
    "update",
    "promote",
    "set",
    "as",
    "tag",
    "in",
    "on",
    "env",
    "environment",
    "deploy",
    "release",
    "please",
    "with",
    "a",
    "an",
    "image",
    "version",
    "bump",
    "rollout",
    "roll",
    "out",
} | _ENV_WORDS


def _looks_like_tag(tag: str) -> bool:
    """A tag looks like a version / sha / 'latest' — NOT an environment word."""
    t = tag.strip().lower()
    if not t:
        return False
    if t == "latest":
        return True
    if t[0] == "v" and len(t) > 1 and t[1].isdigit():  # v1.2.3
        return True
    if t[0].isdigit():  # 2.0.0
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
    "find",
    "show",
    "list",
    "get",
    "summarize",
    "summarise",
    "track",
    "status",
    "comment",
    "ticket",
    "chg",
    "rmg",
    "pr",
    "prs",
    "pull",
    "request",
    "which",
    "what",
    "where",
    "when",
    "how",
    "why",
    "check",
    "view",
    "read",
    "tell",
    "is",
    "are",
    "did",
    "does",
    # verification / lookups (route to the LLM, not the promote gate)
    "verify",
    "verified",
    "validate",
    "build",
    "built",
    "builds",
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


def _extract_json_objects(text: str) -> list:
    """Brace-scan for every balanced {...} substring and json.loads each independently
    (string-aware, regex-free). Lets us recover chart entries from loose/concatenated
    objects — e.g. two entries pasted with no comma and no include[] wrapper."""
    out = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth, in_str, esc, end = 0, False, False, -1
        for j in range(i, n):
            ch = text[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = j
                    break
        if end == -1:
            break
        try:
            obj = json.loads(text[i : end + 1])
        except (json.JSONDecodeError, TypeError):
            # This {...} isn't valid on its own (e.g. an outer wrapper with a missing
            # inner comma) — descend one char so nested valid entry objects are found.
            i += 1
            continue
        if isinstance(obj, dict) and ("helm_chart_name" in obj or "helm_chart_version" in obj):
            out.append(obj)
        i = end + 1
    return out


def _try_parse_json_payload(text: str) -> Optional[dict]:
    """Parse a pasted JSON deploy payload into a release_request. Accepts:
      - the deploy form entry: {"helm_chart_name", "helm_chart_version", "gke_namespace"}
      - an include list: {"include": [{helm_chart_name, helm_chart_version, gke_namespace}, ...]}
      - legacy: {"images": {name: tag}} or {"images": [{image|name, tag}]}
      - loose/concatenated entry objects (missing commas / no include[] wrapper) — recovered
    Returns {images: [{name, tag}], entries, environment, namespace, ...} or None."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, TypeError):
        # Lenient recovery: brace-scan loose entry objects and wrap them in include[].
        recovered = _extract_json_objects(text)
        if not recovered:
            return None
        data = {"include": recovered}
    if not isinstance(data, dict):
        return None

    pairs: list[dict] = []
    entries: list[dict] = []  # full deployment.json entries (preserved for override + multi-chart)
    namespace = ""
    chart_dir = ""
    values_file = ""

    def _add(e):
        nonlocal namespace, chart_dir, values_file
        if not isinstance(e, dict):
            return
        n = e.get("helm_chart_name") or e.get("name") or e.get("image")
        v = e.get("helm_chart_version") or e.get("tag") or e.get("version")
        if n and v:
            pairs.append({"name": str(n), "tag": str(v)})
            entries.append(dict(e))
            if not namespace and e.get("gke_namespace"):
                namespace = str(e["gke_namespace"])
            if not chart_dir and e.get("helm_chart_dir"):
                chart_dir = str(e["helm_chart_dir"])
            if not values_file and e.get("helm_values_file_name"):
                values_file = str(e["helm_values_file_name"])

    if isinstance(data.get("include"), list):
        for e in data["include"]:
            _add(e)
    elif data.get("helm_chart_name"):
        _add(data)
    else:
        images = data.get("images")
        if isinstance(images, dict):
            pairs = [{"name": str(k), "tag": str(v)} for k, v in images.items()]
        elif isinstance(images, list):
            for it in images:
                _add(it)

    if not pairs:
        return None
    namespace = namespace or str(data.get("gke_namespace") or data.get("namespace") or "")
    chart_dir = chart_dir or str(data.get("helm_chart_dir") or "")
    values_file = values_file or str(data.get("helm_values_file_name") or "")
    env = str(data.get("environment") or "uat").lower()
    env = "prod" if env in ("prod", "prd", "production") else "uat"
    return {
        "images": pairs,
        "entries": entries,
        "environment": env,
        "namespace": namespace,
        "chart_dir": chart_dir,
        "values_file": values_file,
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
    rerun_phrases = (
        " rerun ",
        " re run ",
        " retry ",
        " re try ",
        " reexecute ",
        " re execute ",
        " redo ",
        " run again ",
        " try again ",
    )
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

def _message_text(msg: Any) -> str:
    content = getattr(msg, "content", msg)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(p for p in parts if p)
    return str(content or "")


def _is_human_message(msg: Any) -> bool:
    role = getattr(msg, "role", None) or getattr(msg, "type", None)
    if str(role).lower() in {"user", "human"}:
        return True
    return msg.__class__.__name__ == "HumanMessage"


def _last_human_text(messages: list) -> str:
    """Text of the most recent human turn — what the supervisor routes on."""
    for m in reversed(messages or []):
        if _is_human_message(m):
            return _message_text(m)
    return _message_text(messages[-1]) if messages else ""


def _is_retrigger(text: str) -> bool:
    """True when the user asks to RE-TRIGGER the deployment workflow (an ops action),
    as opposed to re-running a promote step (handled earlier in parse_intent)."""
    low = text.lower()
    if "retrigger" in low or "re trigger" in low:
        return True
    words = set(_norm_words(text))
    mentions_deploy = bool(words & {"deployment", "deploy", "workflow"})
    asks_run = bool(words & {"rerun", "retrigger", "trigger"}) or "re run" in low
    return mentions_deploy and asks_run


# Interrogative leaders — a message that opens with one (or contains '?') reads as a
# QUESTION, so it must not take the deterministic mutate fast-path (let the LLM route
# it, which lands a pure question on a READ-ONLY specialist instead of ops).
_QUESTION_LEADERS = {
    "how", "what", "why", "when", "where", "which", "who", "whom", "whose",
    "can", "could", "would", "should", "do", "does", "did", "is", "are",
    "was", "were", "will", "explain", "tell", "describe",
}


def _is_question(text: str) -> bool:
    if "?" in text:
        return True
    words = _norm_words(text)
    return bool(words) and words[0] in _QUESTION_LEADERS
