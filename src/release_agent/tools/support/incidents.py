"""The L1 layer: each problem in the report as something to DO.

An L1 engineer does not want analytics; they want, per problem: how urgent,
is it a known issue, what to do now (wait / re-trigger / check / escalate),
and to whom it goes — with a ticket note ready to paste. The runbook and the
owners are the team's own (private config); without them each category has a
sensible built-in action. Everything here is a SUGGESTION for a person: the
portal never re-runs, restarts or changes a job.

"""
from __future__ import annotations

import json
from typing import Any

from .config import DIMENSIONS, _names
from .report import _text

ACTIONS = ("wait", "retrigger", "check", "escalate")
_FALLBACK_OWNER = "L2 support"
_PRIORITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def parse_owners(raw: str) -> dict[str, str]:
    """'role:value=Team,…,default=Team' → {'role:value': team, 'default': team}."""
    owners: dict[str, str] = {}
    for item in _names(raw):
        left, eq, team = item.partition("=")
        if eq and left.strip() and team.strip():
            owners[left.strip()] = team.strip()
    return owners


def load_runbook(path: str) -> tuple[list[dict[str, Any]], str | None]:
    """The runbook file → (entries, problem). Each entry: match (text, or a list
    of texts that must ALL appear in the error, case-insensitive), title,
    action (one of ACTIONS), steps (list), optional escalate_to, optional
    category and when ({role: value}) to narrow it. Bad entries are skipped."""
    if not path:
        return [], None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [], f"The runbook file could not be read ({type(e).__name__})."
    entries = data.get("entries") if isinstance(data, dict) else data
    out = []
    for e in entries if isinstance(entries, list) else []:
        if not isinstance(e, dict):
            continue
        match = e.get("match")
        texts = [match] if isinstance(match, str) else match if isinstance(match, list) else []
        texts = [str(t).lower() for t in texts if str(t).strip()]
        if not texts or not _text(e.get("title")):
            continue
        action = _text(e.get("action")).lower()
        out.append({"match": texts, "title": _text(e.get("title")),
                    "action": action if action in ACTIONS else "check",
                    "steps": [_text(x) for x in e.get("steps") or [] if _text(x)],
                    "escalate_to": _text(e.get("escalate_to")) or None,
                    "category": _text(e.get("category")) or None,
                    "when": {str(k): str(v) for k, v in (e.get("when") or {}).items()}})
    return out, None


def _runbook_match(err: dict[str, Any], runbook: list[dict[str, Any]]) -> dict[str, Any] | None:
    haystack = f"{err.get('signature') or ''} {err.get('sample') or ''}".lower()
    for entry in runbook:
        if entry["category"] and entry["category"] != err.get("category"):
            continue
        if any(err.get("shared", {}).get(r) != v for r, v in entry["when"].items()):
            continue
        if all(t in haystack for t in entry["match"]):
            return entry
    return None


def _owner(shared: dict[str, str], owners: dict[str, str], cause: dict[str, str] | None = None) -> str:
    """The cause's owner first (the feed of an upstream failure, the process of
    a process one), then any other shared value, then the default."""
    for role, value in (cause or {}).items():
        if f"{role}:{value}" in owners:
            return owners[f"{role}:{value}"]
    for role in DIMENSIONS:
        if role in shared and f"{role}:{shared[role]}" in owners:
            return owners[f"{role}:{shared[role]}"]
    return owners.get("default", _FALLBACK_OWNER)


def _named(shared: dict[str, str], labels: dict[str, str]) -> str:
    """"system SYS-A · process REPORT-B" — what a group of runs has in common."""
    return " · ".join(f"{labels.get(r, r)} {v}" for r, v in shared.items())


def _builtin_action(err: dict[str, Any], labels: dict[str, str], owner: str) -> tuple[str, list[str]]:
    shared = err.get("shared") or {}
    cat = err.get("category")
    def one(role: str) -> str:
        return f"{labels.get(role, role)} {shared.get(role, '')}".strip()
    if cat == "upstream":
        who = one("system") if "system" in shared else one("source")
        return "wait", [f"Check that {who} delivered its data for this date.",
                        "Do not re-run before it has landed — the same error will repeat.",
                        "Once it has landed, re-trigger the failed runs.",
                        f"Not landed by your cut-off: escalate to {owner}."]
    if cat == "process":
        n_units = (err.get("spread") or {}).get("unit")
        who = (f"{n_units} {labels.get('unit', 'unit')} values of {one('process')} fail"
               if n_units else f"Runs of {one('process')} fail")
        return "escalate", [f"{who} with the same error: likely a change or defect in that process "
                            "(or its input for them), not one unit's data.",
                            "Check what was deployed for it recently (the release history).",
                            f"Escalate to {owner} with the note; re-running will not help."]
    if cat == "unit":
        steps = [f"Only {one('unit')} fails: check that unit's input and static data."]
        if (err.get("max_streak") or 0) > 1:
            return "escalate", steps + [f"It has failed {err['max_streak']} dates in a row: "
                                        f"escalate to {owner} rather than retry."]
        return "check", steps + [f"Input looks right: re-trigger once, then escalate to {owner}."]
    if cat == "single":
        if (err.get("max_streak") or 0) > 1:
            return "escalate", [f"This run has failed {err['max_streak']} dates in a row — "
                                "a retry is unlikely to help.", f"Escalate to {owner}."]
        return "retrigger", ["One run failed: re-trigger it once.",
                             f"Fails again with the same error: escalate to {owner}."]
    return "escalate", ["No shared system, process or unit: this points at the platform "
                        "(quota, permissions, capacity).", f"Escalate to {owner}."]


