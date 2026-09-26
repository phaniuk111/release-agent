"""Control 1691 may fail without stopping a chart being queued (CARE or DF) — it
can be a false positive, closed by hand. It shows as OPEN in the release queue and
nowhere else; the release is not stopped by it. Every other failure still
refuses, and a run where everything passed queues exactly as before."""
import pytest

from adk_release_agent import tools as T
from release_agent.tools import queue_gate as QG
from release_agent.tools import chg_defaults as CHG
from release_agent.tools import controls as C
from release_agent.tools import release_queue as RQ

RUN = {"url": "https://github.com/o/r/actions/runs/1", "conclusion": "failure"}


def ctl(name, job="build", ok=True):
    return {"control": name, "job": job, "passed": ok, "failed": not ok,
            "conclusion": "success" if ok else "failure", "status": "completed"}


def report(controls, run_ok=None, failed_steps=()):
    failed = [c for c in controls if c["failed"]]
    return {
        "found": True, "tag": "payments-api-1.4.2", "run": RUN,
        "run_succeeded": (not failed and not failed_steps) if run_ok is None else run_ok,
        "controls": controls,
        "failed_controls": [{"control": c["control"], "job": c["job"], "conclusion": "failure"} for c in failed],
        "open_controls": [], "failed_steps": list(failed_steps),
        "gate": "PASS" if not failed else "FAIL",
    }


@pytest.fixture
def queue(monkeypatch):
    monkeypatch.setattr(C.settings, "queue_allowed_failing_controls", "1691", raising=False)
    writes = []

    def run(rep):
        monkeypatch.setattr(QG, "_invoke_tool", lambda name, args=None: rep)
        monkeypatch.setattr(RQ, "add_intent", lambda **kw: writes.append(kw) or {"ok": True, "intent": kw})
        return T.queue_release_intent(
            artifact="payments-api:1.4.2", requested_by="dev@example.com",
            build_run_url=RUN["url"], jira_ticket="ABC-1", change_details="d")

    run.writes = writes
    return run


# --- which controls the setting names ------------------------------------------------
@pytest.mark.parametrize("name, token, expected", [
    # "1691": the name CONTAINS the number, whatever the format around it
    ("RCTLDEF0001691", "1691", True),
    ("RCTLDEF0001691 - Peer review evidence", "1691", True),
    ("RCTL-1691 peer review", "1691", True),
    ("Control 1691: peer review", "1691", True),
    ("1691_peer_review", "1691", True),
    ("RCTLDEF_0001691", "1691", True),
    ("RCTLDEF0001691v2", "1691", True),
    ("CTL1691B check", "1691", True),
    # ...but a bigger number that merely contains the digits is another control
    ("RCTLDEF0016910", "1691", False),
    ("RCTLDEF0021691", "1691", False),
    ("RCTLDEF0000104", "1691", False),
    # full ID, and explicit wildcard patterns
    ("rctldef0001691 (build)", "RCTLDEF0001691", True),
    ("RCTLDEF0021691", "*1691*", True),
    ("Peer review - RCTLDEF0001691", "*peer review*", True),
    ("RCTLDEF0001691", "", False),
])
def test_a_token_names_a_control_by_its_id(name, token, expected):
    assert C.control_matches(name, token) is expected


# --- scenario 1: everything passes ----------------------------------------------------
def test_all_controls_passing_queues_as_verified_exactly_as_before(queue):
    out = queue(report([ctl("RCTLDEF0000104"), ctl("RCTLDEF0001691")]))
    assert out["ok"] and out["eligible"] is True
    [w] = queue.writes
    assert w["build_verified"] is True and w["allowed_failures"] == []
    assert "allowed_failures" not in out and not out.get("warnings")


# --- scenario 2: only 1691 fails -------------------------------------------------------
def test_only_1691_failing_still_queues_but_is_recorded_as_failed(queue):
    out = queue(report([ctl("RCTLDEF0000104"), ctl("RCTLDEF0001691", ok=False)], run_ok=False))
    assert out["ok"] and out["eligible"] is True
    [w] = queue.writes
    assert w["allowed_failures"] == ["RCTLDEF0001691 in job build"]
    assert "OPEN" in out["warnings"][0] and "RCTLDEF0001691" in out["warnings"][0]


