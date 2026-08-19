"""CARE and DF releases are independent: separate repos, separate queues.

A Dataflow release is raised in its own GitOps repo, so it must not pick up CARE
artifacts, must not default to the CARE repo, and must not be blocked by (or
block) an in-flight CARE release.
"""
import pytest

from release_agent import app_fastapi as APP
from release_agent.config import settings
from release_agent.tools import release_fileset as RF


@pytest.fixture
def repos(monkeypatch):
    def _set(deploy_repo, df_release_repo):
        monkeypatch.setattr(settings, "deploy_repo", deploy_repo)
        monkeypatch.setattr(settings, "df_release_repo", df_release_repo)
        monkeypatch.setattr(APP.app_settings, "deploy_repo", deploy_repo, raising=False)
        monkeypatch.setattr(APP.app_settings, "df_release_repo", df_release_repo, raising=False)
        monkeypatch.setattr(APP, "active_deploy_repo", lambda: deploy_repo, raising=False)

    return _set


def _queue_ctx(monkeypatch, deploy_repo, df_release_repo):
    """The context the release forms render from."""
    monkeypatch.setattr(APP.app_settings, "deploy_repo", deploy_repo, raising=False)
    monkeypatch.setattr(APP.app_settings, "df_release_repo", df_release_repo, raising=False)
    monkeypatch.setattr(
        "release_agent.tools.release_queue.current_queue",
        lambda *a, **k: {"ok": True, "queue": []},
    )
    monkeypatch.setattr(APP, "_known_charts", lambda: [])
    return APP.release_queue_get()


def test_df_release_defaults_to_its_own_repo(monkeypatch):
    ctx = _queue_ctx(monkeypatch, "acme/care-deploy", "acme/df-deploy")
    assert ctx["default_repo"] == "acme/care-deploy"
    assert ctx["df_default_repo"] == "acme/df-deploy"


def test_single_repo_setups_still_work(monkeypatch):
    """DF_RELEASE_REPO unset = one repo for both, i.e. the previous behaviour."""
    ctx = _queue_ctx(monkeypatch, "acme/deploy", "")
    assert ctx["df_default_repo"] == ctx["default_repo"] == "acme/deploy"


def test_the_in_flight_guard_is_checked_against_the_target_repo(monkeypatch, repos):
    """An open CARE release must not block a DF release raised elsewhere — the
    guard reads the repo the release actually targets, not the configured one."""
    repos("acme/care-deploy", "acme/df-deploy")
    checked = []

    class _Repo:
        def __init__(self, full):
            self.full = full

    monkeypatch.setattr(
        RF, "_get_github_client",
        lambda: type("C", (), {"get_repo": staticmethod(lambda full: _Repo(full))})(),
    )

    def _blocker(repo, exclude_head=""):
        checked.append(repo.full)
        return None                      # no blocker in either repo

    monkeypatch.setattr(RF, "_open_prd_pr_blocker", _blocker)
    # stop right after the guard — the clone is not what this test is about
    monkeypatch.setattr(
        RF, "validate_release",
        lambda p: ({**p, "release_name": "R1", "change_initiator": "d@e.com"}, []),
    )
    monkeypatch.setattr(RF.tempfile, "mkdtemp", lambda **kw: (_ for _ in ()).throw(RuntimeError("stop")))

    for repo in ("acme/df-deploy", "acme/care-deploy"):
        with pytest.raises(RuntimeError, match="stop"):   # guard passed, clone begins
            RF.prepare_release_fileset({"deployment_repo": repo})

    assert checked == ["acme/df-deploy", "acme/care-deploy"]


def test_the_banner_reports_care_and_df_separately(monkeypatch):
    """One release open must not be reported as the other's — they are different
    repos with different PRs and different guards."""
    calls = []

    def _status(deployment_repo: str = ""):
        calls.append(deployment_repo)
        if deployment_repo == "acme/df-deploy":
            return {"prd_release_pr": {"number": 7, "url": "u7", "charts": []},
                    "prd_charts": [], "blocking_pr": None}
        return {"prd_release_pr": None, "prd_charts": [{"helm_chart_name": "a"}],
                "blocking_pr": None}

    monkeypatch.setattr("release_agent.tools.gh_tools.get_release_status", _status)
    monkeypatch.setattr(APP.app_settings, "deploy_repo", "acme/care-deploy", raising=False)
    monkeypatch.setattr(APP.app_settings, "df_release_repo", "acme/df-deploy", raising=False)
    monkeypatch.setattr(APP, "_status_cache", {"at": 0.0, "value": None}, raising=False)

    out = APP.release_status_endpoint(fresh=1)
    assert out["prd_release_pr"] is None                 # CARE has none open
    assert out["df"]["prd_release_pr"]["number"] == 7    # DF does
    assert sorted(c for c in calls) == ["", "acme/df-deploy"]


