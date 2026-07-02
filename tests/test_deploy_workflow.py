"""End-to-end tests for the ADK Workflow deploy graph.

These drive the real ``Workflow`` through ``InMemoryRunner`` (no LLM): turn 1
produces the preview and pauses on the ``RequestInput`` interrupt; turn 2 resumes
with a function-response and asserts the confirmed/rejected routing.
"""
import asyncio
import warnings

import pytest

from adk_release_agent import deploy, deploy_workflow

pytest.importorskip("google.adk")

from google.adk.apps import App, ResumabilityConfig  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402


def _runner() -> InMemoryRunner:
    app = App(
        name=deploy_workflow.DEPLOY_APP_NAME,
        root_agent=deploy_workflow.build_deploy_workflow(),
        resumability_config=ResumabilityConfig(is_resumable=True),
    )
    return InMemoryRunner(app=app)


async def _preview_then_resume(runner: InMemoryRunner, message: str, confirmed: bool):
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        session = await runner.session_service.create_session(
            app_name=deploy_workflow.DEPLOY_APP_NAME, user_id="u"
        )

        preview_text = ""
        token = None
        async for event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part.from_text(text=message)]),
        ):
            lro = getattr(event, "long_running_tool_ids", None) or set()
            for fc in event.get_function_calls() or []:
                if fc.id in lro:
                    token = fc.id
            if event.content and event.content.parts:
                preview_text += "".join(p.text for p in event.content.parts if getattr(p, "text", None))

        resume_msg = types.Content(
            role="user",
            parts=[
                types.Part(
                    function_response=types.FunctionResponse(
                        id=token, name="adk_request_input", response={"confirmed": confirmed}
                    )
                )
            ],
        )
        output = None
        async for event in runner.run_async(
            user_id="u", session_id=session.id, new_message=resume_msg
        ):
            if getattr(event, "output", None) is not None:
                output = event.output
    return preview_text, token, output


def test_deploy_workflow_previews_then_applies_on_confirmation(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    calls = []

    def fake_invoke(name, args):
        calls.append((name, args))
        return {"ok": True, "note": "deployed via workflow"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)

    preview_text, token, output = asyncio.run(
        _preview_then_resume(_runner(), "deploy abc-client-api-svc:1.1.1230 to uat", confirmed=True)
    )

    assert token and token.startswith("CONFIRM-")
    assert "uat/deployment.json" in preview_text
    assert output["ok"] is True
    assert output["confirmed_token"] == token
    assert calls == [
        ("open_release_pr", {"environment": "uat", "image_tags": "abc-client-api-svc:1.1.1230"})
    ]
    # Pending preview consumed on apply.
    assert token not in deploy._PENDING_PREVIEWS


def test_deploy_workflow_cancels_without_mutation_on_rejection(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    calls = []
    monkeypatch.setattr(deploy, "_invoke_tool", lambda name, args: calls.append((name, args)))

    preview_text, token, output = asyncio.run(
        _preview_then_resume(_runner(), "deploy abc-client-api-svc:1.1.1230 to uat", confirmed=False)
    )

    assert token and token.startswith("CONFIRM-")
    assert output["status"] == "cancelled"
    assert output["ok"] is False
    assert calls == []
    # Rejected preview is discarded.
    assert token not in deploy._PENDING_PREVIEWS
