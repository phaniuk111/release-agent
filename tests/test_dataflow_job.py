"""Dataflow job lookup: validation, the failure kind, the answer from fake
responses, and the error hints. No network — _session is replaced."""
from datetime import datetime, timezone

import pytest

from release_agent.config import settings
from release_agent.tools import dataflow_job as dj

JOB = "2026-10-03_03_28_27-1234567890123456789"


class FakeResponse:
    def __init__(self, status: int, body):
        self.status_code = status
        self._body = body

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeHttp:
    """Routes by URL tail; records every call. A route value may be a list
    (successive pages) or a callable taking the request params."""

    def __init__(self, routes: dict[str, object]):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def request(self, method, url, **kwargs):
        params = kwargs.get("params") or kwargs.get("json") or {}
        self.calls.append((method, url, params))
        if url.endswith("entries:list"):
            key = "logs"
        elif url.endswith("/messages"):
            key = params["minimumImportance"]
        else:
            key = params["view"]
        value = self.routes[key]
        if isinstance(value, list):
            value = value.pop(0)
        return value if isinstance(value, FakeResponse) else FakeResponse(200, value)


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(settings, "support_dataflow_region", "europe-west2", raising=False)
    monkeypatch.setattr(settings, "support_dataflow_project", "demo-project", raising=False)


def _use(monkeypatch, routes):
    http = FakeHttp(routes)
    monkeypatch.setattr(dj, "_session", lambda: http)
    return http


SUMMARY = {"name": "daily-load", "type": "JOB_TYPE_BATCH", "currentState": "JOB_STATE_FAILED",
           "createTime": "2026-10-03T10:00:00.123456789Z", "startTime": "2026-10-03T10:01:00Z",
           "currentStateTime": "2026-10-03T10:04:30Z"}
DESCRIPTION = {"environment": {"workerPools": [{"machineType": "n1-standard-1", "numWorkers": 1}],
                               "sdkPipelineOptions": {"options": {"maxNumWorkers": 4}},
                               "userAgent": {"name": "Apache Beam SDK for Python", "version": "2.60.0"}}}


# ----- validation ---------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "   ", "job id", "a/b", "a..b", "x\"y", "job;1", "a" * 101, "jé"])
def test_bad_job_ids_are_refused_before_any_call(monkeypatch, bad):
    monkeypatch.setattr(dj, "_session", lambda: pytest.fail("no call for a bad id"))
    out = dj.inspect(bad)
    assert out["ok"] is False and "job id" in out["error"]


@pytest.mark.parametrize("bad", ["europe west2", "europe/west2", "eu_west2", "x?y=1"])
def test_bad_regions_are_refused(monkeypatch, bad):
    monkeypatch.setattr(dj, "_session", lambda: pytest.fail("no call for a bad region"))
    out = dj.inspect(JOB, region=bad)
    assert out["ok"] is False and "region" in out["error"]


def test_missing_region_and_project_say_which_setting(monkeypatch):
    monkeypatch.setattr(dj, "_session", lambda: pytest.fail("no call"))
    monkeypatch.setattr(settings, "support_dataflow_region", "", raising=False)
    assert "SUPPORT_DATAFLOW_REGION" in dj.inspect(JOB)["hint"]
    monkeypatch.setattr(settings, "support_dataflow_project", "", raising=False)
    monkeypatch.setattr(settings, "gcp_project", "", raising=False)
    assert "SUPPORT_DATAFLOW_PROJECT" in dj.inspect(JOB, region="europe-west2")["hint"]


def test_project_falls_back_to_gcp_project(monkeypatch):
    monkeypatch.setattr(settings, "support_dataflow_project", "", raising=False)
    monkeypatch.setattr(settings, "gcp_project", "fallback-project", raising=False)
    http = _use(monkeypatch, {"JOB_VIEW_SUMMARY": SUMMARY, "JOB_VIEW_DESCRIPTION": DESCRIPTION,
                              "JOB_MESSAGE_ERROR": {}, "JOB_MESSAGE_WARNING": {}, "logs": {}})
    out = dj.inspect(JOB)
    assert out["project"] == "fallback-project"
    assert "/projects/fallback-project/locations/europe-west2/jobs/" in http.calls[0][1]


