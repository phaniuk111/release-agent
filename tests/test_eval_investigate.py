"""The investigate eval (scripts/eval_investigate.py): the scorer on hand-written
good and bad answers, the cases file, and the runner with a fake answer_fn. No
model and no network — the live run is the script's job."""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("eval_investigate", ROOT / "scripts" / "eval_investigate.py")
ev = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ev)

CASES = {c["name"]: c for c in ev.load_cases()}


def case(name):
    return CASES[name]


GOOD = {
    "upstream file not delivered":
        "### What I checked\n- sys-a-fetcher: 4 restarts, upstream returned 404\n\n"
        "The FEED-A file for 2026-10-02 has not been delivered: the fetcher logged upstream 404s and restarted "
        "four times, and the job found no files matched. Confidence: high.\n\n"
        "### What L1 should do\nDo now: wait for FEED-A to land. Do not re-run before it has.",
    "release changed the schema":
        "### What I checked\n- release log: report-b-transform 1.4.1 to 1.4.2\n\n"
        "The release of report-b-transform 1.4.2 on 30 September changed a column type, so STRING values no "
        "longer cast to NUMERIC; the failure began two days after the deploy. Confidence: high.\n\n"
        "Do now: escalate to the Reporting platform team. Re-running will not help.",
    "permission revoked":
        "A permission was removed: an IAM policy change at 02:09 UTC on the landing bucket came five minutes "
        "before the first denied storage.objects.get. Confidence: high.\n\n"
        "L1 should escalate to Platform operations to restore the role.",
    "out of memory on one unit":
        "BOOK-17 ran out of memory: its input doubled to 41.2M rows and the 8 GB workers were killed. "
        "Confidence: medium.\n\nL1 should escalate so the memory can be raised. A retry would fail the same way.",
    "quota exceeded, transient":
        "The two jobs hit the shared CPUS quota at start; the other 12 runs fit. Likely temporary. "
        "Confidence: medium.\n\nDo now: re-trigger the two runs once.",
    "nothing conclusive":
        "I could not determine the cause: the source is healthy, nothing was denied, nothing was released and "
        "the job reported no step error. Confidence: low.\n\nDo now: check BOOK-23's input and static data.",
    "run's own lines: input missing, source denied elsewhere":
        "Run rc-20261002-0007 gave up because its input file landing/sys_c_20261002.csv was not found after "
        "three retries. The source's 403s were on an archive bucket, hours later, by another job. "
        "Confidence: high.\n\nDo now: wait for the file to land, then re-trigger.",
}


@pytest.mark.parametrize("name", sorted(GOOD))
def test_a_good_answer_passes_its_case(name):
    res = ev.score(case(name), GOOD[name], {"model_calls": 1})
    assert res["passed"], {k: v for k, v in res["checks"].items() if not v["passed"]}


# --- each check has a bad answer that fails it ------------------------------------

def failed(res):
    return sorted(k for k, v in res["checks"].items() if not v["passed"])


def test_an_answer_that_misses_the_cause_fails_mentions():
    bad = "Something went wrong with the run.\n\nDo now: wait."
    assert "mentions" in failed(ev.score(case("upstream file not delivered"), bad))


def test_the_evidence_echo_is_not_a_finding():
    only_echo = ("### What I checked\n- the feed file was not delivered, upstream returned 404\n\n"
                 "### Timeline\n- 01:05 fetch failed: 404\n\nDo now: wait.")
    res = ev.score(case("upstream file not delivered"), only_echo)
    assert "mentions" in failed(res)
    assert "feed" not in ev.finding_text(only_echo).lower()


def test_a_bold_title_ends_an_echo_section_and_a_bare_one_starts_it():
    text = "What I checked:\n- 404 everywhere\n**Finding**\nThe feed is late.\nTimeline\n- 01:05\n"
    kept = ev.finding_text(text)
    assert "The feed is late." in kept and "404" not in kept and "01:05" not in kept


def test_inventing_a_cause_fails_not_invented():
    bad = "I could not determine the cause, but it is a permission problem. Confidence: low. Do now: check."
    res = ev.score(case("nothing conclusive"), bad)
    assert failed(res) == ["not_invented"]
    assert "permission" in res["checks"]["not_invented"]["detail"]


def test_advice_against_a_step_is_not_advice_for_it():
    said_no = ("The 1.4.2 release changed a column type (schema). Do not re-run it, and re-trying will not help. "
               "Do now: escalate.")
    assert ev.score(case("release changed the schema"), said_no)["passed"]
    said_yes = "The 1.4.2 release changed a column type (schema). Re-run it once. Do now: escalate."
    assert "not_invented" in failed(ev.score(case("release changed the schema"), said_yes))


