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
                        ("deploy_workflows", ""), ("notify_workflow", ""), ("ticket_prefixes", "CHG,RMG"),
                        ("notify_test", False)):
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
    def __init__(self, number, merged=False, state="open", when=None, by="approver", body="", comments=(),
                 base=None, head="UAT"):
        if base:
            self.base, self.head = SimpleNamespace(ref=base), SimpleNamespace(ref=head)
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
        self.list_error = self.workflow_error = None
        self.files = {}
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

            def get_pulls(self, state="open", base=None, **kw):
                if gh.list_error:
                    raise gh.list_error
                found = [p for p in gh.prs.values() if not isinstance(p, Exception) and p.state == state
                         and getattr(getattr(p, "base", None), "ref", None) == base]
                return sorted(found, key=lambda p: -p.number)

            def get_workflow_runs(self, head_sha=None, **kw):
                return list(gh.runs.get(head_sha, []))

            def get_workflow(self, file):
                if gh.workflow_error:
                    raise gh.workflow_error
                return gh.workflow(gh.dispatches)

            def get_contents(self, path, ref=None):
                if path not in gh.files:
                    raise Exception("404 Not Found")
                return SimpleNamespace(decoded_content=gh.files[path].encode())
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
    assert out == {"ok": True, "open": 0, "in_flight": 0, "started": [], "finished": [], "abandoned": [],
                   "watching": [], "errors": []}


def test_no_requester_means_records_but_no_bell():
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0)},
                 runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    DT.run_pass(github=gh, events=[_pending(7, requested_by=None)], now=T0)
    assert [r["event_type"] for r in _rows()] == ["finished", "merged"]
    assert _items() == []


# ----- the one GitHub write ------------------------------------------------------------

def _start(chg="CHG-12345", env="PROD"):
    return {"release_notification_type": "Start", "change_request": chg, "environment": env,
            "create_release_draft": "true", "test_notification": "false"}


def test_the_inputs_are_the_workflows_own_fixed_names_and_the_environment_follows_the_base_branch(monkeypatch):
    monkeypatch.setattr(settings, "prd_branch", "PRD")
    monkeypatch.setattr(settings, "prl1_branch", "PRL1")
    assert DT.notify_inputs("starting", "CHG-12345", "PRD") == _start()
    assert DT.notify_inputs("completed", "CHG-12345", "PRD") == {**_start(), "release_notification_type": "Complete"}
    assert DT.notify_inputs("starting", "CHG-1", "PRL1")["environment"] == "PRL1"
    assert DT.notify_inputs("starting", "CHG-1", "RELEASE_UAT")["environment"] == "RELEASE_UAT", "any other base as it is"
    assert DT.notify_inputs("starting", "", "PRD")["change_request"] == "", "no CHG yet: empty, not missing"
    assert DT.notify_inputs("starting", "", "")["environment"] == ""
    assert DT.notify_inputs("failed", "CHG-1", "PRD") is None and DT.notify_inputs("still_running", "", "PRD") is None
    monkeypatch.setattr(settings, "prd_branch", "release-prd")           # the branch names are settings, not literals
    assert DT.notify_inputs("starting", "", "release-prd")["environment"] == "PROD"


def test_notify_workflow_is_dispatched_when_it_starts_and_when_it_completes_with_exactly_these_inputs(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    pr = _PR(7, merged=True, state="closed", when=T0, body="CHG-12345 approved\nRMG-678", base=settings.prd_branch)
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    gh.runs[SHA] = [_Run(1, status="completed", conclusion="success")]
    DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=1))
    assert [d[0] for d in gh.dispatches] == ["POST", "POST"]
    assert all(d[1].endswith("/dispatches") for d in gh.dispatches)
    assert gh.dispatches[0][2] == {"ref": "main", "return_run_details": True, "inputs": _start()}
    assert gh.dispatches[1][2] == {"ref": "main", "return_run_details": True,
                                   "inputs": {**_start(), "release_notification_type": "Complete"}}
    merged, finished = _rows("merged")[0], _rows("finished")[0]
    assert '"notify_run_id":4242' in merged["details"] and '"notify_run_id":4242' in finished["details"]
    assert [i["title"] for i in _items() if i["kind"] == "deploy_started" or i["kind"] == "deploy_finished"] == [
        "Deployment completed: svc-a:2.0.0"], "the requester's bell is as before"