def parse_critical(raw: str) -> set[tuple[str, str]]:
    """'role:value,…' → {(role, value)} — work that is always high priority
    (a regulatory report, a key entity). Unknown roles are ignored."""
    out = set()
    for item in _names(raw):
        role, sep, value = item.partition(":")
        if sep and role.strip() in DIMENSIONS and value.strip():
            out.add((role.strip(), value.strip()))
    return out


class PriorityRules:
    """How urgent an incident is. Built-in defaults; a deployment tunes them
    (SUPPORT_HIGH_COUNT, SUPPORT_MEDIUM_COUNT, SUPPORT_CRITICAL). Deliberately no
    clock: a business date's deadline was tried and dropped — the person
    working the queue knows the time; the list should not reshuffle by the hour."""

    def __init__(self, *, high_count: int = 10, medium_count: int = 3,
                 critical: set[tuple[str, str]] | None = None):
        self.high_count = max(1, int(high_count))
        self.medium_count = max(1, int(medium_count))
        self.critical = critical or set()


def _runs(n: int) -> str:
    return f"{n} run" if n == 1 else f"{n} runs"


def _critical_hits(values: dict[str, Any], rules: PriorityRules) -> list[str]:
    hits = []
    for role, value in sorted(rules.critical):
        have = values.get(role)
        if have is not None and (value in have if isinstance(have, (list, set, tuple)) else value == have):
            hits.append(f"{role} {value}")
    return hits


def _priority(kind: str, count: int, err: dict[str, Any] | None, rules: PriorityRules,
              values: dict[str, Any]) -> tuple[str, str]:
    """(priority, why) — the first rule that applies decides."""
    hits = _critical_hits(values, rules)
    streak = (err or {}).get("max_streak") or 0
    if hits:
        level, why = "high", "critical: " + ", ".join(hits)
    elif kind == "volume":
        level, why = "high", "output collapsed on a run that succeeded"
    elif count >= rules.high_count:
        level, why = "high", f"{_runs(count)} (≥ {rules.high_count})"
    elif err and err.get("category") in ("process", "spread", "upstream") and count >= rules.medium_count:
        cause = {"upstream": "one upstream system", "process": "one process",
                 "spread": "the same error everywhere (platform)"}[err["category"]]
        level, why = "high", f"{_runs(count)}, {cause}"
    elif kind in ("stuck", "missing"):
        level, why = "medium", f"{kind} work will not finish on its own"
    elif count >= rules.medium_count:
        level, why = "medium", f"{_runs(count)} (≥ {rules.medium_count})"
    elif streak > 1:
        level, why = "medium", f"failing {streak} dates in a row"
    else:
        level, why = "low", "one run, first time" if count == 1 else _runs(count)
    return level, why


def _note(inc: dict[str, Any], report: dict[str, Any], *, with_error_text: bool) -> str:
    """The ticket note: what L1 saw, what it checked, who it goes to."""
    head = f"[Support triage] {report.get('date_label', 'Business date')} {report.get('business_date')}: {inc['title']}"
    lines = [head, f"Priority: {inc['priority']} ({inc['priority_reason']}) · Action: {inc['action']}"]
    lines += [f"- {fact}" for fact in inc["facts"]]
    if with_error_text and inc.get("error_text"):
        lines.append(f"- Error: {inc['error_text']}")
    if inc.get("job_ids"):
        lines.append(f"- Jobs: {', '.join(inc['job_ids'])}")
    lines.append(f"- Known issue: {inc['runbook'] or 'no runbook match'}")
    lines.append("- Next steps: " + " ".join(inc["steps"]))
    lines.append(f"- Suggested owner: {inc['owner']}")
    return "\n".join(lines)


def incident_id(signature: str) -> str:
    """A stable name for an error incident: the same error keeps its id while
    counts and ordering change between refreshes — so "Investigate incident
    e-1a2b3c4d" still means the same thing a minute later, and feedback about
    it can be found again. A checksum, not the text: an id may travel in a URL."""
    import zlib

    return f"e-{zlib.crc32(signature.encode('utf-8')) & 0xFFFFFFFF:08x}"


