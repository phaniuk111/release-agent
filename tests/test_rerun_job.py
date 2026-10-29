"""Re-run a job (tools/rerun_job.py, design/RERUN_JOB.md): the pure rules, the
preview, the single-use plan, and the batch against a scripted GitHub — every
way GitHub can say no, not only the happy path."""
import datetime as dt
import json

import pytest

from release_agent.tools import events_store, notifications, release_queue, rerun_job as R
from release_agent.tools.rerun_job import Resp

NOW = dt.datetime(2026, 10, 29, 12, 0, tzinfo=dt.timezone.utc)
PROTECT = R.protected_words("build,publish,release,deploy")


def job(id_, name, concl="failure", steps=(), attempt=1):
    return {"id": id_, "name": name, "status": "completed", "conclusion": concl, "run_attempt": attempt,
            "html_url": f"https://github.com/o/r/actions/runs/1/job/{id_}",
            "steps": [{"name": n, "conclusion": c} for n, c in steps]}


def run(created="2026-10-28T10:00:00Z", status="completed", attempt=1, path=".github/workflows/b.yml"):
    return {"status": status, "created_at": created, "run_attempt": attempt, "html_url": "https://x/run",
            "name": "build", "path": path, "head_sha": "abc"}


WORKFLOW = """
jobs:
  build-and-publish: {runs-on: x}
  sdlc-peer-review: {needs: build-and-publish, runs-on: x}
  notify-sdlc: {needs: [sdlc-peer-review], runs-on: x}
  unit-tests: {runs-on: x}
"""
GATED = """
jobs:
  build: {}
  sdlc-peer-review: {needs: build}
  publish: {needs: sdlc-peer-review}
"""


# ---------------------------------------------------------------- pure rules

def test_protected_words_look_at_the_last_segment_only():
    assert R.words("build-and-publish (3.11)") == ["build", "and", "publish", "3", "11"]
    assert R.is_protected("build-and-publish", PROTECT)
    assert R.is_protected("Publish image", PROTECT)
    assert not R.is_protected("sdlc-peer-review", PROTECT)
    # A reusable workflow's caller prefix is where it is called from, not what it does.
    assert not R.is_protected("build-deploy-publish / RCTLDEF0001691 check", PROTECT)
    assert not R.is_protected("builder-tests", PROTECT)        # a word, not a substring


def test_match_jobs_exact_then_pattern_then_the_job_holding_a_step():
    jobs = [job(1, "sdlc-peer-review"), job(2, "unit-tests"),
            job(3, "build-and-publish", steps=[("RCTLDEF0001691 peer review", "failure")]),
            job(4, "caller / RCTLDEF0002000")]
    assert R.match_jobs(jobs, "SDLC-Peer-Review") == ([jobs[0]], "job")
    assert R.match_jobs(jobs, "RCTLDEF0002000") == ([jobs[3]], "job")      # last segment, exact
    assert R.match_jobs(jobs, "2000") == ([jobs[3]], "job")                 # the control number
    assert R.match_jobs(jobs, "1691") == ([jobs[2]], "step")                # only a step is named so
    assert R.match_jobs(jobs, "nothing") == ([], "")
    assert R.match_jobs(jobs, "  ") == ([], "")
    matrix = [job(5, "test (3.11)"), job(6, "test (3.12)")]
    assert len(R.match_jobs(matrix, "test")[0]) == 2


def test_dependents_follow_needs_transitively_and_say_none_when_unknown():
    g = R.workflow_graph(WORKFLOW)
    assert R.dependents(g, "sdlc-peer-review") == ["notify-sdlc"]
    assert R.dependents(g, "build-and-publish") == ["notify-sdlc", "sdlc-peer-review"]
    assert R.dependents(g, "unit-tests") == []
    assert R.dependents(g, "not-in-the-file") is None
    assert R.dependents(None, "x") is None
    assert R.workflow_graph("jobs: [") is None                 # invalid YAML
    assert R.workflow_graph({"not": "text"}) is None
    named = R.workflow_graph("jobs:\n  t:\n    name: test\n  n: {needs: t}\n")
    assert R.dependents(named, "test (3.11)") == ["n"]          # a matrix leg of `name:`
    reusable = R.workflow_graph("jobs:\n  bdp: {uses: x}\n  after: {needs: bdp}\n")
    assert R.dependents(reusable, "bdp / RCTLDEF0001691") == ["after"]


