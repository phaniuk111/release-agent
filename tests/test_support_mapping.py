"""The mapping generator (scripts/support_mapping.py), the chart's readable
supportTable block, and the live-table check — all on a made-up schema."""
from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from release_agent.tools import support_triage as st

ROOT = pathlib.Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("support_mapping", ROOT / "scripts" / "support_mapping.py")
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

SCHEMA = [
    {"name": "written_at", "type": "TIMESTAMP", "description": "When the row was written."},
    {"name": "eventReceivedAt", "type": "TIMESTAMP", "description": "When the trigger arrived."},
    {"name": "event_key", "type": "STRING", "description": "The event that triggered the run."},
    {"name": "runId", "type": "STRING", "description": "One execution."},
    {"name": "accountId", "type": "STRING", "description": "The account the run is for."},
    {"name": "accountName", "type": "STRING", "description": "Its display name."},
    {"name": "biz_date", "type": "DATE", "description": "The business date. This is the partitioning key."},
    {"name": "loaded_on", "type": "DATE", "description": "Load date."},
    {"name": "feedName", "type": "STRING", "description": "The upstream system name."},
    {"name": "reportKind", "type": "STRING", "description": "The type of report being run."},
    {"name": "state", "type": "STRING", "description": "Current state."},
    {"name": "fetcher", "type": "STRING", "description": "The source of data."},
    {"name": "job_ref", "type": "STRING", "description": "The job started."},
    {"name": "row_count", "type": "STRING", "description": "Rows produced."},
    {"name": "err_msg", "type": "STRING", "description": "Error text."},
    {"name": "err_details", "type": "STRING", "description": "JSON details."},
    {"name": "extra", "type": "STRING", "description": "Anything else."},
]


def test_names_split_into_words_by_case_and_separators():
    assert gen.name_words("pipelineJobId") == ["pipeline", "job", "id"]
    assert gen.name_words("err_msg") == ["err", "msg"] and gen.name_words("HTTPStatus") == ["httpstatus"]


def test_the_obvious_roles_are_settled_and_the_grouping_ones_are_marked_to_confirm():
    p = gen.propose(SCHEMA)
    got = {r: h["column"] for r, h in p["roles"].items()}
    assert got == {"date": "biz_date", "run_id": "runId", "status": "state", "updated_at": "written_at",
                   "event_at": "eventReceivedAt", "event_id": "event_key", "job_id": "job_ref", "error": "err_msg",
                   "details": "err_details", "members": "row_count", "system": "feedName", "source": "fetcher",
                   "process": "reportKind", "unit": "accountId"}
    assert p["roles"]["date"]["confirm"] is False            # two DATE columns, one described as the partition key
    assert all(p["roles"][r]["confirm"] for r in ("system", "source", "process", "unit"))
    assert [c["name"] for c in p["unmapped"]] == ["accountName", "loaded_on", "extra"]   # a display name is not a unit
    assert p["missing"] == []


def test_a_schema_without_the_required_columns_says_so_and_exits_nonzero(tmp_path, capsys):
    import json

    f = tmp_path / "s.json"
    f.write_text(json.dumps([{"name": "x", "type": "STRING"}]))
    assert gen.main(["--schema", str(f)]) == 1
    out = capsys.readouterr().out
    assert "REQUIRED" in out and "run_id" in out


def test_the_draft_is_the_block_the_chart_reads_and_the_app_parses(tmp_path):
    """Generator → values → chart → SUPPORT_COLUMNS → parse_columns: one round trip."""
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    block = gen.render(gen.propose(SCHEMA), "my-project.ops.control_runs")
    values = tmp_path / "v.yaml"
    values.write_text(block)
    out = subprocess.run(["helm", "template", "t", str(ROOT / "helm" / "release-copilot"), "-f", str(values)],
                         capture_output=True, text=True, check=True).stdout
    line = next(x for x in out.splitlines() if x.strip().startswith("SUPPORT_COLUMNS:"))
    rendered = line.split(":", 1)[1].strip().strip('"')
    columns, _ = st.parse_columns(rendered)
    assert columns["date"] == "biz_date" and columns["unit"] == "accountId" and len(columns) == 14
    assert out.count("SUPPORT_TABLE:") == 1 and 'SUPPORT_TABLE: "my-project.ops.control_runs"' in out
    assert 'SUPPORT_FAILED_STATUSES: "FAILED,ERROR"' in out and 'SUPPORT_OWNERS: "default=L2 support"' in out


