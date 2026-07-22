"""Intake queue: event reduction, routing veto, deployment-repo plumbing."""
from release_agent.agent.parsing import is_queue_intent
from release_agent.tools import release_queue as RQ
from release_agent.tools import release_fileset as RF


def _ev(etype, name, ts, **over):
    base = {
        "event_type": etype,
        "artifact_name": name,
        "event_ts": ts,
        "artifact_version": "1.0.0",
        "requested_by": "dev@db.com",
        "prl1_only": False,
        "df_only": False,
        "note": "",
        "deployment_repo": "",
        "release_name": None,
        "pr_number": None,
        "build_verified": None,
    }
    base.update(over)
    return base


def test_reduce_queue_latest_event_wins():
    events = [
        _ev("queued", "svc-a", "2026-07-13T10:00:00", artifact_version="1.0.0"),
        _ev("queued", "svc-b", "2026-07-14T09:00:00", prl1_only=True),
        # dev bumps svc-a's version by re-queuing — latest wins
        _ev("queued", "svc-a", "2026-07-15T08:00:00", artifact_version="1.0.1"),
        # svc-b withdrawn
        _ev("withdrawn", "svc-b", "2026-07-15T09:00:00"),
    ]
    queue = RQ.reduce_queue(events)
    assert [q["artifact_name"] for q in queue] == ["svc-a"]
    assert queue[0]["artifact_version"] == "1.0.1"


def test_reduce_queue_drains_on_release_and_requeues():
    events = [
        _ev("queued", "svc-a", "2026-07-13T10:00:00"),
        _ev("released", "svc-a", "2026-07-16T12:00:00", release_name="Release 32", pr_number=109),
        # re-queued after shipping → next release's queue
        _ev("queued", "svc-a", "2026-07-17T10:00:00", artifact_version="1.1.0"),
    ]
    queue = RQ.reduce_queue(events)
    assert len(queue) == 1 and queue[0]["artifact_version"] == "1.1.0"
    assert RQ.last_shipped(events, "svc-a")["release_name"] == "Release 32"


def test_last_queued_flags_skips_current_entry():
    events = [
        _ev("queued", "svc-a", "2026-07-01T10:00:00", prl1_only=True),
        _ev("released", "svc-a", "2026-07-02T10:00:00"),
        _ev("queued", "svc-a", "2026-07-15T10:00:00"),  # the entry just written
    ]
    assert RQ.last_queued_flags(events, "svc-a") == {"prl1_only": True, "df_only": False}


def test_split_artifact_handles_urls_and_bare():
    assert RQ._split_artifact("acme-x:1.2.3") == ("acme-x", "1.2.3")
    assert RQ._split_artifact("https://art.example/com/db/acme-x:1.2.3") == ("acme-x", "1.2.3")
    assert RQ._split_artifact("no-version") == ("no-version", "")


def test_queue_intent_veto_routes_to_chat():
    assert is_queue_intent("add acme-risk-fetcher:4.0.153 to the next release prl1 only")
    assert is_queue_intent("queue acme-x:1.2.3 for thursday")
    assert is_queue_intent("what's in the release queue?")
    assert not is_queue_intent("deploy acme-x:1.2.3 to uat")
    assert not is_queue_intent("release prod")


def test_reduce_queue_carries_change_context():
    events = [
        _ev("queued", "svc-a", "2026-07-13T10:00:00",
            jira_ticket="REL-1234", change_details="fixes schema drift"),
    ]
    q = RQ.reduce_queue(events)[0]
    assert q["jira_ticket"] == "REL-1234"
    assert q["change_details"] == "fixes schema drift"


def test_add_intent_normalizes_jira(monkeypatch):
    captured = {}

    def _fake_insert(rows):
        captured["row"] = rows[0]
        return {"ok": True}

    monkeypatch.setattr(RQ, "_insert", _fake_insert)
    RQ.add_intent("svc-a:1.0.0", "dev@db.com", jira_ticket="rel-1234",
                  change_details="  why text  ")
    assert captured["row"]["jira_ticket"] == "REL-1234"
    assert captured["row"]["change_details"] == "why text"


def test_reduce_queue_ignores_deployed_events():
    events = [
        _ev("queued", "svc-a", "2026-07-13T10:00:00"),
        _ev("deployed", "svc-b", "2026-07-14T10:00:00", environment="uat"),
        _ev("deployed", "svc-a", "2026-07-14T11:00:00", environment="uat"),
    ]
    queue = RQ.reduce_queue(events)
    # svc-a stays queued (a UAT deploy is not a release); svc-b never enters
    assert [q["artifact_name"] for q in queue] == ["svc-a"]