def test_1691_as_a_whole_job_takes_its_own_failed_steps_with_it(queue):
    """A control that IS a job fails through its steps — those are that
    control's failure, not a second one."""
    rep = report([ctl("RCTLDEF0000104"), ctl("RCTLDEF0001691", job="RCTLDEF0001691", ok=False)],
                 run_ok=False, failed_steps=[{"job": "RCTLDEF0001691", "name": "Run scan"}])
    out = queue(rep)
    assert out["ok"] and queue.writes[0]["allowed_failures"] == ["RCTLDEF0001691"]


def test_1691_as_the_only_control_failing_is_queued_and_marked_failed(queue):
    out = queue(report([ctl("RCTLDEF0001691", ok=False)], run_ok=False))
    assert out["ok"] and queue.writes[0]["allowed_failures"] == ["RCTLDEF0001691 in job build"]


# --- everything else still refuses -------------------------------------------------------
def test_another_control_failing_is_refused_even_when_1691_failed_too(queue):
    out = queue(report([ctl("RCTLDEF0000104", ok=False), ctl("RCTLDEF0001691", ok=False)], run_ok=False))
    assert not out["ok"] and out["failed_controls"] == ["RCTLDEF0000104"] and not queue.writes


def test_a_failed_build_step_no_longer_refuses_but_is_reported(queue):
    """Changed 2026-09-18 on the owner's instruction: only RCTLDEF controls decide
    eligibility. A failed ordinary step is said out loud, not made a blocker."""
    rep = report([ctl("RCTLDEF0001691", ok=False)], run_ok=False,
                 failed_steps=[{"job": "build", "name": "Unit tests"}])
    out = queue(rep)
    assert out["ok"] and out["failed_steps"]
    assert any("Unit tests" in w for w in out["warnings"])


def test_a_run_that_failed_with_no_detail_queues_but_says_so(queue):
    """Every control passed and nothing else is recorded as failed, so the controls
    say ship it — but a red run is never queued silently."""
    out = queue(report([ctl("RCTLDEF0000104")], run_ok=False))
    assert out["ok"] and queue.writes
    assert any("conclusion" in w for w in out["warnings"])


def test_with_nothing_allowed_1691_refuses_as_it_always_did(queue, monkeypatch):
    monkeypatch.setattr(C.settings, "queue_allowed_failing_controls", "", raising=False)
    out = queue(report([ctl("RCTLDEF0000104"), ctl("RCTLDEF0001691", ok=False)], run_ok=False))
    assert not out["ok"] and out["failed_controls"] == ["RCTLDEF0001691"]


# --- where it shows ----------------------------------------------------------------------------
def test_the_event_and_the_queue_carry_it(monkeypatch):
    rows = []
    monkeypatch.setattr(RQ, "_insert", lambda r: rows.extend(r) or {"ok": True})
    RQ.add_intent(artifact="svc:1", requested_by="d@x", build_verified=True,
                  allowed_failures=["RCTLDEF0001691 in job build"])
    RQ.add_intent(artifact="svc2:1", requested_by="d@x", build_verified=True)
    assert rows[0]["allowed_failures"] == "RCTLDEF0001691 in job build"
    assert "allowed_failures" not in rows[1], "clean builds never need the new column"
    by_name = {q["artifact_name"]: q for q in RQ.reduce_queue(rows)}
    assert by_name["svc"]["allowed_failures"] == "RCTLDEF0001691 in job build"
    assert by_name["svc2"]["allowed_failures"] == ""


def test_a_table_without_the_column_keeps_the_fact_in_the_note(monkeypatch):
    calls = []

    class _Client:
        def insert_rows_json(self, table, rows, row_ids=None):
            calls.append(rows)
            if any("allowed_failures" in r for r in rows):
                return [{"index": 0, "errors": [{"message": "no such field: allowed_failures."}]}]
            return []

    monkeypatch.setattr(RQ, "queue_enabled", lambda: True)
    monkeypatch.setattr(RQ, "_get_client", lambda: _Client())
    monkeypatch.setattr(RQ, "_table_id", lambda: "p.d.t")
    out = RQ.add_intent(artifact="svc:1", requested_by="d@x", build_verified=True, note="hi",
                        allowed_failures=["RCTLDEF0001691"])
    assert out["ok"] and len(calls) == 2
    assert calls[1][0]["note"] == "[control failed, allowed: RCTLDEF0001691] hi"


