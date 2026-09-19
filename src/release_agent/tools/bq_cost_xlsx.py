"""The BQ cost report as an .xlsx to download — one sheet per section
(Summary, Top queries, Storage, Writes, History), built from the same report
dict the pill and the chat read, so the file says exactly what the screen
says.

No spreadsheet library: an .xlsx is a zip of XML parts, and the minimal set
Excel needs is small enough to write here — inline strings (no shared-string
table), one bold header row, a frozen top row, column widths from the data.
Pure: report in, bytes out; nothing here touches BigQuery.
"""
from __future__ import annotations

import io
import math
import zipfile
from typing import Any
from xml.sax.saxutils import escape

_GIB = 2**30
_MIB = 2**20

# (sheet name, header, rows) — a row is a list of cell values: str, int,
# float, bool or None (None = an empty cell).
Sheet = tuple[str, list[str], list[list[Any]]]

_MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_DOC_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_XML_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'

MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ----- the workbook writer (generic) ------------------------------------------

def column_letter(index: int) -> str:
    """1 -> A, 26 -> Z, 27 -> AA: Excel's column naming."""
    if index < 1:
        raise ValueError("columns start at 1")
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _text(value: Any) -> str:
    """Cell text safe for XML: control characters (which Excel rejects even
    escaped) dropped, tab/newline kept, markup escaped."""
    raw = str(value)
    cleaned = "".join(ch for ch in raw if ch in "\t\n\r" or ord(ch) >= 32)
    return escape(cleaned)


def _cell(ref: str, value: Any, header: bool = False) -> str:
    style = ' s="1"' if header else ""
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{style}><v>{int(value)}</v></c>'
    if isinstance(value, int):
        return f'<c r="{ref}"{style}><v>{value}</v></c>'
    if isinstance(value, float) and math.isfinite(value):
        return f'<c r="{ref}"{style}><v>{value!r}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{style}><is><t xml:space="preserve">{_text(value)}</t></is></c>'


def _widths(header: list[str], rows: list[list[Any]]) -> list[int]:
    """A column as wide as its longest value, within reason — a 180-character
    SQL preview gets 60, a count gets 8."""
    widths = []
    for i, name in enumerate(header):
        longest = len(str(name))
        for row in rows:
            if i < len(row) and row[i] is not None:
                longest = max(longest, len(str(row[i]).split("\n")[0]))
        widths.append(min(60, max(8, longest + 2)))
    return widths


def _sheet_xml(header: list[str], rows: list[list[Any]]) -> str:
    cols = "".join(
        f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
        for i, w in enumerate(_widths(header, rows), start=1)
    )
    lines = [f'<row r="1">{"".join(_cell(f"{column_letter(c)}1", h, header=True) for c, h in enumerate(header, start=1))}</row>']
    for r, row in enumerate(rows, start=2):
        cells = "".join(_cell(f"{column_letter(c)}{r}", v) for c, v in enumerate(row, start=1))
        lines.append(f'<row r="{r}">{cells}</row>')
    return (
        f'{_XML_HEAD}<worksheet xmlns="{_MAIN_NS}">'
        '<sheetViews><sheetView workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft"/></sheetView></sheetViews>'
        f"<cols>{cols}</cols><sheetData>{''.join(lines)}</sheetData></worksheet>"
    )


def sheet_name(name: str) -> str:
    """Excel's rules: at most 31 characters, none of []:*?/\\ ."""
    cleaned = "".join(" " if ch in "[]:*?/\\" else ch for ch in name).strip()
    return (cleaned or "Sheet")[:31]


_STYLES = (
    f'{_XML_HEAD}<styleSheet xmlns="{_MAIN_NS}">'
    '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
    '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
    '<fills count="2"><fill><patternFill patternType="none"/></fill>'
    '<fill><patternFill patternType="gray125"/></fill></fills>'
    '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
    '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
    '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
    '<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
    '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
    "</styleSheet>"
)