def test_a_failed_deployment_dispatches_nothing_but_is_still_told_and_recorded(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0, base=settings.prd_branch)},
                 runs={SHA: [_Run(1, status="completed", conclusion="failure")]})
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["finished"] == [f"deploy:{REPO}#7"]
    assert [d[2]["inputs"]["release_notification_type"] for d in gh.dispatches] == ["Start"]
    assert [i["title"] for i in _items() if i["kind"] == "deploy_finished"] == ["Deployment failed: svc-a:2.0.0"]
    assert _rows("finished")[0]["conclusion"] == "failure"


def test_only_merges_into_prd_or_prl1_are_release_notifications(monkeypatch):
    """A portal PR stopped at review in SIT or UAT is followed for its own
    requester, but its merge is not a release: the team's workflow can only
    name PROD or PRL1, and GitHub would refuse anything else."""
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0, base=settings.prl1_branch),
                  8: _PR(8, merged=True, state="closed", when=T0, base="SIT"),
                  9: _PR(9, merged=True, state="closed", when=T0, base="UAT")}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[_pending(7), _pending(8), _pending(9)], now=T0)
    assert [d[2]["inputs"]["environment"] for d in gh.dispatches] == ["PRL1"]


def test_a_dispatch_that_fails_is_recorded_beside_the_merge_never_raised(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    class _Broken(_Workflow):
        def requestJsonAndCheck(self, *a, **k):
            raise RuntimeError("403 resource not accessible")
    gh = _GitHub({7: _PR(7, merged=True, state="closed", when=T0, base=settings.prd_branch)},
                 runs={SHA: [_Run(1)]}, workflow=_Broken)
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["ok"] and out["started"]
    assert '"notify_error":"RuntimeError: 403 resource not accessible"' in _rows("merged")[0]["details"]


def test_the_dispatch_is_the_one_write_approvals_allows_without_a_yes():
    assert "dispatch_notify_workflow" in approvals.ALLOWED_WITHOUT_YES
    assert approvals.needs_approval("dispatch_notify_workflow", DT.notify_inputs("starting", "", "PRD")) is False
    assert approvals.needs_approval("promote_release", {"target": "prd"}) is True, "nothing else changed"
    assert approvals.ALLOWED_WITHOUT_YES == frozenset({"dispatch_notify_workflow"})


def test_the_dispatch_refuses_if_the_rule_is_ever_removed(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    monkeypatch.setattr(approvals, "ALLOWED_WITHOUT_YES", frozenset())
    gh = _GitHub({})
    out = DT.dispatch_notify_workflow(gh, "starting", chg="", base="PRD")
    assert out["ok"] is False and "approval" in out["error"] and gh.dispatches == []


def test_the_dispatch_is_skipped_when_notify_workflow_is_unset():
    gh = _GitHub({})
    out = DT.dispatch_notify_workflow(gh, "starting", chg="", base="PRD")
    assert out == {"ok": True, "skipped": "NOTIFY_WORKFLOW is not set"} and gh.dispatches == []


# ----- guard-branch PRs: raised by the mono repo's action, not by the portal ----------------

@pytest.fixture
def guard(monkeypatch):
    """The deployment repo with PRD and PRL1 guard branches, and the team's
    send-notification workflow set."""
    for name, value in (("deploy_repo", REPO), ("prd_branch", "PRD"), ("prl1_branch", "PRL1"),
                        ("release_guard_branches", ["PRD", "PRL1"]),
                        ("notify_workflow", "example-org/ops:send-notification.yml")):
        monkeypatch.setattr(settings, name, value, raising=False)


def _merge(pr, when):
    pr.merged, pr.state, pr.merged_at = True, "closed", when
    pr.merged_by, pr.merge_commit_sha = SimpleNamespace(login="approver"), SHA


def _close(pr, when):
    pr.state, pr.closed_at = "closed", when


def _complete(gh, conclusion="success"):
    gh.runs[SHA] = [_Run(1, status="completed", conclusion=conclusion)]


def _inputs(gh):
    return [d[2]["inputs"] for d in gh.dispatches]


def _bell():
    return events_store.history(N.DOMAIN)["rows"]


def _guard_pr(number=12, base="PRD", body="CHG-12345 approved"):
    return _PR(number, base=base, head="UAT", body=body)


def test_a_guard_branch_pr_first_seen_open_is_recorded_as_watching_and_nobody_is_told(guard):
    gh = _GitHub({12: _guard_pr()})
    out = DT.run_pass(github=gh, events=[], now=T0)
    assert out["ok"] and out["open"] == 1 and out["watching"] == [f"deploy:{REPO}#12"] and not out["started"]
    (row,) = _rows("watching")
    assert (row["subject"], row["scope"], row["repo"], row["pr_number"], row["title"], row["actor"]) == (
        f"deploy:{REPO}#12", f"deploy:{REPO}#12", REPO, 12, "UAT → PRD", "portal")
    assert row["details"] == '{"base":"PRD"}'
    assert gh.dispatches == [] and _bell() == [] and gh.pulls == [], "found by the listing: no extra fetch"
    # the CHG on it is recorded as a ticket row, like any open PR
    assert [r["chg"] for r in _rows("tickets")] == ["CHG-12345"]


def test_a_watched_pr_is_fetched_once_per_pass_and_recorded_once(guard):
    gh = _GitHub({12: _guard_pr()})
    DT.run_pass(github=gh, events=[], now=T0)
    out = DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=5))
    assert out["watching"] == [] and out["open"] == 1 and gh.pulls == [12]
    assert len(_rows("watching")) == 1


