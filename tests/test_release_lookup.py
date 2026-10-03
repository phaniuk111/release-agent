"""release_lookup: matching rules and state derivation are pure; the two BigQuery
statements are stubbed (conftest refuses any real query)."""
from datetime import date, datetime, timezone

import pytest

from release_agent.config import settings
from release_agent.tools import release_lookup as rl


def ev(etype, ts, name="svc-a", version="1.0.0", env=None, **extra):
    return {"event_type": etype, "event_ts": ts, "artifact_name": name, "artifact_version": version,
            "environment": env, "requested_by": extra.pop("by", "dev@example.com"), **extra}


def at(day, hour=9):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


UNTIL = datetime(2026, 10, 4, tzinfo=timezone.utc)


# ----- name validation and matching ---------------------------------------------

@pytest.mark.parametrize("name", ["svc-a", "Report_B", "team/svc.v2", "a1"])
def test_valid_names_pass(name):
    assert rl.validate_name(f"  {name} ") == (name, None)


@pytest.mark.parametrize("name", ["", "   ", None, "a b", "a;b", "a'b", "a%", "café", "a" * 129, "a`b"])
def test_invalid_names_refused(name):
    clean, problem = rl.validate_name(name)
    assert clean == "" and problem


def test_invalid_name_never_reaches_bigquery(monkeypatch):
    monkeypatch.setattr(settings, "bq_dataset", "ds", raising=False)
    monkeypatch.setattr(settings, "gcp_project", "p", raising=False)
    monkeypatch.setattr(rl, "_run", lambda *a, **k: pytest.fail("queried"))
    assert rl.lookup("a b; DROP")["ok"] is False


def test_exact_beats_substring():
    assert rl.pick_match("svc-a", ["svc-a", "svc-a-worker"]) == (["svc-a"], "exact", None)


def test_substring_is_case_insensitive():
    assert rl.pick_match("SYS-B-fetcher", ["sys-b-fetcher-svc", "other"]) == (
        ["sys-b-fetcher-svc"], "substring", None)


def test_no_match():
    assert rl.pick_match("zzz", ["svc-a"]) == ([], "none", None)


def test_more_than_five_refused_and_listed():
    cands = [f"svc-{i}" for i in range(7)]
    matched, how, refusal = rl.pick_match("svc", cands)
    assert how == "substring" and refusal and all(c in refusal for c in cands) and len(matched) == 7


def test_exactly_five_allowed():
    assert rl.pick_match("svc", [f"svc-{i}" for i in range(5)])[2] is None


# ----- state derivation -----------------------------------------------------------

def test_deployed_removed_deployed_again():
    events = [
        ev("deployed", at(1), version="1.0.0", env="prd"),
        ev("removed", at(2), version="1.0.0", env="prd"),
    ]
    d = rl.derive("svc-a", events, until=UNTIL)
    assert d["current"] == {}
    events.append(ev("deployed", at(3), version="1.1.0", env="prd", jira_ticket="ABC-1", pr_number=7))
    d = rl.derive("svc-a", events, until=UNTIL)
    cur = d["current"]["prd"]
    assert (cur["version"], cur["jira_ticket"], cur["pr_number"]) == ("1.1.0", "ABC-1", 7)
    assert cur["since"].startswith("2026-09-03")
    # the version before a removal is not "replaced" by the next deploy
    assert [(c["event_type"], c["from_version"], c["to_version"]) for c in d["changes"]] == [
        ("deployed", None, "1.0.0"), ("removed", "1.0.0", None), ("deployed", None, "1.1.0")]


def test_from_version_is_previous_on_the_same_env_only():
    events = [
        ev("deployed", at(1), version="1.0.0", env="uat"),
        ev("deployed", at(2), version="1.0.0", env="prd"),
        ev("deployed", at(3), version="1.1.0", env="uat"),
    ]
    changes = rl.derive("svc-a", events, until=UNTIL)["changes"]
    assert [(c["environment"], c["from_version"], c["to_version"]) for c in changes] == [
        ("uat", None, "1.0.0"), ("prd", None, "1.0.0"), ("uat", "1.0.0", "1.1.0")]