def test_the_change_request_does_not_mention_it():
    """Only the release queue shows it — a possible false positive, closed by
    hand, does not belong in the change record."""
    items = [{"name": "payments-api", "version": "1.4.2", "build_verified": True,
              "allowed_failures": "RCTLDEF0001691 in job build"}]
    text = " ".join(str(v) for v in CHG.build_defaults(items, "care", None, 7).values())
    assert "1691" not in text and "with all release controls (RCTLD) passing." in text


def test_a_dataflow_chart_follows_the_same_rule(queue, monkeypatch):
    monkeypatch.setattr(RQ, "add_intent", lambda **kw: queue.writes.append(kw) or {"ok": True, "intent": kw})
    rep = report([ctl("RCTLDEF0000104"), ctl("RCTLDEF0001691", ok=False)], run_ok=False)
    monkeypatch.setattr(QG, "_invoke_tool", lambda name, args=None: rep)
    out = T.queue_release_intent(artifact="df-orders:2.1.0", requested_by="d@x", df_only=True,
                                 build_run_url=RUN["url"], jira_ticket="ABC-1", change_details="d")
    assert out["ok"] and queue.writes[-1]["df_only"] is True
    assert queue.writes[-1]["allowed_failures"] == ["RCTLDEF0001691 in job build"]


def test_a_failure_kept_in_the_note_still_reads_back():
    """Found live: on a table without the column the fact went to the note, and
    the queue read the build back as plain 'verified'."""
    ev = {"event_type": "queued", "artifact_name": "svc", "artifact_version": "1", "build_verified": True,
          "note": "[control failed, allowed: RCTLDEF0001691 in job build] ship after 5pm"}
    [q] = RQ.reduce_queue([ev])
    assert q["allowed_failures"] == "RCTLDEF0001691 in job build" and q["note"] == "ship after 5pm"


# --- only RELEASE CONTROLS decide eligibility --------------------------------------
# Reported from the enterprise (2026-09-18): "Generate HCC Report" failing blocked
# queueing, and it also defeated the 1691 allowance, because the gate refused on any
# failed step and on the run's own red conclusion. Steps the release process does not
# govern must not hold the release.

def test_a_failed_non_control_step_does_not_block_the_queue(queue):
    rep = report([ctl("RCTLDEF0000104"), ctl("RCTLDEF0001691")], run_ok=False,
                 failed_steps=[{"job": "build-deploy-publish", "name": "Generate HCC Report"}])
    out = queue(rep)
    assert out["ok"] and out["eligible"] is True, "a non-control step must not refuse"
    assert queue.writes[0]["build_verified"] is True
    assert any("Generate HCC Report" in w for w in out["warnings"]), "but it must be SAID"


def test_1691_may_fail_alongside_a_failed_non_control_step(queue):
    """Both of the reported problems at once — this is the combination that was
    blocking: the HCC step turns the run red, which used to defeat the allowance."""
    rep = report([ctl("RCTLDEF0000104"), ctl("RCTLDEF0001691", ok=False)], run_ok=False,
                 failed_steps=[{"job": "build-deploy-publish", "name": "Generate HCC Report"}])
    out = queue(rep)
    assert out["ok"] and out["eligible"] is True
    assert queue.writes[0]["allowed_failures"] == ["RCTLDEF0001691 in job build"]
    assert any("OPEN" in w for w in out["warnings"])


def test_a_red_run_with_no_failed_control_still_queues(queue):
    """A run can be red for reasons that are not controls at all."""
    out = queue(report([ctl("RCTLDEF0000104"), ctl("RCTLDEF0001691")], run_ok=False))
    assert out["ok"] and out["eligible"] is True


def test_a_real_control_failure_still_refuses(queue):
    """The rule narrowed to controls — it did not go away."""
    rep = report([ctl("RCTLDEF0000043", ok=False)], run_ok=False,
                 failed_steps=[{"job": "build", "name": "Generate HCC Report"}])
    out = queue(rep)
    assert not out["ok"] and out["failed_controls"] == ["RCTLDEF0000043"]
    assert "RCTLDEF0000043" in out["reason"], "the refusal names the control, not the step"
    assert not queue.writes


def test_queueing_with_no_queue_configured_never_reads_bigquery(queue, monkeypatch):
    """The hint read after a queue write ('last shipped', 'last time flags') ran
    even with no queue configured and reached real BigQuery."""
    reads = []
    monkeypatch.setattr(RQ, "_fetch_events", lambda *a, **k: reads.append(1) or [])
    out = queue(report([ctl("RCTLDEF0000104")]))
    assert out["ok"] and reads == [], "no dataset, no BigQuery read"