# ----- kind -----------------------------------------------------------------------

@pytest.mark.parametrize("text, kind", [
    ("Container killed by the kernel", "out_of_memory"),
    ("java.lang.OutOfMemoryError: Java heap space, not enough memory", "out_of_memory"),
    ("Quota 'CPUS' exceeded in region", "quota"),
    ("RESOURCE EXHAUSTED: the zone has no capacity", "quota"),
    ("Resources exhausted", "quota"),
    ("Permission denied on bucket", "permission"),
    ("HTTP 403 Forbidden", "permission"),
    ("401 Unauthorized", "permission"),
    ("Cannot cast STRING to INT64", "schema"),
    ("Field mismatch in row", "schema"),
    ("Input file gs://b/x.txt not found", "not_found"),
    ("java.io.FileNotFoundException: No files matched spec: gs://b/x.txt", "not_found"),
    ("No such file or directory", "not_found"),
    ("404 from the metadata server", "not_found"),
    ("NoSuchKey: the specified key", "not_found"),
    ("Lost contact with the worker for 10 minutes", "worker_lost"),
    ("worker lost", "worker_lost"),
    ("The step timed out", "worker_lost"),
    ("something nobody has seen before", "unknown"),
    ("", "unknown"),
])
def test_kind_table(text, kind):
    assert dj.classify([text]) == kind


def test_first_match_in_table_order_wins():
    # a hidden bucket reads as "not found" — it is a permission problem first
    assert dj.classify(["Permission denied: object not found"]) == "permission"
    assert dj.classify(["not found", "killed"]) == "out_of_memory"


def test_words_match_whole_not_as_substrings():
    assert dj.classify(["a prototype of the denial"]) == "unknown"
    assert dj.classify(["error code 4030"]) == "unknown"
    assert dj.classify(["not  \n found"]) == "not_found"       # any separator between the words
    assert dj.classify(["found not"]) == "unknown"             # order matters in a phrase


def test_job_messages_decide_before_worker_logs():
    now = datetime(2026, 10, 3, 10, 10, tzinfo=timezone.utc)
    logs = [{"signature": "s", "sample": "container killed", "count": 9}]
    got = dj.build_result(job_id=JOB, region="r", project="p", job=SUMMARY, options={}, warnings_count=0,
                          errors=[{"text": "Input file not found", "time": None, "count": 1}],
                          log_groups=logs, notes=[], now=now)
    assert got["kind"] == "not_found"
    got = dj.build_result(job_id=JOB, region="r", project="p", job=SUMMARY, options={}, warnings_count=0,
                          errors=[{"text": "Workflow failed", "time": None, "count": 1}],
                          log_groups=logs, notes=[], now=now)
    assert got["kind"] == "out_of_memory"          # the messages said nothing


def test_repeated_job_errors_fold_into_one_with_a_count(monkeypatch):
    twice = [{"messageText": "Failed on part-00001: UserCodeException: java.io.FileNotFoundException: "
                             "No files matched spec: gs://b/x.txt", "time": f"2026-10-03T10:0{i}:00Z"}
             for i in (1, 2)]
    _use(monkeypatch, {"JOB_VIEW_SUMMARY": SUMMARY, "JOB_VIEW_DESCRIPTION": DESCRIPTION,
                       "JOB_MESSAGE_ERROR": {"jobMessages": twice}, "JOB_MESSAGE_WARNING": {}, "logs": {}})
    out = dj.inspect(JOB)
    assert len(out["errors"]) == 1 and out["errors"][0]["count"] == 2
    assert out["errors"][0]["time"] == "2026-10-03T10:01:00Z"
    assert "'No files matched spec: gs://b/x.txt' (job message)" in out["summary"]
    assert out["kind"] == "not_found"


# ----- the answer -------------------------------------------------------------------

