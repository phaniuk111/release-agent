"""Agent observability (adk_release_agent/telemetry.py): the exporter wiring
is decided from configuration and silent without it; the turn span carries
the ids Langfuse groups by; no key ever reaches a log or a status payload."""
import base64
import os

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from adk_release_agent import telemetry as T

OTEL_KEYS = (
    "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT",
    "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "OTEL_EXPORTER_OTLP_HEADERS", "OTEL_SERVICE_NAME",
    "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS", "OPENINFERENCE_HIDE_INPUTS", "OPENINFERENCE_HIDE_OUTPUTS",
)


# --- exporter_env: pure ---------------------------------------------------------

def test_off_without_a_host_or_an_endpoint():
    assert T.exporter_env(host="", public_key="pk", secret_key="sk", service_name="x", env={}) == {}, \
        "keys alone enable nothing"


def test_langfuse_host_and_keys_become_the_traces_endpoint_and_basic_auth():
    out = T.exporter_env(host="https://langfuse.example.com/", public_key="pk-lf-1", secret_key="sk-lf-2",
                         service_name="release-copilot", env={})
    auth = base64.b64encode(b"pk-lf-1:sk-lf-2").decode()
    assert out == {
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://langfuse.example.com/api/public/otel/v1/traces",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": f"Authorization=Basic {auth},x-langfuse-ingestion-version=4",
        "OTEL_SERVICE_NAME": "release-copilot",
    }
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in out, "traces only — Langfuse ingests no metrics or logs"


def test_an_operator_s_own_otlp_settings_win():
    env = {"OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318/v1/traces",
           "OTEL_EXPORTER_OTLP_HEADERS": "x=y", "OTEL_SERVICE_NAME": "mine"}
    out = T.exporter_env(host="https://langfuse.example.com", public_key="pk", secret_key="sk",
                         service_name="release-copilot", env=env)
    assert out == {}, "nothing overridden, nothing added"


def test_a_generic_endpoint_without_keys_still_gets_a_service_name():
    out = T.exporter_env(host="", public_key="", secret_key="", service_name="release-copilot",
                         env={"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"})
    assert out == {"OTEL_SERVICE_NAME": "release-copilot"}


def test_the_content_switches_agree_with_each_other():
    assert T.content_env(False) == {"ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "false",
                                    "OPENINFERENCE_HIDE_INPUTS": "true", "OPENINFERENCE_HIDE_OUTPUTS": "true"}
    assert T.content_env(True) == {"ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": "true",
                                   "OPENINFERENCE_HIDE_INPUTS": "false", "OPENINFERENCE_HIDE_OUTPUTS": "false"}


# --- the turn span ----------------------------------------------------------------

@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(T, "_provider", provider)
    return exporter


def test_the_turn_span_carries_the_ids_langfuse_groups_by(spans, monkeypatch):
    monkeypatch.setattr(T.settings, "trace_content", False, raising=False)
    with T.turn_span("chat", thread_id="t1", user_id="alice@example.com", session_id="t1:chat",
                     detail='{"chart": "payments-api"}'):
        pass
    [span] = spans.get_finished_spans()
    assert span.name == "turn"
    a = dict(span.attributes)
    assert a["release_copilot.route"] == "chat" and a["release_copilot.thread_id"] == "t1"
    assert a["session.id"] == "t1:chat" == a["langfuse.session.id"]
    assert a["user.id"] == "alice@example.com" == a["langfuse.user.id"]
    assert "release_copilot.route_detail" not in a, "the classifier's payload is content"


def test_detail_is_kept_only_with_trace_content_and_capped(spans, monkeypatch):
    monkeypatch.setattr(T.settings, "trace_content", True, raising=False)
    with T.turn_span("deploy_workflow:classifier", thread_id="t1", user_id="u", session_id="s", detail="p" * 900):
        pass
    [span] = spans.get_finished_spans()
    assert len(span.attributes["release_copilot.route_detail"]) == 500


def test_the_turn_span_is_a_no_op_without_tracing(monkeypatch):
    monkeypatch.setattr(T, "_provider", None)   # the SDK-less proxy provider: non-recording
    with T.turn_span("chat", thread_id="t", user_id="u", session_id="s"):
        pass   # no exception, nothing to export


# --- setup_tracing: decided once, from settings + environment ---------------------

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(T, "_state", {})
    for k in OTEL_KEYS:
        monkeypatch.delenv(k, raising=False)   # restored (removed again) at teardown
    monkeypatch.setattr(T.settings, "langfuse_host", "", raising=False)
    monkeypatch.setattr(T.settings, "langfuse_public_key", "", raising=False)
    monkeypatch.setattr(T.settings, "langfuse_secret_key", "", raising=False)
    monkeypatch.setattr(T.settings, "otel_service_name", "release-copilot", raising=False)
    monkeypatch.setattr(T.settings, "trace_content", False, raising=False)


def test_setup_is_off_and_silent_without_configuration(clean_env):
    installed = []
    assert T.setup_tracing(install=lambda: installed.append(1)) == {"enabled": False}
    assert not installed
    assert "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT" not in os.environ


def test_setup_wires_langfuse_and_never_reveals_the_key(clean_env, monkeypatch):
    monkeypatch.setattr(T.settings, "langfuse_host", "https://langfuse.example.com", raising=False)
    monkeypatch.setattr(T.settings, "langfuse_public_key", "pk-lf-1", raising=False)
    monkeypatch.setattr(T.settings, "langfuse_secret_key", "sk-lf-secret", raising=False)
    installed = []
    status = T.setup_tracing(install=lambda: installed.append(1))
    assert installed == [1]
    assert status == {"enabled": True, "endpoint": "https://langfuse.example.com/api/public/otel/v1/traces",
                      "content": False, "service": "release-copilot"}
    assert "sk-lf-secret" not in str(status)
    assert os.environ["OTEL_EXPORTER_OTLP_TRACES_HEADERS"].startswith("Authorization=Basic ")
    assert os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] == "false"
    assert os.environ["OPENINFERENCE_HIDE_INPUTS"] == "true"
    assert T.status() == status
    assert T.setup_tracing(install=lambda: installed.append(2)) == status and installed == [1], "once per process"


def test_a_failing_install_leaves_the_app_running(clean_env, monkeypatch):
    monkeypatch.setattr(T.settings, "langfuse_host", "https://langfuse.example.com", raising=False)

    def boom():
        raise RuntimeError("no exporter for you")

    status = T.setup_tracing(install=boom)
    assert status["enabled"] is False and "no exporter for you" in status["error"]