def _row_values(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    vals: dict[str, set[str]] = {}
    for r in rows:
        for role, v in (r.get("dims") or {}).items():
            vals.setdefault(role, set()).add(v)
    return {k: sorted(v) for k, v in vals.items()}


def incidents(report: dict[str, Any], *, labels: dict[str, str], runbook: list[dict[str, Any]],
              owners: dict[str, str], rules: PriorityRules | None = None) -> list[dict[str, Any]]:
    """PURE. The report's problems as L1 incidents, most urgent first."""
    rules = rules or PriorityRules()
    out: list[dict[str, Any]] = []
    unit_label = labels.get("unit", "unit")
    for i, err in enumerate(report.get("errors") or []):
        owner = _owner(err.get("shared") or {}, owners, err.get("cause"))
        known = _runbook_match(err, runbook)
        if known:
            action, steps = known["action"], known["steps"] or ["Follow the runbook entry."]
            owner = known["escalate_to"] or owner
        else:
            action, steps = _builtin_action(err, labels, owner)
        shared = _named(err.get("shared") or {}, labels)
        facts = [f"{err['count']} run(s) failed" + (f", all {shared}" if shared else "") + "."]
        if err.get("spread", {}).get("unit", 0) > 1:
            facts.append(f"{err['spread']['unit']} different {unit_label} values affected.")
        if err.get("recurring"):
            facts.append(f"{err['recurring']} of them failed on earlier dates too"
                         + (f" (up to {err['max_streak']} in a row)" if err.get("max_streak") else "")
                         + (f"; last success {err['last_success']}." if err.get("last_success")
                            else "; no success in the dates read."))
        elif err.get("new") is not None:
            facts.append("New today: these succeeded on the previous date they ran.")
        cause = _named(err.get("cause") or {}, labels)
        out.append({"id": incident_id(err.get("signature") or ""), "kind": "error", "error_index": i,
                    "title": f"{err['count']} failed · {err['category']}" + (f" · {cause}" if cause else ""),
                    "category": err["category"], "count": err["count"], "facts": facts,
                    "error_text": err.get("sample") or err.get("signature"),
                    "shared": dict(err.get("shared") or {}),
                    "job_ids": err.get("job_ids") or [], "runs": err.get("runs") or [],
                    "runbook": known["title"] if known else None,
                    "action": action, "steps": steps, "owner": owner,
                    "_rank": ("error", err["count"], err, err.get("values") or err.get("shared") or {})})
    stuck = report.get("stuck") or []
    if stuck:
        no_job = sum(1 for s in stuck if not s.get("job_id"))
        owner = owners.get("default", _FALLBACK_OWNER)
        out.append({"id": "stuck", "kind": "stuck", "title": f"{len(stuck)} stuck", "category": "stuck",
                    "count": len(stuck),
                    "facts": [f"{len(stuck)} run(s) neither finished nor failed, with no update for a while.",
                              f"{no_job} of them never logged a job id." if no_job else "All of them logged a job id."],
                    "job_ids": [s["job_id"] for s in stuck if s.get("job_id")][:3],
                    # a stuck run's last row: its log lines say what it was doing when it went quiet
                    "runs": [{"run_id": s["run_id"], "at": s["at"]} for s in stuck if s.get("run_id") and s.get("at")][:3],
                    "runbook": None, "action": "check",
                    "steps": ["Open the job of each stuck run.",
                              "No job id: the job never started — re-trigger once.",
                              f"Still running far beyond normal, or fails again: escalate to {owner}."],
                    "owner": owner, "_rank": ("stuck", len(stuck), None, _row_values(stuck))})
    missing = report.get("missing") or []
    if missing:
        owner = owners.get("default", _FALLBACK_OWNER)
        out.append({"id": "missing", "kind": "missing", "title": f"{len(missing)} missing", "category": "missing",
                    "count": len(missing),
                    "facts": [f"{len(missing)} piece(s) of work ran on {report.get('previous_date')} "
                              "but have no run at all for this date."],
                    "job_ids": [], "runbook": None, "action": "check",
                    "steps": ["Check that the triggering event arrived (upstream, scheduler).",
                              "Confirm it was expected for this date (holiday, decommissioned).",
                              f"Expected and not triggered: escalate to {owner}."],
                    "owner": owner, "_rank": ("missing", len(missing), None, _row_values(missing))})
    volume = report.get("volume") or []
    if volume:
        owner = owners.get("default", _FALLBACK_OWNER)
        out.append({"id": "volume", "kind": "volume", "title": f"{len(volume)} low output", "category": "volume",
                    "count": len(volume),
                    "facts": [f"{len(volume)} run(s) succeeded with far less output than on "
                              f"{report.get('previous_date')} (or none at all)."],
                    "job_ids": [], "runbook": None, "action": "escalate",
                    "steps": ["Tell the data owner before downstream consumers use it.",
                              "Re-run once the source data is complete.", f"Escalate to {owner}."],
                    "owner": owner, "_rank": ("volume", len(volume), None, _row_values(volume))})
    for inc in out:
        kind, count, err, values = inc.pop("_rank")
        inc["priority"], inc["priority_reason"] = _priority(kind, count, err, rules, values)
    out.sort(key=lambda x: (_PRIORITY_ORDER[x["priority"]], -x["count"]))
    for inc in out:
        inc["note"] = _note(inc, report, with_error_text=True)
        inc["note_without_error"] = _note(inc, report, with_error_text=False)
    return out