def answer(jobs, wanted="sdlc-peer-review", r=None, graph=WORKFLOW, include_passed=False):
    return R.answer_for_run(r or run(), jobs, wanted, include_passed=include_passed,
                            graph=R.workflow_graph(graph) if graph else None, now=NOW, protect=PROTECT)


def test_one_answer_per_run():
    jobs = [job(1, "build-and-publish", "success"), job(2, "sdlc-peer-review"), job(3, "notify-sdlc", "success")]
    ok = answer(jobs)
    assert ok["state"] == "rerun" and ok["job_id"] == 2 and ok["dependents"] == ["notify-sdlc"]
    assert answer(jobs, r=run(status="in_progress"))["state"] == "running"
    old = answer(jobs, r=run(created="2026-09-28T10:00:00Z"))
    assert old["state"] == "too_old" and "31 days old" in old["detail"]
    assert answer(jobs, wanted="lint")["state"] == "no_job"
    assert answer(jobs, wanted="build-and-publish")["state"] == "protected"
    passed = [job(1, "sdlc-peer-review", "success", attempt=2)]
    assert answer(passed)["state"] == "passed"
    assert answer(passed, include_passed=True)["state"] == "rerun"
    amb = answer([job(5, "test (3.11)"), job(6, "test (3.12)")], wanted="test")
    assert amb["state"] == "ambiguous" and amb["matches"] == ["test (3.11)", "test (3.12)"]


def test_a_control_step_inside_the_build_job_is_refused():
    jobs = [job(1, "build-and-publish", steps=[("Create new tag", "success"),
                                              ("RCTLDEF0001691 peer review evidence", "failure")])]
    a = answer(jobs, wanted="RCTLDEF0001691 peer review evidence")
    assert a["state"] == "protected" and "is a step inside it" in a["detail"]


def test_a_job_a_publish_job_depends_on_is_refused():
    jobs = [job(1, "build", "success"), job(2, "sdlc-peer-review"), job(3, "publish", "success")]
    a = answer(jobs, graph=GATED)
    assert a["state"] == "protected" and "also re-runs publish" in a["detail"]


def test_unknown_dependents_still_rerun_with_a_caution_in_preview(monkeypatch):
    a = answer([job(2, "sdlc-peer-review")], graph=None)
    assert a["state"] == "rerun" and a["dependents"] is None


def test_classify_every_way_github_says_no():
    t = 1_000_000.0
    prim = R.classify(Resp(403, {"message": "API rate limit exceeded for user"},
                           {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(t) + 600)}), now=t)
    assert prim["kind"] == "primary_limit" and prim["until"] == t + 600 and "resets at" in prim["text"]
    sec = R.classify(Resp(429, {"message": "slow down"}, {"retry-after": "30", "x-ratelimit-remaining": "4000"}), now=t)
    assert sec["kind"] == "secondary_limit" and sec["until"] == t + 30
    sec2 = R.classify(Resp(403, {"message": "You have exceeded a secondary rate limit."}, {}), now=t)
    assert sec2["kind"] == "secondary_limit" and sec2["until"] == t + 60
    perm = R.classify(Resp(403, {"message": "Resource not accessible by integration"}, {}))
    assert perm["kind"] == "permission" and "Actions: write" in perm["text"]
    old = R.classify(Resp(403, {"message": "Unable to retry this workflow run because it was created over a month ago"}))
    assert old["kind"] == "too_old"
    assert R.classify(Resp(403, {"message": "This workflow is already running"}))["kind"] == "already_running"
    assert R.classify(Resp(404, {"message": "Not Found"}))["kind"] == "not_found"
    assert R.classify(Resp(0, error="ConnectionError: refused"))["kind"] == "network"
    assert R.classify(Resp(502, "bad gateway"))["kind"] == "server"
    assert R.classify(Resp(422, {"message": "odd"}))["kind"] == "refused"
    assert R.classify(Resp(201, None))["kind"] == "ok"