def test_answer_from_fake_responses(monkeypatch):
    http = _use(monkeypatch, {
        "JOB_VIEW_SUMMARY": SUMMARY,
        "JOB_VIEW_DESCRIPTION": DESCRIPTION,
        "JOB_MESSAGE_ERROR": {"jobMessages": [
            {"messageText": "Input file gs://b/in.csv not found", "time": "2026-10-03T10:04:00Z",
             "messageImportance": "JOB_MESSAGE_ERROR"}]},
        "JOB_MESSAGE_WARNING": {"jobMessages": [
            {"messageText": "slow", "messageImportance": "JOB_MESSAGE_WARNING"},
            {"messageText": "slower", "messageImportance": "JOB_MESSAGE_WARNING"}]},
        "logs": {"entries": [
            {"jsonPayload": {"message": "Failed to read part-00001: boom"}, "timestamp": "2026-10-03T10:03:00.5Z",
             "resource": {"labels": {"step_id": "ReadInput"}}},
            {"jsonPayload": {"message": "Failed to read part-00002: boom"}, "timestamp": "2026-10-03T10:03:00Z",
             "resource": {"labels": {"step_id": "ReadInput"}}},
            {"textPayload": "Other trouble", "timestamp": "2026-10-03T10:03:30Z"}]},
    })
    out = dj.inspect(JOB)
    assert out["ok"] is True
    assert (out["name"], out["state"], out["type"]) == ("daily-load", "JOB_STATE_FAILED", "JOB_TYPE_BATCH")
    assert out["created"].startswith("2026-10-03T10:00:00.123456")
    assert out["ended"] == "2026-10-03T10:04:30+00:00"
    assert out["duration_minutes"] == 4.5
    assert out["machine_type"] == "n1-standard-1" and out["max_workers"] == 4
    assert out["sdk"] == "Apache Beam SDK for Python" and out["sdk_version"] == "2.60.0"
    assert out["errors"] == [{"text": "Input file gs://b/in.csv not found", "time": "2026-10-03T10:04:00Z",
                              "count": 1}]
    assert out["warnings_count"] == 2
    assert out["kind"] == "not_found"
    groups = {g["signature"]: g for g in out["log_groups"]}
    read = groups["Failed to read # boom"]
    assert read["count"] == 2 and read["step"] == "ReadInput"
    assert read["first_seen"] == "2026-10-03T10:03:00+00:00"
    assert read["last_seen"] == "2026-10-03T10:03:00.500000+00:00"
    assert out["log_groups"][0] is read       # biggest first
    assert out["summary"].startswith("JOB_STATE_FAILED after 4 min — kind: not_found — "
                                     "'Input file gs://b/in.csv not found' (job message)")
    assert out["summary"].endswith("2 worker error groups")
    assert out["console_url"] == f"https://console.cloud.google.com/dataflow/jobs/europe-west2/{JOB}?project=demo-project"
    assert "notes" not in out
    # every call is a read: GETs on Dataflow, one list POST on Logging
    assert {(m, u.endswith("entries:list")) for m, u, _ in http.calls} <= {("GET", False), ("POST", True)}
    log_call = next(c for c in http.calls if c[1].endswith("entries:list"))
    body = log_call[2]
    assert body["resourceNames"] == ["projects/demo-project"]
    assert f'resource.labels.job_id="{JOB}"' in body["filter"] and "severity>=ERROR" in body["filter"]


def test_messages_page_until_the_cap(monkeypatch):
    def page(n, token):
        return {"jobMessages": [{"messageText": f"e{n}-{i}", "messageImportance": "JOB_MESSAGE_ERROR"}
                                for i in range(60)], "nextPageToken": token}

    http = _use(monkeypatch, {"JOB_VIEW_SUMMARY": SUMMARY, "JOB_VIEW_DESCRIPTION": DESCRIPTION,
                              "JOB_MESSAGE_ERROR": [page(1, "t1"), page(2, "t2"), page(3, "t3")],
                              "JOB_MESSAGE_WARNING": {}, "logs": {}})
    out = dj.inspect(JOB)
    assert sum(e["count"] for e in out["errors"]) == 100   # all read, folded by signature
    pages = [c for c in http.calls if c[2].get("minimumImportance") == "JOB_MESSAGE_ERROR"]
    assert len(pages) == 2 and pages[1][2]["pageToken"] == "t1" and pages[1][2]["pageSize"] == 40


