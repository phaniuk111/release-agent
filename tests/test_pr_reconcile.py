"""Settling PRs the portal raised but could not merge (pr_reconcile)."""
import datetime as dt
from types import SimpleNamespace

import pytest

from release_agent.tools import pr_reconcile as R

REPO = "example-org/deploy"


def _pending(pr, name="svc-a", version="2.0.0", env="uat", on_merge="deployed", tag="deployed",
             ts="2026-01-01T10:00:00+00:00"):
    return {"event_type": "pending", "event_ts": ts, "artifact_name": name,
            "artifact_version": version, "environment": env, "deployment_repo": REPO,
            "pr_number": pr, "note": R.pending_note(on_merge, tag)}


class _PR:
    def __init__(self, merged=False, state="open", when=None, by="approver"):
        self.merged, self.state = merged, state
        self.merged_at = when if merged else None
        self.closed_at = when if state == "closed" else None
        self.merged_by = SimpleNamespace(login=by) if merged else None


class _GitHub:
    def __init__(self, prs):
        self.prs, self.reads = prs, []

    def get_repo(self, full):
        gh = self

        class _Repo:
            def get_pull(self, n):
                gh.reads.append(n)
                pr = gh.prs[n]
                if isinstance(pr, Exception):
                    raise pr
                return pr
        return _Repo()


@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch):
    monkeypatch.setitem(R._state, "at", float("-inf"))


@pytest.fixture
def written(monkeypatch):
    from release_agent.tools import release_queue as RQ

    rows = []
    monkeypatch.setattr(RQ, "_insert", lambda batch: rows.extend(batch) or {"ok": True})
    return rows


def test_pending_note_round_trips_and_rejects_everything_else():
    assert R.parse_pending_note(R.pending_note("removed", "removed_from_live")) == (
        "removed", "removed_from_live")
    for note in (None, "", "deployed", "release_promoted", "awaiting_merge:queued:x"):
        assert R.parse_pending_note(note) is None
    with pytest.raises(ValueError):
        R.pending_note("queued")


def test_unresolved_pending_skips_prs_that_already_settled():
    events = [
        _pending(10), _pending(10, name="svc-b"),        # one PR, two charts
        _pending(11),
        {"event_type": "deployed", "deployment_repo": REPO, "pr_number": 11},
        _pending(12),
        {"event_type": "abandoned", "deployment_repo": REPO, "pr_number": 12},
        {"event_type": "pending", "deployment_repo": REPO, "pr_number": 13,
         "note": "not a pending note"},
    ]
    open_ = R.unresolved_pending(events)
    assert list(open_) == [(REPO, 10)]
    assert {e["artifact_name"] for e in open_[(REPO, 10)]} == {"svc-a", "svc-b"}


def test_merge_is_recorded_at_merge_time_by_whoever_merged():
    merged_at = dt.datetime(2026, 1, 2, 9, 30)            # naive, as PyGithub can return
    [row] = R.resolution_rows([_pending(10)], "merged", merged_at, "approver")
    assert row["event_type"] == "deployed"
    assert row["event_ts"] == "2026-01-02T09:30:00+00:00"
    assert row["pr_number"] == 10 and row["artifact_version"] == "2.0.0"
    assert "merged after review by approver" in row["note"]

    [closed] = R.resolution_rows([_pending(10)], "closed", merged_at)
    assert closed["event_type"] == "abandoned"


def test_resolution_ids_are_deterministic_so_two_pods_dedupe():
    when = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    a = R.resolution_rows([_pending(10)], "merged", when)
    b = R.resolution_rows([_pending(10)], "merged", when)
    assert a[0]["event_id"] == b[0]["event_id"]
    assert a[0]["event_id"] != R.resolution_rows([_pending(11)], "merged", when)[0]["event_id"]