def test_several_environments_and_prod_alias():
    events = [ev("deployed", at(1), env="uat"), ev("deployed", at(2), env="prod", version="0.9.0"),
              ev("deployed", at(3), env="prl1")]
    cur = rl.derive("svc-a", events, until=UNTIL)["current"]
    assert set(cur) == {"uat", "prd", "prl1"} and cur["prd"]["version"] == "0.9.0"


def test_prl1_only_chart_is_never_on_prd():
    events = [ev("queued", at(1), prl1_only=True, df_only=False),
              ev("deployed", at(2), env="prl1")]
    d = rl.derive("svc-a", events, until=UNTIL)
    assert set(d["current"]) == {"prl1"} and d["flags"] == {"prl1_only": True, "df_only": False}


def test_events_after_until_are_ignored():
    events = [ev("deployed", at(1), version="1.0.0", env="prd"), ev("deployed", at(5), version="2.0.0", env="prd")]
    d = rl.derive("svc-a", events, until=datetime(2026, 9, 3, tzinfo=timezone.utc))
    assert d["current"]["prd"]["version"] == "1.0.0" and len(d["changes"]) == 1


def test_unordered_rows_and_string_timestamps():
    events = [ev("deployed", "2026-09-03T09:00:00+00:00", version="2", env="prd"),
              ev("deployed", "2026-09-01T09:00:00Z", version="1", env="prd")]
    d = rl.derive("svc-a", events, until=UNTIL)
    assert d["current"]["prd"]["version"] == "2" and d["changes"][1]["from_version"] == "1"


def test_pending_and_other_events_do_not_count():
    events = [ev("pending", at(1), env="prd"), ev("withdrawn", at(2), env="prd")]
    assert rl.derive("svc-a", events, until=UNTIL) == {"current": {}, "changes": [], "flags": {}}


def test_release_and_ticket_borrowed_from_context_events():
    events = [ev("queued", at(1), version="1.4.2", jira_ticket="ABC-2231"),
              ev("released", at(2), version="1.4.2", release_name="R-39"),
              ev("released", at(2), version="1.4.0", release_name="R-38"),
              ev("deployed", at(3), version="1.4.2", env="prd")]
    cur = rl.derive("svc-a", events, until=UNTIL)["current"]["prd"]
    assert (cur["jira_ticket"], cur["release_name"]) == ("ABC-2231", "R-39")


def test_own_values_beat_borrowed_ones():
    events = [ev("released", at(2), version="1", release_name="R-1"),
              ev("deployed", at(3), version="1", env="prd", release_name="R-2")]
    assert rl.derive("svc-a", events, until=UNTIL)["current"]["prd"]["release_name"] == "R-2"


# ----- entry points over a stubbed log -------------------------------------------

@pytest.fixture
def log(monkeypatch):
    """Enable the queue and serve `rows` for the two statements."""
    monkeypatch.setattr(settings, "bq_dataset", "ds", raising=False)
    monkeypatch.setattr(settings, "gcp_project", "p", raising=False)
    state = {"rows": [], "calls": []}

    def fake_candidates(needles):
        state["calls"].append(("candidates", needles))
        low = [n.lower() for n in needles]
        names = {r["artifact_name"] for r in state["rows"]}
        return sorted(n for n in names if any(x in n.lower() for x in low)), 10

    def fake_events(artifacts, until):
        state["calls"].append(("events", artifacts))
        return [r for r in state["rows"] if r["artifact_name"] in artifacts], 20

    monkeypatch.setattr(rl, "_candidates", fake_candidates)
    monkeypatch.setattr(rl, "_events", fake_events)
    return state


