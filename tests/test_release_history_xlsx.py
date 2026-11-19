"""The Release history period as a workbook: what the card shows, two sheets."""
import io
import zipfile

from release_agent.tools import release_history_xlsx as X
from release_agent.tools import release_queue as RQ

HISTORY = {
    "ok": True, "days": 21, "since": "2026-10-19", "until": "2026-10-25",
    "releases": [{
        "release_name": "R1", "pr_number": 10, "deployment_repo": "example-org/deploy",
        "released_at": "2026-10-24T09:30:00+00:00", "released_by": "rm@example.com",
        "items": [{"artifact_name": "svc-a", "artifact_version": "1.0.1", "df_only": False, "target_envs": "prd,prl1",
                   "jira_ticket": "ABC-1 ABC-2", "change_details": "Adds X\nFixes Y", "note": "", "queued_by": "bob@example.com",
                   "build_run_url": "https://github.com/o/r/actions/runs/1", "previous_tag": "1.0.0", "in_queue": True}],
    }],
    "deployments": [{"artifact_name": "df-x", "artifact_version": "3.0.0", "environment": "dataflow-uat",
                     "deployed_at": "2026-10-24T18:12:00+00:00", "pr_number": None, "deployment_repo": "",
                     "requested_by": "ann@example.com", "note": ""}],
}


def test_the_period_is_two_sheets_one_row_per_chart_and_per_deploy():
    (rname, rhead, rrows), (dname, dhead, drows) = X.sheets_for(HISTORY)
    assert (rname, dname) == ("Releases", "Deployed")
    row = dict(zip(rhead, rrows[0]))
    assert row["Released (UTC)"] == "2026-10-24 09:30" and row["Release"] == "R1" and row["PR"] == 10
    assert row["JIRA"] == "ABC-1 ABC-2" and row["Change details"] == "Adds X\nFixes Y"
    assert row["Goes to"] == "PRD, PRL1" and row["Kind"] == "CARE" and row["In the next release"] == "yes"
    dep = dict(zip(dhead, drows[0]))
    assert dep["Deployed (UTC)"] == "2026-10-24 18:12" and dep["Environment"] == "DATAFLOW-UAT" and dep["PR"] is None


def test_the_workbook_opens_as_a_zip_and_is_named_by_its_period():
    data = X.workbook(HISTORY)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "xl/workbook.xml" in names and sum(n.startswith("xl/worksheets/sheet") for n in names) == 2
    assert X.filename(HISTORY) == "release-history-2026-10-19-to-2026-10-25.xlsx"
    assert X.filename({"days": 21}).startswith("release-history-last-21-days-")
    assert X.filename({"since": '2026-10-19"; x="', "until": "2026-10-25"}).count('"') == 0


def test_the_route_downloads_the_period_and_refuses_a_bad_one(monkeypatch):
    from release_agent import app_fastapi as APP

    seen = {}
    monkeypatch.setattr(RQ, "history", lambda **kw: seen.update(kw) or HISTORY)
    res = APP.release_history_xlsx(since="2026-10-21", until="2026-10-25")
    assert res.media_type == X.MEDIA_TYPE and seen["since"] == "2026-10-21"
    assert 'filename="release-history-2026-10-19-to-2026-10-25.xlsx"' in res.headers["content-disposition"]
    monkeypatch.setattr(RQ, "history", lambda **kw: {"ok": False, "error": "Dates are YYYY-MM-DD."})
    assert APP.release_history_xlsx(since="nope").status_code == 400
    monkeypatch.setattr(RQ, "history", lambda **kw: {"ok": False, "error": "BigQuery unavailable: boom"})
    assert APP.release_history_xlsx().status_code == 503
