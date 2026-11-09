"""The previous release tag comes from the build: each chart's build run logs
the tag it started from ("Using previous signed tag (N-1): 5.0.989"). The queue
gate reads it from the run it just verified and records it on the queued
entry; a CARE release writes it into CARE_PREVIOUS_TAGS_FILE for that chart.
"""
import json
from types import SimpleNamespace

import pytest

from adk_release_agent import tools as T
from release_agent.config import settings
from release_agent.tools import care_release as CR
from release_agent.tools import controls as C
from release_agent.tools import queue_gate as QG
from release_agent.tools import release_queue as RQ
from release_agent.tools import release_fileset as RF
from tests import test_care_release as TCR

STEP = "Determine base tag"
MARKER = "Using previous signed tag (N-1):"
RUN = "https://github.com/example-org/build/actions/runs/41"
LOG = ("2026-11-03T10:00:00Z \x1b[36;1mcore.info(`Using previous signed tag (N-1): ${base}`)\x1b[0m\n"
       "2026-11-03T10:00:01Z Resolving base tag from signed tags...\n"
       "2026-11-03T10:00:01Z Found 3 signed tag(s): 5.0.990, 5.0.989, 5.0.988\n"
       "2026-11-03T10:00:01Z Using previous signed tag (N-1): 5.0.989\n")


@pytest.fixture
def base_tag_on(monkeypatch):
    monkeypatch.setattr(settings, "build_base_tag_step", STEP)
    monkeypatch.setattr(settings, "build_base_tag_marker", MARKER)


def _job(name, steps, conclusion="success", job_id=7):
    return SimpleNamespace(id=job_id, name=name, conclusion=conclusion,
                           steps=[SimpleNamespace(name=n, conclusion=c) for n, c in steps])


