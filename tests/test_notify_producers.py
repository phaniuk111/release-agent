"""The notification producers: one best-effort call at the place each fact is
already known — a settled PR (pr_reconcile), the SIT -> UAT PR of a UAT deploy
(promotion), a DF run's end (df_run, started by dataflow), a chart put back
from the release history (release_queue). Each names the right person, kind
and scope, and NONE of them can fail the action it rides on: notify raising or
answering an error dict leaves the action's result exactly as it was."""
import datetime as dt
import time
from types import SimpleNamespace

import pytest

from release_agent.tools import attribution, dataflow as DF, df_run, notifications as N, pr_reconcile as R
from release_agent.tools import promotion as P, release_queue as RQ

ME = "dev@example.com"
YOU = "you@example.com"
REPO = "example-org/deploy"


@pytest.fixture
def sent(monkeypatch):
    """Every notify call, recorded; `mode['fail']` makes the fake raise,
    `mode['err']` makes it answer an error dict (what the real one does when
    the store is down)."""
    box = SimpleNamespace(calls=[], mode={"fail": False, "err": False})

    def fake(recipient, **kw):
        box.calls.append({"recipient": recipient, **kw})
        if box.mode["fail"]:
            raise RuntimeError("store down")
        if box.mode["err"]:
            return {"ok": False, "error": "store down"}
        return {"ok": True, "event_id": "e", "subject": "s", "event_type": "created"}

    monkeypatch.setattr(N, "notify", fake)
    return box


# ----- pr_reconcile: a pending PR settled outside the chat --------------------------------

def _pending(pr, name, version, who, env="uat", on_merge="deployed", tag="deployed"):
    return {"event_type": "pending", "event_ts": "2026-01-01T10:00:00+00:00", "artifact_name": name,
            "artifact_version": version, "environment": env, "deployment_repo": REPO,
            "pr_number": pr, "requested_by": who, "note": R.pending_note(on_merge, tag)}


def _github(pr):
    return SimpleNamespace(get_repo=lambda full: SimpleNamespace(get_pull=lambda n: pr))


def _merged_pr(n):
    return SimpleNamespace(merged=True, state="closed", merged_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
                           closed_at=None, merged_by=SimpleNamespace(login="approver"),
                           html_url=f"https://github.com/{REPO}/pull/{n}")


def _closed_pr(n):
    return SimpleNamespace(merged=False, state="closed", merged_at=None, merged_by=None,
                           closed_at=dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc),
                           html_url=f"https://github.com/{REPO}/pull/{n}")


@pytest.fixture
def reconcile(monkeypatch):
    monkeypatch.setitem(R._state, "at", float("-inf"))
    rows = []
    monkeypatch.setattr(RQ, "_insert", lambda batch: rows.extend(batch) or {"ok": True})
    return rows


def test_a_merged_pr_tells_each_requester_how_many_charts_landed_where(sent, reconcile):
    events = [_pending(10, "svc-a", "2.0.0", ME), _pending(10, "svc-b", "1.4.1", ME),
              _pending(10, "svc-c", "0.9.0", YOU),
              # The chart the override dropped: a removal, not a chart "on UAT".
              _pending(10, "svc-old", "0.1.0", ME, on_merge="removed", tag="dropped by override")]
    assert R.reconcile_pending(events, github=_github(_merged_pr(10))) == 4

    assert [c["recipient"] for c in sent.calls] == [ME, YOU], "one per requester, once"
    mine = sent.calls[0]
    assert mine["kind"] == "release_pr" and mine["severity"] == "info"
    assert mine["title"] == "Release PR #10 merged — 3 charts on UAT"
    assert mine["body"] == "svc-a:2.0.0, svc-b:1.4.1, svc-c:0.9.0"
    assert mine["link"] == f"https://github.com/{REPO}/pull/10"
    assert mine["scope"] == f"release-pr:{REPO}#10" and mine["repo"] == REPO and mine["pr_number"] == 10


def test_a_pr_closed_without_merging_is_a_warning(sent, reconcile):
    R.reconcile_pending([_pending(11, "svc-a", "2.0.0", ME)], github=_github(_closed_pr(11)))
    (call,) = sent.calls
    assert call["title"] == "Release PR #11 closed without merging" and call["severity"] == "warning"
    assert call["kind"] == "release_pr" and call["scope"] == f"release-pr:{REPO}#11"


