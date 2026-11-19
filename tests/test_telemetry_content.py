"""TRACE_CONTENT is the single switch for what reaches a span (AGENTS.md
invariant 5), proven on real ADK runs: a fake model, one tool given a
secret-looking argument, an in-memory exporter. Each run is a subprocess
(tests/telemetry_run_probe.py) because the global tracer provider can be set
once per process and OpenInference patches ADK module state.

The probe sets OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true and
ADK_CAPTURE_MCP_HTTP_BODIES=true first, the way a cluster operator might."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from adk_release_agent import telemetry as T

ROOT = Path(__file__).resolve().parent.parent
PROBE = ROOT / "tests" / "telemetry_run_probe.py"
SECRETS = ("alice@example.test", "PROMPT-SECRET-quokka", "ARG-SECRET-wombat", "RESULT-SECRET-numbat", "REPLY-SECRET-dingo")


def _run(trace_content: bool, mode: str, kind: str = "agent") -> list[dict]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "ADK_", "OPENINFERENCE_"))}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT / "src"), str(ROOT)])
    done = subprocess.run([sys.executable, str(PROBE), str(trace_content).lower(), mode, kind],
                          env=env, capture_output=True, text=True, timeout=120, check=False)
    lines = [ln for ln in done.stdout.splitlines() if ln.startswith("PROBE-JSON ")]
    assert lines, f"probe failed:\n{done.stderr[-2000:]}"
    return json.loads(lines[-1].removeprefix("PROBE-JSON "))


def _text(spans: list[dict]) -> str:
    return json.dumps(spans)


@pytest.mark.parametrize("mode", ["oi", "native"])
def test_nothing_the_user_or_model_said_reaches_a_span_with_content_off(mode):
    spans = _run(False, mode)
    names = [s["name"] for s in spans]
    assert any(n.startswith("execute_tool") for n in names), names
    assert any(n == "call_llm" or n.startswith("generate_content") for n in names), names
    assert any(n.startswith(("invocation", "invoke_agent", "invoke_workflow", "agent_run")) for n in names), names
    leaked = [s for s in SECRETS if s in _text(spans)]
    assert not leaked, f"content leaked into spans: {leaked}"


@pytest.mark.parametrize("mode", ["oi", "native"])
def test_content_is_on_the_spans_with_content_on(mode):
    spans = _run(True, mode)
    for secret in SECRETS[1:]:   # the person is only on OpenInference's spans
        assert secret in _text(spans), secret
    assert not any(s["events"] for s in spans), "SPAN_ONLY: content is never sent as log-like span events"


def test_the_person_is_a_stable_digest_on_adk_spans_with_content_off():
    spans = _run(False, "oi")
    ids = {s["attrs"]["user.id"] for s in spans if "user.id" in s["attrs"]}
    assert ids == {T.user_ref("alice@example.test", False)}


def test_metadata_survives_with_content_off():
    spans = _run(False, "oi")
    llm = [s for s in spans if s["attrs"].get("openinference.span.kind") == "LLM"]
    assert llm and all(s["attrs"].get("llm.model_name") == "fake-model" for s in llm)
    assert all(s["attrs"].get("llm.token_count.total") == "10" for s in llm)
    tool = next(s for s in spans if s["attrs"].get("openinference.span.kind") == "TOOL")
    assert tool["attrs"]["tool.name"] == "lookup"


@pytest.mark.parametrize("trace_content", [False, True])
def test_a_workflow_without_a_model_still_yields_spans(trace_content):
    spans = _run(trace_content, "oi", "workflow")
    assert spans, "LLM_ENABLED=false must still be observable"
    assert any(s["name"].startswith(("invocation", "invoke_workflow")) for s in spans)
    assert ("WORKFLOW-SECRET-ocelot" in _text(spans)) is trace_content


def test_content_env_pins_every_content_switch():
    off, on = T.content_env(False), T.content_env(True)
    assert off["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "NO_CONTENT"
    assert off["ADK_TELEMETRY_IGNORE_RUN_CONFIG"] == "true", "no per-request RunConfig may re-enable content"
    assert off["ADK_CAPTURE_MCP_HTTP_BODIES"] == "false" == on["ADK_CAPTURE_MCP_HTTP_BODIES"]
    assert on["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] == "SPAN_ONLY"
    assert on["ADK_TELEMETRY_IGNORE_RUN_CONFIG"] == "false"