def test_lookup_summary_and_shape(log):
    log["rows"] = [
        ev("queued", at(20), name="report-b", version="1.4.2", jira_ticket="ABC-2231"),
        ev("released", at(21), name="report-b", version="1.4.2", release_name="R-39"),
        ev("deployed", at(29), name="report-b", version="1.4.2", env="prd", pr_number=12),
    ]
    out = rl.lookup("report-b", as_of=date(2026, 9, 30))
    assert out["ok"] and out["match"] == "exact" and out["matched"] == ["report-b"]
    assert out["current"]["prd"]["pr_number"] == 12
    assert out["summary"] == ("report-b: 1.4.2 on prd since 2026-09-29 (ABC-2231, release R-39); "
                              "1 change in 14 days")
    assert out["changed_within_days"] == 1 and out["total_changes"] == 1
    assert len(log["calls"]) == 2   # one matching statement, one events statement


def test_as_of_answers_as_it_stood(log):
    log["rows"] = [ev("deployed", at(10), version="1", env="prd"), ev("deployed", at(20), version="2", env="prd")]
    out = rl.lookup("svc-a", as_of=date(2026, 9, 15))
    assert out["current"]["prd"]["version"] == "1" and out["total_changes"] == 1


def test_as_of_includes_the_whole_day(log):
    log["rows"] = [ev("deployed", at(15, hour=23), version="1", env="prd")]
    assert rl.lookup("svc-a", as_of=date(2026, 9, 15))["current"]["prd"]["version"] == "1"


def test_window_excludes_old_changes_but_keeps_state(log):
    log["rows"] = [ev("deployed", at(1), version="1", env="prd")]
    out = rl.lookup("svc-a", days=7, as_of=date(2026, 9, 30))
    assert out["changes"] == [] and out["current"]["prd"]["version"] == "1"
    assert out["changed_within_days"] == 29
    assert out["summary"].endswith("0 changes in 7 days")


def test_changes_newest_first_with_from_version(log):
    log["rows"] = [ev("deployed", at(d), version=f"1.{d}", env="prd") for d in (3, 1, 2)]
    out = rl.lookup("svc-a", as_of=date(2026, 9, 10))
    assert [c["to_version"] for c in out["changes"]] == ["1.3", "1.2", "1.1"]
    assert out["changes"][0]["from_version"] == "1.2"


def test_changes_capped_at_fifty(log):
    log["rows"] = [ev("deployed", datetime(2026, 9, 1, h, m, tzinfo=timezone.utc), version=f"{h}.{m}", env="prd")
                   for h in range(10) for m in range(0, 60, 10)]
    out = rl.lookup("svc-a", as_of=date(2026, 9, 10))
    assert len(out["changes"]) == 50 and out["total_changes"] == 60


def test_substring_match_reports_which_was_used(log):
    log["rows"] = [ev("deployed", at(1), name="sys-b-fetcher-svc", env="uat")]
    out = rl.lookup("sys-b-fetcher", as_of=date(2026, 9, 30))
    assert out["match"] == "substring" and out["matched"] == ["sys-b-fetcher-svc"]
    assert out["current"]["uat"]["version"] == "1.0.0"


def test_too_many_matches_refused_before_reading_events(log):
    log["rows"] = [ev("deployed", at(1), name=f"svc-{i}", env="uat") for i in range(6)]
    out = rl.lookup("svc")
    assert out["ok"] is False and "svc-5" in out["error"] and len(out["matched"]) == 6
    assert [c[0] for c in log["calls"]] == ["candidates"]


def test_several_matches_read_by_artifact(log):
    log["rows"] = [ev("deployed", at(1), name="svc-a", env="uat"), ev("deployed", at(2), name="svc-b", env="prd")]
    out = rl.lookup("svc", as_of=date(2026, 9, 30))
    assert out["current"] == {} and set(out["by_artifact"]) == {"svc-a", "svc-b"} and out["note"]
    assert "svc-a: 1.0.0 on uat" in out["summary"] and "svc-b: 1.0.0 on prd" in out["summary"]