def test_a_pending_event_with_nobody_to_tell_sends_nothing(sent, reconcile):
    R.reconcile_pending([_pending(12, "svc-a", "2.0.0", None), _pending(12, "svc-b", "1.0.0", "")],
                        github=_github(_merged_pr(12)))
    assert sent.calls == []


def test_a_notification_failure_never_undoes_or_blocks_the_reconciliation(sent, reconcile):
    sent.mode["fail"] = True
    events = [_pending(13, "svc-a", "2.0.0", ME)]
    assert R.reconcile_pending(events, github=_github(_merged_pr(13))) == 1
    assert len(reconcile) == 1 and reconcile[0]["event_type"] == "deployed"
    assert len(sent.calls) == 1, "it was tried"


def test_nothing_is_announced_when_the_events_could_not_be_written(sent, monkeypatch):
    monkeypatch.setitem(R._state, "at", float("-inf"))
    monkeypatch.setattr(RQ, "_insert", lambda batch: {"ok": False, "error": "BQ down"})
    assert R.reconcile_pending([_pending(14, "svc-a", "2.0.0", ME)], github=_github(_merged_pr(14))) == 0
    assert sent.calls == []


# ----- promotion: the SIT -> UAT PR of a UAT deploy -------------------------------------------

@pytest.fixture
def sit_to_uat(monkeypatch):
    monkeypatch.setattr(P.settings, "uat_pr_wait_seconds", 0)
    monkeypatch.setattr(P.settings, "sit_branch", "SIT")
    monkeypatch.setattr(P.settings, "uat_branch", "UAT")

    def make(pr):
        return SimpleNamespace(html_url=f"https://github.com/{REPO}",
                               get_pulls=lambda **kw: [pr] if pr else [])
    return make


def _pr(n):
    return SimpleNamespace(number=n, html_url=f"https://github.com/{REPO}/pull/{n}")


def test_the_developer_hears_their_sit_to_uat_pr_is_up(sent, sit_to_uat, monkeypatch):
    monkeypatch.setattr(attribution, "requester_email", lambda: ME)
    out = P._via_sit_outcome(sit_to_uat(_pr(40)), REPO, [], "Deploy svc-a:1.0.0 to UAT", charts="svc-a:1.0.0")
    assert out["pending_prs"][0]["number"] == 40
    (call,) = sent.calls
    assert call["recipient"] == ME and call["kind"] == "uat_pr"
    assert call["title"] == "SIT → UAT PR #40 raised for your deploy of svc-a:1.0.0 — merge when ready"
    assert call["link"] == f"https://github.com/{REPO}/pull/40" and call["scope"] == f"uat-pr:{REPO}#40"
    assert call["pr_number"] == 40 and call["repo"] == REPO


def test_no_verified_caller_means_no_uat_pr_notification(sent, sit_to_uat, monkeypatch):
    monkeypatch.setattr(attribution, "requester_email", lambda: "")
    P._via_sit_outcome(sit_to_uat(_pr(40)), REPO, [], "Deploy svc-a:1.0.0 to UAT", charts="svc-a:1.0.0")
    assert sent.calls == []


def test_no_sit_to_uat_pr_yet_means_nothing_to_announce(sent, sit_to_uat, monkeypatch):
    monkeypatch.setattr(attribution, "requester_email", lambda: ME)
    out = P._via_sit_outcome(sit_to_uat(None), REPO, [], "Deploy svc-a:1.0.0 to UAT", charts="svc-a:1.0.0")
    assert out["pending_prs"] == [] and sent.calls == []


def test_a_uat_pr_notification_failure_leaves_the_deploy_outcome_as_it_was(sent, sit_to_uat, monkeypatch):
    monkeypatch.setattr(attribution, "requester_email", lambda: ME)
    sent.mode["fail"] = True
    out = P._via_sit_outcome(sit_to_uat(_pr(41)), REPO, [], "Deploy svc-a:1.0.0 to UAT", charts="svc-a:1.0.0")
    assert out["pending_prs"][0]["number"] == 41 and "PR #41" in out["note"]


# ----- df_run: a dispatched DF run, followed to its end -----------------------------------------

RUN_URL = "https://github.com/example-org/df/actions/runs/7"


