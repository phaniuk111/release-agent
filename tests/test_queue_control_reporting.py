"""What the queue gate TELLS the developer about controls.

A verdict the developer cannot act on is not a gate: "a control failed" on a
40-job run leaves them hunting, and "no controls found" when controls exist but
are still open says the opposite of what happened.
"""
import pytest

from adk_release_agent import tools as T
from release_agent.tools import release_queue as RQ


@pytest.fixture
def queue(monkeypatch):
    """Run queue_release_intent against a canned build report."""
    def _run(report, **kwargs):
        def fake_invoke(tool_name, args=None):
            if tool_name == "get_build_report":
                return report
            raise AssertionError(f"unexpected tool {tool_name}")

        monkeypatch.setattr(T, "_invoke_tool", fake_invoke)
        # imported inside the function, so patch the source module
        monkeypatch.setattr(RQ, "add_intent", lambda **kw: {"ok": True, "intent": kw})
        monkeypatch.setattr(RQ, "_fetch_events", lambda: [])
        return T.queue_release_intent(
            artifact=kwargs.pop("artifact", "payments-api:1.4.2"),
            requested_by="dev@example.com",
            build_run_url="https://github.com/o/r/actions/runs/1",
            **kwargs,
        )

    return _run


def _report(**over):
    base = {
        "found": True, "tag": "payments-api-1.4.2", "run_succeeded": True,
        "run": {"url": "https://github.com/o/r/actions/runs/1", "conclusion": "success"},
        "controls": [], "failed_controls": [], "open_controls": [],
        "failed_steps": [], "gate": "PASS",
    }
    base.update(over)
    return base


def test_a_failed_control_is_named_with_its_job(queue):
    result = queue(_report(
        run_succeeded=False, gate="FAIL",
        run={"url": "https://github.com/o/r/actions/runs/1", "conclusion": "failure"},
        controls=[{"control": "RCTLDEF0001691", "job": "publish-helm-chart",
                   "failed": True, "passed": False, "conclusion": "failure"}],
        failed_controls=[{"control": "RCTLDEF0001691", "job": "publish-helm-chart",
                          "conclusion": "failure"}],
    ))
    assert result["eligible"] is False
    assert result["failed_controls"] == ["RCTLDEF0001691"]
    # the job is what makes it findable in a many-job run
    assert result["failed_controls_detail"][0]["job"] == "publish-helm-chart"


def test_open_controls_are_named_not_reported_as_absent(queue):
    """Regression: this used to say "no RLFT/RFTL control steps were found" —
    the opposite of the truth when controls exist and simply have not run."""
    result = queue(_report(
        gate="UNKNOWN",
        controls=[{"control": "RCTLDEF0001691", "job": "publish-helm-chart",
                   "failed": False, "passed": False, "status": "queued"}],
        open_controls=[{"control": "RCTLDEF0001691", "job": "publish-helm-chart",
                        "status": "queued", "conclusion": None}],
    ))
    assert result["ok"] is True                      # open != failed, still queued
    assert result["open_controls"][0]["control"] == "RCTLDEF0001691"
    warning = " ".join(result["warnings"])
    assert "RCTLDEF0001691" in warning
    assert "no step or job matched" not in warning
    assert "were found" not in warning


def test_no_matching_control_says_so_explicitly(queue):
    """The dangerous case: nothing matched, so the build is queued UNGATED.
    That must not read like a run whose controls simply have not finished."""
    result = queue(_report(gate="UNKNOWN"))
    warning = " ".join(result["warnings"])
    assert "NO step or job matched" in warning
    assert "RCTLD" in warning                        # names the configured prefixes
    assert "ungated" in warning
    assert "open_controls" not in result


def test_all_controls_passed_warns_about_nothing(queue):
    result = queue(_report(
        gate="PASS",
        controls=[{"control": "RCTLDEF0001691", "job": "publish-helm-chart",
                   "failed": False, "passed": True, "conclusion": "success"}],
    ))
    assert result["eligible"] is True
    assert not result.get("warnings")
    assert "open_controls" not in result