def test_unknown_name_is_ok_but_empty(log):
    out = rl.lookup("nothing-here")
    assert out["ok"] and out["matched"] == [] and out["changed_within_days"] is None


def test_never_deployed_artifact(log):
    log["rows"] = [ev("queued", at(1))]
    out = rl.lookup("svc-a", as_of=date(2026, 9, 30))
    assert out["current"] == {} and "not deployed" in out["summary"]


def test_bad_arguments(log):
    assert rl.lookup("svc-a", days=0)["ok"] is False
    assert rl.lookup("svc-a", days=400)["ok"] is False
    assert rl.lookup("svc-a", as_of="2026-09-30")["ok"] is False


def test_disabled(monkeypatch):
    out = rl.lookup("svc-a")
    assert out["ok"] is False and out["disabled"] is True
    assert rl.what_changed(["svc-a"], date(2026, 9, 30))["disabled"] is True


def test_permission_denied_carries_the_role_hint(log, monkeypatch):
    def deny(needles):
        raise RuntimeError("403 Access Denied: Table ds.release_intents: User does not have permission")

    monkeypatch.setattr(rl, "_candidates", deny)
    for out in (rl.lookup("svc-a"), rl.what_changed(["svc-a"], date(2026, 9, 30))):
        assert out["ok"] is False and "roles/bigquery.dataViewer" in out["hint"] and "ds" in out["hint"]


def test_other_errors_have_no_hint(log, monkeypatch):
    def boom(needles):
        raise RuntimeError("socket closed")

    monkeypatch.setattr(rl, "_candidates", boom)
    out = rl.lookup("svc-a")
    assert out["ok"] is False and "socket closed" in out["error"] and "hint" not in out


# ----- what_changed ---------------------------------------------------------------

def test_what_changed_merges_names_newest_first(log):
    log["rows"] = [
        ev("deployed", at(25), name="svc-a", version="2", env="prd"),
        ev("deployed", at(28), name="report-b", version="9", env="uat"),
        ev("deployed", at(1), name="svc-a", version="1", env="prd"),   # before the window
    ]
    out = rl.what_changed(["svc-a", "report-b"], date(2026, 9, 30))
    assert out["ok"] and [c["artifact"] for c in out["changes"]] == ["report-b", "svc-a"]
    assert out["changes"][1]["from_version"] == "1"
    assert out["total_changes"] == 2 and out["summary"].startswith("2 changes in the 7 days up to 2026-09-30")
    assert "latest: report-b 9 on uat, 2026-09-28" in out["summary"]
    assert len(log["calls"]) == 2   # all names share the two statements


def test_what_changed_nothing_in_window(log):
    log["rows"] = [ev("deployed", at(1), env="prd")]
    out = rl.what_changed(["svc-a"], date(2026, 9, 30))
    assert out["changes"] == [] and out["summary"].startswith("No changes in the 7 days")


def test_what_changed_caps_names_and_reports_skips(log):
    log["rows"] = [ev("deployed", at(28), name=f"x{i}", env="prd") for i in range(12)]
    names = [f"x{i}" for i in range(12)] + ["bad name", "x0"]
    out = rl.what_changed(names, date(2026, 9, 30))
    assert len(out["names"]) == 10 and out["dropped"] == ["x10", "x11"] and "bad name" in out["skipped"]
    assert out["total_changes"] == 10


def test_what_changed_skips_vague_and_unknown_names(log):
    log["rows"] = [ev("deployed", at(28), name=f"svc-{i}", env="prd") for i in range(6)]
    out = rl.what_changed(["svc", "ghost"], date(2026, 9, 30))
    assert out["ok"] and set(out["skipped"]) == {"svc", "ghost"} and out["changes"] == []


def test_what_changed_needs_a_valid_name(log):
    assert rl.what_changed([], date(2026, 9, 30))["ok"] is False
    assert rl.what_changed(["a b"], date(2026, 9, 30))["ok"] is False
    assert rl.what_changed(["svc-a"], "2026-09-30")["ok"] is False