def test_recommending_a_rerun_for_a_release_bug_fails():
    bad = GOOD["release changed the schema"].replace("Re-running will not help.", "Re-trigger the runs once.")
    assert "not_invented" in failed(ev.score(case("release changed the schema"), bad))


@pytest.mark.parametrize("answer,expected", [
    ("Do now: wait for the file.", "wait"),
    ("Do now: re-trigger the run once, then escalate.", "retrigger"),
    ("L1 should check the input.\nThen escalate if it persists.", "check"),
    ("L1 should escalate to the platform team.", "escalate"),
    ("Do now: do not re-run; escalate.", "escalate"),            # a negated action is not the action
    ("### What L1 should do\n\nAction: wait · Owner: Data intake\n", "wait"),
    ("The feed is late. Re-run later.", None),                    # no marker, no action
    ("Do now: have a coffee.", None),
])
def test_the_stated_action(answer, expected):
    assert ev.stated_action(answer) == expected


def test_a_wrong_or_missing_action_fails():
    c = case("upstream file not delivered")
    wrong = GOOD["upstream file not delivered"].replace("Do now: wait for FEED-A to land.", "Do now: escalate.")
    assert "action" in failed(ev.score(c, wrong))
    none = GOOD["upstream file not delivered"].replace("Do now: wait for FEED-A to land.", "")
    assert "action" in failed(ev.score(c, none))


@pytest.mark.parametrize("answer,expected", [
    ("Confidence: low.", "low"), ("I have high confidence in this.", "high"),
    ("Confidence is medium because the logs agree.", "medium"), ("No idea.", None),
])
def test_the_stated_confidence(answer, expected):
    assert ev.stated_confidence(answer) == expected


def test_confidence_above_the_cap_or_missing_fails():
    c = case("nothing conclusive")
    assert ev.score(c, GOOD["nothing conclusive"])["passed"]
    high = GOOD["nothing conclusive"].replace("Confidence: low", "Confidence: high")
    assert failed(ev.score(c, high)) == ["confidence"]
    silent = GOOD["nothing conclusive"].replace(" Confidence: low.", "")
    assert failed(ev.score(c, silent)) == ["confidence"]


def test_too_many_model_calls_fails():
    c = case("quota exceeded, transient")
    assert ev.score(c, GOOD["quota exceeded, transient"], {"model_calls": 4})["passed"]
    assert failed(ev.score(c, GOOD["quota exceeded, transient"], {"model_calls": 9})) == ["model_calls"]
    assert "model_calls" not in ev.score(c, GOOD["quota exceeded, transient"], {})["checks"]   # unreported


def test_a_fallback_summary_is_never_the_models_pass():
    res = ev.score(case("quota exceeded, transient"), GOOD["quota exceeded, transient"],
                   {"model_calls": 1, "fallback": True})
    assert failed(res) == ["model_answered"] and not res["passed"]


def test_an_empty_answer_fails():
    res = ev.score(case("nothing conclusive"), "")
    assert not res["passed"] and "answered" in failed(res)


def test_matching_is_by_word_start_and_case_insensitive():
    toks = ev.words("Williams CLAIMED the IAM policy; re-trigger it")
    assert ev.find(toks, "iam") == [toks.index("iam")]      # not inside "Williams"
    assert ev.find(toks, "claim") == [toks.index("claimed")]  # a word may continue past the term
    assert ev.find(toks, "re trigger") == [toks.index("re")]  # a hyphen is a word break
    assert ev.find(toks, "policy re") == []                   # a term never spans a clause
    assert ev.find(toks, "") == []


# --- the cases file -----------------------------------------------------------------

def test_the_cases_file_is_valid():
    cases = ev.load_cases()
    assert len(cases) == 7
    assert len({c["name"] for c in cases}) == 7
    for c in cases:
        assert ev.case_problems(c) == [], c["name"]
        assert c["expect"]["max_model_calls"] == 4


def test_every_bundle_has_the_contract_shape():
    for c in ev.load_cases():
        b = c["bundle"]
        assert b["ok"] is True and b["business_date"] and b["window"]
        assert set(b["incident"]) >= {"id", "title", "category", "facts", "action", "steps", "owner", "runbook",
                                      "note", "shared", "job_ids"}
        # `runs` (a failed run's own lines) arrived later: older cases have none
        assert set(b["evidence"]) - {"runs"} == {"metrics", "logs", "audit", "dataflow", "changes"}
        assert isinstance(b["unavailable"], list) and isinstance(b["checked"], list)
        assert all(set(t) == {"time", "kind", "source", "text"} for t in b["timeline"])
        assert set(b["signals"]) >= {"source_healthy", "security_in_logs", "denials", "iam_changes", "job_kinds",
                                     "recent_change", "streak", "category", "leads"}
        assert isinstance(b["elapsed_ms"], int)