def test_a_watched_prs_merge_starts_the_dispatch_with_the_mapped_inputs_and_tells_nobody(guard):
    pr = _guard_pr()
    gh = _GitHub({12: pr}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[], now=T0)
    _merge(pr, T0 + dt.timedelta(minutes=1))
    out = DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=2))
    assert out["started"] == [f"deploy:{REPO}#12"] and out["in_flight"] == 1
    (row,) = _rows("merged")
    assert (row["scope"], row["merge_sha"], row["chg"], row["title"]) == (f"deploy:{REPO}#12", SHA, "CHG-12345", "UAT → PRD")
    assert _inputs(gh) == [{"release_notification_type": "Start", "change_request": "CHG-12345",
                            "environment": "PROD", "create_release_draft": "true", "test_notification": "false"}]
    assert '"notify_run_id":4242' in row["details"]
    assert _bell() == [], "nobody in the portal raised it: no bell item"

    _complete(gh)
    out = DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=3))
    assert out["finished"] == [f"deploy:{REPO}#12"]
    (done,) = _rows("finished")
    assert (done["scope"], done["conclusion"]) == (f"deploy:{REPO}#12", "success")
    assert _inputs(gh)[1] == {"release_notification_type": "Complete", "change_request": "CHG-12345",
                              "environment": "PROD", "create_release_draft": "true", "test_notification": "false"}
    assert _bell() == []


def test_a_watched_prs_failed_runs_dispatch_nothing_and_are_still_recorded(guard):
    pr = _guard_pr()
    gh = _GitHub({12: pr}, runs={SHA: [_Run(1, status="completed", conclusion="failure")]})
    DT.run_pass(github=gh, events=[], now=T0)
    _merge(pr, T0)
    out = DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=1))
    assert out["started"] and out["finished"]
    assert [i["release_notification_type"] for i in _inputs(gh)] == ["Start"]
    assert _rows("finished")[0]["conclusion"] == "failure" and _bell() == []


def test_a_prl1_pr_dispatches_environment_prl1(guard):
    pr = _guard_pr(13, base="PRL1")
    gh = _GitHub({13: pr}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[], now=T0)
    assert _rows("watching")[0]["title"] == "UAT → PRL1"
    _merge(pr, T0)
    DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=1))
    assert _inputs(gh)[0]["environment"] == "PRL1"


def test_the_newest_open_pr_of_each_guard_branch_is_watched(guard):
    gh = _GitHub({12: _guard_pr(12), 13: _guard_pr(13, base="PRL1"), 11: _guard_pr(11)})
    out = DT.run_pass(github=gh, events=[], now=T0)
    assert sorted(out["watching"]) == [f"deploy:{REPO}#12", f"deploy:{REPO}#13"]


def test_a_watched_pr_closed_without_a_merge_is_recorded_closed_and_dropped(guard):
    pr = _guard_pr()
    gh = _GitHub({12: pr})
    DT.run_pass(github=gh, events=[], now=T0)
    _close(pr, T0)
    out = DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=1))
    assert out["started"] == [] and out["ok"]
    (row,) = _rows("closed")
    assert row["scope"] == f"deploy:{REPO}#12" and row["pr_number"] == 12
    assert gh.dispatches == [] and _bell() == []
    DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=2))
    assert gh.pulls == [12], "closed is final: it is not fetched again"
    assert len(_rows("closed")) == 1


