"""Change-request fields filled from facts (tools/chg_defaults + the endpoint)."""
import datetime as dt

import pytest

from release_agent.tools import chg_defaults as C

DAY = dt.date(2026, 9, 11)


def _item(name, version="1.0.0", **facts):
    return {"name": name, "version": version, **facts}


@pytest.mark.parametrize("day,expected", [
    (1, "1st"), (2, "2nd"), (3, "3rd"), (4, "4th"), (11, "11th"), (12, "12th"),
    (13, "13th"), (21, "21st"), (22, "22nd"), (23, "23rd"), (31, "31st"),
])
def test_ordinals_match_how_release_names_spell_dates(day, expected):
    assert C.format_release_date(dt.date(2026, 7, day)) == f"July {expected} 2026"


def test_next_number_reads_only_numbered_release_titles():
    titles = ["Team July 23rd 2026 : Release 33", "Team July 21st 2026 : Release 32",
              "PRD release 2026-07-16", "Deploy svc-a:1.0 to uat (→ SIT)", "x : Release notes"]
    assert C.next_release_number(titles) == 34
    assert C.next_release_number(["no numbers here"]) is None


def test_release_name_uses_the_configured_prefix_and_the_number(monkeypatch):
    monkeypatch.setattr(C.settings, "release_name_prefix", "Team ")
    assert C.release_name(DAY, 34) == "Team September 11th 2026 : Release 34"
    assert C.release_name(DAY, None) == "Team September 11th 2026", "no number is still a usable name"


def test_every_field_is_filled_from_the_items():
    d = C.build_defaults([
        _item("orders-api", jira_ticket="REL-10", change_details="fixes retries",
              requested_by="dev@example.com", build_verified=True),
        _item("payments-api", "3.1.0", jira_ticket="REL-11", build_verified=True),
    ], "care", DAY, 34)
    assert set(d) == {"release_name", "change_summary", "change_description", "change_reason",
                      "associated_risk", "consequence", "user_service_impact"}
    assert all(d.values()), "every field filled — start and end are the only gaps"
    assert d["release_name"].endswith("September 11th 2026 : Release 34")
    assert "2 charts: orders-api 1.0.0, payments-api 3.1.0" in d["change_summary"]
    assert "- orders-api:1.0.0 (REL-10): fixes retries — dev" in d["change_description"]
    assert d["change_reason"] == "Delivers REL-10, REL-11."
    assert "Every build was verified" in d["associated_risk"] and "Rollback" in d["associated_risk"]
    assert "REL-10, REL-11 will not be delivered" in d["consequence"]
    assert d["user_service_impact"].startswith("Rolling Helm deployment of 2 charts")


def test_an_unverified_build_is_named_not_smoothed_over():
    d = C.build_defaults([_item("a", build_verified=True), _item("b"), _item("c")], "care", DAY, 1)
    assert "1 of 3 builds were verified at queue time; b, c were not" in d["associated_risk"]
    assert "Every build was verified" not in d["associated_risk"]


def test_prl1_only_charts_are_named_with_correct_agreement():
    one = C.build_defaults([_item("a", prl1_only=True, build_verified=True)], "care", DAY, 1)
    assert "a is PRL1-only and stops at PRL1." in one["associated_risk"]
    two = C.build_defaults([_item("a", prl1_only=True), _item("b", prl1_only=True)], "care", DAY, 1)
    assert "a, b are PRL1-only and stop at PRL1." in two["associated_risk"]


def test_dataflow_wording_talks_about_templates_not_helm():
    d = C.build_defaults([_item("job-a", "2.0", jira_ticket="REL-1", build_verified=True)], "df", DAY, 5)
    assert "1 Dataflow image" in d["change_summary"]
    assert "Composer DAGs" in d["associated_risk"] and "Helm" not in d["user_service_impact"]


def test_duplicate_jira_keys_appear_once_and_missing_ones_fall_back():
    d = C.build_defaults([_item("a", jira_ticket="REL-1"), _item("b", jira_ticket="REL-1")], "care", DAY, 1)
    assert d["change_reason"] == "Delivers REL-1."
    assert C.build_defaults([_item("a")], "care", DAY, 1)["change_reason"] == \
        "Delivers the changes listed in the description."


