"""Subprocess probe for tests/test_telemetry_content.py: one real ADK run with a
fake model and one tool under an in-memory exporter, spans printed as JSON.

A process of its own because the global tracer provider can be set only once
and ADK/OpenInference patch module state that must not leak into other tests.
argv: <trace_content true|false> <oi|native> <agent|workflow>
"""
import asyncio
import json
import os
import sys

trace_content, mode, kind = sys.argv[1] == "true", sys.argv[2], sys.argv[3]
# What a cluster operator might have set; TRACE_CONTENT must still win.
os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
os.environ["ADK_CAPTURE_MCP_HTTP_BODIES"] = "true"

from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter  # noqa: E402

from adk_release_agent import telemetry as T  # noqa: E402

os.environ.update(T.content_env(trace_content))
exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)
if mode == "oi":
    from openinference.instrumentation.google_adk import GoogleADKInstrumentor
    GoogleADKInstrumentor().instrument(tracer_provider=provider, config=T.adk_trace_config())

from google.adk.agents import LlmAgent  # noqa: E402
from google.adk.models.base_llm import BaseLlm  # noqa: E402
from google.adk.models.llm_response import LlmResponse  # noqa: E402
from google.adk.runners import Runner  # noqa: E402
from google.adk.sessions import InMemorySessionService  # noqa: E402
from google.adk.tools import FunctionTool  # noqa: E402
from google.adk.workflow import FunctionNode, Workflow  # noqa: E402
from google.genai import types  # noqa: E402

PROMPT = "PROMPT-SECRET-quokka"
USER = "alice@example.test"
ARG = "ARG-SECRET-wombat"
RESULT = "RESULT-SECRET-numbat"
REPLY = "REPLY-SECRET-dingo"


def lookup(token: str) -> dict:
    """Look something up."""
    return {"found": RESULT}


class Fake(BaseLlm):
    model: str = "fake-model"

    async def generate_content_async(self, llm_request, stream: bool = False):
        called = any(p.function_response for c in llm_request.contents for p in c.parts or [])
        part = types.Part(text=REPLY) if called else types.Part(
            function_call=types.FunctionCall(name="lookup", args={"token": ARG}))
        yield LlmResponse(content=types.Content(role="model", parts=[part]),
                          usage_metadata=types.GenerateContentResponseUsageMetadata(
                              prompt_token_count=7, candidates_token_count=3, total_token_count=10))


def step(node_input) -> str:
    return "WORKFLOW-SECRET-ocelot"


if kind == "agent":
    root = LlmAgent(name="probe", model=Fake(), tools=[FunctionTool(lookup)], instruction="be brief")
else:
    root = Workflow(name="probe_wf", edges=[("START", FunctionNode(func=step, name="step"))])


async def main():
    sessions = InMemorySessionService()
    runner = Runner(app_name="probe", agent=root, session_service=sessions)
    await sessions.create_session(app_name="probe", user_id=USER, session_id="s")
    msg = types.Content(role="user", parts=[types.Part.from_text(text=PROMPT)])
    async for _ in runner.run_async(user_id=USER, session_id="s", new_message=msg):
        pass


asyncio.run(main())
provider.force_flush()
out = []
for s in exporter.get_finished_spans():
    out.append({"name": s.name, "attrs": {k: str(v) for k, v in s.attributes.items()},
                "events": [{"name": e.name, "attrs": {k: str(v) for k, v in e.attributes.items()}} for e in s.events]})
print("PROBE-JSON " + json.dumps(out))
