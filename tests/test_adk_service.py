import json
import asyncio

from adk_release_agent import deploy
from release_agent.adk_service import AdkChatService, _looks_like_deploy_request


def _collect(service: AdkChatService, message: str, thread_id: str = "t-adk"):
    async def _run():
        return [event async for event in service.stream_chat(message, thread_id)]

    return asyncio.run(_run())


def _token_events(events):
    return [event for event in events if event.get("type") == "token"]


def test_adk_service_streams_deterministic_deploy_preview():
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()

    events = _collect(service, "deploy abc-client-api-svc:1.1.1230 to uat")

    assert [event["type"] for event in events] == ["token", "interrupt", "done"]
    assert "uat/deployment.json" in _token_events(events)[0]["content"]
    interrupt = events[1]["data"]
    assert interrupt["type"] == "confirmation"
    assert interrupt["token"].startswith("CONFIRM-")
    assert interrupt["token"] in deploy._PENDING_PREVIEWS


def test_adk_service_confirmation_applies_pending_deploy(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    preview_events = _collect(service, "deploy abc-client-api-svc:1.1.1230 to uat")
    token = preview_events[1]["data"]["token"]
    calls = []

    def fake_invoke(name, args):
        calls.append((name, args))
        return {"ok": True, "note": "deployed via test"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)

    events = _collect(service, token)

    assert events == [{"type": "token", "content": "deployed via test"}, {"type": "done"}]
    assert calls == [
        (
            "open_release_pr",
            {"environment": "uat", "image_tags": "abc-client-api-svc:1.1.1230"},
        )
    ]


def test_query_with_chart_tag_is_not_treated_as_deploy():
    deploy._PENDING_PREVIEWS.clear()

    assert not _looks_like_deploy_request("find the PR for abc-client-api-svc:1.1.1230")
    assert deploy._PENDING_PREVIEWS == {}


def test_adk_service_accepts_ui_deploy_json_payload():
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    payload = json.dumps(
        {
            "environment": "prod",
            "include": [
                {
                    "helm_chart_name": "abc-client-api-svc",
                    "helm_chart_version": "1.1.1230",
                    "gke_namespace": "eod1",
                }
            ],
        }
    )

    events = _collect(service, payload)

    interrupt = events[1]["data"]
    assert interrupt["environment"] == "prod"
    assert {"uat/deployment.json", "prd/deployment.json"} == set(interrupt["proposed"])
