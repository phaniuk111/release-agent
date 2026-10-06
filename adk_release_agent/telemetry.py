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
nothing it should not. The person is named by a stable digest rather than
their email under that default too (``user_ref``), so per-user grouping still
works without the address itself leaving. Export is batched on a background thread: a sink that
is down drops spans and never delays a chat turn.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import os
from collections.abc import Mapping
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

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
    instrumented ones.

    Unlike ``exporter_env``, which leaves an operator's own OTEL_* settings
    alone, these are set unconditionally: the two families must agree, and a
    half-set pair would let one path export what the other hides. TRACE_CONTENT
    is the single switch that decides it."""
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


def user_ref(user_id: str, trace_content: bool) -> str:
    """PURE: how a person is named in a span. Langfuse groups by this, so it
    must be STABLE — but a verified corporate email is exactly the kind of
    thing that should not leave the bank for a sink that may sit outside it.
    With content off it is a short digest: the same person is still one user in
    Langfuse, without the address being readable there. With content on (a
    self-hosted Langfuse, where the prompts are already going) it is the email,
    because then a name is more useful than a pseudonym."""
    text = (user_id or "").strip()
    if not text or trace_content:
        return text
    return "u:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _turn_attributes(span, route, thread_id, user_id, session_id, detail):
    who = user_ref(user_id, settings.trace_content)
    try:
        span.set_attribute("release_copilot.route", route)
        span.set_attribute("release_copilot.thread_id", thread_id)
        # Both spellings: OpenInference's, and Langfuse's own trace-level keys.
        span.set_attribute("session.id", session_id)
        span.set_attribute("user.id", who)
        span.set_attribute("langfuse.session.id", session_id)
        span.set_attribute("langfuse.user.id", who)
        if detail and settings.trace_content:
            span.set_attribute("release_copilot.route_detail", detail[:500])
    except Exception:  # noqa: BLE001 — an attribute must never break a turn
        logger.debug("turn span attributes failed", exc_info=True)


async def traced_stream(source, route, *, thread_id, user_id, session_id, detail=""):
    """Re-yield ``source``'s events under a ``turn`` span — the router's
    decision for one chat turn, and the parent of everything ADK records for it.

    The span is made CURRENT only while ``source`` is producing, never across a
    ``yield``. That is the whole point of this function rather than a plain
    ``with`` block around the loop: an async generator runs in its CALLER's
    context, so a context attached before a yield is detached from a different
    context when the generator resumes — OpenTelemetry refuses that token
    ("was created in a different Context"), logs it with a traceback on every
    turn, and leaves the context attached. The leak then reparents whatever the
    caller does next onto the finished turn, and two turns handled in one task
    collapse into one trace.

    Without a configured exporter the span is non-recording, so callers never
    check whether tracing is on. ``detail`` (the classifier's payload) is
    content and only kept with TRACE_CONTENT.
    """
    from opentelemetry import context as otel_context

    span = _tracer().start_span("turn")
    _turn_attributes(span, route, thread_id, user_id, session_id, detail)
    ctx = trace.set_span_in_context(span)
    try:
        while True:
            token = otel_context.attach(ctx)
            try:
                event = await source.__anext__()
            except StopAsyncIteration:
                break
            finally:
                otel_context.detach(token)   # same context that attached it
            yield event                       # nothing attached while suspended
    except BaseException as e:
        # GeneratorExit (the client disconnected) is an ending, not a failure.
        if not isinstance(e, GeneratorExit):
            span.record_exception(e)
            span.set_status(Status(StatusCode.ERROR, str(e)[:200]))
        raise
    finally:
        span.end()
        await source.aclose()   # the inner stream must not outlive the turn
