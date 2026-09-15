"""Monitoring checks over PromQL: firing / ok / unknown, never a silent "OK"."""
import json

import pytest

from release_agent.tools import monitoring as M

TARGET = {"configured": True, "base_url": "https://prom.example/api-base", "managed": False, "auth": "none"}


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Prom:
    """Answers per query, like a Prometheus HTTP API."""

    def __init__(self, answers):
        self.answers, self.queries = answers, []

    def get(self, url, params=None, timeout=None):
        q = params["query"]
        self.queries.append(q)
        a = self.answers[q]
        if isinstance(a, Exception):
            raise a
        return a


def vector(*rows):
    return _Resp(200, {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": labels, "value": [1700000000, str(v)]} for labels, v in rows]}})


@pytest.fixture(autouse=True)
def target(monkeypatch):
    monkeypatch.setattr(M._probe, "resolve_target", lambda: dict(TARGET))
    monkeypatch.setattr(M.settings, "monitor_checks", "", raising=False)


def test_default_check_is_targets_down():
    checks, err = M.configured_checks()
    assert err == "" and checks[0]["query"] == "up == 0"


def test_checks_from_config_and_bad_entries_reported(monkeypatch):
    monkeypatch.setattr(M.settings, "monitor_checks", json.dumps([
        {"name": "DAG failures", "query": "sum(x) > 0", "severity": "warn"},
        {"name": "no query"},
    ]), raising=False)
    checks, err = M.configured_checks()
    assert [c["name"] for c in checks] == ["DAG failures"] and checks[0]["severity"] == "warn"
    assert "entry 2" in err
    monkeypatch.setattr(M.settings, "monitor_checks", "{not json", raising=False)
    assert M.configured_checks()[0] == [] and "not valid JSON" in M.configured_checks()[1]


def test_firing_ok_and_unknown(monkeypatch):
    monkeypatch.setattr(M.settings, "monitor_checks", json.dumps([
        {"name": "down", "query": "up == 0"},
        {"name": "errors", "query": "increase(err[10m]) > 0"},
        {"name": "broken", "query": "bad("},
    ]), raising=False)
    prom = _Prom({
        "up == 0": vector(),
        "increase(err[10m]) > 0": vector(({"job": "api"}, 4), ({"job": "web"}, 0)),
        "bad(": _Resp(400, {"status": "error", "error": "parse error at char 5"}),
    })
    out = M.run_checks(session=prom)
    by = {c["name"]: c for c in out["checks"]}
    assert by["down"]["state"] == "ok"
    assert by["errors"]["state"] == "firing" and by["errors"]["count"] == 1, "a zero is not firing"
    assert by["errors"]["series"] == [{"labels": {"job": "api"}, "value": 4.0}]
    assert by["broken"]["state"] == "unknown" and "parse error" in by["broken"]["error"]
    assert out["firing"] == 1 and out["unknown"] == 1 and out["ok"] is False


def test_an_unreachable_endpoint_is_unknown_never_ok():
    out = M.run_checks(session=_Prom({"up == 0": ConnectionError("proxy said no")}))
    assert out["checks"][0]["state"] == "unknown" and out["ok"] is False


def test_a_proxy_login_page_is_not_a_result():
    res = M.run_query("up", session=_Prom({"up": _Resp(200, ValueError("html"))}), target=dict(TARGET))
    assert res["ok"] is False and "proxy" in res["error"]


def test_big_answers_are_capped_with_the_true_count():
    rows = [({"pod": f"p{i}"}, 1) for i in range(55)]
    res = M.run_query("up", session=_Prom({"up": vector(*rows)}), target=dict(TARGET))
    assert res["count"] == 55 and res["truncated"] is True and len(res["series"]) == M._MAX_SERIES


def test_scalars_and_nan():
    scalar = _Resp(200, {"status": "success", "data": {"resultType": "scalar", "result": [1, "2.5"]}})
    assert M.run_query("2.5", session=_Prom({"2.5": scalar}), target=dict(TARGET))["series"][0]["value"] == 2.5
    assert M._value("NaN") is None


def test_empty_and_oversized_queries_are_refused_before_any_call():
    assert M.run_query("  ")["ok"] is False
    assert "longer" in M.run_query("x" * 5000)["error"]


def test_the_chat_tools_are_unlocked_by_the_monitoring_skill():
    from adk_release_agent import tools as T

    assert T.monitoring_checks in T.ADK_CHAT_TOOLS and T.query_metrics in T.ADK_CHAT_TOOLS


def test_the_endpoint_caches_but_refresh_bypasses(monkeypatch):
    from release_agent import app_fastapi as APP

    calls = []
    monkeypatch.setattr("release_agent.tools.monitoring.run_checks", lambda: calls.append(1) or {"checks": []})
    monkeypatch.setattr(APP, "_monitor_cache", {"at": 0.0, "value": None})
    APP.monitoring_endpoint()
    APP.monitoring_endpoint()
    APP.monitoring_endpoint(fresh=1)
    assert len(calls) == 2


def test_a_promql_typo_is_blamed_on_the_query_not_the_project():
    """Seen live: the managed service answers a parse error with HTTP 400, the
    same status as an unknown project — the hint must tell them apart."""
    managed = {**TARGET, "managed": True,
               "base_url": "https://monitoring.googleapis.com/v1/projects/p1/location/global/prometheus"}
    typo = _Resp(400, {"status": "error", "error": 'invalid parameter "query": 1:10: parse error: unclosed left parenthesis'})
    res = M.run_query("sum(rate(", session=_Prom({"sum(rate(": typo}), target=managed)
    assert "does not parse" in res["hint"] and "project" not in res["hint"]
    bad_project = _Resp(400, {"error": {"message": "Request contains an invalid argument."}})
    res = M.run_query("up", session=_Prom({"up": bad_project}), target=managed)
    assert "project" in res["hint"]
