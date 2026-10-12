"""Deployment tracking (adk_release_agent/deploy_tracker.py): the cadence as pure
functions, and one pass against a fake GitHub — a merge starts a bell item and
a record, the runs' end updates the same item, a failure names the step, a
re-run updates again, the window ends in "still running", nothing raises, and
the NOTIFY_WORKFLOW dispatch is the one write that needs no yes."""
import datetime as dt
from types import SimpleNamespace

import pytest

from adk_release_agent import approvals, deploy_tracker as DT
from release_agent.config import settings
from release_agent.tools import events_store, notifications as N, pr_reconcile as R, tickets

REPO = "example-org/deploy"
ME = "me@example.com"
# Near the real clock: the event store reads "the last N days" against now, and
# a merge the tracker records is stamped with the PR's merge time.
T0 = dt.datetime.now(dt.timezone.utc).replace(microsecond=0) - dt.timedelta(minutes=30)
SHA = "abc1234def5678"
_REAL_RECONCILE = R.reconcile_pending      # the fixture below stubs it; one test wants the real one


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    for name, value in (("track_open_minutes", 5), ("track_run_seconds", 60), ("track_run_max_minutes", 90),
                        ("deploy_workflows", ""), ("notify_workflow", ""), ("ticket_prefixes", "CHG,RMG")):
        monkeypatch.setattr(settings, name, value, raising=False)
    monkeypatch.setitem(R._state, "at", float("-inf"))
    monkeypatch.setattr(R, "reconcile_pending", lambda events, github=None, outcomes=None: 0)
    tickets.clear_cache()
    DT._status.update(running=False)
    yield
    tickets.clear_cache()


# ----- the cadence ---------------------------------------------------------------------

def test_cadence_is_a_pure_function_of_what_the_pass_found():
    assert DT.next_delay({"open": 0, "in_flight": 0}) == 300, "nothing pending: one log read per 5 min"
    assert DT.next_delay({"open": 2, "in_flight": 0}) == 300, "a PR open: every 5 min"
    assert DT.next_delay({"open": 2, "in_flight": 1}) == 60, "runs in flight: every minute"
    assert DT.next_delay({"open": 0, "in_flight": 3, "error": "GitHub 502"}) == 300, "a failing pass never speeds up"
    assert DT.state_of({"open": 1, "in_flight": 2, "error": ""}) == {"open": 1, "in_flight": 2, "error": ""}
    assert DT.state_of(None) == {"open": 0, "in_flight": 0, "error": ""}
    assert DT.state_of({"ok": False, "error": "down"})["error"] == "down"


def test_the_cadence_follows_the_settings(monkeypatch):
    monkeypatch.setattr(settings, "track_open_minutes", 10)
    monkeypatch.setattr(settings, "track_run_seconds", 30)
    assert DT.next_delay({"open": 1}) == 600 and DT.next_delay({"in_flight": 1}) == 30


def test_one_pass_at_a_time_per_process():
    assert DT.begin() and not DT.begin()
    DT.finish()
    assert DT.begin()
    DT.finish()


# ----- what to follow (pure) ------------------------------------------------------------

def _pending(pr, name="svc-a", version="2.0.0", ts="2026-01-02T09:00:00+00:00", **more):
    return {"event_type": "pending", "event_ts": ts, "artifact_name": name, "artifact_version": version,
            "environment": "uat", "deployment_repo": REPO, "pr_number": pr, "requested_by": ME,
            "jira_ticket": "REL-1", "note": R.pending_note("deployed", "deployed"), **more}


def test_candidates_are_unresolved_pending_prs_and_recent_merges_pr_reconcile_settled():
    events = [
        _pending(10), _pending(10, name="svc-b"),
        _pending(11), {"event_type": "deployed", "deployment_repo": REPO, "pr_number": 11,
                       "event_ts": (T0 - dt.timedelta(minutes=30)).isoformat()},
        _pending(12), {"event_type": "deployed", "deployment_repo": REPO, "pr_number": 12,
                       "event_ts": (T0 - dt.timedelta(hours=3)).isoformat()},
        _pending(13), {"event_type": "abandoned", "deployment_repo": REPO, "pr_number": 13,
                       "event_ts": T0.isoformat()},
        {"event_type": "deployed", "deployment_repo": REPO, "pr_number": 14, "event_ts": T0.isoformat()},
    ]
    out = DT.candidates(events, now=T0)
    assert {k[1]: v["settled"] for k, v in out.items()} == {10: "", 11: "merged"}
    assert len(out[(REPO, 10)]["pending"]) == 2
    assert DT.candidates([], now=T0) == {}


