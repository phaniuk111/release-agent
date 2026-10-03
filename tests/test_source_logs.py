"""Source-service logs: the filter is built from checked parts, entries group and
get security-tagged deterministically, paging stops at the cap, and a failure is
a dict with a hint — never an exception. No network: a fake session stands in."""
from datetime import datetime, timedelta, timezone

import pytest

from release_agent.config import settings
from release_agent.tools import source_logs as S

START = datetime(2026, 10, 2, 9, 0, tzinfo=timezone.utc)
END = START + timedelta(hours=1)


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    for name, value in {"support_cluster": "cluster-a", "support_namespace": "ns-a",
                        "support_source_label": "app", "support_logs_project": "proj-x",
                        "support_logs_max_lines": 500}.items():
        monkeypatch.setattr(settings, name, value)


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    """Answers entries:list pages in order and records every request body."""

    def __init__(self, *pages):
        self.pages, self.bodies = list(pages), []

    def post(self, url, json=None, timeout=None):
        assert url == S._ENTRIES_URL
        self.bodies.append(json)
        return self.pages.pop(0)


def entry(text, when="2026-10-02T09:10:00Z", severity="ERROR", **extra):
    return {"textPayload": text, "timestamp": when, "severity": severity, **extra}


def page(entries, token=""):
    body = {"entries": entries}
    if token:
        body["nextPageToken"] = token
    return _Resp(200, body)


# ----- filter ------------------------------------------------------------------------

def test_the_source_filter_is_exact():
    assert S.build_filter("sys-b-fetcher", START, END) == (
        'resource.type="k8s_container" AND resource.labels.cluster_name="cluster-a" '
        'AND resource.labels.namespace_name="ns-a" AND labels."k8s-pod/app"="sys-b-fetcher" '
        'AND timestamp>="2026-10-02T09:00:00.000000Z" AND timestamp<="2026-10-02T10:00:00.000000Z" '
        "AND severity>=ERROR")


def test_a_naive_datetime_is_utc_and_the_label_is_configurable(monkeypatch):
    monkeypatch.setattr(settings, "support_source_label", "k8s.name")
    f = S.build_filter("svc-a", datetime(2026, 10, 2, 9, 0), END, "warning")
    assert 'labels."k8s-pod/k8s.name"="svc-a"' in f
    assert 'timestamp>="2026-10-02T09:00:00.000000Z"' in f and f.endswith("severity>=WARNING")


@pytest.mark.parametrize("bad", ['svc-a" OR 1=1', "svc a", "svc-a\nx", "", "  ", "svcé", "a/b", "x'y", "a)b"])
def test_an_unsafe_source_never_reaches_the_filter(bad):
    with pytest.raises(ValueError):
        S.build_filter(bad, START, END)
    out = S.read(bad, START, END, session=_Session())
    assert out["ok"] is False and out["hint"] == "nothing was queried"


def test_unsafe_configuration_and_severity_and_window_are_refused(monkeypatch):
    with pytest.raises(ValueError):
        S.build_filter("svc-a", START, END, "ERROR OR severity>=DEBUG")
    with pytest.raises(ValueError):
        S.build_filter("svc-a", END, START)
    monkeypatch.setattr(settings, "support_namespace", 'ns-a" OR x="')
    with pytest.raises(ValueError):
        S.build_filter("svc-a", START, END)


def test_audit_filters_and_principal_validation():
    assert S.denial_filter(START, END) == (
        'logName:"cloudaudit.googleapis.com" AND protoPayload.status.code=7 '
        'AND timestamp>="2026-10-02T09:00:00.000000Z" AND timestamp<="2026-10-02T10:00:00.000000Z"')
    f = S.denial_filter(START, END, "sa-a@proj-x.iam.gserviceaccount.com")
    assert 'protoPayload.authenticationInfo.principalEmail="sa-a@proj-x.iam.gserviceaccount.com"' in f
    for bad in ['a@b.c" OR 1=1', "no-at-sign", "a@b@c.d", "a@nodot", "a b@c.d"]:
        with pytest.raises(ValueError):
            S.denial_filter(START, END, bad)
    assert 'protoPayload.methodName:"SetIamPolicy"' in S.iam_change_filter(START, END)