def test_a_teams_own_wording_is_used_and_unknown_placeholders_survive(monkeypatch):
    monkeypatch.setattr(C.settings, "chg_risk_template",
                        "CAB-standard: {count} {noun}; {verification} Owner: {team}.")
    d = C.build_defaults([_item("a", build_verified=True)], "care", DAY, 1)
    assert d["associated_risk"].startswith("CAB-standard: 1 chart; Every build was verified")
    assert d["associated_risk"].endswith("Owner: {team}."), "a typo'd placeholder must not break the form"


# --- the endpoint the form calls ---------------------------------------------

def test_endpoint_matches_the_queue_and_numbers_from_the_repo(monkeypatch):
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi as A
    from release_agent.tools import release_queue as RQ

    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": [
        {"artifact_name": "orders-api", "artifact_version": "1.0.0", "jira_ticket": "REL-10",
         "build_verified": True, "requested_by": "dev@example.com"}]})
    seen = {}
    monkeypatch.setattr(C, "next_release_number_for_repo", lambda repo: seen.setdefault("repo", repo) and 34)

    r = TestClient(A.app).post("/api/release-defaults", json={
        "artifacts": ["orders-api:1.0.0", "https://artifactory.example/typed-in:2.0", "orders-api:1.0.0"],
        "kind": "care", "repo": "example-org/deploy", "date": "2026-09-17",
    }).json()
    f = r["fields"]
    assert seen["repo"] == "example-org/deploy"
    assert f["release_name"].endswith("September 17th 2026 : Release 34"), "named for the chosen date"
    assert "orders-api:1.0.0 (REL-10)" in f["change_description"]
    assert f["change_description"].count("orders-api") == 1, "a repeated line is one item"
    assert "typed-in was not" in f["associated_risk"], "not from the queue → never verified"


def test_consequence_agrees_with_one_or_many():
    one = C.build_defaults([_item("a", jira_ticket="REL-1")], "care", DAY, 1)["consequence"]
    assert one.endswith("the chart stays on its current version.")
    many = C.build_defaults([_item("a"), _item("b")], "care", DAY, 1)["consequence"]
    assert many.endswith("the charts stay on their current versions.")


def test_a_passed_back_number_never_waits_on_github(monkeypatch):
    """The form's first call looks the number up; every recompute after that
    (a date change, a tick) passes it back and must not hit GitHub again — the
    lookup is several pages of PRs, and a submit waits on the recompute."""
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi as A
    from release_agent.tools import release_queue as RQ

    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": []})
    def must_not_run(repo):
        raise AssertionError("looked the release number up again")
    monkeypatch.setattr(C, "next_release_number_for_repo", must_not_run)

    r = TestClient(A.app).post("/api/release-defaults", json={
        "artifacts": ["a:1.0"], "repo": "example-org/deploy", "date": "2026-09-24", "number": 34}).json()
    assert r["number"] == 34
    assert r["fields"]["release_name"].endswith("September 24th 2026 : Release 34")


# --- CARE release as one mono-repo file (CARE_RELEASE_MODE=mono) ----------------

MONO_FORMAT = "<TEAM> CARE Release - %Y.%m.%d"


@pytest.fixture
def mono(monkeypatch):
    monkeypatch.setattr(C.settings, "care_release_mode", "mono")
    monkeypatch.setattr(C.settings, "care_release_name_format", MONO_FORMAT)
    return C.settings


def _two():
    return [_item("svc-a", "5.0.463", jira_ticket="REL-10", build_verified=True),
            _item("svc-b", "5.0.458", build_verified=True)]


def test_mono_care_is_named_by_its_start_date_and_its_summary_is_its_name(mono):
    d = C.build_defaults(_two(), "care", dt.date(2026, 9, 24), 34)
    assert d["release_name"] == "<TEAM> CARE Release - 2026.09.24", "no release number in a formatted name"
    assert d["change_summary"] == d["release_name"]


def test_mono_care_risk_and_impact_open_with_the_team_leads(mono):
    d = C.build_defaults(_two(), "care", DAY, None)
    assert d["associated_risk"].startswith("Low risk. Standard release of 2 charts")
    assert "Every build was verified" in d["associated_risk"], "the facts still follow the lead"
    assert d["user_service_impact"].startswith("No user impact is expected. Rolling Helm deployment")
    assert d["change_reason"] == "Delivers REL-10.", "the other wording is unchanged"