def test_budget_counts_the_post_and_the_follow():
    assert R.budget(None, 8) == {"known": False, "needed": 24, "fits": 8}
    b = R.budget({"remaining": 40, "limit": 1000, "reset": 5}, 20)
    assert b["fits"] == 10 and b["needed"] == 60
    assert R.budget({"remaining": 5, "limit": 1000, "reset": 5}, 3)["fits"] == 0


def test_poll_delay_stretches_as_the_budget_runs_out():
    t = 1000.0
    assert R.poll_delay(5, None, now=t) == 30
    assert R.poll_delay(5, {"remaining": 4000, "reset": t + 3000}, now=t) == 30
    assert R.poll_delay(5, {"remaining": 12, "reset": t + 900}, now=t) == 600     # nothing left: to the reset, capped
    assert R.poll_delay(5, {"remaining": 12, "reset": t + 90}, now=t) == 91
    mid = R.poll_delay(5, {"remaining": 40, "reset": t + 600}, now=t)
    assert 30 < mid < 600


def test_summary_line_names_the_job_the_runs_resolved_to():
    b = {"job": "1691", "status": "finished", "runs": [{"job": "sdlc-peer-review"}],
         "rows": [{"result": "passed"}] * 7 + [{"result": "failed_again"}] + [{"result": "not_started"}] * 2}
    assert R.summary(b)["title"] == "sdlc-peer-review re-run finished: 7 passed, 1 failed again, 2 not started"


# ---------------------------------------------------------------- a scripted GitHub

class FakeApi:
    """Answers by (method, path); a list is consumed in order (its last answer
    repeats). Records every call."""

    def __init__(self, routes, rate=None):
        self.routes = routes
        self._rate = rate
        self.sent = []
        self.calls = 0

    def call(self, method, path, params=None, etag="", accept=""):
        self.calls += 1
        self.sent.append((method, path, etag))
        ans = self.routes.get((method, path))
        if ans is None:
            return Resp(404, {"message": "Not Found"})
        if isinstance(ans, list):
            return ans.pop(0) if len(ans) > 1 else ans[0]
        return ans(etag) if callable(ans) else ans

    def rate(self):
        return self._rate() if callable(self._rate) else self._rate

    def posts(self):
        return [p for m, p, _ in self.sent if m == "POST"]


QUEUE = [("svc-a:1.0", "o/r", 1), ("svc-b:1.0", "o/r", 1), ("svc-c:1.0", "o/r", 2), ("svc-d:1.0", "o/r", 3),
         ("svc-e:1.0", "o/other", 4)]


@pytest.fixture
def queue(monkeypatch):
    items = [{"artifact": a, "requested_by": "dev@example.com", "allowed_failures": "RCTLDEF0001691",
              "build_verified": True, "run_url": f"https://github.com/{repo}/actions/runs/{rid}",
              "repo": repo, "run_id": rid} for a, repo, rid in QUEUE]
    monkeypatch.setattr(R, "candidates", lambda: {"ok": True, "images": items})
    monkeypatch.setattr(R, "START_GAP_SECONDS", 0)
    clock = {"t": 1_000_000.0}

    def sleep(seconds):
        clock["t"] += seconds          # time passes; nothing waits

    monkeypatch.setattr(R, "_sleep", sleep)
    monkeypatch.setattr(R, "_clock", lambda: clock["t"])
    R._plans.clear()
    R._batches.clear()
    events_store.clear_memory()
    return items