def test_the_chart_refuses_a_name_that_would_split_the_string(tmp_path):
    if shutil.which("helm") is None:
        pytest.skip("helm not installed")
    values = tmp_path / "v.yaml"
    values.write_text('supportTable:\n  table: "p.d.t"\n  columns:\n    date: { column: d, label: "a,b" }\n')
    res = subprocess.run(["helm", "template", "t", str(ROOT / "helm" / "release-copilot"), "-f", str(values)],
                         capture_output=True, text=True)
    assert res.returncode != 0 and "supportTable.columns.date" in res.stderr


# ----- the live-table check ------------------------------------------------------------

def _table(fields, partition="biz_date"):
    return SimpleNamespace(schema=[SimpleNamespace(name=n, field_type=t) for n, t in fields],
                           time_partitioning=SimpleNamespace(field=partition) if partition else None)


@pytest.fixture
def mapped(monkeypatch):
    monkeypatch.setattr(st.settings, "support_table", "p.ops.control_runs")
    monkeypatch.setattr(st.settings, "support_columns",
                        "date=biz_date,run_id=runId,status=state,updated_at=written_at,event_id=event_key,unit=acountId")

    def use(table):
        monkeypatch.setattr(st, "_get_client", lambda: SimpleNamespace(get_table=lambda ref: table))
    return use


def test_a_mapped_column_the_table_lacks_is_named_with_the_nearest_real_one(mapped):
    mapped(_table([("biz_date", "DATE"), ("runId", "STRING"), ("state", "STRING"), ("written_at", "TIMESTAMP"),
                   ("event_key", "STRING"), ("accountId", "STRING"), ("extra", "STRING")]))
    out = st.check_config()
    assert out["ok"] is False
    assert out["problems"] == ["role unit is mapped to column 'acountId', which the table does not have"
                               " — did you mean 'accountId'?"]
    assert out["unmapped"] == ["accountId", "extra"]


def test_a_good_mapping_is_ok_and_notes_what_costs_or_weakens_it(mapped, monkeypatch):
    monkeypatch.setattr(st.settings, "support_columns", "date=biz_date,run_id=runId,status=state")
    mapped(_table([("biz_date", "TIMESTAMP"), ("runId", "STRING"), ("state", "STRING")], partition=None))
    out = st.check_config()
    assert out["ok"] is False and "not DATE" in out["problems"][0]
    mapped(_table([("biz_date", "DATE"), ("runId", "STRING"), ("state", "STRING")], partition=None))
    out = st.check_config()
    assert out["ok"] is True and out["problems"] == []
    assert any("not partitioned" in n for n in out["notes"]) and any("stuck" in n for n in out["notes"])


def test_the_check_never_raises_and_is_gated(mapped, monkeypatch):
    def boom(ref):
        raise RuntimeError("403 Access Denied: permission bigquery.tables.get")
    monkeypatch.setattr(st, "_get_client", lambda: SimpleNamespace(get_table=boom))
    out = st.check_config()
    assert out["ok"] is False and "could not be read" in out["problems"][0] and "Data Viewer" in out["hint"]

    from fastapi.testclient import TestClient

    from release_agent import app_fastapi, features

    monkeypatch.setattr(features.settings, "preview_features", "support-triage")
    monkeypatch.setattr(features.settings, "preview_users", "")
    assert TestClient(app_fastapi.app).get("/api/support/config-check").status_code == 403