def test_mono_without_a_name_format_keeps_todays_naming(mono, monkeypatch):
    monkeypatch.setattr(C.settings, "care_release_name_format", "")
    monkeypatch.setattr(C.settings, "release_name_prefix", "Team ")
    d = C.build_defaults(_two(), "care", DAY, 34)
    assert d["release_name"] == "Team September 11th 2026 : Release 34"
    assert d["change_summary"] == d["release_name"]
    assert not C.uses_name_format("care"), "so the endpoint still looks the number up"


def test_a_teams_own_template_is_its_whole_wording_in_mono_mode(mono, monkeypatch):
    monkeypatch.setattr(C.settings, "chg_risk_template", "CAB-standard: {count} {noun}.")
    monkeypatch.setattr(C.settings, "chg_impact_template", "Impact per CAB: none for {names}.")
    d = C.build_defaults(_two(), "care", DAY, None)
    assert d["associated_risk"] == "CAB-standard: 2 charts."
    assert d["user_service_impact"] == "Impact per CAB: none for svc-a, svc-b."


def test_dataflow_naming_and_wording_are_unchanged_in_mono_mode(mono, monkeypatch):
    monkeypatch.setattr(C.settings, "release_name_prefix", "Team ")
    d = C.build_defaults([_item("job-a", "2.0", jira_ticket="REL-1", build_verified=True)], "df", DAY, 5)
    assert d["release_name"] == "Team September 11th 2026 : Release 5"
    assert d["change_summary"].startswith(d["release_name"] + " — 1 Dataflow image")
    assert not d["associated_risk"].startswith("Low risk.")
    assert not d["user_service_impact"].startswith("No user impact")
    assert not C.uses_name_format("df")


def test_file_set_care_ignores_the_mono_name_format(monkeypatch):
    monkeypatch.setattr(C.settings, "care_release_mode", "fileset")
    monkeypatch.setattr(C.settings, "care_release_name_format", MONO_FORMAT)
    monkeypatch.setattr(C.settings, "release_name_prefix", "Team ")
    d = C.build_defaults(_two(), "care", DAY, 34)
    assert d["release_name"] == "Team September 11th 2026 : Release 34"
    assert d["change_summary"].startswith(d["release_name"] + " — 2 charts: svc-a 5.0.463")
    assert d["associated_risk"].startswith("Standard release of 2 charts")
    assert d["user_service_impact"].startswith("Rolling Helm deployment of 2 charts")
    assert not C.uses_name_format("care")


def test_a_name_format_that_cannot_be_applied_keeps_todays_naming(mono, monkeypatch):
    monkeypatch.setattr(C, "formatted_release_name", lambda day: "")
    d = C.build_defaults(_two(), "care", DAY, 34)
    assert d["release_name"].endswith("September 11th 2026 : Release 34")


def test_mono_endpoint_names_from_the_start_date_without_a_number_lookup(mono, monkeypatch):
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi as A
    from release_agent.tools import release_queue as RQ

    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": []})

    def must_not_run(repo):
        raise AssertionError("scanned the mono repo's PRs for a number the name does not carry")
    monkeypatch.setattr(C, "next_release_number_for_repo", must_not_run)

    r = TestClient(A.app).post("/api/release-defaults", json={
        "artifacts": ["https://artifactory.example.com/docker/example-ds/svc-a:5.0.463"],
        "kind": "care", "repo": "example-org/mono-repo", "date": "2026-09-24"}).json()
    assert r["number"] is None and r["numbered_from"] == ""
    assert r["fields"]["release_name"] == "<TEAM> CARE Release - 2026.09.24"
    assert r["fields"]["change_summary"] == "<TEAM> CARE Release - 2026.09.24"


def test_the_queue_context_tells_the_form_where_a_care_release_goes(monkeypatch):
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi as A
    from release_agent.tools import release_queue as RQ

    monkeypatch.setattr(RQ, "current_queue", lambda *a, **k: {"ok": True, "queue": []})
    monkeypatch.setattr(A, "_known_charts", lambda: [])
    monkeypatch.setattr(A.app_settings, "care_release_mode", "mono")
    monkeypatch.setattr(A.app_settings, "care_release_repo", "example-org/mono-repo")
    monkeypatch.setattr(A.app_settings, "care_release_file", ".github/release/release_details.json")

    r = TestClient(A.app).get("/api/release-queue").json()
    assert r["care_release_mode"] == "mono"
    assert r["care_release_repo"] == "example-org/mono-repo"
    assert r["care_release_file"] == ".github/release/release_details.json"