def test_describe_names_the_release_else_the_charts_capped():
    assert DT.describe([_pending(1, release_name="2026-01-release")], 1) == "2026-01-release"
    assert DT.describe([_pending(1), _pending(1, name="svc-b", version="1.1")], 1) == "svc-a:2.0.0, svc-b:1.1"
    five = [_pending(1, name=f"svc-{i}", version="1") for i in range(5)]
    assert DT.describe(five, 1) == "svc-0:1, svc-1:1, svc-2:1 +2 more"
    assert DT.describe([], 9) == "PR #9"


def test_known_merges_is_the_latest_tracker_row_per_scope():
    rows = [{"scope": "deploy:x#1", "event_type": "finished"}, {"scope": "deploy:x#1", "event_type": "merged"},
            {"scope": "deploy:x#1", "event_type": "tickets"}, {"scope": "deploy:x#2", "event_type": "merged"}]
    known = DT.known_merges(rows)
    assert known["deploy:x#1"]["event_type"] == "finished" and known["deploy:x#2"]["event_type"] == "merged"


def test_run_filter_by_workflow_name_or_file(monkeypatch):
    run = {"name": "Deploy to UAT", "workflow": "deploy-uat.yml"}
    assert DT.run_matches(run, set())
    assert DT.run_matches(run, {"deploy to uat"}) and DT.run_matches(run, {"deploy-uat.yml"})
    assert not DT.run_matches(run, {"lint.yml"})
    monkeypatch.setattr(settings, "deploy_workflows", " Lint.yml , deploy-uat.yml")
    assert DT._wanted_workflows() == {"lint.yml", "deploy-uat.yml"}


