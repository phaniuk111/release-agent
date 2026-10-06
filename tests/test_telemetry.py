"""Agent observability (adk_release_agent/telemetry.py): the exporter wiring
is decided from configuration and silent without it; the turn span carries
the ids Langfuse groups by; no key ever reaches a log or a status payload."""
import asyncio
import base64
import os

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

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


# --- the turn span over a stream ------------------------------------------------
# traced_stream() exists instead of a plain `with` block around the loop because
# an async generator runs in its CALLER's context: a context attached before a
# yield is detached from a different one when the generator resumes, which
# OpenTelemetry refuses ("Token was created in a different Context") — it logs a
# traceback and leaves the context attached. These tests pin the consequences.

@pytest.fixture
def spans(monkeypatch):
    """The module's tracer, plus an `inner` tracer from the SAME provider —
    trace.get_tracer() would return the global proxy provider's, whose spans
    this exporter never sees (and the global provider cannot be set twice)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(T, "_provider", provider)
    monkeypatch.setattr(T.settings, "trace_content", False, raising=False)
    exporter.inner = provider.get_tracer("inner")
    return exporter


async def _events(n=2, tracer=None):
    """Stands in for the deploy/chat streams: traced work, then an event."""
    for i in range(n):
        if tracer is not None:
            with tracer.start_as_current_span(f"work_{i}"):
                pass
        yield {"type": "token", "i": i}


def _drain(agen):
    async def run():
        return [e async for e in agen]
    return asyncio.run(run())


def test_the_turn_span_carries_the_ids_langfuse_groups_by(spans):
    events = _drain(T.traced_stream(_events(), "chat", thread_id="t1",
                                    user_id="alice@example.com", session_id="t1:chat",
                                    detail='{"chart": "payments-api"}'))
    assert [e["i"] for e in events] == [0, 1], "every event is passed through, in order"
    [span] = spans.get_finished_spans()
    assert span.name == "turn"
    a = dict(span.attributes)
    assert a["release_copilot.route"] == "chat" and a["release_copilot.thread_id"] == "t1"
    assert a["session.id"] == "t1:chat" == a["langfuse.session.id"]
    # the person is a stable digest by default — see user_ref below
    assert a["user.id"] == T.user_ref("alice@example.com", False) == a["langfuse.user.id"]
    assert "release_copilot.route_detail" not in a, "the classifier's payload is content"


def test_detail_is_kept_only_with_trace_content_and_capped(spans, monkeypatch):
    monkeypatch.setattr(T.settings, "trace_content", True, raising=False)
    _drain(T.traced_stream(_events(1), "deploy_workflow:classifier", thread_id="t1",
                           user_id="u", session_id="s", detail="p" * 900))
    [span] = spans.get_finished_spans()
    assert len(span.attributes["release_copilot.route_detail"]) == 500


def test_the_work_that_produces_each_event_nests_under_the_turn(spans):
    """The whole point of the span: ADK's own spans must be its children."""
    tracer = spans.inner
    _drain(T.traced_stream(_events(2, tracer), "chat", thread_id="t", user_id="u", session_id="s"))
    by_name = {s.name: s for s in spans.get_finished_spans()}
    turn = by_name["turn"]
    assert turn.parent is None, "a turn is the root of its trace"
    for name in ("work_0", "work_1"):
        assert by_name[name].parent.span_id == turn.context.span_id


def test_the_context_does_not_leak_past_the_turn(spans):
    """The regression: with a `with` block around the loop, the next span in the
    caller became a CHILD of the finished turn, and two turns in one task
    collapsed into one trace."""
    tracer = spans.inner

    async def run():
        async for _ in T.traced_stream(_events(2), "chat", thread_id="a", user_id="u", session_id="a"):
            pass
        async for _ in T.traced_stream(_events(2), "chat", thread_id="b", user_id="u", session_id="b"):
            pass
        with tracer.start_as_current_span("after"):
            pass

    asyncio.run(run())
    finished = spans.get_finished_spans()
    turns = [s for s in finished if s.name == "turn"]
    assert len(turns) == 2
    assert all(t.parent is None for t in turns), "neither turn may parent the other"
    assert len({t.context.trace_id for t in turns}) == 2, "one trace per turn"
    after = next(s for s in finished if s.name == "after")
    assert after.parent is None, "the turn's context outlived it"


def test_an_abandoned_stream_still_ends_the_span(spans):
    """A client disconnects mid-answer: the span must close there and then, or
    it never reaches the collector."""
    async def run():
        agen = T.traced_stream(_events(5), "chat", thread_id="t", user_id="u", session_id="s")
        await agen.__anext__()
        assert not spans.get_finished_spans(), "still streaming"
        await agen.aclose()

    asyncio.run(run())
    [span] = spans.get_finished_spans()
    assert span.name == "turn"


def test_a_failing_stream_ends_the_span_and_records_the_error(spans):
    async def boom():
        yield {"type": "token"}
        raise RuntimeError("tool blew up")

    async def run():
        async for _ in T.traced_stream(boom(), "chat", thread_id="t", user_id="u", session_id="s"):
            pass

    with pytest.raises(RuntimeError):
        asyncio.run(run())
    [span] = spans.get_finished_spans()
    assert span.status.status_code is StatusCode.ERROR
    assert span.events, "the exception is recorded on the span"


def test_the_turn_span_is_a_no_op_without_tracing(monkeypatch):
    monkeypatch.setattr(T, "_provider", None)   # the SDK-less proxy provider: non-recording
    assert len(_drain(T.traced_stream(_events(2), "chat", thread_id="t", user_id="u", session_id="s"))) == 2


# --- setup_tracing: decided once, from settings + environment ---------------------

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.setattr(T, "_state", {})
    for k in OTEL_KEYS:
        # setenv FIRST so monkeypatch always records a restore for this key: delenv
        # on a key that is absent records nothing, and setup_tracing below then sets
        # it for real — leaking a live exporter endpoint into every later test in the
        # session (app_fastapi calls setup_tracing at import, so it would wire the
        # real instrumentors at langfuse.example.com). Both calls undo to "absent".
        monkeypatch.setenv(k, "")
        monkeypatch.delenv(k, raising=False)
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


# --- who a span says ran the turn -------------------------------------------

def test_a_person_is_a_stable_digest_unless_content_is_on():
    """Langfuse groups by user.id, so it must be stable — but a verified
    corporate email should not leave for a sink outside the bank just because
    someone opened the portal. With content on (self-hosted, where the prompts
    already go) the email is more useful than a pseudonym."""
    a = T.user_ref("alice@example.com", False)
    assert a.startswith("u:") and "alice" not in a and "@" not in a
    assert T.user_ref("alice@example.com", False) == a, "the same person is the same user"
    assert T.user_ref("bob@example.com", False) != a
    assert T.user_ref("alice@example.com", True) == "alice@example.com"
    assert T.user_ref("", False) == "" and T.user_ref("  ", False) == ""


def test_the_span_carries_the_digest_not_the_email_by_default(spans):
    _drain(T.traced_stream(_events(1), "chat", thread_id="t",
                           user_id="alice@example.com", session_id="s"))
    [span] = spans.get_finished_spans()
    a = dict(span.attributes)
    assert a["user.id"] == a["langfuse.user.id"] == T.user_ref("alice@example.com", False)
    assert "alice@example.com" not in str(a), "the address must not be anywhere on the span"
