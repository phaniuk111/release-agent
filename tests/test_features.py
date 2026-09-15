"""Preview features: hidden from everyone but the people testing them — in the
page, the API and the chat alike."""
import json
from types import SimpleNamespace

import pytest

from release_agent import app_fastapi as APP
from release_agent import features, identity

TESTER = identity.Caller(email="Tester@Example.com")
OTHER = identity.Caller(email="someone@example.com")


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    s = features.settings
    monkeypatch.setattr(s, "preview_users", "tester@example.com", raising=False)
    monkeypatch.setattr(s, "preview_groups", "Check", raising=False)
    monkeypatch.setattr(s, "preview_features", "monitoring", raising=False)


def test_only_listed_verified_users_are_testers(monkeypatch):
    assert features.is_preview_user(TESTER), "matched case-insensitively"
    assert not features.is_preview_user(OTHER)
    assert not features.is_preview_user(None), "an anonymous caller is never a tester"
    monkeypatch.setattr(features.settings, "preview_users", "*", raising=False)
    assert features.is_preview_user(None), "'*' = everyone: a testers-only deployment"


def test_the_check_group_is_hidden_by_default_for_everyone_else():
    assert features.hidden_groups(OTHER) == ["Check"]
    assert features.hidden_groups(TESTER) == []
    assert features.ui_config(TESTER) == {"hiddenGroups": [], "previewGroups": ["Check"], "preview": True}
    assert features.ui_config(None) == {"hiddenGroups": ["Check"], "previewGroups": [], "preview": False}


def test_monitoring_is_refused_server_side_not_just_hidden(monkeypatch):
    monkeypatch.setattr(APP, "_caller", lambda request: OTHER)
    res = APP.monitoring_endpoint(SimpleNamespace(headers={}))
    assert res.status_code == 403 and "preview" in json.loads(res.body)["error"]
    res = APP.monitoring_alert_policy(SimpleNamespace(headers={}), name="x")
    assert res.status_code == 403


def test_a_tester_gets_monitoring(monkeypatch):
    monkeypatch.setattr(APP, "_caller", lambda request: TESTER)
    monkeypatch.setattr(APP, "_monitor_cache", {"at": 0.0, "value": None})
    monkeypatch.setattr("release_agent.tools.monitoring.run_checks", lambda: {"checks": [], "ok": False})
    assert APP.monitoring_endpoint(SimpleNamespace(headers={})) == {"checks": [], "ok": False}


def test_the_chat_cannot_reach_it_either():
    from adk_release_agent import tools as T

    with identity.activate(OTHER):
        assert "preview" in T.monitoring_checks()["error"]
        assert "preview" in T.query_metrics("up")["error"]


def test_the_page_is_rendered_with_this_callers_view(monkeypatch):
    import asyncio

    monkeypatch.setattr(APP, "_caller", lambda request: None)
    html = asyncio.run(APP.chat_page(SimpleNamespace(headers={}))).body.decode()
    assert 'window.PORTAL_UI = {"hiddenGroups": ["Check"], "previewGroups": [], "preview": false};' in html
    monkeypatch.setattr(APP, "_caller", lambda request: TESTER)
    html = asyncio.run(APP.chat_page(SimpleNamespace(headers={}))).body.decode()
    assert '"preview": true' in html and "{PORTAL_UI}" not in html


def test_the_injected_config_cannot_close_the_script_tag(monkeypatch):
    monkeypatch.setattr(features.settings, "preview_groups", "</script><b>", raising=False)
    import asyncio

    monkeypatch.setattr(APP, "_caller", lambda request: None)
    html = asyncio.run(APP.chat_page(SimpleNamespace(headers={}))).body.decode()
    assert "</script><b>" not in html and "\\u003c/script>" in html