# ----- security tagging --------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "403 Forbidden from storage.googleapis.com", "PERMISSION_DENIED: caller lacks access",
    "Access denied for user", "x509: certificate has expired", "TLS handshake timeout",
    "token refresh failed", "HTTP 401", "iam check failed"])
def test_security_messages(text):
    assert S.is_security(text)


@pytest.mark.parametrize("text", [
    "connection reset by peer while reading feed", "tokenizer ran out of input",
    "secretary field missing", "wrote 14031 rows", "sslike thing", ""])
def test_a_word_match_not_a_substring_match(text):
    assert not S.is_security(text)


# ----- grouping ----------------------------------------------------------------------

def test_grouping_by_signature_with_counts_times_and_worst_severity():
    entries = [
        entry("connection reset reading feed 20261002", "2026-10-02T09:30:00Z"),
        entry("connection reset reading feed 20261001", "2026-10-02T09:05:00Z", "CRITICAL"),
        entry("403 Forbidden from bucket landing-20261002", "2026-10-02T09:20:00Z"),
        {"jsonPayload": {"message": "403 Forbidden from bucket landing-1"}, "timestamp": "2026-10-02T09:40:00Z",
         "severity": "ERROR"},
        {"jsonPayload": {"msg": "oops 7"}, "timestamp": "2026-10-02T09:41:00Z", "severity": "ERROR"},
        {"jsonPayload": {"code": 5, "what": "x"}, "timestamp": "2026-10-02T09:42:00Z", "severity": "ERROR"},
    ]
    groups = S.group_entries(entries)
    by = {g["signature"]: g for g in groups}
    forb = by["# Forbidden from bucket #"]
    assert forb["count"] == 2 and forb["security"] is True
    assert (forb["first_seen"], forb["last_seen"]) == ("2026-10-02T09:20:00Z", "2026-10-02T09:40:00Z")
    conn = by["connection reset reading feed #"]
    assert conn["count"] == 2 and conn["severity"] == "CRITICAL" and conn["security"] is False
    assert conn["first_seen"] == "2026-10-02T09:05:00Z" and conn["last_seen"] == "2026-10-02T09:30:00Z"
    assert "oops #" in by
    assert '{"code":5,"what":"x"}' in {g["sample"] for g in groups}      # no message key: compact JSON
    assert [g["count"] for g in groups] == sorted((g["count"] for g in groups), reverse=True)


def test_sample_is_capped():
    g = S.group_entries([entry("x" * 1000)])[0]
    assert len(g["sample"]) == 300


# ----- read: paging, truncation, summary ---------------------------------------------

def test_read_pages_and_summarises():
    sec = [entry("403 Forbidden from bucket landing-20261002") for _ in range(5)]
    other = [entry("connection reset by peer reading feed 20261002") for _ in range(3)]
    sess = _Session(page(sec, "t1"), page(other))
    out = S.read("sys-b-fetcher", START, END, session=sess)
    assert out["ok"] and out["lines_read"] == 8 and out["truncated"] is False
    assert (out["source"], out["cluster"], out["namespace"]) == ("sys-b-fetcher", "cluster-a", "ns-a")
    assert out["window"] == {"start": "2026-10-02T09:00:00.000000Z", "end": "2026-10-02T10:00:00.000000Z"}
    assert [g["count"] for g in out["groups"]] == [5, 3]
    assert (out["security_groups_count"], out["security_lines"]) == (1, 5)
    assert out["summary"] == "8 error lines in 2 groups; 1 security group (403 Forbidden from bucket landing-20261002 × 5)"
    assert sess.bodies[0]["resourceNames"] == ["projects/proj-x"] and sess.bodies[0]["pageSize"] == 200
    assert "pageToken" not in sess.bodies[0] and sess.bodies[1]["pageToken"] == "t1"
    assert sess.bodies[0]["orderBy"] == "timestamp desc"