def test_notify_workflow_target_parses_owner_repo_colon_file(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    assert DT.notify_workflow_target() == ("example-org/ops", "send-notification.yml")
    for bad in ("", "example-org/ops", "ops:file.yml", "example-org/ops:"):
        monkeypatch.setattr(settings, "notify_workflow", bad)
        assert DT.notify_workflow_target() == ("", "")


# ----- a fake GitHub --------------------------------------------------------------------

class _Step:
    def __init__(self, name, conclusion):
        self.name, self.conclusion = name, conclusion


class _Job:
    def __init__(self, name, steps):
        self.name, self.steps = name, steps


class _Run:
    def __init__(self, id, name="Deploy to UAT", path=".github/workflows/deploy-uat.yml", status="in_progress",
                 conclusion=None, attempt=1, jobs=()):
        self.id, self.name, self.path, self.status, self.conclusion = id, name, path, status, conclusion
        self.run_attempt, self.html_url, self._jobs = attempt, f"https://github.com/{REPO}/actions/runs/{id}", list(jobs)

    def jobs(self):
        return self._jobs


class _PR:
    def __init__(self, number, merged=False, state="open", when=None, by="approver", body="", comments=()):
        self.number, self.merged, self.state, self.html_url = number, merged, state, f"https://github.com/{REPO}/pull/{number}"
        self.merged_at = when if merged else None
        self.closed_at = when if state == "closed" else None
        self.merged_by = SimpleNamespace(login=by) if merged else None
        self.merge_commit_sha = SHA if merged else None
        self.body, self._comments = body, list(comments)

    def get_issue_comments(self):
        return list(self._comments)


class _Workflow:
    """A workflow object as PyGithub hands it over, with GitHub's
    return_run_details answer so the dispatch carries its run id."""

    def __init__(self, log, run_id=4242):
        self.url, self.log, self.run_id = "https://api.github.com/repos/example-org/ops/actions/workflows/1", log, run_id
        self._requester = self

    def requestJsonAndCheck(self, method, url, input=None):
        self.log.append((method, url, input))
        return {}, {"workflow_run_id": self.run_id, "html_url": f"https://github.com/example-org/ops/actions/runs/{self.run_id}"}


class _GitHub:
    """PRs by number (an Exception in place of one is raised on read), runs by
    head SHA, and the workflow the NOTIFY_WORKFLOW dispatch reaches."""

    def __init__(self, prs, runs=None, workflow=None):
        self.prs, self.runs, self.dispatches, self.pulls = prs, runs or {}, [], []
        self.workflow = workflow or (lambda log: _Workflow(log))

    def get_repo(self, full):
        gh = self

        class _Repo:
            default_branch = "main"

            def get_pull(self, n):
                gh.pulls.append(n)
                pr = gh.prs[n]
                if isinstance(pr, Exception):
                    raise pr
                return pr

            def get_workflow_runs(self, head_sha=None, **kw):
                return list(gh.runs.get(head_sha, []))

            def get_workflow(self, file):
                return gh.workflow(gh.dispatches)
        return _Repo()


def _comment(body, n=1):
    return SimpleNamespace(body=body, html_url=f"https://github.com/{REPO}/pull/7#issuecomment-{n}",
                           created_at=T0 - dt.timedelta(hours=1))


def _items():
    return N.list_for(ME)["items"]


def _rows(event_type=None):
    rows = events_store.history("deploy")["rows"]
    return [r for r in rows if event_type is None or r["event_type"] == event_type]


# ----- one pass -------------------------------------------------------------------------

def test_an_open_pr_is_counted_and_nothing_is_written():
    gh = _GitHub({7: _PR(7)})
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["ok"] and out["open"] == 1 and out["in_flight"] == 0 and not out["started"]
    assert _rows() == [] and _items() == []


def test_a_merge_is_recorded_with_its_sha_and_the_requester_is_told_the_deployment_is_starting():
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0 - dt.timedelta(minutes=2),
                         body="CHG-12345 created", comments=[_comment("RMG-678 approved")])},
                 runs={SHA: [_Run(1)]})
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["started"] == [f"deploy:{REPO}#7"] and out["in_flight"] == 1
    (row,) = _rows("merged")
    assert (row["subject"], row["scope"], row["merge_sha"], row["repo"], row["pr_number"], row["jira"], row["actor"]) == (
        f"deploy:{REPO}#7", f"deploy:{REPO}#7", SHA, REPO, 7, "REL-1", "portal")
    assert (row["chg"], row["rmg"]) == ("CHG-12345", "RMG-678")
    assert row["event_ts"] == (T0 - dt.timedelta(minutes=2)).isoformat(), "stamped with the merge time"
    started = [i for i in _items() if i["kind"] == "deploy_started"]
    assert [i["title"] for i in started] == ["Deployment starting: svc-a:2.0.0 — PR #7 merged"]
    assert started[0]["link"] == f"https://github.com/{REPO}/pull/7" and started[0]["scope"] == f"deploy:{REPO}#7"
    chg = [i for i in _items() if i["kind"] == "chg"]
    assert [i["title"] for i in chg] == ["CHG-12345 for release svc-a:2.0.0"]
    assert gh.dispatches == [], "NOTIFY_WORKFLOW unset: nothing is dispatched"


def test_a_second_pass_on_the_same_merge_writes_no_second_merged_row():
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0)}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=1))
    assert out["started"] == [] and out["in_flight"] == 1
    assert len(_rows("merged")) == 1 and len([i for i in _items() if i["kind"] == "deploy_started"]) == 1


def test_every_run_concluded_is_finished_on_the_same_scope_so_the_bell_shows_one_item():
    pr = _PR(7, merged=True, state="closed", when=T0)
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    gh.runs[SHA] = [_Run(1, status="completed", conclusion="success")]
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=3))
    assert out["finished"] == [f"deploy:{REPO}#7"] and out["in_flight"] == 0
    (row,) = _rows("finished")
    assert (row["conclusion"], row["run_id"], row["workflow"], row["merge_sha"]) == ("success", 1, "deploy-uat.yml", SHA)
    assert row["run_url"].endswith("/runs/1")
    deploy = [i for i in _items() if i["kind"] in ("deploy_started", "deploy_finished")]
    assert len(deploy) == 1, "one bell item for the deployment, its text changed"
    assert deploy[0]["title"] == "Deployment completed: svc-a:2.0.0" and deploy[0]["event_type"] == "updated"
    assert deploy[0]["link"].endswith("/runs/1") and deploy[0]["severity"] == "info"
    assert deploy[0]["status"] == "unread"


