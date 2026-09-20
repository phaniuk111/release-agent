"""Preview features: hidden from everyone but the people testing them — in the
page, the API and the chat alike."""
import json
import pathlib
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


def test_every_server_gated_pill_sits_in_a_preview_group_by_default():
    """A pill whose feature the server refuses by default (PREVIEW_FEATURES)
    must live in a group PREVIEW_GROUPS hides by default — otherwise everyone
    sees a pill that only ever answers 403. A pill's form key is its feature key."""
    from release_agent.config import Settings

    fields = Settings.model_fields
    default_groups = {g.strip() for g in fields["preview_groups"].default.split(",") if g.strip()}
    gated = {f.strip() for f in fields["preview_features"].default.split(",") if f.strip()}
    assert gated, "nothing is gated by default — the test would be vacuous"
    palette = (pathlib.Path(APP.__file__).parent / "static" / "palette.js").read_text()
    for feature in gated:
        lines = [line for line in palette.splitlines() if f"form:'{feature}'" in line and "group:'" in line]
        assert lines, f"no pill for the gated feature {feature}"
        for line in lines:
            group = line.split("group:'", 1)[1].split("'", 1)[0]
            assert group in default_groups, (feature, group)


def test_the_bq_cost_report_is_released_not_preview():
    """Asked for explicitly: the BQ cost report is for everyone — nothing gates
    its routes or tools by default and its Monitoring group is not hidden."""
    from release_agent.config import Settings

    fields = Settings.model_fields
    assert "bq-cost" not in fields["preview_features"].default
    assert "Monitoring" not in fields["preview_groups"].default
    palette = (pathlib.Path(APP.__file__).parent / "static" / "palette.js").read_text()
    line = next(line for line in palette.splitlines() if "form:'bq-cost'" in line)
    assert "group:'Monitoring'" in line
    assert features.allowed("bq-cost", None), "an anonymous caller is allowed under the default gating"


def test_a_refusal_names_the_feature_like_a_person_would():
    assert features.refusal("monitoring") == "Monitoring is not available yet — it is a preview feature."
    assert features.refusal("bq-cost") == "The BigQuery cost report is not available yet — it is a preview feature."