def gh_runs(repo_runs, follow_attempt=2, follow_job_concl=None):
    """Routes for runs that fail sdlc-peer-review on attempt 1 and finish the re-run."""
    routes = {}
    follow_job_concl = follow_job_concl or {}
    for repo, rid in repo_runs:
        base = f"/repos/{repo}/actions/runs/{rid}"
        first = Resp(200, {**run(), "html_url": f"https://github.com/{repo}/actions/runs/{rid}"}, {"etag": "e1"})
        done = Resp(200, {**run(attempt=follow_attempt), "html_url": f"https://github.com/{repo}/actions/runs/{rid}"},
                    {"etag": "e2"})
        routes[("GET", base)] = [first, done]
        concl = follow_job_concl.get(rid, "success")
        routes[("GET", base + "/jobs")] = [
            Resp(200, {"jobs": [job(rid * 10, "sdlc-peer-review"), job(rid * 10 + 1, "notify-sdlc", "success")]}),
            Resp(200, {"jobs": [job(rid * 10, "sdlc-peer-review", concl, attempt=follow_attempt,
                                    steps=[("RCTLDEF0001691 peer review evidence", concl)])]}),
        ]
        routes[("GET", f"/repos/{repo}/contents/.github/workflows/b.yml")] = Resp(200, WORKFLOW)
        routes[("POST", f"/repos/{repo}/actions/jobs/{rid * 10}/rerun")] = Resp(201, None)
    return routes


ALL = [("o/r", 1), ("o/r", 2), ("o/r", 3), ("o/other", 4)]
IMAGES = [a for a, _, _ in QUEUE]
RATE = {"remaining": 4000, "limit": 5000, "reset": 2_000_000_000}


def test_preview_reads_each_run_once_and_shares_it(queue):
    api = FakeApi(gh_runs(ALL), rate=RATE)
    p = R.preview("sdlc-peer-review", IMAGES + ["gone:1.0"], api=api, owner="me@example.com")
    assert p["ok"] and p["plan_id"]
    assert [r["run_id"] for r in p["runs"]] == [1, 2, 3, 4]
    assert p["runs"][0]["images"] == ["svc-a:1.0", "svc-b:1.0"]
    rows = {r["artifact"]: r for r in p["rows"]}
    assert rows["svc-a:1.0"]["shared"] and rows["svc-a:1.0"]["state"] == "rerun"
    assert rows["gone:1.0"]["state"] == "unreadable"
    run_reads = [path for m, path, _ in api.sent if path == "/repos/o/r/actions/runs/1"]
    assert len(run_reads) == 1
    assert p["budget"]["fits"] == 4 and p["calls"] == api.calls


def test_preview_with_nothing_to_run_has_no_plan(queue):
    routes = gh_runs([("o/r", 1)])
    routes[("GET", "/repos/o/r/actions/runs/1/jobs")] = Resp(200, {"jobs": [job(10, "sdlc-peer-review", "success")]})
    p = R.preview("sdlc-peer-review", ["svc-a:1.0"], api=FakeApi(routes, rate=RATE))
    assert p["ok"] and "plan_id" not in p and p["rows"][0]["state"] == "passed"


def test_preview_says_which_runs_a_limit_kept_it_from_reading(queue):
    routes = gh_runs(ALL)
    routes[("GET", "/repos/o/r/actions/runs/3")] = Resp(403, {"message": "API rate limit exceeded"},
                                                       {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "2000000000"})
    p = R.preview("sdlc-peer-review", IMAGES, api=FakeApi(routes, rate=RATE))
    row = next(r for r in p["rows"] if r["artifact"] == "svc-d:1.0")
    assert row["state"] == "unreadable" and "API limit" in row["detail"]
    assert len(p["runs"]) == 3


def test_preview_caps_a_batch(queue, monkeypatch):
    monkeypatch.setattr(R.settings, "rerun_max_runs", 2)
    p = R.preview("sdlc-peer-review", IMAGES, api=FakeApi(gh_runs(ALL), rate=RATE))
    assert len(p["runs"]) == 2
    assert {r["state"] for r in p["rows"] if r["run_id"] in (3, 4)} == {"over_cap"}


def test_preview_refuses_empty_input(queue):
    assert not R.preview("", IMAGES)["ok"]
    assert not R.preview("x", [])["ok"]


def test_the_plan_is_single_use_and_bound_to_its_owner(queue):
    api = FakeApi(gh_runs(ALL), rate=RATE)
    p = R.preview("sdlc-peer-review", IMAGES, api=api, owner="me@example.com", thread_id="t1")
    other = R.start(p["plan_id"], owner="you@example.com", thread_id="t1", api=api, background=False)
    assert not other["ok"] and "someone else" in other["error"]
    wrong_thread = R.start(p["plan_id"], owner="me@example.com", thread_id="t2", api=api, background=False)
    assert not wrong_thread["ok"]
    first = R.start(p["plan_id"], owner="me@example.com", thread_id="t1", api=api, background=False)
    assert first["ok"]
    again = R.start(p["plan_id"], owner="me@example.com", thread_id="t1", api=api, background=False)
    assert not again["ok"] and "already used" in again["error"]