def test_a_failure_names_the_job_and_step_and_is_critical():
    pr = _PR(7, merged=True, state="closed", when=T0)
    failing = _Run(2, status="completed", conclusion="failure",
                   jobs=[_Job("deploy", [_Step("Checkout", "success"), _Step("helm upgrade", "failure")])])
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1, status="completed", conclusion="success"), failing]})
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=1))
    assert out["started"] == [f"deploy:{REPO}#7"] and out["finished"] == [f"deploy:{REPO}#7"]
    (row,) = _rows("finished")
    assert row["conclusion"] == "failure" and row["run_id"] == 2
    assert '"job":"deploy"' in row["details"] and '"step":"helm upgrade"' in row["details"]
    (item,) = [i for i in _items() if i["kind"] == "deploy_finished"]
    assert item["title"] == "Deployment failed at deploy › helm upgrade: svc-a:2.0.0"
    assert item["severity"] == "critical" and item["link"].endswith("/runs/2")


def test_a_rerun_of_a_finished_run_updates_the_same_scope():
    pr = _PR(7, merged=True, state="closed", when=T0)
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1, status="completed", conclusion="failure")]})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=1))
    # Nothing new: the same attempt is not reported twice.
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=2))
    assert out["finished"] == [] and out["in_flight"] == 0 and len(_rows("finished")) == 1
    # Someone re-runs it: in flight again, then completed "after re-run".
    gh.runs[SHA] = [_Run(1, status="in_progress", conclusion=None, attempt=2)]
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=3))
    assert out["in_flight"] == 1
    gh.runs[SHA] = [_Run(1, status="completed", conclusion="success", attempt=2)]
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=4))
    assert out["finished"] == [f"deploy:{REPO}#7"]
    assert [r["conclusion"] for r in _rows("finished")] == ["success", "failure"]
    (item,) = [i for i in _items() if i["kind"] == "deploy_finished"]
    assert item["title"] == "Deployment completed (after re-run): svc-a:2.0.0"


def test_after_the_window_with_no_conclusion_one_still_running_item_then_tracking_stops():
    pr = _PR(7, merged=True, state="closed", when=T0)
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=1))
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=91))
    assert out["abandoned"] == [f"deploy:{REPO}#7"] and out["in_flight"] == 0
    assert len(_rows("abandoned_tracking")) == 1
    (item,) = [i for i in _items() if i["kind"] == "deploy_finished"]
    assert item["title"] == "Deployment still running after 90 min: svc-a:2.0.0" and item["severity"] == "warning"
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=92))
    assert out["abandoned"] == [] and out["in_flight"] == 0 and len(_rows()) == 2


def test_a_merge_pr_reconcile_settled_between_passes_is_still_followed():
    pr = _PR(7, merged=True, state="closed", when=T0)
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    events = [_pending(7), {"event_type": "deployed", "deployment_repo": REPO, "pr_number": 7,
                            "event_ts": T0.isoformat()}]
    out = DT.run_pass(github=gh, events=events, now=T0 + dt.timedelta(minutes=5))
    assert out["started"] == [f"deploy:{REPO}#7"] and out["finished"] == [f"deploy:{REPO}#7"]


def test_a_pass_fetches_each_pr_once_the_settlement_reuses_what_the_tracker_read(monkeypatch):
    from release_agent.tools import release_queue as RQ

    inserted = []
    monkeypatch.setattr(RQ, "_insert", lambda batch: inserted.extend(batch) or {"ok": True})
    monkeypatch.setattr(R, "reconcile_pending", _REAL_RECONCILE)
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0), 8: _PR(8),
                  9: _PR(9, state="closed", when=T0)}, runs={SHA: [_Run(1)]})
    out = DT.run_pass(github=gh, events=[_pending(7), _pending(8), _pending(9)], now=T0)
    assert out["ok"] and out["open"] == 1
    assert sorted(gh.pulls) == [7, 8, 9], "one get_pull per PR, tracker and settlement together"
    assert sorted((r["pr_number"], r["event_type"]) for r in inserted) == [(7, "deployed"), (9, "abandoned")]