def test_a_restart_carries_on_from_the_event_log(guard):
    pr = _guard_pr()
    gh = _GitHub({12: pr}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[], now=T0)
    _merge(pr, T0)
    DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=1))
    # A new process: nothing in memory but the event log.
    DT._status.update(running=False, last_run_at=None, last_summary=None)
    tickets.clear_cache()
    gh2 = _GitHub({12: pr}, runs={SHA: [_Run(1)]})
    out = DT.run_pass(github=gh2, events=[], now=T0 + dt.timedelta(minutes=2))
    assert out["started"] == [] and out["watching"] == [] and out["in_flight"] == 1 and gh2.dispatches == []
    _complete(gh2)
    out = DT.run_pass(github=gh2, events=[], now=T0 + dt.timedelta(minutes=3))
    assert out["finished"] == [f"deploy:{REPO}#12"]
    assert [i["release_notification_type"] for i in _inputs(gh2)] == ["Complete"]
    assert len(_rows("merged")) == 1 and len(_rows("watching")) == 1


def test_a_finished_watched_pr_stops_being_fetched_after_its_window(guard):
    pr = _guard_pr()
    gh = _GitHub({12: pr}, runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    DT.run_pass(github=gh, events=[], now=T0)
    _merge(pr, T0)
    DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=1))
    assert gh.pulls == [12]
    DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=5))      # inside the window: a re-run is seen
    assert gh.pulls == [12, 12]
    DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=95))
    assert gh.pulls == [12, 12], "past the window: left alone"


def test_a_pr_that_is_both_pending_and_on_a_guard_branch_is_tracked_once_as_pending(guard):
    pr = _PR(7, base="PRD", head="UAT", body="CHG-12345")
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert _rows("watching") == [] and gh.pulls == [7]
    _merge(pr, T0 + dt.timedelta(minutes=1))
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=2))
    assert out["started"] == [f"deploy:{REPO}#7"] and len(_rows("merged")) == 1
    assert [i["release_notification_type"] for i in _inputs(gh)] == ["Start"]
    assert gh.pulls == [7, 7], "one fetch per pass"
    assert [i["title"] for i in _items() if i["kind"] == "deploy_started"] == [
        "Deployment starting: svc-a:2.0.0 — PR #7 merged"], "the requester is told, as always"
    # Even if a watching row exists from before, the pending PR wins the scope.
    events_store.record({"domain": "deploy", "event_type": "watching", "subject": f"deploy:{REPO}#7",
                         "scope": f"deploy:{REPO}#7", "repo": REPO, "pr_number": 7})
    DT.run_pass(github=gh, events=[_pending(7)], now=T0 + dt.timedelta(minutes=3))
    assert gh.pulls == [7, 7, 7]


def test_nothing_is_watched_without_a_deployment_repo(monkeypatch):
    monkeypatch.setattr(settings, "deploy_repo", "")
    gh = _GitHub({12: _guard_pr()})
    gh.list_error = RuntimeError("must not list")
    out = DT.run_pass(github=gh, events=[], now=T0)
    assert out["ok"] and out["errors"] == [] and out["watching"] == []


def test_a_guard_branch_that_cannot_be_listed_is_left_out_and_pending_prs_still_run(guard):
    gh = _GitHub({8: _PR(8, merged=True, state="closed", when=T0)}, runs={SHA: [_Run(1)]})
    gh.list_error = RuntimeError("502")                 # release_window._open_release_prs leaves such a branch out
    out = DT.run_pass(github=gh, events=[_pending(8)], now=T0)
    assert out["ok"] and out["started"] == [f"deploy:{REPO}#8"] and out["watching"] == [] and out["errors"] == []