def test_a_df_failure_does_not_blank_the_care_status(monkeypatch):
    def _status(deployment_repo: str = ""):
        if deployment_repo:
            raise RuntimeError("DF repo unreachable")
        return {"prd_release_pr": None, "prd_charts": [], "blocking_pr": None}

    monkeypatch.setattr("release_agent.tools.gh_tools.get_release_status", _status)
    monkeypatch.setattr(APP.app_settings, "deploy_repo", "acme/care-deploy", raising=False)
    monkeypatch.setattr(APP.app_settings, "df_release_repo", "acme/df-deploy", raising=False)
    monkeypatch.setattr(APP, "_status_cache", {"at": 0.0, "value": None}, raising=False)

    out = APP.release_status_endpoint(fresh=1)
    assert "error" not in out                            # CARE still reported
    assert "unreachable" in out["df"]["error"]


def test_single_repo_setups_do_not_pay_for_a_second_read(monkeypatch):
    calls = []

    def _status(deployment_repo: str = ""):
        calls.append(deployment_repo)
        return {"prd_release_pr": None, "prd_charts": [], "blocking_pr": None}

    monkeypatch.setattr("release_agent.tools.gh_tools.get_release_status", _status)
    monkeypatch.setattr(APP.app_settings, "deploy_repo", "acme/deploy", raising=False)
    monkeypatch.setattr(APP.app_settings, "df_release_repo", "", raising=False)
    monkeypatch.setattr(APP, "_status_cache", {"at": 0.0, "value": None}, raising=False)

    out = APP.release_status_endpoint(fresh=1)
    assert calls == [""]
    assert "df" not in out


# ---------------------------------------------- multi-chart intake (batch)

def _row(artifact, run="https://gh/actions/runs/1", jira="ABC-1"):
    return {"artifact": artifact, "build_run_url": run, "jira_ticket": jira}


def _batch(monkeypatch, outcomes):
    """POST /api/release-queue/batch with queue_release_intent stubbed per artifact."""
    calls = []

    def _queue(**kw):
        calls.append(kw)
        return outcomes[kw["artifact"]]

    monkeypatch.setattr("adk_release_agent.tools.queue_release_intent", _queue)
    return calls


def test_a_failed_control_on_one_row_does_not_discard_the_others(monkeypatch):
    """Partial success: losing a developer's good rows because a sibling failed
    a control is the worse outcome — but it splits one change, so say so."""
    _batch(monkeypatch, {
        "a:1": {"ok": True, "eligible": True},
        "b:2": {"ok": False, "eligible": False,
                "failed_controls": ["RCTLDEF0000043"],
                "failed_controls_detail": [{"control": "RCTLDEF0000043", "job": "build"}]},
    })
    out = APP.release_queue_add_batch(APP.QueueBatchRequest(
        rows=[APP.QueueRow(**_row("a:1")), APP.QueueRow(**_row("b:2"))],
        requested_by="dev@acme.com", change_details="d",
    ))
    assert out["ok"] is True
    assert [q["artifact"] for q in out["queued"]] == ["a:1"]
    assert [r["artifact"] for r in out["refused"]] == ["b:2"]
    assert out["split"] is True          # the caller must be able to say so


def test_all_rows_refused_is_not_reported_as_a_split(monkeypatch):
    _batch(monkeypatch, {"a:1": {"ok": False, "error": "nope"}})
    out = APP.release_queue_add_batch(APP.QueueBatchRequest(
        rows=[APP.QueueRow(**_row("a:1"))], requested_by="d@e.com", change_details="d"))
    assert out["ok"] is False and out["split"] is False


def test_each_row_carries_its_own_ticket_and_run(monkeypatch):
    """One build run builds one tag, and a change spanning charts often spans
    tickets — so neither can be a shared field."""
    calls = _batch(monkeypatch, {"a:1": {"ok": True}, "b:2": {"ok": True}})
    APP.release_queue_add_batch(APP.QueueBatchRequest(
        rows=[
            APP.QueueRow(**_row("a:1", run="https://gh/actions/runs/11", jira="ABC-1")),
            APP.QueueRow(**_row("b:2", run="https://gh/actions/runs/22", jira="ABC-2")),
        ],
        requested_by="dev@acme.com", change_details="one change, two charts", note="n",
    ))
    by_artifact = {c["artifact"]: c for c in calls}
    assert by_artifact["a:1"]["build_run_url"].endswith("/11")
    assert by_artifact["b:2"]["build_run_url"].endswith("/22")
    assert by_artifact["a:1"]["jira_ticket"] == "ABC-1"
    assert by_artifact["b:2"]["jira_ticket"] == "ABC-2"
    # shared context reaches every row
    assert all(c["change_details"] == "one change, two charts" for c in calls)


def test_one_exploding_row_does_not_kill_the_batch(monkeypatch):
    def _queue(**kw):
        if kw["artifact"] == "boom:1":
            raise RuntimeError("github exploded")
        return {"ok": True}

    monkeypatch.setattr("adk_release_agent.tools.queue_release_intent", _queue)
    out = APP.release_queue_add_batch(APP.QueueBatchRequest(
        rows=[APP.QueueRow(**_row("boom:1")), APP.QueueRow(**_row("fine:1"))],
        requested_by="d@e.com", change_details="d"))
    assert [q["artifact"] for q in out["queued"]] == ["fine:1"]
    assert "github exploded" in out["refused"][0]["error"]


def test_an_empty_submission_is_refused():
    out = APP.release_queue_add_batch(APP.QueueBatchRequest(
        rows=[], requested_by="d@e.com", change_details="d"))
    assert out["ok"] is False and "at least one" in out["error"]
