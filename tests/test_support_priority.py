"""Priority as configuration (tools/support/priority.py): the built-in rules keep
the old policy, configured rules decide first-match, and a rule set with any
problem is refused whole — named, never half-applied."""
from __future__ import annotations

import json

import pytest

from release_agent.tools.support import incidents as s_incidents
from release_agent.tools.support import priority as P


def facts(**over):
    return {"kind": "error", "category": "single", "count": 1, "streak": 0, "recurring": False,
            "critical": [], "values": {}, "runbook": None, **over}


# ----- the built-in rules are the old policy ------------------------------------------

@pytest.mark.parametrize("given, expect", [
    (facts(critical=["unit ACCT-26"]), ("high", "critical: unit ACCT-26")),
    (facts(kind="volume", category="volume", count=2), ("high", "output collapsed on a run that succeeded")),
    (facts(category="upstream", count=24), ("high", "24 runs (≥ 10)")),
    (facts(category="process", count=4), ("high", "4 runs, one process")),
    (facts(category="spread", count=3), ("high", "3 runs, the same error everywhere (platform)")),
    (facts(category="unit", count=4), ("medium", "4 runs (≥ 3)")),
    (facts(kind="stuck", category="stuck"), ("medium", "stuck work will not finish on its own")),
    (facts(kind="missing", category="missing", count=5), ("medium", "missing work will not finish on its own")),
    (facts(count=1, streak=3), ("medium", "failing 3 dates in a row")),
    (facts(count=1), ("low", "one run, first time")),
    (facts(category="unit", count=2), ("low", "2 runs")),
])
def test_the_built_in_rules_keep_the_old_policy(given, expect):
    assert P.decide(P.default_rules(), given) == expect


def test_the_built_in_counts_follow_the_two_settings():
    rules, problems = P.parse_rules("", high_count=20, medium_count=5)
    assert problems == []
    assert P.decide(rules, facts(category="upstream", count=12)) == ("high", "12 runs, one upstream system")
    assert P.decide(rules, facts(category="unit", count=12)) == ("medium", "12 runs (≥ 5)")
    assert P.decide(rules, facts(category="unit", count=25)) == ("high", "25 runs (≥ 20)")


# ----- configured rules ---------------------------------------------------------------

TEAM = {"rules": [
    {"level": "high", "when": {"values": {"process": ["REG-REPORT"]}}, "reason": "regulatory report"},
    {"level": "low", "when": {"runbook": "Cloud quota exceeded"}, "reason": "known transient: {runbook}"},
    {"level": "high", "when": {"min_runs": 5, "recurring": True}, "reason": "{runs}, failing {streak} dates"},
    {"level": "medium", "when": {"kind": ["stuck", "missing"]}, "reason": "{kind}"},
    {"level": "low", "reason": "{runs} · {category}"},
]}


def test_configured_rules_decide_first_match_wins():
    rules, problems = P.parse_rules(json.dumps(TEAM))
    assert problems == [] and len(rules) == 5
    assert P.decide(rules, facts(values={"process": ["REG-REPORT"]}, runbook="Cloud quota exceeded")) == \
        ("high", "regulatory report")
    assert P.decide(rules, facts(count=30, runbook="cloud quota exceeded")) == \
        ("low", "known transient: cloud quota exceeded")
    assert P.decide(rules, facts(count=6, recurring=True, streak=4)) == ("high", "6 runs, failing 4 dates")
    assert P.decide(rules, facts(count=6, recurring=False, category="process")) == ("low", "6 runs · process")
    assert P.decide(rules, facts(kind="stuck", category="stuck")) == ("medium", "stuck")


def test_a_bare_list_is_accepted_and_runbook_true_false_means_any_or_none():
    rules, problems = P.parse_rules(json.dumps([
        {"level": "medium", "when": {"runbook": False}, "reason": "unknown error — nobody has seen it"},
        {"level": "low", "reason": "known: {runbook}"}]))
    assert problems == []
    assert P.decide(rules, facts())[0] == "medium"
    assert P.decide(rules, facts(runbook="Upstream file not delivered")) == ("low", "known: Upstream file not delivered")