def test_a_merge_noticed_late_does_not_overwrite_a_newer_deploy():
    """Why event_ts is the merge time: v2 sat in review, v3 was deployed
    directly afterwards, and only then did anyone look. Stamping v2 with the
    time it was NOTICED would make it the latest event and put v2 back on UAT."""
    from release_agent.tools.release_queue import aggregate_env_state

    events = [
        _pending(10, version="2.0.0", ts="2026-01-01T10:00:00+00:00"),
        {"event_type": "deployed", "event_ts": "2026-01-03T10:00:00+00:00",
         "artifact_name": "svc-a", "artifact_version": "3.0.0", "environment": "uat",
         "deployment_repo": REPO, "pr_number": 20},
    ]
    merged_at = dt.datetime(2026, 1, 2, 10, tzinfo=dt.timezone.utc)
    events += R.resolution_rows([events[0]], "merged", merged_at)
    events.sort(key=lambda e: e["event_ts"])            # _fetch_events orders by event_ts

    [uat] = aggregate_env_state(events)["environments"]
    assert [(i["artifact_name"], i["version"]) for i in uat["images"]] == [("svc-a", "3.0.0")]


def test_reconcile_writes_merged_and_abandoned_and_leaves_open_prs(written):
    when = dt.datetime(2026, 1, 2, tzinfo=dt.timezone.utc)
    events = [_pending(10), _pending(11), _pending(12)]
    gh = _GitHub({10: _PR(merged=True, state="closed", when=when),
                  11: _PR(state="closed", when=when),
                  12: _PR()})

    assert R.reconcile_pending(events, github=gh) == 2
    assert {(r["pr_number"], r["event_type"]) for r in written} == {(10, "deployed"),
                                                                   (11, "abandoned")}


def test_reconcile_is_throttled(written):
    gh = _GitHub({10: _PR()})
    R.reconcile_pending([_pending(10)], github=gh)
    R.reconcile_pending([_pending(10)], github=gh)
    assert gh.reads == [10], "the second read inside the interval must not hit GitHub"


def test_reconcile_never_raises_into_the_read(written):
    """It runs inside a stats read; GitHub being down must leave that read
    working on the events it already has."""
    gh = _GitHub({10: RuntimeError("502"), 11: _PR(merged=True, state="closed",
                                                   when=dt.datetime(2026, 1, 2))})
    assert R.reconcile_pending([_pending(10), _pending(11)], github=gh) == 1
    assert [r["pr_number"] for r in written] == [11]


def test_nothing_pending_means_no_github_call(written):
    gh = _GitHub({})
    assert R.reconcile_pending([{"event_type": "deployed", "pr_number": 1}], github=gh) == 0
    assert gh.reads == []


def test_stats_read_includes_a_deploy_merged_outside_the_chat(monkeypatch):
    """The whole point: the answer someone gets must already include it."""
    from release_agent.tools import release_queue as RQ

    fetches = []
    monkeypatch.setattr(RQ, "queue_enabled", lambda: True)
    monkeypatch.setattr(RQ, "_fetch_events", lambda days: fetches.append(days) or [])
    monkeypatch.setattr(R, "reconcile_pending", lambda events: 3)
    RQ.history_stats(event_type="state")
    assert len(fetches) == 2, "must re-read after reconciliation wrote events"

    fetches.clear()
    monkeypatch.setattr(R, "reconcile_pending", lambda events: 0)
    RQ.history_stats(event_type="state")
    assert len(fetches) == 1, "no second query when nothing changed"


def test_pending_uat_deploy_is_recorded_against_the_final_pr(monkeypatch):
    from adk_release_agent import deploy as D

    calls = []
    monkeypatch.setattr(R, "record_pending", lambda *a, **kw: calls.append((a, kw)))
    req = {"images": [{"name": "svc-a", "tag": "2.0.0"}], "deployment_repo": REPO}

    D._record_deploy_event(req, "uat", {"ok": True, "action": "pending_review", "pending_prs": [
        {"number": 7, "stage": "→UAT", "final": True}]})
    assert len(calls) == 1 and calls[0][0][3] == 7

    # Blocked at SIT: merging that PR lands nothing on UAT — the UAT PR has not
    # been raised yet — so it must not be recorded as what completes the deploy.
    calls.clear()
    D._record_deploy_event(req, "uat", {"ok": True, "action": "pending_review", "pending_prs": [
        {"number": 6, "stage": "→SIT", "final": False}]})
    assert calls == []