def test_watch_targets_leave_out_the_taken_the_closed_and_the_long_finished():
    def row(kind, n, ts=T0, **more):
        scope = f"deploy:{REPO}#{n}"
        return {"event_type": kind, "scope": scope, "repo": REPO, "pr_number": n, "event_ts": ts.isoformat(), **more}

    now = T0 + dt.timedelta(hours=3)
    rows = [row("finished", 4), row("merged", 4), row("watching", 4),                # finished, window over
            row("finished", 5, now), row("merged", 5, now - dt.timedelta(minutes=10)), row("watching", 5),
            row("closed", 6), row("watching", 6), row("watching", 7), row("watching", 8),
            row("abandoned_tracking", 9), row("watching", 9), row("merged", 10), row("watching", 10)]
    known = DT.known_merges(rows)
    out = DT.watch_targets(rows, known, {(REPO, 8)}, now)
    assert sorted(k[1] for k in out) == [5, 7, 10]
    assert out[(REPO, 7)]["pending"] == [] and out[(REPO, 7)]["watched"]["event_type"] == "watching"


# ----- a guard-branch PR opened and merged between two passes -----------------------------

def _caught_up(monkeypatch, started):
    """This process started at ``started`` (the catch-up floor with the memory store)."""
    monkeypatch.setattr(DT, "_STARTED_AT", started)


def _starts_and_completes(gh):
    return [(i["release_notification_type"], i["environment"]) for i in _inputs(gh)]


def test_a_pr_opened_and_merged_between_two_passes_gets_start_then_complete_once(guard, monkeypatch):
    _caught_up(monkeypatch, T0 - dt.timedelta(hours=1))
    gh = _GitHub({}, runs={SHA: [_Run(1)]})
    DT.run_pass(github=gh, events=[], now=T0)
    pr = _guard_pr()
    _merge(pr, T0 + dt.timedelta(minutes=2))          # opened and merged before the next pass
    gh.prs[12] = pr
    out = DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=5))
    scope = f"deploy:{REPO}#12"
    assert out["watching"] == [scope] and out["started"] == [scope] and out["in_flight"] == 1 and out["open"] == 0
    assert _starts_and_completes(gh) == [("Start", "PROD")]
    assert _inputs(gh)[0]["change_request"] == "CHG-12345"
    (row,) = _rows("watching")
    assert (row["title"], row["details"]) == ("UAT → PRD", '{"base":"PRD"}')
    _complete(gh)
    out = DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=6))
    assert out["finished"] == [scope] and out["watching"] == []
    for minutes in (7, 8):                             # the rows remember: nothing goes out twice
        out = DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=minutes))
        assert out["watching"] == [] and out["started"] == [] and out["finished"] == []
    assert _starts_and_completes(gh) == [("Start", "PROD"), ("Complete", "PROD")]
    assert [len(_rows(k)) for k in ("watching", "merged", "finished")] == [1, 1, 1]
    assert _bell() == [], "nobody in the portal raised it: no bell item"


