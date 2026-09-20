"""Agent observability: ADK's OpenTelemetry spans, exported over OTLP — to
Langfuse by default — switched on by configuration and silent without it.

Nothing here is hand-written tracing. ADK already traces every turn: the
invocation, each model call with its token usage, each tool call, the deploy
Workflow's nodes. The OpenInference instrumentors take those spans over so
Langfuse shows them as generations and tools, and the google-genai one covers
the scope-guard and intent classifiers, which call Gemini outside ADK. This
module decides only WHERE they go:

    LANGFUSE_HOST + LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY
        -> <host>/api/public/otel/v1/traces, Basic auth      (the common case)
    OTEL_EXPORTER_OTLP_TRACES_ENDPOINT (+ _HEADERS) already in the environment
        -> used as-is: any OTLP sink, a collector, Cloud Trace
    neither
        -> off: no exporter, no local file, nothing

and adds the one span ADK cannot know about — the router's decision for a
turn (deterministic deploy parse, classifier rescue, or chat) — as the parent
of everything ADK records for it, carrying the session and user ids Langfuse
groups by.

Content (prompts, tool arguments, results) enters the spans only with
TRACE_CONTENT=true; the default sends model names, token counts, latency,
tool names and errors — never the text — so a sink outside the bank sees
nothing it should not. Export is batched on a background thread: a sink that
is down drops spans and never delays a chat turn.
"""
from __future__ import annotations

import base64
import contextlib
import logging
import os
from collections.abc import Iterator, Mapping
from typing import Any

from opentelemetry import trace

from release_agent.config import settings

logger = logging.getLogger("release_copilot")

_TRACER_NAME = "release_copilot"
_LANGFUSE_OTEL_PATH = "/api/public/otel/v1/traces"

# Set once per process; tests replace both.
_provider: Any = None
_state: dict[str, Any] = {}


def exporter_env(
    *, host: str, public_key: str, secret_key: str, service_name: str, env: Mapping[str, str]
) -> dict[str, str]:
    """PURE: the exporter variables to set for this configuration — {} when
    tracing is off. Standard OTEL_* variables already present are left alone,
    so an operator pointing at a collector always wins over the Langfuse
    convenience; only the trace signal is configured, because Langfuse ingests
    no metrics or logs and the generic OTEL_EXPORTER_OTLP_ENDPOINT would make
    ADK register those exporters too."""
    out: dict[str, str] = {}
    endpoint = env.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or env.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    base = (host or "").strip().rstrip("/")
    if not endpoint and base:
        endpoint = out["OTEL_EXPORTER_OTLP_TRACES_ENDPOINT"] = base + _LANGFUSE_OTEL_PATH
    if not endpoint:
        return {}
    has_headers = env.get("OTEL_EXPORTER_OTLP_TRACES_HEADERS") or env.get("OTEL_EXPORTER_OTLP_HEADERS")
    if public_key and secret_key and not has_headers:
        auth = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
        out["OTEL_EXPORTER_OTLP_TRACES_HEADERS"] = f"Authorization=Basic {auth},x-langfuse-ingestion-version=4"
    if not env.get("OTEL_SERVICE_NAME") and service_name:
        out["OTEL_SERVICE_NAME"] = service_name
    return out


def content_env(trace_content: bool) -> dict[str, str]:
    """PURE: the switches that keep prompts, arguments and results out of the
    spans (or let them in) — ADK's for its own spans, OpenInference's for the
    instrumented ones. Both must agree or one path leaks what the other hides."""
    flag = "true" if trace_content else "false"
    hide = "false" if trace_content else "true"
    return {
        "ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS": flag,
        "OPENINFERENCE_HIDE_INPUTS": hide,
        "OPENINFERENCE_HIDE_OUTPUTS": hide,
    }


def _install() -> None:
    """Register the exporters ADK builds from the OTEL_* environment, then let
    the OpenInference instrumentors take over ADK's spans and cover google-genai."""
    from google.adk.telemetry.setup import maybe_set_otel_providers
    from openinference.instrumentation.google_adk import GoogleADKInstrumentor
    from openinference.instrumentation.google_genai import GoogleGenAIInstrumentor

    maybe_set_otel_providers()
    GoogleADKInstrumentor().instrument()
    GoogleGenAIInstrumentor().instrument()


def setup_tracing(install=_install) -> dict[str, Any]:
    """Wire tracing once, from settings + environment. Returns what was decided
    — the endpoint and switches, never a key — for the startup log and
    diagnostics. Never raises: tracing must not stop the app."""
    if _state:
        return dict(_state)
    try:
        wanted = exporter_env(
            host=settings.langfuse_host,
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            service_name=settings.otel_service_name,
            env=os.environ,
        )
        os.environ.update(wanted)
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT") or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        if not endpoint:
            _state.update({"enabled": False})
            return dict(_state)
        os.environ.update(content_env(settings.trace_content))
        install()
        _state.update({"enabled": True, "endpoint": endpoint, "content": settings.trace_content,
                       "service": os.environ.get("OTEL_SERVICE_NAME", "")})
        logger.info("tracing: spans go to %s (content in spans: %s)", endpoint, settings.trace_content)
    except Exception as e:  # noqa: BLE001 — observability never takes the portal down
        logger.warning("tracing: setup failed, running without it: %s", e)
        _state.update({"enabled": False, "error": str(e)})
    return dict(_state)


def status() -> dict[str, Any]:
    """What setup_tracing decided (empty before it ran)."""
    return dict(_state)


def _tracer():
    return (_provider or trace.get_tracer_provider()).get_tracer(_TRACER_NAME)


@contextlib.contextmanager
def turn_span(route: str, *, thread_id: str, user_id: str, session_id: str, detail: str = "") -> Iterator[None]:
    """The router's decision for one chat turn — deterministic deploy parse,
    classifier rescue, or chat — as the parent of everything ADK records for
    it. Without a configured exporter this is a non-recording span, so callers
    never check whether tracing is on. ``detail`` (the classifier's payload)
    is content and only kept with TRACE_CONTENT."""
    with _tracer().start_as_current_span("turn") as span:
        try:
            span.set_attribute("release_copilot.route", route)
            span.set_attribute("release_copilot.thread_id", thread_id)
            # Both spellings: OpenInference's, and Langfuse's own trace-level keys.
            span.set_attribute("session.id", session_id)
            span.set_attribute("user.id", user_id)
            span.set_attribute("langfuse.session.id", session_id)
            span.set_attribute("langfuse.user.id", user_id)
            if detail and settings.trace_content:
                span.set_attribute("release_copilot.route_detail", detail[:500])
        except Exception:  # noqa: BLE001 — an attribute must never break a turn
            logger.debug("turn span attributes failed", exc_info=True)
        yield