def test_record_deployment_rows(monkeypatch):
    captured = {}
    def _fake_insert(rows):
        captured["rows"] = rows
        return {"ok": True}

    monkeypatch.setattr(RQ, "_insert", _fake_insert)
    out = RQ.record_deployment(
        environment="uat",
        artifacts=[{"name": "svc-a", "tag": "1.0.0"}, {"name": "svc-b", "tag": "2.0.0"}],
        deployment_repo="org/deploy-repo",
        pr_number=77,
        note="applied",
    )
    assert out["ok"] is True
    rows = captured["rows"]
    assert len(rows) == 2
    assert all(r["event_type"] == "deployed" for r in rows)
    assert all(r["environment"] == "uat" for r in rows)
    assert all(r["deployment_repo"] == "org/deploy-repo" for r in rows)
    assert all(r["pr_number"] == 77 for r in rows)


def test_aggregate_history_pattern_and_counts():
    events = [
        _ev("released", "acme-capability-svc", "2026-07-01T10:00:00",
            artifact_version="1.2.0", release_name="Release 30", pr_number=100),
        _ev("released", "acme-capability-svc", "2026-07-10T10:00:00",
            artifact_version="1.3.0", release_name="Release 31", pr_number=105),
        _ev("released", "acme-workflow-service", "2026-07-10T10:00:00",
            artifact_version="4.0.66", release_name="Release 31", pr_number=105),
        _ev("deployed", "acme-capability-svc", "2026-07-11T10:00:00", environment="uat"),
        _ev("queued", "acme-capability-svc", "2026-07-12T10:00:00"),
    ]
    # glob pattern, released only
    out = RQ.aggregate_history(events, pattern="acme-capability*")
    assert out["total_events"] == 2 and out["chart_count"] == 1
    cap = out["charts"][0]
    assert cap["count"] == 2
    assert cap["versions"] == ["1.2.0", "1.3.0"]
    assert [r["release"] for r in cap["releases"]] == ["Release 30", "Release 31"]

    # substring match + all charts ranked by count
    out = RQ.aggregate_history(events, pattern="acme")
    assert [c["artifact_name"] for c in out["charts"]] == [
        "acme-capability-svc", "acme-workflow-service"
    ]

    # deployed lens carries the environment counts
    out = RQ.aggregate_history(events, pattern="", event_types=("deployed",))
    assert out["charts"][0]["environments"] == {"uat": 1}


def test_pattern_match_modes():
    assert RQ._pattern_match("acme-capability-svc", "")
    assert RQ._pattern_match("acme-capability-svc", "acme-capability*")
    assert RQ._pattern_match("acme-capability-svc", "capability")
    assert not RQ._pattern_match("acme-workflow-service", "acme-capability*")


def test_workflow_chart_entries_parses_multi_deployments_json():
    wf = """
name: deploy prd
jobs:
  deploy:
    with:
      multi_deployments_json: |
        [
          {
            "helm_chart_name": "acme-capability-consumerA",
            "helm_chart_version": "1.2.0",
            "gke_namespace": "apps1"
          },
          {
            "helm_chart_name": "acme-workflow-service",
            "helm_chart_version": "4.0.67"
          }
        ]
"""
    assert RF._workflow_chart_entries(wf) == [
        {"name": "acme-capability-consumerA", "tag": "1.2.0"},
        {"name": "acme-workflow-service", "tag": "4.0.67"},
    ]
    assert RF._workflow_chart_entries("") == []


def test_aggregate_env_state_latest_event_wins():
    events = [
        # consumerA reaches uat and prl1; consumerB only uat, later removed
        _ev("deployed", "acme-capability-consumerA", "2026-07-01T10:00:00",
            environment="uat", artifact_version="1.0.0"),
        _ev("deployed", "acme-capability-consumerB", "2026-07-02T10:00:00", environment="uat"),
        _ev("deployed", "acme-capability-consumerA", "2026-07-03T10:00:00", environment="prl1"),
        # version bump on uat — latest wins
        _ev("deployed", "acme-capability-consumerA", "2026-07-05T10:00:00",
            environment="uat", artifact_version="1.1.0"),
        _ev("removed", "acme-capability-consumerB", "2026-07-06T10:00:00", environment="uat"),
        # legacy 'prod' rows normalize into prd
        _ev("deployed", "acme-capability-consumerA", "2026-07-07T10:00:00", environment="prod"),
        # non-matching chart excluded by pattern
        _ev("deployed", "acme-workflow-service", "2026-07-07T11:00:00", environment="uat"),
    ]
    out = RQ.aggregate_env_state(events, pattern="acme-capability*")
    envs = {e["environment"]: e for e in out["environments"]}
    assert envs["uat"]["count"] == 1  # consumerB removed
    assert envs["uat"]["images"][0]["version"] == "1.1.0"
    assert envs["prl1"]["count"] == 1
    assert envs["prd"]["count"] == 1  # 'prod' normalized
    assert out["distinct_images"] == 1  # only consumerA still deployed anywhere


