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


def _types(events):
    """Event types EXCLUDING progress — progress is advisory UI chatter that may
    appear anywhere in a turn, so contract assertions ignore it."""
    return [e["type"] for e in events if e.get("type") != "progress"]


def _interrupt(events):
    return next(e["data"] for e in events if e.get("type") == "interrupt")


def test_adk_service_streams_deterministic_deploy_preview():
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()

    events = _collect(service, "deploy abc-client-api-svc:1.1.1230 to uat")

    assert _types(events) == ["token", "interrupt", "done"]
    assert "uat/deployment.json" in _token_events(events)[0]["content"]
    interrupt = _interrupt(events)
    assert interrupt["type"] == "confirmation"
    assert interrupt["token"].startswith("CONFIRM-")
    assert interrupt["token"] in deploy._PENDING_PREVIEWS


def test_adk_service_confirmation_applies_pending_deploy(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    preview_events = _collect(service, "deploy abc-client-api-svc:1.1.1230 to uat")
    token = _interrupt(preview_events)["token"]
    calls = []

    def fake_invoke(name, args):
        calls.append((name, args))
        return {"ok": True, "note": "deployed via test"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)

    events = _collect(service, token)

    # an applied deploy reports mutated=True so the UI refreshes the banner
    # (a plain question does not — that's what keeps the banner off GitHub)
    assert [e for e in events if e.get("type") != "progress"] == [
        {"type": "token", "content": "deployed via test"},
        {"type": "done", "mutated": True},
    ]
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
                    "gke_namespace": "default",
                }
            ],
        }
    )

    events = _collect(service, payload)

    interrupt = _interrupt(events)
    assert interrupt["environment"] == "prod"
    assert {"uat/deployment.json", "prd/deployment.json"} == set(interrupt["proposed"])


def test_progress_events_describe_tool_calls():
    """Long turns stream `progress` labels so the UI shows what the agent is
    doing instead of silent dots. Labels are human text, never raw tool names
    for known tools, and the HITL confirmation call is excluded (it becomes an
    interrupt of its own)."""
    from types import SimpleNamespace

    from release_agent import adk_service as S

    def call(name, args=None):
        return SimpleNamespace(name=name, args=args or {})

    event = SimpleNamespace(get_function_calls=lambda: [
        call("release_stats"),
        call("load_skill", {"skill_name": "release-queue"}),
        call("some_new_tool"),
        call(S._REQUEST_CONFIRMATION),
    ])
    assert S._progress_events(event) == [
        "Reading the release history",
        "Loading release-queue guidance",
        "Some new tool",
    ]
    # no function calls -> no progress
    assert S._progress_events(SimpleNamespace(get_function_calls=lambda: [])) == []


def test_only_state_changing_tools_mark_a_turn_mutated():
    """The banner is refreshed only when a turn actually changed release state —
    that is what keeps read-only questions off GitHub's rate limit."""
    from types import SimpleNamespace

    from release_agent import adk_service as S

    def ev(*names):
        return SimpleNamespace(
            get_function_calls=lambda: [SimpleNamespace(name=n, args={}) for n in names]
        )

    assert S._changes_release_state(ev("promote_release")) is True
    assert S._changes_release_state(ev("merge_prod_release")) is True
    assert S._changes_release_state(ev("remove_from_release")) is True
    # reads must NOT trigger a refresh
    assert S._changes_release_state(ev("release_stats", "find_prs")) is False
    assert S._changes_release_state(ev("list_release_queue")) is False
    assert S._changes_release_state(ev()) is False