def workbook_bytes(sheets: list[Sheet]) -> bytes:
    """The .xlsx file for these sheets, in order. At least one sheet."""
    if not sheets:
        raise ValueError("a workbook needs at least one sheet")
    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{i}.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    content_types = (
        f'{_XML_HEAD}<Types xmlns="{_CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f"{overrides}</Types>"
    )
    root_rels = (
        f'{_XML_HEAD}<Relationships xmlns="{_PKG_REL}">'
        f'<Relationship Id="rId1" Type="{_DOC_REL}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )
    sheet_tags = "".join(
        f'<sheet name="{escape(sheet_name(name), {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>'
        for i, (name, _h, _r) in enumerate(sheets, start=1)
    )
    workbook = (
        f'{_XML_HEAD}<workbook xmlns="{_MAIN_NS}" xmlns:r="{_DOC_REL}">'
        f"<sheets>{sheet_tags}</sheets></workbook>"
    )
    wb_rels = "".join(
        f'<Relationship Id="rId{i}" Type="{_DOC_REL}/worksheet" Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(sheets) + 1)
    )
    wb_rels = (
        f'{_XML_HEAD}<Relationships xmlns="{_PKG_REL}">{wb_rels}'
        f'<Relationship Id="rId{len(sheets) + 1}" Type="{_DOC_REL}/styles" Target="styles.xml"/>'
        "</Relationships>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels)
        zf.writestr("xl/styles.xml", _STYLES)
        for i, (_name, header, rows) in enumerate(sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{i}.xml", _sheet_xml(header, rows))
    return buf.getvalue()


# ----- the BQ cost report's sheets --------------------------------------------

def _words(kind: Any) -> str:
    return str(kind or "").replace("_", " ")


def _join(values: Any) -> str:
    return ", ".join(str(v) for v in (values or []) if v)


def _mb(bytes_: Any) -> float | None:
    return round(bytes_ / _MIB, 1) if isinstance(bytes_, (int, float)) else None


def sheets_for(report: dict[str, Any]) -> list[Sheet]:
    """The workbook's sheets for a successful scan. A section with nothing to
    say gets one row saying so — a blank sheet reads as a broken export."""
    totals = report.get("totals") or {}
    history = report.get("history") or {}
    summary_rows: list[list[Any]] = [
        ["Project", report.get("project")],
        ["Region", report.get("region")],
        ["Window (days)", report.get("days")],
        ["Billing model", report.get("billing")],
        ["Scanned at", report.get("scanned_at")],
        ["Queries in window", totals.get("queries")],
        ["Slot-hours", totals.get("slot_hours")],
        ["GB billed", totals.get("gb_billed")],
        ["Cache hit %", totals.get("cache_hit_pct")],
        ["This report read (MB)", _mb(report.get("report_cost_bytes"))],
        ["Statements the scan issued", report.get("queries_run")],
        ["Storage read via", _words(report.get("storage_source")) or "not available"],
    ]
    for label, key in (("Note", "hint"), ("Storage note", "storage_hint"), ("Writes note", "writes_hint")):
        if report.get(key):
            summary_rows.append([label, report[key]])
    if history:
        summary_rows.append(["History", " · ".join(
            f"{history.get(k, 0)} {k.replace('_', ' ')}" for k in ("adopted", "still_open", "new", "gone")
            if history.get(k))])

    shapes = [
        [i, s.get("qhash"), s.get("who"), s.get("users_count"), s.get("runs"), s.get("gb_billed"),
         s.get("approx_usd"), s.get("slot_hours"), s.get("p50_bytes"), s.get("last_run"),
         _join(s.get("insights")), _join(s.get("referenced_tables")), s.get("sql_preview")]
        for i, s in enumerate(report.get("shapes") or [], start=1)
    ] or [["no query shape worth a row in this window"]]
    storage = [
        [t.get("table"), _words(t.get("kind")), t.get("gb"), t.get("physical_gb"), t.get("reads"),
         t.get("last_read"), t.get("last_modified"), t.get("expiration"), t.get("approx_usd_month")]
        for t in report.get("storage") or []
    ] or [[report.get("storage_error") or "no storage findings"]]
    writes = [
        [w.get("table"), w.get("source"), _words(w.get("kind")), w.get("requests"), w.get("rows"),
         w.get("rows_per_request"), w.get("input_gb"), w.get("errors")]
        for w in report.get("writes") or []
    ] or [[report.get("writes_error") or "no write findings"]]
    adoption = [
        [a.get("qhash"), _words(a.get("status")), a.get("times_reported"), a.get("cost_before_gb"),
         a.get("cost_now_gb"), a.get("change_pct")]
        for a in history.get("adoption") or []
    ] or [["no earlier runs to compare with" if not history else "nothing changed since the last run"]]

    return [
        ("Summary", ["Field", "Value"], summary_rows),
        ("Top queries", ["#", "Query hash", "Run by", "Distinct users", "Runs", "GB billed", "≈ USD",
                         "Slot-hours", "p50 bytes per run", "Last run", "Insights", "Tables read",
                         "Query (first 180 chars)"], shapes),
        ("Storage", ["Table", "Finding", "GB", "Physical GB", "Reads in window", "Last read",
                     "Last modified", "Expiration", "≈ USD per month"], storage),
        ("Writes", ["Table", "Source", "Finding", "Requests", "Rows", "Rows per request", "Input GB",
                    "Errors"], writes),
        ("History", ["Query hash", "Status", "Times reported", "GB before", "GB now", "Change %"], adoption),
    ]


def report_workbook(report: dict[str, Any]) -> bytes:
    return workbook_bytes(sheets_for(report))


def report_filename(report: dict[str, Any]) -> str:
    """bq-cost-report-<project>-<date>.xlsx — only characters a browser keeps."""
    project = "".join(ch for ch in str(report.get("project") or "project") if ch.isalnum() or ch in "-_.")
    day = str(report.get("scanned_at") or "")[:10] or "today"
    return f"bq-cost-report-{project}-{day}.xlsx"
