"""The Release notifications card's route: /api/deploy-tracking is gated by the
deploy-tracking preview feature and hands back deploy_tracker.report as is."""
import pytest
from fastapi.testclient import TestClient

from adk_release_agent import deploy_tracker
from release_agent import app_fastapi as APP
from release_agent import features, identity

TESTER = identity.Caller(email="tester@example.com")
OTHER = identity.Caller(email="someone@example.com")


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_users", "tester@example.com", raising=False)
    monkeypatch.setattr(features.settings, "preview_features", "deploy-tracking", raising=False)


def test_a_non_preview_caller_is_refused(monkeypatch):
    monkeypatch.setattr(APP, "_caller", lambda request: OTHER)
    res = TestClient(APP.app).get("/api/deploy-tracking")
    assert res.status_code == 403 and res.json() == {"ok": False, "error": features.refusal("deploy-tracking")}


def test_a_preview_caller_gets_the_report_even_with_tracking_switched_off(monkeypatch):
    monkeypatch.setattr(APP, "_caller", lambda request: TESTER)
    monkeypatch.setattr(features.settings, "deploy_tracking_enabled", False, raising=False)
    monkeypatch.setattr(deploy_tracker, "report", lambda: {"ok": True, "checks": [], "prs": []})
    res = TestClient(APP.app).get("/api/deploy-tracking")
    assert res.status_code == 200 and res.json() == {"ok": True, "checks": [], "prs": []}