def test_an_expired_plan_is_refused(queue, monkeypatch):
    api = FakeApi(gh_runs(ALL), rate=RATE)
    p = R.preview("sdlc-peer-review", IMAGES, api=api)
    R._plans[p["plan_id"]]["created"] -= R.PLAN_TTL_SECONDS + 1
    assert not R.start(p["plan_id"], owner=None, api=api, background=False)["ok"]


@pytest.fixture
def sinks(monkeypatch):
    told, reverified = [], []
    monkeypatch.setattr(notifications, "notify", lambda who, **kw: told.append((who, kw)) or {"ok": True})
    monkeypatch.setattr(release_queue, "queue_enabled", lambda: True)
    monkeypatch.setattr(release_queue, "reverify",
                        lambda art, actor, failing, **kw: reverified.append((art, failing)) or {"ok": True})
    monkeypatch.setattr(R.settings, "queue_allowed_failing_controls", "1691", raising=False)
    return told, reverified


def go(api, images=IMAGES, job_name="sdlc-peer-review", **kw):
    p = R.preview(job_name, images, api=api)
    return R.start(p["plan_id"], owner=None, api=api, background=False, requested_by="dev@example.com", **kw)


def test_happy_batch_starts_follows_tells_once_and_updates_the_queue(queue, sinks):
    told, reverified = sinks
    api = FakeApi(gh_runs(ALL, follow_job_concl={2: "failure"}), rate=RATE)
    out = go(api)
    b = out["batch"]
    assert b["status"] == "finished" and len(api.posts()) == 4
    res = {r["artifact"]: r["result"] for r in b["rows"]}
    assert res == {"svc-a:1.0": "passed", "svc-b:1.0": "passed", "svc-c:1.0": "failed_again",
                   "svc-d:1.0": "passed", "svc-e:1.0": "passed"}
    failed = next(r for r in b["rows"] if r["artifact"] == "svc-c:1.0")
    assert "RCTLDEF0001691" in failed["detail"]
    assert [a for a, _ in reverified] == ["svc-a:1.0", "svc-b:1.0", "svc-d:1.0", "svc-e:1.0"]
    assert all(f == [] for _, f in reverified)
    assert len(told) == 1 and told[0][0] == "dev@example.com"
    assert told[0][1]["title"] == "sdlc-peer-review re-run finished: 4 passed, 1 failed again"
    assert told[0][1]["link"] == f"portal:rerun:{b['id']}" and told[0][1]["severity"] == "warning"


def test_a_304_is_free_and_the_etag_is_sent_back(queue, sinks):
    routes = gh_runs([("o/r", 2)])
    base = "/repos/o/r/actions/runs/2"
    unchanged = Resp(304, None, {})
    done = Resp(200, run(attempt=2), {"etag": "e2"})
    seq = [Resp(200, run(), {"etag": "e1"}), Resp(200, run(), {"etag": "e1"}), unchanged, unchanged, done]
    routes[("GET", base)] = seq
    api = FakeApi(routes, rate=RATE)
    out = go(api, images=["svc-c:1.0"])
    assert out["batch"]["rows"][0]["result"] == "passed"
    etags = [e for m, p, e in api.sent if p == base]
    assert etags[0] == "" and "e1" in etags[2:]


def test_primary_limit_on_a_post_stops_the_rest(queue, sinks):
    routes = gh_runs(ALL)
    routes[("POST", "/repos/o/r/actions/jobs/20/rerun")] = Resp(
        403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "2000000000"})
    api = FakeApi(routes, rate=RATE)
    b = go(api)["batch"]
    assert api.posts() == ["/repos/o/r/actions/jobs/10/rerun", "/repos/o/r/actions/jobs/20/rerun"]
    res = {r["artifact"]: (r["result"], r.get("detail", "")) for r in b["rows"]}
    assert res["svc-a:1.0"][0] == "passed"
    for art in ("svc-c:1.0", "svc-d:1.0", "svc-e:1.0"):
        assert res[art][0] == "not_started" and "API limit" in res[art][1]
    assert "API limit" in b["stopped_starts"]


