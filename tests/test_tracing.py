"""Trace collection: JSONL writing, plugin callbacks, and the never-break contract."""
import asyncio
import json
from types import SimpleNamespace

from adk_release_agent import tracing


def _read_lines(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def test_log_event_appends_jsonl(tmp_path, monkeypatch):
    trace = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRACE_LOG_PATH", str(trace))
    tracing.log_event({"type": "x", "a": 1})
    tracing.log_router_decision("t1", "deploy svc:1 to uat", "deploy_workflow:deterministic")
    lines = _read_lines(trace)
    assert [ln["type"] for ln in lines] == ["x", "router_decision"]
    assert all("ts" in ln for ln in lines)
    assert lines[1]["thread_id"] == "t1"
    assert lines[1]["route"] == "deploy_workflow:deterministic"


def test_tracing_disabled_with_empty_path(tmp_path, monkeypatch):
    monkeypatch.setenv("TRACE_LOG_PATH", "")
    tracing.log_event({"type": "x"})  # must not raise or create anything
    assert list(tmp_path.iterdir()) == []


def test_log_event_never_raises(monkeypatch):
    # An unwritable path (directory collision) must be swallowed.
    monkeypatch.setenv("TRACE_LOG_PATH", "/dev/null/nope/t.jsonl")
    tracing.log_event({"type": "x"})  # no exception


def test_plugin_records_call_result_and_error(tmp_path, monkeypatch):
    trace = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRACE_LOG_PATH", str(trace))
    plugin = tracing.TraceLoggerPlugin()
    tool = SimpleNamespace(name="check_release_window")
    ctx = SimpleNamespace(session=SimpleNamespace(id="sess-1"))

    async def run():
        r1 = await plugin.before_tool_callback(tool=tool, tool_args={"q": 1}, tool_context=ctx)
        r2 = await plugin.after_tool_callback(
            tool=tool, tool_args={"q": 1}, tool_context=ctx, result={"ok": True}
        )
        r3 = await plugin.on_tool_error_callback(
            tool=tool, tool_args={"q": 1}, tool_context=ctx, error=RuntimeError("boom")
        )
        return r1, r2, r3

    r1, r2, r3 = asyncio.run(run())
    # Callbacks never short-circuit or rewrite results.
    assert r1 is None and r2 is None and r3 is None

    lines = _read_lines(trace)
    assert [ln["type"] for ln in lines] == ["tool_call", "tool_result", "tool_error"]
    assert lines[0]["tool"] == "check_release_window"
    assert lines[0]["session_id"] == "sess-1"
    assert lines[1]["latency_ms"] is not None and lines[1]["latency_ms"] >= 0
    assert "ok" in lines[1]["result_preview"]
    assert "RuntimeError: boom" in lines[2]["error"]


def test_result_preview_is_bounded(tmp_path, monkeypatch):
    trace = tmp_path / "t.jsonl"
    monkeypatch.setenv("TRACE_LOG_PATH", str(trace))
    plugin = tracing.TraceLoggerPlugin()
    tool = SimpleNamespace(name="big")
    ctx = SimpleNamespace(session=SimpleNamespace(id="s"))

    asyncio.run(
        plugin.after_tool_callback(
            tool=tool, tool_args={}, tool_context=ctx, result={"blob": "x" * 100_000}
        )
    )
    (line,) = _read_lines(trace)
    assert len(line["result_preview"]) <= 2000
