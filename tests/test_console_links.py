"""The console-links strip: per-environment GKE + dashboard deep links.

CONSOLE_LINKS is operator config that ships EMPTY, so the contract the UI relies
on is: environments always come back (so the strip can name what is unset), and
`url` is only ever a renderable http(s) link.
"""
import json

from fastapi.testclient import TestClient

from release_agent.app_fastapi import app
from release_agent.config import settings


def _payload():
    return TestClient(app).get("/api/console-links").json()


def _links(env_name):
    envs = {e["name"]: e for e in _payload()["envs"]}
    return {link["label"]: link for link in envs[env_name]["links"]}


def test_unset_config_renders_the_placeholder_environments():
    """Defaults are placeholders — the UI needs the entries to render the disabled
    chips that name the variable, so nothing may vanish just because it is unset."""
    payload = _payload()
    assert [e["name"] for e in payload["envs"]] == ["UAT", "PRL1", "PRD"]
    assert payload["configured"] is False
    assert payload["env_var"] == "CONSOLE_LINKS"
    for env in payload["envs"]:
        assert [link["label"] for link in env["links"]] == ["GKE", "Grafana"]
        assert all(link["configured"] is False and link["url"] == "" for link in env["links"])


def test_configured_environments_keep_declared_order(monkeypatch):
    """Key order is tab order and list order is chip order — the operator decides
    both, so neither may be re-sorted on the way out."""
    monkeypatch.setattr(settings, "console_links", json.dumps({
        "PRD": [
            {"label": "GKE", "url": "https://console.example/prd"},
            {"label": "Latency", "url": "https://grafana.example/d/1"},
            {"label": "Errors", "url": "https://grafana.example/d/2"},
        ],
        "UAT": [{"label": "GKE", "url": "https://console.example/uat"}],
    }))
    payload = _payload()
    assert [e["name"] for e in payload["envs"]] == ["PRD", "UAT"]
    assert payload["configured"] is True
    assert [link["label"] for link in payload["envs"][0]["links"]] == ["GKE", "Latency", "Errors"]
    assert _links("PRD")["Latency"]["url"] == "https://grafana.example/d/1"


def test_icon_follows_kind_then_label(monkeypatch):
    """The cluster link and the dashboards must be distinguishable at a glance
    without the operator having to annotate every entry."""
    monkeypatch.setattr(settings, "console_links", json.dumps({
        "UAT": [
            {"label": "GKE", "url": "https://console.example/uat"},
            {"label": "Latency", "url": "https://grafana.example/d/1"},
            {"label": "Fleet", "url": "https://console.example/f", "kind": "cluster"},
        ],
    }))
    links = _links("UAT")
    assert links["GKE"]["icon"] == "fa-cubes"          # inferred from the label
    assert links["Latency"]["icon"] == "fa-chart-line"
    assert links["Fleet"]["icon"] == "fa-cubes"        # explicit kind wins


def test_non_http_url_is_refused(monkeypatch):
    """The value becomes an anchor href, so a javascript:/data: URL would be a
    stored-XSS vector via config. Anything not http(s) degrades to unconfigured."""
    for bad in ("javascript:alert(1)", "data:text/html,<script>", "grafana.example/d/1"):
        monkeypatch.setattr(settings, "console_links", json.dumps(
            {"UAT": [{"label": "Latency", "url": bad}]}))
        link = _links("UAT")["Latency"]
        assert link["configured"] is False, f"{bad!r} should not render as a link"
        assert link["url"] == ""


def test_malformed_config_degrades_to_placeholders(monkeypatch):
    """Optional config must never take the strip (or the app) down — a typo falls
    back to placeholders and reports why."""
    for bad in ("{not json", '["UAT"]', '{"UAT": "https://console.example"}'):
        monkeypatch.setattr(settings, "console_links", bad)
        payload = _payload()
        assert payload["envs"], f"{bad!r} left the strip with nothing to render"
        assert payload["configured"] is False
        if bad == '{"UAT": "https://console.example"}':
            # Valid JSON, wrong inner shape: the env survives, the junk does not.
            assert payload["envs"] == [{"name": "UAT", "links": []}]
        else:
            assert payload["error"], f"{bad!r} should explain itself"
            assert [e["name"] for e in payload["envs"]] == ["UAT", "PRL1", "PRD"]
