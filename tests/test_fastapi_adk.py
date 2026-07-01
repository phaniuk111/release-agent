import json

from fastapi.testclient import TestClient

from adk_release_agent import deploy
from release_agent.app_fastapi import app


def _sse_payloads(text: str):
    out = []
    for block in text.strip().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: ") :]))
    return out


def test_chat_endpoint_uses_adk_deploy_preview_path():
    deploy._PENDING_PREVIEWS.clear()
    client = TestClient(app)

    res = client.post(
        "/api/chat",
        json={"thread_id": "fastapi-adk-test", "message": "deploy abc-client-api-svc:1.1.1230 to uat"},
    )

    assert res.status_code == 200
    payloads = _sse_payloads(res.text)
    assert [p["type"] for p in payloads] == ["token", "interrupt", "done"]
    assert "uat/deployment.json" in payloads[0]["content"]
    assert payloads[1]["data"]["token"].startswith("CONFIRM-")


def test_chat_endpoint_applies_confirmed_adk_deploy(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    client = TestClient(app)
    preview = client.post(
        "/api/chat",
        json={"thread_id": "fastapi-adk-apply", "message": "deploy abc-client-api-svc:1.1.1230 to uat"},
    )
    token = _sse_payloads(preview.text)[1]["data"]["token"]
    calls = []

    def fake_invoke(name, args):
        calls.append((name, args))
        return {"ok": True, "note": "fastapi deployed"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)

    res = client.post("/api/chat", json={"thread_id": "fastapi-adk-apply", "message": token})

    assert res.status_code == 200
    assert _sse_payloads(res.text) == [
        {"type": "token", "content": "fastapi deployed"},
        {"type": "done"},
    ]
    assert calls == [
        (
            "open_release_pr",
            {"environment": "uat", "image_tags": "abc-client-api-svc:1.1.1230"},
        )
    ]