def test_no_rule_matching_is_low_and_says_so():
    rules, _ = P.parse_rules(json.dumps([{"level": "high", "when": {"min_runs": 100}, "reason": "huge"}]))
    assert P.decide(rules, facts(count=3)) == ("low", "no priority rule matched")


# ----- a rule set with a problem is refused whole -------------------------------------

@pytest.mark.parametrize("bad, says", [
    ("{not json", "not valid JSON"),
    ("[]", "non-empty list"),
    (json.dumps({"rules": "high"}), "non-empty list"),
    (json.dumps([{"level": "urgent", "reason": "x"}]), "level must be one of"),
    (json.dumps([{"level": "high"}]), "reason must be a text"),
    (json.dumps([{"level": "high", "reason": "x", "colour": "red"}]), "unknown key 'colour'"),
    (json.dumps([{"level": "high", "when": {"minruns": 3}, "reason": "x"}]), "unknown condition 'minruns'"),
    (json.dumps([{"level": "high", "when": {"min_runs": "3"}, "reason": "x"}]), "min_runs must be a whole number"),
    (json.dumps([{"level": "high", "when": {"min_runs": True}, "reason": "x"}]), "min_runs must be a whole number"),
    (json.dumps([{"level": "high", "when": {"critical": "yes"}, "reason": "x"}]), "critical must be true or false"),
    (json.dumps([{"level": "high", "when": {"kind": "failed"}, "reason": "x"}]), "kind must be one of"),
    (json.dumps([{"level": "high", "when": {"values": {"colour": "red"}}, "reason": "x"}]), "values names 'colour'"),
    (json.dumps([{"level": "high", "when": {"values": {"unit": []}}, "reason": "x"}]), "values.unit must be"),
    (json.dumps([{"level": "high", "reason": "{runz} failed"}]), "reason uses {runz}"),
    (json.dumps([{"level": "high", "reason": "{runs.__class__}"}]), "reason uses {runs.__class__}"),
    (json.dumps([{"level": "high", "reason": "{runs failed"}]), "unmatched"),
])
def test_a_rule_set_with_a_problem_is_named_and_the_built_in_rules_apply(bad, says):
    rules, problems = P.parse_rules(bad)
    assert rules == P.default_rules()
    assert any(says in p for p in problems), problems


def test_every_problem_is_listed_with_its_rule_number():
    _, problems = P.parse_rules(json.dumps([{"level": "low", "reason": "ok"},
                                            {"level": "nope", "reason": "x", "when": {"bad": 1}}]))
    assert problems == ["priority rule 2: level must be one of high, medium, low",
                        "priority rule 2: unknown condition 'bad' (use kind, category, values, runbook, "
                        "min_runs, max_runs, min_streak, recurring, critical)"]


# ----- through the incidents ----------------------------------------------------------

def test_incidents_use_the_configured_rules_and_the_runbook_title():
    report = {"business_date": "2026-10-03", "errors": [
        {"signature": "quota exceeded for #", "sample": "Quota exceeded for CPUS", "count": 30, "category": "spread",
         "shared": {}, "spread": {}, "values": {"process": ["REPORT-A"]}, "job_ids": [], "recurring": 0}]}
    runbook = [{"match": ["quota exceeded"], "title": "Cloud quota exceeded", "action": "retrigger",
                "steps": ["Re-trigger once."], "escalate_to": None, "category": None, "when": {}}]
    rules = s_incidents.PriorityRules(rules=P.parse_rules(json.dumps(TEAM))[0])
    (inc,) = s_incidents.incidents(report, labels={}, runbook=runbook, owners={}, rules=rules)
    assert (inc["priority"], inc["priority_reason"]) == ("low", "known transient: Cloud quota exceeded")
    assert "Priority: low (known transient: Cloud quota exceeded)" in inc["note"]