def _run(status, conclusion=None, steps=()):
    job = SimpleNamespace(name="deploy", steps=[SimpleNamespace(name=n, conclusion=c, number=i + 1)
                                                for i, (n, c) in enumerate(steps)])
    return SimpleNamespace(status=status, conclusion=conclusion, html_url=RUN_URL, jobs=lambda: [job])


def _repo(*runs):
    """A repo whose run goes through ``runs`` one read at a time, staying on the last."""
    seq = list(runs)

    def get_workflow_run(run_id):
        assert run_id == 7
        got = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(got, Exception):
            raise got
        return got
    return SimpleNamespace(get_workflow_run=get_workflow_run)


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(df_run, "POLL_SECONDS", 0)


def test_a_run_that_succeeds_is_announced_with_its_link(sent, quick):
    out = df_run.watch(_repo(_run("queued"), _run("in_progress"), _run("completed", "success")),
                       7, "df-position-agg", "2.0.0", ME, RUN_URL)
    assert out["ok"] and out["conclusion"] == "success"
    (call,) = sent.calls
    assert call["recipient"] == ME and call["kind"] == "df_run" and call["severity"] == "info"
    assert call["title"] == "DF run finished: df-position-agg:2.0.0" and call["link"] == RUN_URL
    assert call["scope"] == "df-run:7" and call["run_id"] == "7" and call["conclusion"] == "success"


def test_a_run_that_fails_names_the_step_and_links_a_why_question(sent, quick):
    run = _run("completed", "failure", steps=[("Checkout", "success"), ("Build flex template", "failure")])
    out = df_run.watch(_repo(run), 7, "df-position-agg", "2.0.0", ME, RUN_URL)
    assert out["ok"] and out["conclusion"] == "failure"
    (call,) = sent.calls
    assert call["title"] == "DF run failed: df-position-agg:2.0.0 — step Build flex template"
    assert call["severity"] == "warning" and call["link"] == "chat:why did df-position-agg:2.0.0 fail?"
    assert "conclusion: failure" in call["body"] and "job deploy" in call["body"]


def test_a_cancelled_run_with_no_failed_step_still_says_it_did_not_finish(sent, quick):
    df_run.watch(_repo(_run("completed", "cancelled")), 7, "df-position-agg", "2.0.0", ME, RUN_URL)
    (call,) = sent.calls
    assert call["title"] == "DF run failed: df-position-agg:2.0.0" and "conclusion: cancelled" in call["body"]


def test_a_run_still_going_past_the_limit_is_said_so_not_called_failed(sent, quick, monkeypatch):
    monkeypatch.setattr(df_run, "MAX_MINUTES", 0)
    out = df_run.watch(_repo(_run("in_progress")), 7, "df-position-agg", "2.0.0", ME, RUN_URL)
    assert out == {"ok": False, "reason": "timeout"}
    (call,) = sent.calls
    assert call["title"].startswith("DF run still running: df-position-agg:2.0.0") and call["severity"] == "info"
    assert call["link"] == RUN_URL and call["scope"] == "df-run:7"


def test_a_run_that_cannot_be_read_is_given_up_quietly(sent, quick):
    out = df_run.watch(_repo(RuntimeError("401")), 7, "df-position-agg", "2.0.0", ME, RUN_URL)
    assert out == {"ok": False, "reason": "unreadable"} and sent.calls == []


def test_a_read_blip_is_retried_before_giving_up(sent, quick):
    out = df_run.watch(_repo(RuntimeError("502"), RuntimeError("502"), _run("completed", "success")),
                       7, "df-position-agg", "2.0.0", ME, RUN_URL)
    assert out["ok"] and len(sent.calls) == 1


def test_the_store_being_down_at_the_end_is_reported_not_raised(sent, quick):
    sent.mode["fail"] = True
    out = df_run.watch(_repo(_run("completed", "success")), 7, "df-position-agg", "2.0.0", ME, RUN_URL)
    assert out["ok"] is False and "RuntimeError" in out["reason"]


def test_the_background_watch_needs_someone_to_tell_and_a_run(sent, quick):
    assert df_run.watch_in_background(_repo(_run("completed", "success")), 7, "x", "1", "", RUN_URL) is False
    assert df_run.watch_in_background(_repo(_run("completed", "success")), None, "x", "1", ME, RUN_URL) is False
    assert sent.calls == []
    assert df_run.watch_in_background(_repo(_run("completed", "success")), 7, "x", "1", ME, RUN_URL) is True
    deadline = time.time() + 5
    while not sent.calls and time.time() < deadline:
        time.sleep(0.02)
    assert sent.calls and sent.calls[0]["title"] == "DF run finished: x:1"