def test_a_merge_before_the_bound_sends_nothing(guard, monkeypatch):
    _caught_up(monkeypatch, T0 - dt.timedelta(minutes=2))
    old, before_start = _guard_pr(12), _guard_pr(13, base="PRL1")
    _merge(old, T0 - dt.timedelta(minutes=20))         # more than CATCH_UP_PASSES × 5 min ago
    _merge(before_start, T0 - dt.timedelta(minutes=4))  # recent, but before this process started
    gh = _GitHub({12: old, 13: before_start}, runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    out = DT.run_pass(github=gh, events=[], now=T0)
    assert out["ok"] and out["watching"] == [] and out["started"] == [] and out["errors"] == []
    assert gh.dispatches == [] and _rows() == [] and gh.pulls == []


def test_the_catch_up_bound_is_pure_and_never_reaches_past_its_floor(monkeypatch):
    now, started = T0, T0 - dt.timedelta(hours=2)
    assert DT.catch_up_since([], now, "memory", started) == now - dt.timedelta(minutes=15)
    assert DT.catch_up_since([], now, "memory", now - dt.timedelta(minutes=3)) == now - dt.timedelta(minutes=3)
    rows = [{"event_type": "finished", "event_ts": (now - dt.timedelta(minutes=9)).isoformat()},
            {"event_type": "tickets", "event_ts": (now - dt.timedelta(minutes=1)).isoformat()}]
    just_started = now - dt.timedelta(minutes=1)
    assert DT.catch_up_since(rows, now, "memory", just_started) == just_started, "memory forgets: the start holds"
    assert DT.catch_up_since(rows, now, "bigquery", just_started) == now - dt.timedelta(minutes=9), \
        "BigQuery remembers: back to the tracker's latest row, not its ticket rows"
    assert DT.catch_up_since([], now, "bigquery", just_started) == just_started, "no row yet: just switched on"
    monkeypatch.setattr(settings, "track_open_minutes", 10)
    assert DT.catch_up_since([], now, "memory", started) == now - dt.timedelta(minutes=30)


def test_a_restart_with_the_memory_store_does_not_resend(guard, monkeypatch):
    _caught_up(monkeypatch, T0 - dt.timedelta(hours=1))
    pr = _guard_pr()
    _merge(pr, T0 - dt.timedelta(minutes=1))
    gh = _GitHub({12: pr}, runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    DT.run_pass(github=gh, events=[], now=T0)
    assert _starts_and_completes(gh) == [("Start", "PROD"), ("Complete", "PROD")]
    # A new process: the memory store is empty, so the merge is behind its start.
    events_store.clear_memory()
    tickets.clear_cache()
    DT._status.update(running=False, last_run_at=None, last_summary=None)
    _caught_up(monkeypatch, T0 + dt.timedelta(minutes=1))
    gh2 = _GitHub({12: pr}, runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    out = DT.run_pass(github=gh2, events=[], now=T0 + dt.timedelta(minutes=2))
    assert out["ok"] and out["watching"] == [] and out["started"] == [] and out["finished"] == []
    assert gh2.dispatches == [] and _rows() == []


def test_a_restart_with_bigquery_does_not_resend_and_still_catches_a_merge_made_while_it_was_down(
        guard, monkeypatch):
    # Near the real clock: the rows the store stamps itself are the floor here.
    t = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    monkeypatch.setattr(events_store, "store_name", lambda: "bigquery")
    _caught_up(monkeypatch, t - dt.timedelta(hours=1))
    first = _guard_pr(12)
    _merge(first, t - dt.timedelta(minutes=2))
    gh = _GitHub({12: first}, runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    DT.run_pass(github=gh, events=[], now=t)
    assert _starts_and_completes(gh) == [("Start", "PROD"), ("Complete", "PROD")]
    # A new process; the rows are kept. PR 13 merged while the pod was down.
    DT._status.update(running=False, last_run_at=None, last_summary=None)
    tickets.clear_cache()
    _caught_up(monkeypatch, t + dt.timedelta(minutes=10))
    during = _guard_pr(13, base="PRL1")
    _merge(during, t + dt.timedelta(minutes=5))
    gh2 = _GitHub({12: first, 13: during}, runs={SHA: [_Run(1, status="completed", conclusion="success")]})
    out = DT.run_pass(github=gh2, events=[], now=t + dt.timedelta(minutes=11))
    assert out["watching"] == [f"deploy:{REPO}#13"] and out["started"] == [f"deploy:{REPO}#13"]
    assert _starts_and_completes(gh2) == [("Start", "PRL1"), ("Complete", "PRL1")], "nothing again for #12"


def test_a_pending_pr_merged_between_passes_is_followed_once_as_pending(guard, monkeypatch):
    _caught_up(monkeypatch, T0 - dt.timedelta(hours=1))
    pr = _PR(7, base="PRD", head="UAT", body="CHG-12345")
    _merge(pr, T0 - dt.timedelta(minutes=1))
    gh = _GitHub({7: pr}, runs={SHA: [_Run(1)]})
    out = DT.run_pass(github=gh, events=[_pending(7)], now=T0)
    assert out["started"] == [f"deploy:{REPO}#7"] and out["watching"] == [] and _rows("watching") == []
    assert _starts_and_completes(gh) == [("Start", "PROD")] and gh.pulls == [7]


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


def test_the_feature_is_registered_and_out_of_preview_by_default():
    from release_agent import features

    assert "deploy-tracking" in features.UI_FEATURES
    assert "deploy-tracking" not in type(settings).model_fields["preview_features"].default.split(",")
    assert features.refusal("deploy-tracking") == "Deployment tracking is not available yet — it is a preview feature."


def test_notify_test_sends_the_dry_run_to_the_test_channel(monkeypatch):
    assert DT.notify_inputs("starting", "CHG-1", settings.prd_branch)["test_notification"] == "false"
    monkeypatch.setattr(settings, "notify_test", True, raising=False)
    assert DT.notify_inputs("starting", "CHG-1", settings.prd_branch)["test_notification"] == "true"
    assert DT.notify_inputs("failed", "CHG-1", settings.prd_branch) is None, "a failure is still not announced"


def test_the_change_request_is_sent_exactly_as_written_on_the_pr():
    """Found live (2026-10-25): a PR saying CHG0099001 dispatched the release
    notification with CHG-0099001 — a ticket the change system does not have."""
    from release_agent.tools import tickets as TK

    found = TK.tickets_from("Promote UAT to PRD.\n\nCHG: CHG0099001\nRMG: RMG0042001", "https://example/pr/4", [])
    cols = TK.columns(found)
    assert (cols["chg"], cols["rmg"]) == ("CHG0099001", "RMG0042001")
    assert DT.notify_inputs("starting", cols["chg"], settings.prd_branch)["change_request"] == "CHG0099001"


def test_the_notification_branches_are_prd_and_prl1_whatever_the_guard_lists(monkeypatch):
    """RELEASE_GUARD_BRANCHES is the release guard and may list SIT and UAT; the
    notifications watch PRD_BRANCH and PRL1_BRANCH only."""
    monkeypatch.setattr(settings, "release_guard_branches", ["SIT", "UAT", "PRD", "PRL1"])
    assert DT.notify_branches() == [settings.prl1_branch, settings.prd_branch]
    monkeypatch.setattr(settings, "prd_branch", "production")
    assert DT.notify_branches() == [settings.prl1_branch, "production"]


def test_a_dispatch_for_a_branch_that_is_not_prd_or_prl1_is_skipped_not_sent(monkeypatch):
    monkeypatch.setattr(settings, "notify_workflow", "example-org/ops:send-notification.yml")
    gh = _GitHub({})
    out = DT.dispatch_notify_workflow(gh, "starting", chg="CHG0000001", base="UAT")
    assert out["ok"] and "not PRD_BRANCH or PRL1_BRANCH" in out["skipped"] and gh.dispatches == []



# ----- the report a person reads: why a notification did or did not go out ---------------

_WORKFLOW_YAML = """on:
  workflow_dispatch:
    inputs:
      release_notification_type: {type: choice}
      change_request: {type: string}
      environment: {type: choice}
      create_release_draft: {type: boolean}
      test_notification: {type: boolean}
"""


@pytest.fixture
def reportable(guard, monkeypatch):
    monkeypatch.setattr(settings, "deploy_tracking_enabled", True)
    DT._status.update(last_run_at=None, last_error="", last_summary=None)


def _gh_with_workflow(prs, **kw):
    gh = _GitHub(prs, **kw)
    gh.files[".github/workflows/send-notification.yml"] = _WORKFLOW_YAML
    return gh


def _check(out, name):
    return next(c for c in out["checks"] if c["name"] == name)


def test_the_report_names_a_switched_off_tracker_and_an_unset_workflow_and_never_raises(monkeypatch):
    monkeypatch.setattr(settings, "deploy_tracking_enabled", False)
    DT._status.update(last_run_at=None, last_error="")
    out = DT.report(github=_GitHub({}), now=T0)
    assert out["ok"] and _check(out, "Tracking switched on")["ok"] is False
    assert _check(out, "Notification workflow setting")["ok"] is False
    assert _check(out, "Notification workflow reachable")["ok"] is None, "nothing to reach: skipped, not failed"
    assert _check(out, "Where it remembers what it sent")["ok"] is None, "pod memory is a warning"


def test_the_report_checks_the_workflow_file_the_token_can_see(reportable):
    gh = _gh_with_workflow({})
    assert _check(DT.report(github=gh, now=T0), "Notification workflow reachable")["ok"] is True
    gh.files[".github/workflows/send-notification.yml"] = _WORKFLOW_YAML.replace("change_request", "chg")
    detail = _check(DT.report(github=gh, now=T0), "Notification workflow reachable")
    assert detail["ok"] is False and "change_request" in detail["detail"]
    gh.workflow_error = Exception("404 Not Found")
    detail = _check(DT.report(github=gh, now=T0), "Notification workflow reachable")
    assert detail["ok"] is False and "cannot see the repo" in detail["detail"]


def _opened(pr, when):
    pr.created_at, pr.title = when, "release"
    return pr


def test_the_report_lists_only_this_weeks_open_release_prs_and_whether_each_is_watched(reportable):
    now = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)          # a Thursday
    assert DT.week_start(now) == dt.datetime(2026, 10, 26, tzinfo=dt.timezone.utc)
    this_week = _opened(_guard_pr(12, body="CHG0099001 RMG0099001"), now - dt.timedelta(days=1))
    prl1 = _opened(_guard_pr(13, base="PRL1"), now - dt.timedelta(hours=2))
    last_week = _opened(_guard_pr(11), now - dt.timedelta(days=6))
    merged = _opened(_guard_pr(14), now - dt.timedelta(days=1))
    _merge(merged, now)
    gh = _gh_with_workflow({12: this_week, 13: prl1, 11: last_week, 14: merged})
    DT.run_pass(github=gh, events=[], now=T0)              # watches the newest open PR of each branch
    out = DT.report(github=gh, now=now)
    assert [(e["pr_number"], e["env"], e["watched"]) for e in out["prs"]] == [(13, "PRL1", True), (12, "PRD", True)]
    assert (out["prs"][1]["chg"], out["prs"][1]["rmg"]) == ("CHG0099001", "RMG0099001")
    assert out["config"]["since"] == "2026-10-26T00:00:00+00:00"
    assert _check(out, "Release branches readable")["ok"] is True
    assert "steps" not in out["prs"][0] and "history_error" not in out, "no history on the card"


def test_an_open_pr_not_yet_seen_or_with_tracking_off_says_so(reportable, monkeypatch):
    now = dt.datetime(2026, 10, 29, 12, tzinfo=dt.timezone.utc)
    gh = _gh_with_workflow({12: _opened(_guard_pr(12), now)})
    (entry,) = DT.report(github=gh, now=now)["prs"]
    assert (entry["watched"], entry["tracking"]) == (False, True)
    monkeypatch.setattr(settings, "deploy_tracking_enabled", False)
    assert DT.report(github=gh, now=now)["prs"][0]["tracking"] is False


def test_the_report_survives_github_being_down(reportable):
    gh = _gh_with_workflow({})
    gh.list_error = Exception("502 Bad Gateway")
    gh.workflow_error = Exception("502 Bad Gateway")
    out = DT.report(github=gh, now=T0)
    check = _check(out, "Release branches readable")
    assert out["ok"] and check["ok"] is False and "PRD: cannot be read" in check["detail"] and out["prs"] == []


def test_a_failed_dispatch_and_a_skipped_one_are_both_kept_on_the_row(reportable, monkeypatch):
    pr = _guard_pr()
    gh = _gh_with_workflow({12: pr}, runs={SHA: [_Run(1)]})
    gh.workflow_error = Exception("403 Resource not accessible by integration")
    DT.run_pass(github=gh, events=[], now=T0)
    _merge(pr, T0)
    DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=1))
    (merged,) = _rows("merged")
    assert '"notify_error":"Exception: 403' in merged["details"] and '"base":"PRD"' in merged["details"]
    monkeypatch.setattr(settings, "notify_workflow", "")
    _complete(gh)
    DT.run_pass(github=gh, events=[], now=T0 + dt.timedelta(minutes=2))
    (finished,) = _rows("finished")
    assert '"notify_skipped":"NOTIFY_WORKFLOW is not set"' in finished["details"]


def test_an_events_table_that_cannot_be_read_is_the_named_reason(reportable, monkeypatch):
    monkeypatch.setattr(events_store, "store_name", lambda: "bigquery")
    monkeypatch.setattr(events_store, "_table_id", lambda: "proj.ds.events")
    monkeypatch.setattr(events_store, "history", lambda *a, **k: {
        "ok": False, "error": "NotFound: 404 Table proj.ds.events not found", "hint": "Create it."})
    out = DT.report(github=_gh_with_workflow({}), now=T0)
    check = _check(out, "Where it remembers what it sent")
    assert check["ok"] is False and "proj.ds.events cannot be read" in check["detail"] and "Create it." in check["detail"]


def test_the_last_pass_errors_are_shown(reportable):
    DT.finish(error="RuntimeError: the event log could not be read: 403")
    assert "403" in _check(DT.report(github=_gh_with_workflow({}), now=T0), "Last pass")["detail"]
    DT.finish(summary={"open": 1, "in_flight": 0, "errors": ["org/deploy#12: GithubException: 502"]})
    check = _check(DT.report(github=_gh_with_workflow({}), now=T0), "Last pass")
    assert check["ok"] is False and "#12" in check["detail"]
    DT.finish(summary={"open": 1, "in_flight": 0, "errors": []})
    assert _check(DT.report(github=_gh_with_workflow({}), now=T0), "Last pass")["ok"] is True