def test_the_cases_cover_the_seven_situations():
    kinds = {c["name"]: [j["kind"] for j in c["bundle"]["evidence"]["dataflow"]] for c in ev.load_cases()}
    assert kinds["upstream file not delivered"] == ["not_found"]
    assert kinds["out of memory on one unit"] == ["out_of_memory"]
    assert kinds["quota exceeded, transient"] == ["quota", "quota"]
    assert CASES["release changed the schema"]["bundle"]["signals"]["recent_change"]["days_before"] == 2
    perm = CASES["permission revoked"]["bundle"]
    assert perm["signals"]["security_in_logs"] > 0 and perm["signals"]["iam_changes"] == 1
    quiet = CASES["nothing conclusive"]["expect"]
    assert quiet["confidence_at_most"] == "low" and CASES["nothing conclusive"]["bundle"]["signals"]["source_healthy"]
    own = CASES["run's own lines: input missing, source denied elsewhere"]["bundle"]["signals"]
    assert own["run_kind"] == "not_found" and own["security_in_logs"] > 0 and own["action_hint"]["action"] == "wait"
    assert {c["expect"]["action"] for c in ev.load_cases()} == {"wait", "retrigger", "escalate", "check"}


def test_case_problems_names_what_is_wrong():
    assert "missing bundle" in ev.case_problems({"name": "x", "expect": {}})
    bad = {"name": "x", "bundle": {}, "expect": {"must_mention_any": [], "must_not_mention": [],
                                                  "action": "reboot", "max_model_calls": 4}}
    problems = ev.case_problems(bad)
    assert any("action" in p for p in problems) and any("must_mention_any" in p for p in problems)


def test_the_cases_use_no_real_names():
    text = (ROOT / "tests" / "eval" / "investigate_cases.json").read_text()
    # Real names are kept out by the private denylist (tests/test_private_names.py);
    # here only that the cases use the neutral stand-ins.
    assert "gserviceaccount.com" in text and "example-project" in text


# --- the runner -----------------------------------------------------------------------

def good_answers(bundle):
    name = next(n for n, c in CASES.items() if c["bundle"] is bundle or c["bundle"] == bundle)
    return {"text": GOOD[name], "model_calls": 2, "seconds": 3.5, "model": "m-1"}


def test_the_runner_scores_every_case_and_prints_a_table():
    lines: list[str] = []
    summary = ev.run(ev.load_cases(), good_answers, out=lines.append)
    assert summary["passed"] == 7 and summary["total"] == 7
    table = "\n".join(lines)
    assert "case" in lines[0] and "failed checks" in lines[0]
    assert table.count("pass") >= 7 and "FAIL" not in table
    assert "passed 7 of 7" in table and "14 model calls" in table and "24.5s" in table


def test_the_runner_reports_failures_and_survives_a_crashing_answer_fn():
    def flaky(bundle):
        if bundle["incident"]["id"] == "e-1a2b3c4d":
            raise RuntimeError("model unavailable")
        if bundle["incident"]["id"] == "e-2b3c4d5e":
            return {"text": "Everything is fine.", "model_calls": 9, "seconds": 1}
        return good_answers(bundle)

    lines: list[str] = []
    summary = ev.run(ev.load_cases(), flaky, out=lines.append)
    by_name = {r["name"]: r for r in summary["results"]}
    assert summary["passed"] == 5
    assert "model unavailable" in by_name["upstream file not delivered"]["checks"]["answer_fn"]["detail"]
    assert {"mentions", "model_calls"} <= {k for k, v in by_name["release changed the schema"]["checks"].items()
                                           if not v["passed"]}
    assert any("FAIL" in line and "model unavailable" in line for line in lines)


def test_a_plain_string_answer_is_accepted():
    summary = ev.run([CASES["nothing conclusive"]], lambda b: GOOD["nothing conclusive"], out=lambda s: None)
    assert summary["passed"] == 1 and summary["results"][0]["model_calls"] is None


