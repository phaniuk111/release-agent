"""The Re-run a job routes: gated by the rerun-job preview feature; the plan a
preview returns is single-use and bound to who asked for it."""
import pytest
from fastapi.testclient import TestClient

from release_agent import app_fastapi as APP
from release_agent import features, identity
from release_agent.tools import rerun_job

TESTER = identity.Caller(email="tester@example.com")
OTHER = identity.Caller(email="someone@example.com")


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_users", "tester@example.com", raising=False)
    monkeypatch.setattr(features.settings, "preview_features", "rerun-job", raising=False)
    rerun_job._plans.clear()


@pytest.mark.parametrize("method,path,body", [
    ("get", "/api/rerun/candidates", None),
    ("post", "/api/rerun/preview", {"job": "x", "images": ["a:1"]}),
    ("post", "/api/rerun/start", {"plan_id": "p"}),
    ("get", "/api/rerun/abc", None),
])
def test_a_non_preview_caller_is_refused_everywhere(monkeypatch, method, path, body):
    monkeypatch.setattr(APP, "_caller", lambda request: OTHER)
    client = TestClient(APP.app)
    res = client.get(path) if method == "get" else client.post(path, json=body)
    assert res.status_code == 403 and res.json()["error"] == features.refusal("rerun-job")


def test_preview_then_start_is_bound_to_the_caller(monkeypatch):
    seen = {}

    def fake_preview(job, images, include_passed=False, owner=None, thread_id=""):
        seen["preview"] = (job, images, include_passed, owner, thread_id)
        rerun_job._plans["p1"] = {"plan_id": "p1", "owner": owner, "thread_id": thread_id, "created": 9e18,
                                  "job": job, "runs": [], "rows": []}
        return {"ok": True, "plan_id": "p1"}

    started = []
    monkeypatch.setattr(rerun_job, "preview", fake_preview)
    monkeypatch.setattr(rerun_job, "run_batch", lambda batch, api, gone: started.append(batch["id"]))
    monkeypatch.setattr(rerun_job, "_default_api", lambda: object())
    client = TestClient(APP.app)

    monkeypatch.setattr(APP, "_caller", lambda request: TESTER)
    res = client.post("/api/rerun/preview", json={"job": "1691", "images": ["a:1"], "thread_id": "t1"})
    assert res.json() == {"ok": True, "plan_id": "p1"}
    assert seen["preview"] == ("1691", ["a:1"], False, "tester@example.com", "t1")

    monkeypatch.setattr(features.settings, "preview_users", "tester@example.com,someone@example.com", raising=False)
    monkeypatch.setattr(APP, "_caller", lambda request: OTHER)
    res = client.post("/api/rerun/start", json={"plan_id": "p1", "thread_id": "t1"})
    assert res.json()["ok"] is False and "someone else" in res.json()["error"]

    monkeypatch.setattr(APP, "_caller", lambda request: TESTER)
    res = client.post("/api/rerun/start", json={"plan_id": "p1", "thread_id": "t1", "requested_by": "typed@example.com"})
    body = res.json()
    assert body["ok"] and body["batch"]["requested_by"] == "tester@example.com"     # the verified caller wins
    again = client.post("/api/rerun/start", json={"plan_id": "p1", "thread_id": "t1"}).json()
    assert again["ok"] is False and "already used" in again["error"]