def test_logs_honour_the_configured_line_cap(monkeypatch):
    monkeypatch.setattr(settings, "support_logs_max_lines", 3, raising=False)
    entries = [{"textPayload": f"boom {i}", "timestamp": "2026-10-03T10:03:00Z"} for i in range(5)]
    http = _use(monkeypatch, {"JOB_VIEW_SUMMARY": SUMMARY, "JOB_VIEW_DESCRIPTION": DESCRIPTION,
                              "JOB_MESSAGE_ERROR": {}, "JOB_MESSAGE_WARNING": {}, "logs": {"entries": entries}})
    out = dj.inspect(JOB)
    assert sum(g["count"] for g in out["log_groups"]) == 3
    assert next(c for c in http.calls if c[1].endswith("entries:list"))[2]["pageSize"] == 3


def test_running_job_has_no_end_and_no_failure_kind():
    now = datetime(2026, 10, 3, 10, 10, tzinfo=timezone.utc)
    out = dj.build_result(job_id=JOB, region="r", project="p",
                          job={**SUMMARY, "currentState": "JOB_STATE_RUNNING"}, options={}, errors=[],
                          warnings_count=0, log_groups=[], notes=[], now=now)
    assert out["ended"] is None and out["kind"] == "none"
    assert out["summary"] == "JOB_STATE_RUNNING for 10 min"


def test_failed_with_no_text_is_unknown():
    now = datetime(2026, 10, 3, 10, 10, tzinfo=timezone.utc)
    out = dj.build_result(job_id=JOB, region="r", project="p", job=SUMMARY, options={}, errors=[],
                          warnings_count=0, log_groups=[], notes=[], now=now)
    assert out["kind"] == "unknown"
    assert out["summary"] == "JOB_STATE_FAILED after 4 min — kind: unknown"


def test_description_failure_only_drops_the_options(monkeypatch):
    _use(monkeypatch, {"JOB_VIEW_SUMMARY": SUMMARY, "JOB_VIEW_DESCRIPTION": FakeResponse(403, {}),
                       "JOB_MESSAGE_ERROR": {}, "JOB_MESSAGE_WARNING": {}, "logs": {}})
    out = dj.inspect(JOB)
    assert out["ok"] is True and "machine_type" not in out


def test_logging_failure_is_a_note_not_a_failure(monkeypatch):
    _use(monkeypatch, {"JOB_VIEW_SUMMARY": SUMMARY, "JOB_VIEW_DESCRIPTION": DESCRIPTION,
                       "JOB_MESSAGE_ERROR": {"jobMessages": [{"messageText": "Quota exceeded"}]},
                       "JOB_MESSAGE_WARNING": {},
                       "logs": FakeResponse(403, {"error": {"message": "denied"}})})
    out = dj.inspect(JOB)
    assert out["ok"] is True and out["kind"] == "quota" and out["log_groups"] == []
    assert "roles/logging.viewer" in out["notes"][0]


# ----- errors → hints --------------------------------------------------------------------

@pytest.mark.parametrize("status, expect", [
    (403, "needs roles/dataflow.viewer and roles/logging.viewer on demo-project"),
    (404, f"no job {JOB} in region europe-west2 — check SUPPORT_DATAFLOW_REGION"),
])
def test_http_errors_become_hints(monkeypatch, status, expect):
    _use(monkeypatch, {"JOB_VIEW_SUMMARY": FakeResponse(status, {"error": {"message": "nope"}})})
    out = dj.inspect(JOB)
    assert out == {"ok": False, "error": f"Dataflow {status}: nope", "hint": expect}


def test_other_errors_never_raise(monkeypatch):
    _use(monkeypatch, {"JOB_VIEW_SUMMARY": FakeResponse(500, ValueError("not json"))})
    out = dj.inspect(JOB)
    assert out["ok"] is False and "500" in out["error"] and "hint" not in out

    class Boom:
        def request(self, *a, **k):
            raise ConnectionError("proxy said no")

    monkeypatch.setattr(dj, "_session", lambda: Boom())
    out = dj.inspect(JOB)
    assert out["ok"] is False and "ConnectionError" in out["error"]


def test_no_credentials_never_raises(monkeypatch):
    def refuse():
        raise RuntimeError("no default credentials")

    monkeypatch.setattr(dj, "_session", refuse)
    out = dj.inspect(JOB)
    assert out["ok"] is False and "no credentials" in out["error"] and out["hint"]