def test_the_tickets_are_noted_for_an_open_or_newly_merged_pr_never_a_settled_one(monkeypatch):
    noted = []
    monkeypatch.setattr(tickets, "note_first_seen", lambda repo, n, tk, **kw: noted.append(n) or {})
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0), 8: _PR(8), 9: _PR(9, state="closed", when=T0)},
                 runs={SHA: [_Run(1)]})
    events = [_pending(7), _pending(8), _pending(9)]
    DT.run_pass(github=gh, events=events, now=T0)
    assert sorted(noted) == [7, 8], "open, and merged for the first time; the closed one is settled"
    noted.clear()
    DT.run_pass(github=gh, events=events, now=T0 + dt.timedelta(minutes=1))
    assert noted == [8], "the merge already recorded is settled: no ticket read for it"
    noted.clear()
    gh.prs[8] = _PR(8, state="closed", when=T0)
    DT.run_pass(github=gh, events=events, now=T0 + dt.timedelta(minutes=2))
    assert noted == []


def test_a_merge_whose_sha_github_has_not_given_yet_is_read_again_next_pass(monkeypatch):
    """merge_commit_sha is briefly absent after a merge. A run lookup on "" lists
    every run in the repo, so nothing may be concluded, recorded or dispatched
    from it."""
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:notify.yml")
    pr = _PR(7, merged=True, state="closed", when=T0)
    pr.merge_commit_sha = None
    gh = _GitHub({7: pr}, runs={"": [_Run(9, status="completed", conclusion="success")]})
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["ok"] and out["in_flight"] == 1 and out["started"] == [] and out["finished"] == []
    assert _rows() == [] and _items() == [] and gh.dispatches == []
    pr.merge_commit_sha = SHA                                      # GitHub caught up
    gh.runs[SHA] = [_Run(1)]
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=1))
    assert out["started"] == [f"deploy:{REPO}#7"] and len(_rows("merged")) == 1


def test_no_commit_means_no_runs_not_every_run():
    repo = SimpleNamespace(get_workflow_runs=lambda **kw: pytest.fail("listed runs without a commit"))
    assert DT.runs_for(repo, "") == []


def test_a_closed_pr_is_left_to_pr_reconcile():
    gh = _GitHub({7: _PR(7, state="closed", when=T0)})
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["open"] == 0 and _rows() == [] and _items() == []


def test_the_pass_never_raises_when_github_errors_and_other_prs_still_get_followed():
    gh = _GitHub({7: RuntimeError("502"), 8: _PR(8, merged=True, state="closed", when=T0)},
                 runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    out = DT.run_pass(github=gh, events=[_pending(7), _pending(8)], now=T0)
    assert out["ok"] and out["started"] == [f"deploy:{REPO}#8"]
    assert len(out["errors"]) == 1 and "example-org/deploy#7: RuntimeError: 502" in out["errors"][0]


def test_a_pass_that_cannot_read_the_event_log_is_an_error_not_a_raise(monkeypatch):
    monkeypatch.setattr(events_store, "history", lambda *a, **k: {"ok": False, "error": "table missing"})
    gh = _GitHub({7: _PR(7)})
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["ok"] is False and "table missing" in out["error"]
    assert DT.status()["last_error"] == out["error"] and not DT.status()["running"]
    assert DT.next_delay(DT.state_of(out)) == 300


def test_without_a_release_log_a_pass_is_one_empty_read(monkeypatch):
    from release_agent.tools import release_queue

    monkeypatch.setattr(release_queue, "queue_enabled", lambda: False)
    out = DT.run_pass(github=_GitHub({}), now=T0)
    assert out == {"ok": True, "open": 0, "in_flight": 0, "started": [], "finished": [], "abandoned": [], "errors": []}


def test_no_requester_means_records_but_no_bell():
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0)},
                 runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    DT.run_pass(github=gh, events=[_pending(7, requested_by=None)], now=T0)
    assert [r["event_type"] for r in _rows()] == ["finished", "merged"]
    assert _items() == []