def test_the_model_free_baseline_does_not_invent_but_does_not_find_the_cause(capsys):
    fn = ev.model_free_answer_fn()
    summary = ev.run(ev.load_cases(), fn, out=lambda s: None)
    # What code decides by itself, the model-free summary gets right: the action
    # a failure kind implies (wait for an unhealthy source). It states leads, not
    # causes — so the cases that need a cause named still fail.
    # A run that logged "input file not found" itself is one code reads alone.
    passed = {r["name"] for r in summary["results"] if r["passed"]}
    assert passed <= {"upstream file not delivered", "out of memory on one unit",
                      "run's own lines: input missing, source denied elsewhere"}
    missed = [r["name"] for r in summary["results"] if not r["checks"]["mentions"]["passed"]]
    assert len(missed) >= 3
    # and whatever it says, it follows the evidence's action, never the generic one
    oom = next(r for r in summary["results"] if r["name"] == "out of memory on one unit")
    assert oom["checks"]["action"]["passed"] and oom["checks"]["not_invented"]["passed"]
    quiet = next(r for r in summary["results"] if r["name"] == "nothing conclusive")
    assert quiet["checks"]["not_invented"]["passed"]


def test_main_runs_the_baseline_and_exits_nonzero_when_cases_fail(capsys):
    assert ev.main(["--model-free", "--case", "quota exceeded, transient"]) == 1
    assert "quota exceeded, transient" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        ev.main(["--model-free", "--case", "no such case"])


def test_main_json_output_is_parseable(capsys):
    ev.main(["--model-free", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["total"] == 7 and len(data["results"]) == 7


def test_a_retry_that_fails_is_a_denied_retry_and_a_ruled_out_cause_is_not_an_invention():
    case = {"name": "x", "bundle": {}, "expect": {"must_mention_any": [["memory"]],
            "must_not_mention": ["retry", "permission"], "action": "escalate"}}
    good = ("**Finding**\nOut of memory. There were no recent code changes or permission issues, and the "
            "evidence rules out a permission problem.\n\n**What L1 should do**\nDo now: escalate — a retry fails "
            "the same way until the job gets more memory.")
    bad = "**Finding**\nOut of memory.\n\n**What L1 should do**\nDo now: escalate, then retry it."
    assert ev.score(case, good, {"model_calls": 1})["checks"]["not_invented"]["passed"]
    assert not ev.score(case, bad, {"model_calls": 1})["checks"]["not_invented"]["passed"]


def test_a_denial_runs_across_a_list_but_not_across_a_plain_comma_or_a_contrast():
    case = {"name": "x", "bundle": {}, "expect": {"must_mention_any": [["memory"]],
            "must_not_mention": ["permission", "re-run"], "action": "escalate"}}

    def invented(finding, action="Do now: escalate."):
        text = f"**Finding**\nOut of memory. {finding}\n\n**What L1 should do**\n{action}"
        return not ev.score(case, text, {"model_calls": 1})["checks"]["not_invented"]["passed"]

    assert not invented("The evidence rules out recent code changes, security issues, or permission problems.")
    assert not invented("There were no releases, no IAM changes and no permission denials.")
    assert invented("It is not a memory problem alone, but a permission problem as well.")
    assert invented("Not a code change, a permission problem.")           # no list word: the comma ends the denial
    assert invented("Fine.", action="Do now: escalate, then re-run it.")
    assert not invented("Fine.", action="Do not re-run, escalate.")       # …and the escalation is still the action
    assert ev.score(case, "**Finding**\nOut of memory.\n\n**What L1 should do**\nDo not re-run, escalate.",
                    {"model_calls": 1})["checks"]["action"]["passed"]


def test_an_invention_is_judged_where_a_cause_is_asserted():
    case = {"name": "x", "bundle": {}, "expect": {"must_mention_any": [["memory"]],
            "must_not_mention": ["permission", "retry"], "action": "escalate"}}

    def not_invented(cause_line, do_now="Do now: escalate — it needs more memory."):
        text = ("**Finding**\nOut of memory. Absent any sign of permission trouble in the audit trail, the "
                f"source is fine.\n\n**What L1 should do**\n{do_now}\n\n**Ticket note**\n```\n- facts\n{cause_line}\n```")
        return ev.score(case, text, {"model_calls": 1})["checks"]["not_invented"]["passed"]

    assert not_invented("Cause: the job ran out of memory on doubled input")     # odd rule-out phrasing above is fine
    assert not not_invented("Cause: a permission was revoked")                   # asserted → an invention
    assert not not_invented("Cause: out of memory", do_now="Do now: escalate, then retry once.")
    assert ev.asserted_text("no cause line here\nDo now: wait") == ""           # no Cause line → whole finding is judged
