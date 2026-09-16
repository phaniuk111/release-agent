"""The diagnostics check for PromQL access."""
import pytest

from release_agent.tools import promql_probe as PQ


class _Resp:
    def __init__(self, status=200, body=None, text=False):
        self.status_code, self._body, self._text, self.reason = status, body, text, "reason"

    def json(self):
        if self._text:
            raise ValueError("not json")
        return self._body


def _vector(*rows):
    return {"status": "success", "data": {"resultType": "vector", "result": list(rows)}}


class _Session:
    """Answers each PromQL expression from a table."""

    def __init__(self, answers, raises=None):
        self.answers, self.raises, self.calls = answers, raises, []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params["query"]))
        if self.raises:
            raise self.raises
        return self.answers[params["query"]]


@pytest.fixture
def cfg(monkeypatch):
    def _set(**kw):
        defaults = {"prometheus_url": "", "prometheus_project": "", "prometheus_auth": "auto",
                    "promql_probe_query": "", "gcp_project": "proj-a"}
        for k, v in {**defaults, **kw}.items():
            monkeypatch.setattr(PQ.settings, k, v)
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    _set()
    return _set


OK = _Resp(200, _vector({"metric": {}, "value": [1, "1"]}))
UP = _Resp(200, _vector({"metric": {}, "value": [1, "42"]}))


def test_no_url_means_the_managed_service_in_the_project_with_google_auth(cfg):
    t = PQ.resolve_target()
    assert t["base_url"] == ("https://monitoring.googleapis.com/v1/projects/proj-a/"
                             "location/global/prometheus")
    assert t["managed"] and t["auth"] == "google"
    cfg(prometheus_project="metrics-scope")
    assert "/projects/metrics-scope/" in PQ.resolve_target()["base_url"]


def test_an_in_cluster_prometheus_needs_no_google_auth_and_api_suffix_is_optional(cfg):
    cfg(prometheus_url="http://prometheus.monitoring.svc:9090/api/v1/")
    t = PQ.resolve_target()
    assert t["base_url"] == "http://prometheus.monitoring.svc:9090" and t["auth"] == "none"


def test_nothing_configured_is_skipped_not_failed(cfg):
    cfg(gcp_project="")
    assert PQ.probe_promql() == {"ok": None, "configured": False,
                                 "note": PQ.probe_promql()["note"]}


def test_access_and_data_both_reported(cfg):
    session = _Session({"vector(1)": OK, "count(up)": UP})
    out = PQ.probe_promql(session=session)
    assert out["ok"] is True and out["access"]["ok"] and out["data"]["count"] == "42"
    assert [q for _, q in session.calls] == ["vector(1)", "count(up)"]
    assert all(u.endswith("/api/v1/query") for u, _ in session.calls)


def test_access_without_any_metrics_says_so(cfg):
    out = PQ.probe_promql(session=_Session({"vector(1)": OK, "count(up)": _Resp(200, _vector())}))
    assert out["ok"] is True and "no `up` series" in out["note"]


def test_a_403_from_the_managed_service_names_the_role_to_grant(cfg):
    denied = _Resp(403, {"error": {"code": 403, "message": "Permission monitoring.timeSeries.list denied"}})
    out = PQ.probe_promql(session=_Session({"vector(1)": denied}))
    assert out["ok"] is False
    assert "monitoring.timeSeries.list" in out["access"]["error"]
    assert "roles/monitoring.viewer" in out["hint"] and "proj-a" in out["hint"]
    assert "data" not in out, "no point querying data without access"


def test_a_self_hosted_endpoint_asking_for_credentials(cfg):
    cfg(prometheus_url="http://prom:9090")
    out = PQ.probe_promql(session=_Session({"vector(1)": _Resp(401, {"status": "error", "error": "unauthorized"})}))
    assert out["ok"] is False and "PROMETHEUS_AUTH" in out["hint"]


def test_a_proxy_page_is_not_mistaken_for_prometheus(cfg):
    out = PQ.probe_promql(session=_Session({"vector(1)": _Resp(200, text=True)}))
    assert out["ok"] is False and "proxy" in out["access"]["error"]


def test_a_cluster_address_behind_a_proxy_gets_the_no_proxy_hint(cfg, monkeypatch):
    cfg(prometheus_url="http://prometheus.monitoring.svc:9090")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:8080")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.corp:8080")
    out = PQ.probe_promql(session=_Session({}, raises=ConnectionError("tunnel failed")))
    assert out["ok"] is False and "NO_PROXY" in out["hint"]
    monkeypatch.setenv("NO_PROXY", "localhost,.svc")
    assert PQ.proxy_hint("http://prometheus.monitoring.svc:9090") is None


def test_the_query_you_rely_on_is_checked_and_values_never_returned(cfg):
    cfg(promql_probe_query='workflow_state{status="error"}')
    rows = [{"metric": {"__name__": "workflow_state", "workflow": "w1"}, "value": [1, "7"]},
            {"metric": {"__name__": "workflow_state", "workflow": "w2"}, "value": [1, "9"]}]
    out = PQ.probe_promql(session=_Session({"vector(1)": OK, "count(up)": UP,
                                            'workflow_state{status="error"}': _Resp(200, _vector(*rows))}))
    assert out["custom"] == {"query": 'workflow_state{status="error"}', "status": 200,
                             "ms": out["custom"]["ms"], "ok": True, "series": 2,
                             "sample_metric": "workflow_state"}
    assert "w1" not in str(out), "labels and values of the series are never returned"


def test_a_custom_query_matching_nothing_is_flagged(cfg):
    cfg(promql_probe_query="missing_metric")
    out = PQ.probe_promql(session=_Session({"vector(1)": OK, "count(up)": UP,
                                            "missing_metric": _Resp(200, _vector())}))
    assert out["custom"]["note"] == "the query ran but matched no series"


def test_the_endpoint_wires_it_in(cfg, monkeypatch):
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi as A

    monkeypatch.setattr(PQ, "probe_promql", lambda: {"ok": True, "target": "t"})
    import google.genai
    monkeypatch.setattr(google.genai, "Client", lambda: (_ for _ in ()).throw(RuntimeError("offline")))
    from release_agent.tools import _common, clone_probe
    monkeypatch.setattr(clone_probe, "probe_clone_paths", lambda *a, **k: {})
    monkeypatch.setattr(_common, "active_deploy_repo", lambda: "")
    monkeypatch.setattr(_common, "_resolve_github_token", lambda: None)
    r = TestClient(A.app).get("/api/diagnostics").json()
    assert r["promql"] == {"ok": True, "target": "t"}


def test_an_unknown_project_is_named_not_reported_as_unreachable(cfg):
    """The real API answers 400 "Request contains an invalid argument." for a
    project that does not exist — it was reached, so never say it was not."""
    bad = _Resp(400, {"error": {"code": 400, "message": "Request contains an invalid argument."}})
    out = PQ.probe_promql(session=_Session({"vector(1)": bad}))
    assert "PROMETHEUS_PROJECT" in out["hint"] and "reach" not in out["hint"]
    cfg(prometheus_url="http://prom:9090")
    out = PQ.probe_promql(session=_Session({"vector(1)": _Resp(500, {"status": "error", "error": "boom"})}))
    assert out["hint"] == "the endpoint answered HTTP 500 — see access.error."