def test_secondary_limit_on_a_post_stops_the_rest_without_retrying(queue, sinks):
    routes = gh_runs(ALL)
    routes[("POST", "/repos/o/r/actions/jobs/10/rerun")] = Resp(
        403, {"message": "You have exceeded a secondary rate limit"}, {"retry-after": "60"})
    api = FakeApi(routes, rate=RATE)
    b = go(api)["batch"]
    assert len(api.posts()) == 1
    assert {r["result"] for r in b["rows"]} == {"not_started"}
    assert all("slow down" in r["detail"] for r in b["rows"])


def test_a_permission_refusal_stops_that_repo_only(queue, sinks):
    routes = gh_runs(ALL)
    routes[("POST", "/repos/o/r/actions/jobs/10/rerun")] = Resp(403, {"message": "Resource not accessible by integration"})
    api = FakeApi(routes, rate=RATE)
    b = go(api)["batch"]
    assert api.posts() == ["/repos/o/r/actions/jobs/10/rerun", "/repos/o/other/actions/jobs/40/rerun"]
    res = {r["artifact"]: r["result"] for r in b["rows"]}
    assert res["svc-e:1.0"] == "passed"
    assert {res[a] for a in ("svc-a:1.0", "svc-c:1.0", "svc-d:1.0")} == {"not_started"}


def test_one_bad_post_does_not_stop_the_others(queue, sinks):
    routes = gh_runs(ALL)
    routes[("POST", "/repos/o/r/actions/jobs/20/rerun")] = Resp(0, error="ConnectionError: reset")
    routes[("POST", "/repos/o/r/actions/jobs/30/rerun")] = Resp(403, {"message": "This workflow is already running"})
    api = FakeApi(routes, rate=RATE)
    b = go(api)["batch"]
    assert len(api.posts()) == 4
    res = {r["artifact"]: (r["result"], r.get("detail", "")) for r in b["rows"]}
    assert res["svc-c:1.0"][0] == "not_started" and "could not be reached" in res["svc-c:1.0"][1]
    assert res["svc-d:1.0"] == ("not_started", "already re-running")
    assert res["svc-e:1.0"][0] == "passed"


def test_the_budget_decides_how_many_start(queue, sinks):
    api = FakeApi(gh_runs(ALL), rate={"remaining": 17, "limit": 1000, "reset": 2_000_000_000})
    b = go(api)["batch"]
    assert len(api.posts()) == 2
    assert sum(r["result"] == "not_started" for r in b["rows"]) == 2
    assert any("not enough GitHub API calls left (17 left" in r.get("detail", "") for r in b["rows"])


def test_a_limit_while_following_waits_for_the_reset_when_it_can(queue, sinks, monkeypatch):
    routes = gh_runs([("o/r", 2)])
    base = "/repos/o/r/actions/runs/2"
    clock = {"t": 1_000_000.0}
    limit = Resp(403, {"message": "API rate limit exceeded"},
                 {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(clock["t"]) + 300)})
    routes[("GET", base)] = [Resp(200, run(), {"etag": "e1"}), limit, Resp(200, run(attempt=2), {"etag": "e2"})]
    api = FakeApi(routes, rate=RATE)
    p = R.preview("sdlc-peer-review", ["svc-c:1.0"], api=api)
    R.start(p["plan_id"], owner=None, api=api, background=False)  # plan spent; rerun below by hand
    batch = list(R._batches.values())[0]
    # replay with a fake clock: the waits are recorded, not slept
    slept = []
    batch.update(status="starting", lines=[], stopped="")
    for r_ in batch["runs"]:
        r_.update(state="pending")
    routes[("GET", base)] = [limit, Resp(200, run(attempt=2), {"etag": "e2"})]
    routes[("GET", base + "/jobs")] = [Resp(200, {"jobs": [job(20, "sdlc-peer-review", "success", attempt=2)]})]

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    R.run_batch(batch, api, lambda: False, sleep=sleep, clock=lambda: clock["t"])
    assert batch["status"] == "finished" and batch["rows"][0]["result"] == "passed"
    assert any(s >= 299 for s in slept)
    assert any("waiting — GitHub API limit" in ln["text"] for ln in batch["lines"])