def test_read_stops_at_the_cap_and_says_so(monkeypatch):
    monkeypatch.setattr(settings, "support_logs_max_lines", 250)
    sess = _Session(page([entry("boom 1")] * 200, "t1"), page([entry("boom 2")] * 50, "t2"))
    out = S.read("svc-a", START, END, session=sess)
    assert out["lines_read"] == 250 and out["truncated"] is True
    assert [b["pageSize"] for b in sess.bodies] == [200, 50]      # never asks for more than it will keep
    assert out["summary"].startswith("250+ error lines in 1 group")


def test_exactly_the_cap_with_nothing_more_is_not_truncated(monkeypatch):
    monkeypatch.setattr(settings, "support_logs_max_lines", 3)
    out = S.read("svc-a", START, END, session=_Session(page([entry("boom")] * 3)))
    assert out["lines_read"] == 3 and out["truncated"] is False


def test_no_lines_is_a_clean_answer():
    out = S.read("svc-a", START, END, session=_Session(page([])))
    assert out["ok"] and out["groups"] == [] and out["summary"] == "no error lines in the window"


def test_a_lower_severity_names_itself_in_the_summary():
    out = S.read("svc-a", START, END, min_severity="warning", session=_Session(page([entry("slow", severity="WARNING")])))
    assert out["summary"] == "1 warning-or-worse lines in 1 group"


# ----- never raises ------------------------------------------------------------------

def test_disabled_without_cluster_or_namespace(monkeypatch):
    monkeypatch.setattr(settings, "support_cluster", "")
    assert not S.enabled()
    out = S.read("svc-a", START, END, session=_Session())
    assert out["ok"] is False and out["disabled"] is True and "SUPPORT_CLUSTER" in out["error"]


def test_403_hints_at_the_role():
    out = S.read("svc-a", START, END, session=_Session(_Resp(403, {"error": {"message": "caller lacks permission"}})))
    assert out["ok"] is False and out["hint"] == "needs roles/logging.viewer on proj-x"
    assert "403" in out["error"] and "caller lacks permission" in out["error"]


def test_other_failures_are_dicts_too():
    out = S.read("svc-a", START, END, session=_Session(_Resp(400, ValueError("not json"))))
    assert out["ok"] is False and "rejected" in out["hint"]

    class Boom:
        def post(self, *a, **k):
            raise ConnectionError("proxy said no")

    out = S.read("svc-a", START, END, session=Boom())
    assert out["ok"] is False and "ConnectionError" in out["error"] and out["hint"]


def test_no_project_is_reported(monkeypatch):
    monkeypatch.setattr(settings, "support_logs_project", "")
    monkeypatch.setattr(settings, "gcp_project", "")
    assert S.read("svc-a", START, END, session=_Session())["ok"] is False
    assert S.audit(START, END, session=_Session())["ok"] is False


def test_the_project_falls_back_to_gcp_project(monkeypatch):
    monkeypatch.setattr(settings, "support_logs_project", "")
    monkeypatch.setattr(settings, "gcp_project", "proj-y")
    sess = _Session(page([]))
    S.read("svc-a", START, END, session=sess)
    assert sess.bodies[0]["resourceNames"] == ["projects/proj-y"]


# ----- audit -------------------------------------------------------------------------

def denial(method, resource, who, when, service="bigquery.googleapis.com"):
    return {"timestamp": when, "protoPayload": {
        "methodName": method, "serviceName": service, "resourceName": resource,
        "authenticationInfo": {"principalEmail": who}, "status": {"code": 7, "message": "Access Denied: nope"}}}


