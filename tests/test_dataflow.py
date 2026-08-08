"""Dataflow flex-template deploys: payload parsing, preview, and the dispatch tool."""
import json
from types import SimpleNamespace

from adk_release_agent import deploy as adk_deploy
from release_agent.agent.parsing import _try_parse_json_payload
from release_agent.tools import dataflow as DF


def _df_payload(**over):
    base = {"deployment_type": "dataflow", "environment": "uat", "image": "order-enrichment", "tag": "1.4.2"}
    base.update(over)
    return json.dumps(base)


def test_parse_df_payload():
    req = _try_parse_json_payload(_df_payload(deployment_repo="my-org/df-repo"))
    assert req["deployment_type"] == "dataflow"
    assert req["images"] == [{"name": "order-enrichment", "tag": "1.4.2"}]
    assert req["environment"] == "uat"
    assert req["deployment_repo"] == "my-org/df-repo"
    # Missing tag -> not a valid DF payload (form enforces both; belt & braces here).
    assert _try_parse_json_payload(_df_payload(tag="")) is None


def test_df_preview_shows_dispatch_inputs():
    req = _try_parse_json_payload(_df_payload())
    preview = adk_deploy._build_preview(req)
    # Labelled with the configured workflow file, and showing the inputs as they
    # will actually be sent (DF_DISPATCH_INPUTS renames them per target workflow).
    assert preview == {
        "workflow_dispatch (df-deploy.yml)": [
            {"image": "order-enrichment", "tag": "1.4.2", "environment": "uat"}
        ]
    }


def test_apply_confirmed_deploy_dispatches_df_tool(monkeypatch):
    res = adk_deploy.prepare_deploy_preview(message=_df_payload())
    assert res["ok"] and res["image_tags"] == "order-enrichment:1.4.2"
    token = res["token"]

    called = {}

    def fake_invoke(tool_name, args=None):
        called["tool"] = tool_name
        called["args"] = args
        return {"ok": True, "action": "df_workflow_dispatched"}

    monkeypatch.setattr(adk_deploy, "_invoke_tool", fake_invoke)
    out = adk_deploy.apply_confirmed_deploy(token)
    assert out["ok"] is True
    assert called["tool"] == "deploy_dataflow"
    assert called["args"] == {"environment": "uat", "image": "order-enrichment", "tag": "1.4.2"}


class _FakeRun(SimpleNamespace):
    pass


class _FakeWorkflow:
    def __init__(self):
        self.dispatched = []
        self._runs = [_FakeRun(id=1, html_url="http://run/1", status="completed")]

    def get_runs(self):
        return iter(self._runs)

    def create_dispatch(self, ref, inputs, throw=False):
        assert throw is True, "a rejected dispatch must raise, not return False"
        self.dispatched.append({"ref": ref, "inputs": inputs})
        self._runs = [_FakeRun(id=2, html_url="http://run/2", status="queued")] + self._runs


def test_deploy_dataflow_dispatches_workflow(monkeypatch):
    wf = _FakeWorkflow()
    repo = SimpleNamespace(default_branch="main", get_workflow=lambda name: wf)
    monkeypatch.setattr(DF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    monkeypatch.setattr(DF.settings, "df_deploy_repo", "o/df", raising=False)

    out = json.loads(DF.deploy_dataflow.invoke({"environment": "uat", "image": "job-a", "tag": "2.0"}))
    assert out["ok"] and out["action"] == "df_workflow_dispatched"
    assert wf.dispatched == [{"ref": "main", "inputs": {"image": "job-a", "tag": "2.0", "environment": "uat"}}]
    assert out["run"]["id"] == 2  # the NEW run, not the pre-existing one


def test_deploy_dataflow_guards():
    assert "not supported yet" in DF.deploy_dataflow.invoke(
        {"environment": "prod", "image": "a", "tag": "1"}
    )
    assert "both image name and tag" in DF.deploy_dataflow.invoke(
        {"environment": "uat", "image": "a", "tag": ""}
    )
    assert "could not parse deployment_repo" in DF.deploy_dataflow.invoke(
        {"environment": "uat", "image": "a", "tag": "1", "deployment_repo": "nonsense"}
    )


def test_deploy_dataflow_blocked_in_freeform_chat():
    from adk_release_agent.safety import BLOCKED_FREEFORM_TOOLS

    assert "deploy_dataflow" in BLOCKED_FREEFORM_TOOLS