def test_a_limit_past_the_deadline_stops_following_and_says_so(queue, sinks, monkeypatch):
    told, _ = sinks
    monkeypatch.setattr(R.settings, "session_idle_minutes", 5, raising=False)
    routes = gh_runs([("o/r", 2)])
    base = "/repos/o/r/actions/runs/2"
    routes[("GET", base)] = [Resp(200, run(), {"etag": "e1"}),
                             Resp(403, {"message": "API rate limit exceeded"},
                                  {"x-ratelimit-remaining": "0", "x-ratelimit-reset": "9999999999"})]
    api = FakeApi(routes, rate=RATE)
    b = go(api, images=["svc-c:1.0"])["batch"]
    assert b["status"] == "stopped" and b["rows"][0]["result"] == "unfollowed"
    assert "stopped following — GitHub API limit" in b["stopped"]
    assert "not followed to the end" in told[0][1]["title"]


def test_a_dropped_session_token_stops_the_follow_without_another_call(queue, sinks):
    told, _ = sinks
    api = FakeApi(gh_runs([("o/r", 2)]), rate=RATE)
    b = go(api, images=["svc-c:1.0"], pat_gone=lambda: True)["batch"]
    follows = [p for m, p, e in api.sent if p == "/repos/o/r/actions/runs/2"]
    assert len(follows) == 1                         # the preview read only
    assert b["rows"][0]["result"] == "unfollowed" and "token was dropped" in b["rows"][0]["detail"]
    assert len(told) == 1


def test_a_run_that_never_reaches_the_new_attempt_is_not_read_as_done(queue, sinks, monkeypatch):
    monkeypatch.setattr(R.settings, "session_idle_minutes", 1, raising=False)
    routes = gh_runs([("o/r", 2)])
    routes[("GET", "/repos/o/r/actions/runs/2")] = [Resp(200, run(), {"etag": "e1"})]   # attempt 1, completed, forever
    api = FakeApi(routes, rate=RATE)
    clock = {"t": 0.0}

    def sleep(s):
        clock["t"] += s

    p = R.preview("sdlc-peer-review", ["svc-c:1.0"], api=api)
    R._take_plan(p["plan_id"], None, "")
    batch = {"id": "b1", "job": "sdlc-peer-review", "requested_by": "", "status": "starting", "lines": [],
             "stopped": "", "runs": [dict(r, state="pending") for r in p["runs"]],
             "rows": [dict(r, result="pending") for r in p["rows"]]}
    R.run_batch(batch, api, lambda: False, sleep=sleep, clock=lambda: clock["t"])
    assert batch["rows"][0]["result"] == "running" and "still running after 1 min" in batch["stopped"]


def test_a_control_not_allowed_to_fail_leaves_the_queue_alone(queue, sinks, monkeypatch):
    _, reverified = sinks
    routes = gh_runs([("o/r", 2)])
    routes[("GET", "/repos/o/r/actions/runs/2/jobs")] = [
        Resp(200, {"jobs": [job(20, "sdlc-peer-review")]}),
        Resp(200, {"jobs": [job(20, "sdlc-peer-review", "success", attempt=2),
                            job(21, "scan", "failure", steps=[("RCTLDEF0009999 scan", "failure")])]})]
    b = go(FakeApi(routes, rate=RATE), images=["svc-c:1.0"])["batch"]
    assert b["rows"][0]["result"] == "passed" and reverified == []
    assert "not allowed to fail" in b["rows"][0]["queue"]


