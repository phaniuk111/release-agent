"""JIRA ticket resolution at queue time.

A typo'd key would otherwise be copied verbatim into the change record and be
found by whoever audits it, so a key that does not exist is refused. A JIRA
outage is a different thing entirely and must never block a release.
"""
import pytest

from adk_release_agent import tools as T
from release_agent.config import settings
from release_agent.tools import jira as J
from release_agent.tools import release_queue as RQ


class _Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self):
        return self._payload


@pytest.fixture
def jira(monkeypatch):
    def _set(response=None, error=None, **over):
        monkeypatch.setattr(settings, "jira_base_url", over.pop("base", "https://acme.atlassian.net"))
        monkeypatch.setattr(settings, "jira_user_email", over.pop("email", "svc@acme.com"))
        monkeypatch.setattr(settings, "jira_api_token", over.pop("token", "secret-token"))

        import requests

        def _get(*a, **kw):
            if error is not None:
                raise error
            return response

        monkeypatch.setattr(requests, "get", _get)

    return _set


def test_unconfigured_jira_is_simply_absent(monkeypatch):
    monkeypatch.setattr(settings, "jira_base_url", "")
    assert J.jira_configured() is False
    with pytest.raises(J.JiraUnavailable):
        J.get_issue("ABC-1")


def test_issue_fields_are_flattened_for_the_change_record(jira):
    jira(_Response(200, {"fields": {
        "summary": "Retry settlement callback",
        "status": {"name": "In Progress"},
        "assignee": {"emailAddress": "dev@acme.com"},
        # v3 returns ADF, not a string
        "description": {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Adds a retry."}]},
            {"type": "paragraph", "content": [{"type": "text", "text": "Idempotent."}]},
        ]},
    }}))
    issue = J.get_issue("ABC-4471")
    assert issue["summary"] == "Retry settlement callback"
    assert issue["status"] == "In Progress"
    assert issue["assignee"] == "dev@acme.com"
    assert issue["url"] == "https://acme.atlassian.net/browse/ABC-4471"
    assert "Adds a retry." in issue["description"] and "Idempotent." in issue["description"]


def test_missing_ticket_raises_not_found(jira):
    jira(_Response(404))
    with pytest.raises(J.JiraIssueNotFound):
        J.get_issue("ABC-9999")


@pytest.mark.parametrize("status", [401, 403, 500, 502])
def test_auth_and_server_errors_are_infrastructure_not_user_error(jira, status):
    jira(_Response(status))
    with pytest.raises(J.JiraUnavailable):
        J.get_issue("ABC-1")


def test_the_token_never_appears_in_an_error(jira):
    jira(_Response(401))
    with pytest.raises(J.JiraUnavailable) as e:
        J.get_issue("ABC-1")
    assert "secret-token" not in str(e.value)


def test_network_failure_is_unavailable_not_not_found(jira):
    jira(error=OSError("proxy refused"))
    with pytest.raises(J.JiraUnavailable):
        J.get_issue("ABC-1")


# ---------------------------------------------------------------- queue gate

@pytest.fixture
def queue(monkeypatch):
    """queue_release_intent with a passing build report and a stubbed queue."""
    inserted = {}

    def _run(**kwargs):
        monkeypatch.setattr(T, "_invoke_tool", lambda name, args=None: {
            "found": True, "run_succeeded": True, "gate": "PASS",
            "tag": "payments-api-1.4.2",
            "run": {"url": "https://gh/actions/runs/1", "conclusion": "success"},
            "controls": [{"control": "RCTLDEF1", "job": "j", "passed": True, "failed": False}],
            "failed_steps": [],
        })
        monkeypatch.setattr(RQ, "add_intent", lambda **kw: inserted.update(kw) or {"ok": True})
        monkeypatch.setattr(RQ, "_fetch_events", lambda: [])
        out = T.queue_release_intent(
            artifact="payments-api:1.4.2", requested_by="dev@acme.com",
            build_run_url="https://github.com/o/r/actions/runs/1", **kwargs,
        )
        return out, inserted

    return _run


def test_a_bad_key_is_refused_before_anything_is_written(jira, queue):
    jira(_Response(404))
    out, inserted = queue(jira_ticket="ABC-9999")
    assert out["ok"] is False
    assert "ABC-9999" in out["error"]
    assert inserted == {}          # nothing reached the queue


def test_a_jira_outage_warns_but_still_queues(jira, queue):
    jira(error=OSError("proxy refused"))
    out, _ = queue(jira_ticket="ABC-4471")
    assert out["ok"] is True
    assert any("ABC-4471" in w for w in out["warnings"])


def test_the_summary_fills_empty_change_details(jira, queue):
    """The dev already told JIRA what changed; don't make them type it twice."""
    jira(_Response(200, {"fields": {"summary": "Retry settlement callback",
                                    "status": {"name": "In Progress"}}}))
    out, inserted = queue(jira_ticket="ABC-4471", change_details="")
    assert inserted["change_details"] == "Retry settlement callback"
    assert out["jira"]["status"] == "In Progress"


def test_the_developers_own_words_are_never_overwritten(jira, queue):
    jira(_Response(200, {"fields": {"summary": "Retry settlement callback"}}))
    _, inserted = queue(jira_ticket="ABC-4471", change_details="mine, keep it")
    assert inserted["change_details"] == "mine, keep it"


def test_no_ticket_means_no_jira_call(jira, queue):
    jira(error=AssertionError("JIRA must not be called without a ticket"))
    out, _ = queue(jira_ticket="")
    assert out["ok"] is True
