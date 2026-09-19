"""The BQ cost report's .xlsx export — a hand-written workbook, so the
structure Excel needs is pinned here part by part."""
import io
import xml.etree.ElementTree as ET
import zipfile

import pytest

from release_agent.tools import bq_cost_xlsx as X

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

REPORT = {
    "ok": True, "project": "team-bq", "region": "region-europe-west3", "days": 14,
    "billing": "on-demand", "scanned_at": "2026-09-19T20:40:58+00:00",
    "report_cost_bytes": 42559434, "queries_run": 9, "storage_source": "per_dataset",
    "totals": {"queries": 328, "slot_hours": 0.09, "gb_billed": 3.8, "cache_hit_pct": 0.0},
    "shapes": [{"qhash": "abc123", "runs": 157, "who": "alice@example.com", "users_count": 2,
                "slot_hours": 0.0, "gb_billed": 1.53, "approx_usd": 0.01, "p50_bytes": 21557,
                "sql_preview": "SELECT * FROM `p.d.t` WHERE x < 3 & y", "insights": ["slot_contention"],
                "referenced_tables": ["p.d.t"], "last_run": "2026-09-19T10:00:00+00:00"}],
    "storage": [], "writes": [{"table": "p.d.events", "source": "streaming", "kind": "unbatched",
                               "requests": 2100000, "rows": 2100000, "rows_per_request": 1.0,
                               "input_gb": 4.2, "errors": 0}],
    "history": {"adoption": [{"qhash": "abc123", "status": "still_open", "times_reported": 3,
                              "cost_before_gb": 1.5, "cost_now_gb": 1.53, "change_pct": 2.0}],
                "adopted": 0, "still_open": 1, "new": 0, "gone": 0},
    "writes_hint": "the write timelines are region-wide views; see the note",
}


def _parts(data: bytes) -> dict[str, str]:
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return {name: zf.read(name).decode("utf-8") for name in zf.namelist()}


def test_column_letters_follow_excel():
    assert [X.column_letter(i) for i in (1, 26, 27, 52, 53, 703)] == ["A", "Z", "AA", "AZ", "BA", "AAA"]
    with pytest.raises(ValueError):
        X.column_letter(0)


def test_every_part_excel_needs_is_present_and_well_formed():
    parts = _parts(X.workbook_bytes([("One", ["a"], [[1]]), ("Two", ["b"], [["x"]])]))
    assert set(parts) == {
        "[Content_Types].xml", "_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels",
        "xl/styles.xml", "xl/worksheets/sheet1.xml", "xl/worksheets/sheet2.xml",
    }
    for name, xml in parts.items():
        ET.fromstring(xml)  # well-formed, or this raises
    assert 'PartName="/xl/worksheets/sheet2.xml"' in parts["[Content_Types].xml"]
    names = [s.get("name") for s in ET.fromstring(parts["xl/workbook.xml"]).iter(f"{MAIN}sheet")]
    assert names == ["One", "Two"]
    assert 'Target="worksheets/sheet2.xml"' in parts["xl/_rels/workbook.xml.rels"]
    assert 'Target="styles.xml"' in parts["xl/_rels/workbook.xml.rels"]


def test_cells_carry_their_type_and_text_is_escaped():
    rows = [[1, 2.5, True, None, "a < b & c", "tab\tkept\x00control dropped"]]
    sheet = _parts(X.workbook_bytes([("S", ["n", "f", "b", "empty", "s", "t"], rows)]))["xl/worksheets/sheet1.xml"]
    root = ET.fromstring(sheet)
    cells = {c.get("r"): c for c in root.iter(f"{MAIN}c")}
    assert cells["A2"].get("t") is None and cells["A2"].find(f"{MAIN}v").text == "1"
    assert cells["B2"].find(f"{MAIN}v").text == "2.5"
    assert cells["C2"].get("t") == "b" and cells["C2"].find(f"{MAIN}v").text == "1"
    assert "D2" not in cells, "None is an empty cell, not a cell saying 'None'"
    assert cells["E2"].get("t") == "inlineStr"
    assert cells["E2"].find(f"{MAIN}is/{MAIN}t").text == "a < b & c"
    assert cells["F2"].find(f"{MAIN}is/{MAIN}t").text == "tab\tkeptcontrol dropped"
    assert cells["A1"].get("s") == "1", "the header row wears the bold style"
    assert root.find(f"{MAIN}sheetViews/{MAIN}sheetView/{MAIN}pane").get("state") == "frozen"


def test_sheet_names_obey_excel():
    assert X.sheet_name("Top queries") == "Top queries"
    assert X.sheet_name("a/b:c?d*e[f]") == "a b c d e f"
    assert len(X.sheet_name("x" * 40)) == 31
    assert X.sheet_name("") == "Sheet"
    with pytest.raises(ValueError):
        X.workbook_bytes([])


def test_the_report_becomes_one_sheet_per_section():
    sheets = X.sheets_for(REPORT)
    assert [s[0] for s in sheets] == ["Summary", "Top queries", "Storage", "Writes", "History"]
    summary = dict((r[0], r[1]) for r in sheets[0][2])
    assert summary["Project"] == "team-bq" and summary["Queries in window"] == 328
    assert summary["This report read (MB)"] == 40.6
    assert summary["Storage read via"] == "per dataset"
    assert summary["Writes note"].startswith("the write timelines")
    assert summary["History"] == "1 still open"
    top = sheets[1][2][0]
    assert top[:6] == [1, "abc123", "alice@example.com", 2, 157, 1.53]
    assert top[10] == "slot_contention" and top[11] == "p.d.t" and top[12].startswith("SELECT *")
    assert sheets[2][2] == [["no storage findings"]], "an empty section says so instead of a blank sheet"
    assert sheets[3][2][0][:3] == ["p.d.events", "streaming", "unbatched"]
    assert sheets[4][2][0] == ["abc123", "still open", 3, 1.5, 1.53, 2.0]


def test_the_workbook_round_trips_the_report_text():
    parts = _parts(X.report_workbook(REPORT))
    top = parts["xl/worksheets/sheet2.xml"]
    assert "abc123" in top and "SELECT * FROM `p.d.t` WHERE x &lt; 3 &amp; y" in top
    assert "no storage findings" in parts["xl/worksheets/sheet3.xml"]


def test_the_filename_names_the_project_and_the_day():
    assert X.report_filename(REPORT) == "bq-cost-report-team-bq-2026-09-19.xlsx"
    assert X.report_filename({"project": "we ird/id", "scanned_at": ""}) == "bq-cost-report-weirdid-today.xlsx"