def test_audit_groups_denials_and_lists_iam_changes():
    denied = [
        denial("jobservice.query", "projects/proj-x/datasets/d1/tables/t1", "sa-a@proj-x.iam.gserviceaccount.com",
               "2026-10-01T10:00:00Z"),
        denial("jobservice.query", "projects/proj-x/datasets/d1/tables/t2", "sa-a@proj-x.iam.gserviceaccount.com",
               "2026-10-01T11:00:00Z"),
        denial("jobservice.query", "projects/proj-x/datasets/d1/tables/t3", "user-a@example.com",
               "2026-10-01T09:00:00Z"),
        denial("storage.objects.get", "projects/_/buckets/b1/objects/o", "sa-a@proj-x.iam.gserviceaccount.com",
               "2026-10-01T12:00:00Z", "storage.googleapis.com"),
    ]
    changed = [{"timestamp": "2026-10-01T08:00:00Z", "protoPayload": {
        "methodName": "SetIamPolicy", "resourceName": "projects/proj-x",
        "authenticationInfo": {"principalEmail": "admin-a@example.com"}}}]
    sess = _Session(page(denied), page(changed))
    out = S.audit(START, END, session=sess)
    assert out["ok"] and out["denials_total"] == 4 and out["denials_truncated"] is False
    top = out["denials"][0]
    assert (top["method"], top["service"], top["resource"], top["count"]) == (
        "jobservice.query", "bigquery.googleapis.com", "projects/proj-x/datasets/d1", 3)
    assert top["first_seen"] == "2026-10-01T09:00:00Z" and top["last_seen"] == "2026-10-01T11:00:00Z"
    assert top["principals"] == ["sa-a@proj-x.iam.gserviceaccount.com", "user-a@example.com"]
    assert out["denials"][1]["resource"] == "projects/_/buckets/b1"
    assert out["iam_changes"] == [{"time": "2026-10-01T08:00:00Z", "principal": "admin-a@example.com",
                                   "resource": "projects/proj-x", "method": "SetIamPolicy"}]
    assert out["summary"] == ("4 permission-denied calls in 2 groups "
                              "(most: jobservice.query on projects/proj-x/datasets/d1 × 3); 1 IAM policy change")
    assert "principalEmail" not in sess.bodies[0]["filter"] and "SetIamPolicy" in sess.bodies[1]["filter"]


def test_audit_caps_at_200_each_and_filters_by_principal():
    many = [denial("m", f"projects/p/datasets/d/tables/t{i}", "sa-a@x.example", "2026-10-01T10:00:00Z")
            for i in range(200)]
    sess = _Session(page(many, "more"), page([]))
    out = S.audit(START, END, principal="sa-a@x.example", session=sess)
    assert out["denials_total"] == 200 and out["denials_truncated"] is True
    assert 'principalEmail="sa-a@x.example"' in sess.bodies[0]["filter"]
    assert out["summary"].startswith("200+ permission-denied calls by sa-a@x.example")


def test_audit_never_raises():
    out = S.audit(START, END, principal='x@y.z" OR 1=1', session=_Session())
    assert out["ok"] is False and out["hint"] == "nothing was queried"
    out = S.audit(START, END, session=_Session(_Resp(403, {})))
    assert out["ok"] is False and out["hint"] == "needs roles/logging.viewer on proj-x"


def test_denied_bigquery_jobs_group_by_collection_not_job_id():
    rows = [denial("google.cloud.bigquery.v2.JobService.InsertJob", f"projects/proj-x/jobs/job-{i}", "user-a@example.com",
                   "2026-10-01T10:00:00Z") for i in range(3)]
    groups = S.group_denials(rows)
    assert len(groups) == 1 and groups[0]["resource"] == "projects/proj-x/jobs" and groups[0]["count"] == 3
    assert S._resource_prefix("") == "" and S._resource_prefix("x") == "x"