def test_the_card_reads_a_finished_batch_after_a_restart_and_a_lost_one_honestly(queue, sinks):
    api = FakeApi(gh_runs([("o/r", 2)]), rate=RATE)
    bid = go(api, images=["svc-c:1.0"])["batch_id"]
    R._batches.clear()                                # the process restarted
    got = R.get_batch(bid)
    assert got["ok"] and not got["live"] and got["batch"]["status"] == "finished"
    events_store.record({"domain": "rerun", "event_type": "started", "subject": "rerun:lost1",
                         "details": json.dumps({"id": "lost1", "job": "j", "status": "following", "rows": []})})
    lost = R.get_batch("lost1")["batch"]
    assert lost["status"] == "stopped" and "portal restarted" in lost["stopped"]
    assert not R.get_batch("nope")["ok"]


# ---------------------------------------------------------------- the queue side

def test_reverified_changes_only_the_same_version_still_queued():
    ev = [
        {"event_type": "queued", "artifact_name": "a", "artifact_version": "1", "event_ts": "1",
         "allowed_failures": "RCTLDEF0001691", "build_verified": True},
        {"event_type": "queued", "artifact_name": "b", "artifact_version": "2", "event_ts": "2",
         "allowed_failures": "RCTLDEF0001691"},
        {"event_type": "reverified", "artifact_name": "a", "artifact_version": "1", "event_ts": "3"},
        {"event_type": "reverified", "artifact_name": "b", "artifact_version": "1", "event_ts": "4"},
        {"event_type": "reverified", "artifact_name": "c", "artifact_version": "1", "event_ts": "5"},
    ]
    q = {x["artifact_name"]: x for x in release_queue.reduce_queue(ev)}
    assert q["a"]["allowed_failures"] == "" and q["a"]["reverified_at"] == "3" and q["a"]["requested_at"] == "1"
    assert q["b"]["allowed_failures"] == "RCTLDEF0001691"
    assert "c" not in q


def test_reverify_writes_one_append_only_event(monkeypatch):
    rows = []
    monkeypatch.setattr(release_queue, "_insert", lambda r: rows.extend(r) or {"ok": True})
    assert release_queue.reverify("a:1", "dev@example.com", [], build_run_url="u")["ok"]
    assert rows[0]["event_type"] == "reverified" and "allowed_failures" not in rows[0]
    release_queue.reverify("a:1", "dev@example.com", ["RCTLDEF0001691"])
    assert rows[1]["allowed_failures"] == "RCTLDEF0001691"
    assert not release_queue.reverify("nope", "x", [])["ok"]


def test_a_budget_too_low_to_poll_stops_at_the_deadline_and_says_why(queue, sinks, monkeypatch):
    monkeypatch.setattr(R.settings, "session_idle_minutes", 3, raising=False)
    routes = gh_runs([("o/r", 2)])
    routes[("GET", "/repos/o/r/actions/runs/2")] = [Resp(200, run(), {"etag": "e1"})]
    low = {"remaining": 12, "limit": 1000, "reset": 1_000_000 + 3000}
    seen = []
    api = FakeApi(routes, rate=lambda: (seen.append(1), RATE if len(seen) == 1 else low)[1])   # spent after the start
    p = R.preview("sdlc-peer-review", ["svc-c:1.0"], api=FakeApi(gh_runs([("o/r", 2)]), rate=RATE))
    slept = []
    clock = {"t": 1_000_000.0}

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    R._take_plan(p["plan_id"], None, "")
    batch = {"id": "b2", "job": "sdlc-peer-review", "requested_by": "", "status": "starting", "lines": [],
             "stopped": "", "runs": [dict(r, state="pending") for r in p["runs"]],
             "rows": [dict(r, result="pending") for r in p["rows"]]}
    R.run_batch(batch, api, lambda: False, sleep=sleep, clock=lambda: clock["t"])
    assert all(s <= 181 for s in slept)                      # never past the 3-minute deadline
    assert batch["status"] == "stopped" and "budget ran low (12 left" in batch["stopped"]
    assert batch["rows"][0]["result"] == "unfollowed"


def test_the_real_already_running_answer_is_not_read_as_a_limit():
    # GitHub's own words, captured live: a 403 with budget left.
    c = R.classify(Resp(403, {"message": "The workflow run containing this job is already running"},
                        {"x-ratelimit-remaining": "4822", "x-ratelimit-resource": "core"}))
    assert c["kind"] == "already_running"
