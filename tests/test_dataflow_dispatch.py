"""DF deploy dispatch: input-name mapping and the ref it dispatches on.

Both are configuration, not constants, because GitHub rejects the dispatch
outright (HTTP 422 "Unexpected inputs provided") when the payload carries an
input the target workflow does not declare — verified live against a workflow
declaring only module/binary_version.
"""
import pytest

from release_agent.config import settings
from release_agent.tools import dataflow


@pytest.fixture
def df_config(monkeypatch):
    def _set(**kwargs):
        for key, value in kwargs.items():
            monkeypatch.setattr(settings, key, value)

    return _set


def test_default_template_sends_image_tag_environment(df_config):
    df_config(df_dispatch_inputs='{"image": "{image}", "tag": "{tag}", '
                                 '"environment": "{environment}"}')
    assert dataflow._dispatch_inputs("order-enrichment", "1.4.2", "uat") == {
        "image": "order-enrichment",
        "tag": "1.4.2",
        "environment": "uat",
    }


def test_template_renames_inputs_and_drops_unsupported_ones(df_config):
    """A workflow taking module/binary_version and NO environment input: the
    environment key must not be sent at all, or the dispatch 422s."""
    df_config(df_dispatch_inputs='{"module": "{image}", "binary_version": "{tag}"}')
    assert dataflow._dispatch_inputs("acme-svc-a", "1.4.2", "uat") == {
        "module": "acme-svc-a",
        "binary_version": "1.4.2",
    }


def test_template_supports_composite_and_literal_values(df_config):
    df_config(df_dispatch_inputs='{"artifact": "{image}:{tag}", "target": "{environment}", '
                                 '"mode": "flex"}')
    assert dataflow._dispatch_inputs("acme-svc-b", "2.0.0", "uat") == {
        "artifact": "acme-svc-b:2.0.0",
        "target": "uat",
        "mode": "flex",
    }


def test_empty_template_falls_back_to_the_built_in_shape(df_config):
    df_config(df_dispatch_inputs="   ")
    assert dataflow._dispatch_inputs("acme-svc-a", "1.0.0", "uat") == {
        "image": "acme-svc-a",
        "tag": "1.0.0",
        "environment": "uat",
    }


@pytest.mark.parametrize("bad", ['{"module": ', '["module"]', '"module"'])
def test_malformed_template_fails_loudly(df_config, bad):
    """Silently dispatching the wrong shape would look like a deploy that ran."""
    df_config(df_dispatch_inputs=bad)
    with pytest.raises(ValueError):
        dataflow._dispatch_inputs("acme-svc-a", "1.0.0", "uat")


class _FakeWorkflow:
    def __init__(self, error: Exception | None = None):
        self.dispatched = None
        self._error = error

    def get_runs(self):
        return []

    def create_dispatch(self, ref, inputs, throw=False):
        # PyGithub defaults throw=False and then SWALLOWS a rejected dispatch,
        # returning False. Mirroring that default here is the point of the test:
        # callers that ignore the return value report a deploy that never ran.
        self.dispatched = {"ref": ref, "inputs": inputs, "throw": throw}
        if self._error is not None:
            if not throw:
                return False
            raise self._error
        return True


class _FakeRepo:
    default_branch = "sit"

    def __init__(self, workflow):
        self._workflow = workflow

    def get_workflow(self, name):
        return self._workflow


@pytest.fixture
def run_deploy(monkeypatch, df_config):
    """Run deploy_dataflow against a fake GitHub; returns (result, dispatched)."""
    def _run(error: Exception | None = None, **config):
        workflow = _FakeWorkflow(error)
        df_config(df_deploy_repo="acme/df-app", df_deploy_workflow="UAT.yaml", **config)
        monkeypatch.setattr(
            dataflow, "_get_github_client",
            lambda: type("C", (), {"get_repo": staticmethod(lambda _: _FakeRepo(workflow))})(),
        )
        monkeypatch.setattr(dataflow, "_find_dispatched_run", lambda *a, **k: None)
        result = dataflow.deploy_dataflow.func(
            environment="uat", image="acme-svc-a", tag="1.4.2",
        )
        return result, workflow.dispatched

    return _run


@pytest.fixture
def dispatched(run_deploy):
    """What a SUCCESSFUL deploy_dataflow sent to GitHub."""
    def _run(**config):
        result, sent = run_deploy(**config)
        assert "ERROR" not in result, result
        return sent

    return _run


def test_dispatch_ref_defaults_to_the_repo_default_branch(dispatched):
    assert dispatched(df_deploy_ref="")["ref"] == "sit"


def test_configured_ref_wins_over_the_default_branch(dispatched):
    """The DF repo keeps its deploy workflow on main; dispatching on the default
    branch would 404 because the workflow file is not on that ref."""
    assert dispatched(df_deploy_ref="main")["ref"] == "main"


def test_deploy_sends_the_mapped_input_names(dispatched):
    sent = dispatched(df_dispatch_inputs='{"module": "{image}", "binary_version": "{tag}"}')
    assert sent["inputs"] == {"module": "acme-svc-a", "binary_version": "1.4.2"}


def test_preview_shows_exactly_what_will_be_dispatched(dispatched, df_config):
    """The confirmation preview and the dispatch must not drift: a developer who
    approves `image/tag` while we send `module/binary_version` has approved
    something we never sent."""
    from adk_release_agent import deploy as adk_deploy

    df_config(df_dispatch_inputs='{"module": "{image}", "binary_version": "{tag}"}',
              df_deploy_workflow="UAT.yaml")
    preview = adk_deploy._build_preview({
        "deployment_type": "dataflow",
        "environment": "uat",
        "images": [{"name": "acme-svc-a", "tag": "1.4.2"}],
    })

    assert list(preview) == ["workflow_dispatch (UAT.yaml)"]
    assert preview["workflow_dispatch (UAT.yaml)"] == [dispatched(
        df_dispatch_inputs='{"module": "{image}", "binary_version": "{tag}"}',
    )["inputs"]]


def test_a_rejected_dispatch_is_reported_as_an_error_not_a_deploy(run_deploy):
    """Regression: PyGithub's create_dispatch defaults to throw=False and returns
    False on rejection. Without throw=True we told the developer their deploy was
    dispatched while GitHub had refused it — verified live against a 422
    "Unexpected inputs provided"."""
    reason = 'Unexpected inputs provided: ["image", "tag", "environment"]'
    result, sent = run_deploy(error=RuntimeError(reason))

    assert sent["throw"] is True, "throw=True is what makes a rejection observable"
    assert result.startswith("ERROR deploying dataflow:")
    # the operator needs the reason AND what we actually sent, to fix the mapping
    assert reason in result
    assert "UAT.yaml" in result and "acme/df-app" in result and "acme-svc-a" in result