def test_bq_fully_optional_when_disabled():
    """BQ is OPTIONAL: with no dataset configured every entry point returns a
    calm disabled dict — never an exception — so releases/deploys (whose queue
    writes are best-effort) and the UI (which renders the disabled shape) work
    without the table existing at all. (conftest blanks bq_dataset globally.)"""
    assert RQ.queue_enabled() is False
    for result in (
        RQ.current_queue(),
        RQ.add_intent("svc-a:1.0.0", "dev@db.com"),
        RQ.withdraw_intent("svc-a", "dev@db.com"),
        RQ.record_deployment("uat", [{"name": "svc-a", "tag": "1.0.0"}]),
        RQ.mark_released("R1", 1, [{"name": "svc-a", "tag": "1.0.0"}]),
        RQ.history_stats(),
        RQ.recent_deployments(),
    ):
        assert result["ok"] is False and result.get("disabled") is True
    assert RQ.cached_queue_count() is None  # banner simply omits the count


def test_bq_unreachable_degrades_not_raises(monkeypatch):
    """Enabled-but-broken (missing table / network down): error dicts, never
    exceptions, so a BQ outage can never block a release or deploy."""
    monkeypatch.setattr(RQ.settings, "bq_dataset", "release_agent", raising=False)
    monkeypatch.setattr(RQ.settings, "gcp_project", "some-project", raising=False)

    def _boom(*a, **k):
        raise RuntimeError("table not found")

    monkeypatch.setattr(RQ, "_get_client", _boom)
    monkeypatch.setattr(RQ, "_fetch_events", _boom)
    assert RQ.current_queue()["ok"] is False
    assert "unavailable" in RQ.add_intent("svc-a:1.0.0", "dev@db.com")["error"].lower()
    assert RQ.record_deployment("uat", [{"name": "s", "tag": "1"}])["ok"] is False
    assert RQ.history_stats()["ok"] is False
    assert RQ.cached_queue_count() is None


def test_queue_intent_blocks_ineligible_build(monkeypatch):
    """A build_run_url whose run failed its build or a control makes the chart
    INELIGIBLE: nothing is queued and the result names what failed."""
    from adk_release_agent import tools as T

    inserted = []
    monkeypatch.setattr(RQ, "add_intent", lambda **kw: inserted.append(kw) or {"ok": True})

    def _report(tool, args):
        assert tool == "get_build_report"
        return {
            "found": True, "run_succeeded": False, "gate": "FAIL",
            "run": {"url": args["workflow_url"], "conclusion": "failure"},
            "controls": [
                {"control": "RLFT approval gate", "passed": True, "failed": False},
                {"control": "RFTL deploy control", "passed": False, "failed": True},
            ],
            "failed_steps": [{"job": "build", "name": "Build image", "conclusion": "failure"}],
        }

    monkeypatch.setattr(T, "_invoke_tool", _report)
    out = T.queue_release_intent(
        "svc-a:1.0.0", "dev@db.com", build_run_url="https://gh/actions/runs/1"
    )
    assert out["ok"] is False and out["eligible"] is False
    assert out["failed_controls"] == ["RFTL deploy control"]
    assert inserted == []  # nothing written to the queue


def test_queue_intent_eligible_build_queues_verified(monkeypatch):
    from adk_release_agent import tools as T

    inserted = {}
    monkeypatch.setattr(RQ, "add_intent", lambda **kw: inserted.update(kw) or {"ok": True})
    monkeypatch.setattr(RQ, "_fetch_events", lambda *a, **k: [])
    monkeypatch.setattr(T, "_invoke_tool", lambda tool, args: {
        "found": True, "run_succeeded": True, "gate": "PASS",
        "run": {"url": args["workflow_url"], "conclusion": "success"},
        "controls": [{"control": "RLFT approval gate", "passed": True, "failed": False}],
        "failed_steps": [], "tag": "svc-a-1.0.0",
    })
    out = T.queue_release_intent(
        "svc-a:1.0.0", "dev@db.com", build_run_url="https://gh/actions/runs/2"
    )
    assert out["ok"] is True and out["eligible"] is True
    assert inserted["build_verified"] is True
    assert inserted["build_run_url"] == "https://gh/actions/runs/2"


def test_validate_release_deployment_repo():
    payload = {
        "release_name": "R1",
        "start_date": "2026-07-20 10:00:00",
        "end_date": "2026-07-21 10:00:00",
        "change_initiator": "dev@db.com",
        "change_summary": "R1",
        "artefact": ["svc-a:1.0.0"],
    }
    details, errors = RF.validate_release({**payload, "deployment_repo": "org/deploy-repo"})
    assert errors == []
    # kept OUT of release_details.json — the live script's input shape is sacred
    assert "deployment_repo" not in details

    _, errors = RF.validate_release({**payload, "deployment_repo": "not-a-repo"})
    assert any("deployment_repo" in e for e in errors)