@pytest.fixture
def run_with(monkeypatch):
    """A build run with the given jobs and one log per job id."""
    def _set(jobs, logs):
        run = SimpleNamespace(jobs=lambda: list(jobs))
        repo = SimpleNamespace(get_workflow_run=lambda run_id: run)
        monkeypatch.setattr(C, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
        monkeypatch.setattr(C, "_fetch_job_log", lambda repo_full, job_id: logs.get(job_id, ""))
    return _set


# --- reading it from a run ---------------------------------------------------------

def test_the_tag_the_build_started_from_is_read_from_its_step(base_tag_on, run_with):
    """The reusable workflow's job is "Get Base Tag / get-base-tag"; the line is
    in its "Determine base tag" step. The echoed script line is not a tag."""
    run_with([_job("Get Base Tag / get-base-tag", [("Checkout repository", "success"), (STEP, "success")])],
             {7: LOG})
    assert C.run_base_tag(RUN) == {"ok": True, "tag": "5.0.989"}


def test_the_job_name_answers_too(base_tag_on, run_with, monkeypatch):
    monkeypatch.setattr(settings, "build_base_tag_step", "get-base-tag")
    run_with([_job("Get Base Tag / get-base-tag", [("Run actions/github-script", "success")])], {7: LOG})
    assert C.run_base_tag(RUN)["tag"] == "5.0.989"


def test_two_different_base_tags_say_nothing_about_which_is_this_charts(base_tag_on, run_with):
    run_with([_job("a", [(STEP, "success")], job_id=1), _job("b", [(STEP, "success")], job_id=2)],
             {1: LOG, 2: LOG.replace("5.0.989", "5.0.700")})
    out = C.run_base_tag(RUN)
    assert out["ok"] is False and "more than one" in out["reason"] and "5.0.700" in out["reason"]


@pytest.mark.parametrize("jobs,logs,says", [
    ([_job("build", [("Build", "success")])], {7: LOG}, "no step or job named"),
    ([_job("get-base-tag", [(STEP, "failure")], conclusion="failure")], {7: LOG}, "did not succeed"),
    ([_job("get-base-tag", [(STEP, "success")])], {}, "could not be read"),
    ([_job("get-base-tag", [(STEP, "success")])], {7: "no marker here\n"}, "has no"),
])
def test_why_no_tag_was_found_is_said(base_tag_on, run_with, jobs, logs, says):
    run_with(jobs, logs)
    out = C.run_base_tag(RUN)
    assert out["ok"] is False and says in out["reason"]


def test_off_and_no_run_never_reach_github(monkeypatch):
    monkeypatch.setattr(C, "_get_github_client", lambda: pytest.fail("no GitHub call"))
    assert "off" in C.run_base_tag(RUN)["reason"]                 # conftest: the step is empty
    monkeypatch.setattr(settings, "build_base_tag_step", STEP)
    assert C.run_base_tag("")["reason"] == "no build run on record"


# --- the queue gate records it --------------------------------------------------------

@pytest.fixture
def queue(monkeypatch):
    writes = []

    def _run(base):
        report = {"found": True, "tag": "svc-a-5.0.990", "run_succeeded": True, "gate": "PASS",
                  "run": {"url": RUN, "conclusion": "success"}, "controls": [], "failed_controls": [],
                  "open_controls": [], "failed_steps": []}
        monkeypatch.setattr(QG, "_invoke_tool", lambda name, args=None: report)
        monkeypatch.setattr(RQ, "add_intent", lambda **kw: writes.append(kw) or {"ok": True, "intent": kw})
        monkeypatch.setattr(RQ, "_fetch_events", lambda: [])
        monkeypatch.setattr(C, "run_base_tag", lambda url: base(url))
        return T.queue_release_intent(artifact="svc-a:5.0.990", requested_by="dev@example.com",
                                      build_run_url=RUN, jira_ticket="ABC-1", note="n", change_details="d")
    _run.writes = writes
    return _run


def test_queueing_records_the_tag_the_build_started_from(base_tag_on, queue):
    seen = []
    out = queue(lambda url: seen.append(url) or {"ok": True, "tag": "5.0.989"})
    assert out["ok"] and out["previous_tag"] == "5.0.989" and not out.get("warnings")
    assert seen == [RUN], "read from the run the gate just verified"
    assert queue.writes[0]["previous_tag"] == "5.0.989"


def test_a_run_that_logs_no_base_tag_still_queues_and_says_what_the_release_will_do(base_tag_on, queue):
    out = queue(lambda url: {"ok": False, "reason": "its build has no step or job named 'x'"})
    assert out["ok"] and queue.writes[0]["previous_tag"] == ""
    assert any("last release's version" in w for w in out["warnings"])


def test_with_the_step_off_the_gate_reads_nothing(queue):
    out = queue(lambda url: pytest.fail("not read when off"))
    assert out["ok"] and queue.writes[0]["previous_tag"] == "" and not out.get("warnings")


# --- the event log keeps it -----------------------------------------------------------

def test_the_queued_event_carries_it_only_when_there_is_one(monkeypatch):
    rows = []
    monkeypatch.setattr(RQ, "_insert", lambda r: rows.extend(r) or {"ok": True})
    RQ.add_intent("svc-a:5.0.990", "dev@example.com", build_run_url=RUN, previous_tag="5.0.989")
    RQ.add_intent("svc-b:1.0.0", "dev@example.com", build_run_url=RUN)
    assert rows[0]["previous_tag"] == "5.0.989" and "previous_tag" not in rows[1]


def test_a_table_without_the_column_still_queues(monkeypatch):
    monkeypatch.setattr(RQ, "queue_enabled", lambda: True)
    sent = []

    class Client:
        def insert_rows_json(self, table, rows, row_ids=None):
            sent.append(rows)
            return [{"errors": [{"message": "no such field: previous_tag"}]}] if "previous_tag" in rows[0] else []

    monkeypatch.setattr(RQ, "_get_client", lambda: Client())
    out = RQ._insert([{"event_id": "e1", "artifact_name": "svc-a", "previous_tag": "5.0.989"}])
    assert out["ok"] and len(sent) == 2 and "previous_tag" not in sent[1][0]


def _queued(name, version, **over):
    return {"event_type": "queued", "artifact_name": name, "artifact_version": version,
            "event_ts": "2026-11-03T10:00:00Z", "requested_by": "dev@example.com",
            "build_run_url": RUN, **over}


def test_the_queue_and_the_history_show_it_and_a_requeue_keeps_it(monkeypatch):
    events = [_queued("svc-a", "5.0.990", previous_tag="5.0.989")]
    assert RQ.reduce_queue(events)[0]["previous_tag"] == "5.0.989"
    events.append({"event_type": "released", "artifact_name": "svc-a", "artifact_version": "5.0.990",
                   "event_ts": "2026-11-03T11:00:00Z", "release_name": "R1"})
    [item] = RQ.release_history(events)[0]["items"]
    monkeypatch.setattr(RQ, "queue_enabled", lambda: True)
    monkeypatch.setattr(RQ, "_fetch_events", lambda *a, **k: list(events))
    assert item["previous_tag"] == "5.0.989"
    added = []
    monkeypatch.setattr(RQ, "add_intent", lambda *a, **kw: added.append(kw) or {"ok": True})
    monkeypatch.setattr(RQ, "_notify_requeued", lambda *a: None)
    assert RQ.requeue_from_history("svc-a", "5.0.990", "dev@example.com")["ok"]
    assert added[0]["previous_tag"] == "5.0.989"


# --- the CARE release writes it ----------------------------------------------------------

@pytest.fixture
def released(TCR_prev, monkeypatch):
    """The release's charts as the queue holds them (svc-a 5.0.470, svc-c 1.2.3)."""
    def _set(items):
        monkeypatch.setattr(RQ, "current_queue", lambda use_cache=True: {"ok": True, "queue": items})
    return _set


@pytest.fixture
def TCR_prev(monkeypatch):
    for k, v in {"care_release_mode": "mono", "care_release_repo": TCR.MONO,
                 "care_release_base_branch": "main", "care_release_file": TCR.FILE,
                 "care_release_branch_prefix": "release/", "artifactory_base_url": "",
                 "care_previous_tags_file": TCR.PREV}.items():
        monkeypatch.setattr(CR.settings, k, v, raising=False)
    from tests.fakes import FakeRepo
    repo = FakeRepo({"main": {TCR.FILE: TCR.COMMITTED, TCR.PREV: TCR.PREV_COMMITTED}})
    repo.strict_sha = True
    monkeypatch.setattr(CR, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    monkeypatch.setattr(RQ, "mark_released", lambda *a, **k: {"ok": True})
    return repo


def test_each_chart_takes_the_tag_recorded_when_it_was_queued(base_tag_on, released, TCR_prev, monkeypatch):
    released([_queued("svc-a", "5.0.470", previous_tag="5.0.469"),
              _queued("svc-c", "1.2.3", previous_tag="1.2.2")])
    monkeypatch.setattr(C, "run_base_tag", lambda url: pytest.fail("recorded already — no log read"))
    prep = RF.prepare_release_fileset(TCR._payload())
    assert prep["ok"], prep
    assert prep["previous_from_builds"] == {"svc-a": "5.0.469", "svc-c": "1.2.2"}
    by_chart = prep["preview"]["previous_tags_by_chart"]
    assert by_chart[0] == "svc-a: 5.0.469, the tag its build started from — the last release had 5.0.463"
    assert by_chart[1] == "svc-c: 1.2.2, the tag its build started from"
    assert "each chart's previous version" in prep["preview"]["previous_tags"]
    out = RF.apply_release_fileset(prep)
    assert out["ok"], out
    written = json.loads(TCR_prev.files[TCR.BRANCH][TCR.PREV])
    assert written == {"svc-a": "5.0.469", "svc-b": "5.0.458", "svc-x": "1.0.0", "svc-c": "1.2.2"}, \
        "the release's charts from their builds; svc-b still carried from the last release"
    [pr] = TCR_prev.prs
    assert "each chart's previous version" in pr.body and "svc-c: 1.2.2" in pr.body


def test_an_entry_queued_before_the_tag_was_recorded_has_its_run_read(base_tag_on, released, TCR_prev,
                                                                       monkeypatch):
    released([_queued("svc-a", "5.0.470")])
    asked = []
    monkeypatch.setattr(C, "run_base_tag", lambda url: asked.append(url) or {"ok": True, "tag": "5.0.468"})
    prep = RF.prepare_release_fileset(TCR._payload())
    assert asked == [RUN] and prep["previous_from_builds"] == {"svc-a": "5.0.468"}
    by_chart = prep["preview"]["previous_tags_by_chart"]
    assert by_chart[1] == ("svc-c: not written — no build run on record (it was not queued at this "
                           "version), and the last release did not carry it")


def test_apply_writes_the_approved_tags_and_never_reads_the_logs_again(base_tag_on, released, TCR_prev,
                                                                       monkeypatch):
    released([_queued("svc-a", "5.0.470")])
    monkeypatch.setattr(C, "run_base_tag", lambda url: {"ok": True, "tag": "5.0.468"})
    prep = RF.prepare_release_fileset(TCR._payload())
    monkeypatch.setattr(C, "run_base_tag", lambda url: pytest.fail("apply never reads the logs"))
    out = RF.apply_release_fileset(prep)
    assert out["ok"], out
    assert json.loads(TCR_prev.files[TCR.BRANCH][TCR.PREV])["svc-a"] == "5.0.468"


def test_a_chart_whose_build_logged_none_keeps_the_last_release_and_says_why(base_tag_on, released, TCR_prev,
                                                                           monkeypatch):
    released([_queued("svc-a", "5.0.470")])
    monkeypatch.setattr(C, "run_base_tag", lambda url: {"ok": False, "reason": "its build has no step or job named 'x'"})
    prep = RF.prepare_release_fileset(TCR._payload())
    assert prep["ok"] and prep["previous_from_builds"] == {}
    assert prep["preview"]["previous_tags_by_chart"][0] == (
        "svc-a: 5.0.463 from the last release — its build has no step or job named 'x'")
    assert "carries the last release's versions" in prep["preview"]["previous_tags"]


def test_with_the_step_off_the_release_is_exactly_as_before(released, TCR_prev, monkeypatch):
    released([_queued("svc-a", "5.0.470", previous_tag="5.0.469")])
    prep = RF.prepare_release_fileset(TCR._payload())
    assert prep["ok"] and prep["previous_from_builds"] == {}
    assert "previous_tags_by_chart" not in prep["preview"]


def test_a_version_that_never_went_through_the_queue_borrows_no_run():
    """Released at 5.0.991 without being queued at it: the history keeps the
    chart's ticket and routing from its last queue entry (5.0.990), never that
    entry's run, base tag or open controls — they belong to another build, and
    Re-run a job would act on that build."""
    events = [_queued("svc-a", "5.0.990", previous_tag="5.0.989", jira_ticket="ABC-1",
                      allowed_failures="RCTLDEF0001691", prl1_only=True),
              {"event_type": "released", "artifact_name": "svc-a", "artifact_version": "5.0.991",
               "event_ts": "2026-11-03T11:00:00Z", "release_name": "R1"}]
    [item] = RQ.release_history(events)[0]["items"]
    assert (item["build_run_url"], item["previous_tag"], item["allowed_failures"]) == ("", "", "")
    assert item["from_queue"] is False and item["requeueable"] is False
    assert (item["jira_ticket"], item["prl1_only"]) == ("ABC-1", True)