# ----- the one GitHub write ------------------------------------------------------------

def test_notify_workflow_is_dispatched_at_each_stage_with_exactly_these_inputs(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    pr = _PR(7, merged=True, state="closed", when=T0, body="CHG-12345 approved\nRMG-678")
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    gh.runs[SHA] = [_Run(1, status="completed", conclusion="success")]
    DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=1))
    assert [d[0] for d in gh.dispatches] == ["POST", "POST"]
    assert all(d[1].endswith("/dispatches") for d in gh.dispatches)
    want = {"release": "svc-a:2.0.0", "chg": "CHG-12345", "rmg": "RMG-678", "pr": "7", "requested_by": ME}
    assert gh.dispatches[0][2] == {"ref": "main", "return_run_details": True, "inputs": {"stage": "starting", **want}}
    assert gh.dispatches[1][2] == {"ref": "main", "return_run_details": True, "inputs": {"stage": "completed", **want}}
    merged, finished = _rows("merged")[0], _rows("finished")[0]
    assert '"notify_run_id":4242' in merged["details"] and '"notify_run_id":4242' in finished["details"]


def test_a_failed_deployment_dispatches_stage_failed(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0)},
                 runs={SHA: [_Run(1, status="completed", conclusion="failure")]})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert [d[2]["inputs"]["stage"] for d in gh.dispatches] == ["starting", "failed"]


def test_a_dispatch_that_fails_is_recorded_beside_the_merge_never_raised(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    class _Broken(_Workflow):
        def requestJsonAndCheck(self, *a, **k):
            raise RuntimeError("403 resource not accessible")
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0)}, runs={SHA: [_Run(1)]}, workflow=_Broken)
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["ok"] and out["started"]
    assert '"notify_error":"RuntimeError: 403 resource not accessible"' in _rows("merged")[0]["details"]


def test_the_dispatch_is_the_one_write_approvals_allows_without_a_yes():
    assert "dispatch_notify_workflow" in approvals.ALLOWED_WITHOUT_YES
    assert approvals.needs_approval("dispatch_notify_workflow", {"stage": "starting"}) is False
    assert approvals.needs_approval("promote_release", {"target": "prd"}) is True, "nothing else changed"
    assert approvals.ALLOWED_WITHOUT_YES == frozenset({"dispatch_notify_workflow"})


def test_the_dispatch_refuses_if_the_rule_is_ever_removed(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    monkeypatch.setattr(approvals, "ALLOWED_WITHOUT_YES", frozenset())
    gh = _GitHub({})
    out = DT.dispatch_notify_workflow(gh, "starting", release="x", chg="", rmg="", pr="1", requested_by=ME)
    assert out["ok"] is False and "approval" in out["error"] and gh.dispatches == []


def test_the_dispatch_is_skipped_when_notify_workflow_is_unset():
    gh = _GitHub({})
    out = DT.dispatch_notify_workflow(gh, "starting", release="x", chg="", rmg="", pr="1", requested_by=ME)
    assert out == {"ok": True, "skipped": "NOTIFY_WORKFLOW is not set"} and gh.dispatches == []


# ----- the app --------------------------------------------------------------------------

def test_the_loop_starts_only_when_tracking_is_enabled(monkeypatch):
    import asyncio

    from release_agent import app_fastapi as APP

    started = []
    monkeypatch.setattr(DT, "track_forever", lambda: started.append("tracker") or asyncio.sleep(0))
    monkeypatch.setattr(APP.settings, "session_idle_minutes", 0)

    async def _open(enabled):
        monkeypatch.setattr(APP.settings, "deploy_tracking_enabled", enabled)
        async with APP._lifespan(APP.app):
            await asyncio.sleep(0)
    asyncio.run(_open(False))
    assert started == []
    asyncio.run(_open(True))
    assert started == ["tracker"]


def test_the_feature_is_registered_the_way_notifications_is():
    from release_agent import features

    assert "deploy-tracking" in features.UI_FEATURES
    assert "deploy-tracking" in type(settings).model_fields["preview_features"].default.split(",")
    assert features.refusal("deploy-tracking") == "Deployment tracking is not available yet — it is a preview feature."