def test_the_dispatch_hands_the_run_to_the_watcher_for_the_verified_caller_only(monkeypatch):
    started = []
    monkeypatch.setattr(df_run, "watch_in_background", lambda *a: started.append(a) or True)
    repo = SimpleNamespace()
    monkeypatch.setattr(attribution, "requester_email", lambda: "")
    DF._follow_run(repo, {"id": 7, "url": RUN_URL}, "df-position-agg", "2.0.0", RUN_URL)
    assert started == [], "nobody verified, nobody to tell"
    monkeypatch.setattr(attribution, "requester_email", lambda: ME)
    DF._follow_run(repo, {"id": 7, "url": RUN_URL}, "df-position-agg", "2.0.0", RUN_URL)
    assert started == [(repo, 7, "df-position-agg", "2.0.0", ME, RUN_URL)]


def test_a_watcher_that_cannot_start_never_fails_the_dispatch(monkeypatch):
    monkeypatch.setattr(attribution, "requester_email", lambda: ME)

    def boom(*a):
        raise RuntimeError("no threads")
    monkeypatch.setattr(df_run, "watch_in_background", boom)
    DF._follow_run(SimpleNamespace(), {"id": 7}, "df-position-agg", "2.0.0", RUN_URL)   # must not raise


# ----- release_queue: a chart put back from the release history -------------------------------

def _ev(event_type, name, ts, **extra):
    return {"event_type": event_type, "artifact_name": name, "event_ts": ts, **extra}


@pytest.fixture
def history(monkeypatch):
    events = [
        _ev("queued", "svc-a", "2026-07-13T10:00:00", artifact_version="1.0.0", build_run_url="https://x/run/1",
            jira_ticket="ABC-1", requested_by=ME),
        _ev("released", "svc-a", "2026-07-14T09:00:00", artifact_version="1.0.0", release_name="Weekly 41", pr_number=1),
        _ev("queued", "svc-b", "2026-07-13T10:00:00", artifact_version="3.0.0", build_run_url="https://x/run/3"),
        _ev("released", "svc-b", "2026-07-14T09:00:00", artifact_version="3.0.0", release_name="Weekly 41", pr_number=1),
    ]
    monkeypatch.setattr(RQ, "queue_enabled", lambda: True)
    monkeypatch.setattr(RQ, "_fetch_events", lambda days=120: events)
    monkeypatch.setattr(RQ, "add_intent", lambda artifact, requested_by, **kw: {"ok": True})
    return events


def test_the_original_requester_hears_their_chart_is_back_in_the_queue(sent, history):
    out = RQ.requeue_from_history("svc-a", "1.0.0", "devops@example.com")
    assert out["ok"]
    (call,) = sent.calls
    assert call["recipient"] == ME, "the person who queued it, not the release manager putting it back"
    assert call["kind"] == "requeue" and call["link"] == "portal:queue"
    assert call["title"] == "Charts put back in the queue — release Weekly 41 did not go through"
    assert call["body"] == "svc-a:1.0.0 is back in the next release" and call["scope"] == "requeue:Weekly 41:svc-a"


def test_a_chart_queued_by_nobody_known_sends_nothing(sent, history):
    assert RQ.requeue_from_history("svc-b", "3.0.0", "devops@example.com")["ok"]
    assert sent.calls == []


@pytest.mark.parametrize("mode", ["fail", "err"])
def test_the_requeue_succeeds_whatever_the_notification_does(sent, history, mode):
    sent.mode[mode] = True
    out = RQ.requeue_from_history("svc-a", "1.0.0", "devops@example.com")
    assert out["ok"] and out["artifact"] == "svc-a:1.0.0"
    assert len(sent.calls) == 1


def test_a_refused_requeue_announces_nothing(sent, history, monkeypatch):
    monkeypatch.setattr(RQ, "add_intent", lambda artifact, requested_by, **kw: {"ok": False, "error": "BQ down"})
    assert not RQ.requeue_from_history("svc-a", "1.0.0", "devops@example.com")["ok"]
    assert sent.calls == []
