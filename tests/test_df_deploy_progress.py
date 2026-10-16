"""The lines a Deploy to DF UAT says while it runs (tools/progress.py).

Each line must describe a step the code really took; the exact lists below are
what the page shows, in order. GitHub is faked at the tool seams.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest
from github import GithubException

from adk_release_agent import deploy
from release_agent.config import settings
from release_agent.tools import composer, dataflow, progress
from tests.fakes import FakeRepo

DAG = "x = \"gs://b/templates/job-a/{{dag_run.conf['version'] | default('1.0.0')}}/f.json\"\n"
RUN_URL = "https://github.com/o/df/actions/runs/7"


def _said(fn, *args, **kwargs):
    """(result, lines) of ``fn`` run on a worker thread, as a turn runs it."""
    async def run():
        with progress.capture() as steps:
            result = await asyncio.to_thread(fn, *args, **kwargs)
            lines = []
            while not steps.queue.empty():
                lines.append(steps.queue.get_nowait())
        return result, lines

    return asyncio.run(run())


class _Workflow:
    """A workflow whose dispatch answers with the run it created (return_run_details)."""

    name, path, url = "Deploy DF", ".github/workflows/df-deploy.yml", "https://api/wf"

    def __init__(self, error=None):
        self.error = error
        self._requester = SimpleNamespace(requestJsonAndCheck=self._post)

    def _post(self, verb, url, input=None):
        if self.error:
            raise self.error
        return {}, {"workflow_run_id": 7, "html_url": RUN_URL}

    def get_runs(self):
        return iter([])


def _no_file(*args, **kwargs):
    raise Exception("404")


@pytest.fixture
def rig(monkeypatch):
    monkeypatch.setattr(settings, "df_deploy_repo", "o/df")
    monkeypatch.setattr(settings, "df_deploy_ref", "")
    monkeypatch.setattr(settings, "df_deploy_workflow", "df-deploy.yml")
    monkeypatch.setattr(settings, "df_dispatch_inputs", "")
    monkeypatch.setattr(settings, "composer_repo", "acme/dags")
    monkeypatch.setattr(settings, "composer_branch", "main")
    monkeypatch.setattr(settings, "composer_dag_dir_pattern", "{env}")
    deploy._PENDING_PREVIEWS.clear()
    state = SimpleNamespace(workflow=_Workflow())
    df_repo = SimpleNamespace(
        default_branch="trunk", html_url="https://github.com/o/df",
        get_workflow=lambda name: state.workflow, get_contents=_no_file)
    monkeypatch.setattr(dataflow, "_get_github_client",
                        lambda: SimpleNamespace(get_repo=lambda full: df_repo))
    state.dags = FakeRepo({"main": {"uat/job_a.py": DAG}})
    state.dags.full_name = "acme/dags"
    monkeypatch.setattr(composer, "_get_github_client",
                        lambda: SimpleNamespace(get_repo=lambda full: state.dags))
    return state


def _req(dag_files):
    return {"deployment_type": "dataflow", "environment": "uat",
            "images": [{"name": "job-a", "tag": "2.0.0"}], "dag_files": dag_files,
            "composer_repo": ""}


def test_apply_with_dag_files_says_every_step_in_order(rig):
    result, lines = _said(deploy._apply_request, _req(["job_a.py"]), "uat", "CONFIRM-ABC123")
    assert result["ok"] and result["dag_bump"]["pr_number"] == 1
    assert lines == [
        "Reading the DF deploy workflow df-deploy.yml in o/df",
        "Deploying job-a at 2.0.0 to UAT on trunk",
        "Dispatching the DF deploy workflow",
        f"Run #7 started: {RUN_URL}",
        "Opening the Composer DAG PR",
        "Creating branch dag-version/uat-job-a-2.0.0 from main",
        "Bumping job-a to 2.0.0 in job_a.py on dag-version/uat-job-a-2.0.0",
        "Raised the DAG PR #1 — merge it once the run is green",
    ]


def test_apply_without_dag_files_stops_at_the_run(rig):
    result, lines = _said(deploy._apply_request, _req([]), "uat", "CONFIRM-ABC123")
    assert result["ok"] and "dag_bump" not in result
    assert lines == [
        "Reading the DF deploy workflow df-deploy.yml in o/df",
        "Deploying job-a at 2.0.0 to UAT on trunk",
        "Dispatching the DF deploy workflow",
        f"Run #7 started: {RUN_URL}",
    ]


def test_a_refused_dispatch_says_so_and_never_opens_the_dag_pr(rig):
    rig.workflow = _Workflow(error=GithubException(422, {"message": "Unexpected inputs provided"}, {}))
    result, lines = _said(deploy._apply_request, _req(["job_a.py"]), "uat", "CONFIRM-ABC123")
    assert not result["ok"] and "dag_bump" not in result
    assert lines == [
        "Reading the DF deploy workflow df-deploy.yml in o/df",
        "Deploying job-a at 2.0.0 to UAT on trunk",
        "Dispatching the DF deploy workflow",
        "GitHub rejected the dispatch (HTTP 422) — nothing was started",
    ]
    assert rig.dags.prs == []


def test_an_unregistered_run_is_not_claimed_as_started(rig, monkeypatch):
    wf = _Workflow()
    wf._requester = None          # a server that does not return run details
    wf.create_dispatch = lambda ref, inputs, throw=False: None
    rig.workflow = wf
    monkeypatch.setattr(dataflow, "_find_dispatched_run", lambda *a, **k: (None, 0))
    _, lines = _said(deploy._apply_request, _req([]), "uat", "CONFIRM-ABC123")
    assert lines[-1] == "GitHub has not registered the run yet — the result links the runs page"
    assert not any("started" in line for line in lines)


def test_competing_runs_are_said_not_guessed(rig, monkeypatch):
    wf = _Workflow()
    wf._requester = None
    wf.create_dispatch = lambda ref, inputs, throw=False: None
    rig.workflow = wf
    monkeypatch.setattr(dataflow, "_find_dispatched_run", lambda *a, **k: (None, 2))
    _, lines = _said(deploy._apply_request, _req([]), "uat", "CONFIRM-ABC123")
    assert lines[-1] == "2 runs of this workflow started at the same moment — not guessing which is yours"


def test_a_dag_that_needs_no_change_says_no_pr_is_raised(rig):
    rig.dags.files["main"]["uat/job_a.py"] = DAG.replace("1.0.0", "2.0.0")
    _, lines = _said(composer.apply_dag_bump, ["job_a.py"], "2.0.0", "uat", image="job-a")
    assert lines[-1] == "No DAG needed a change to 2.0.0 — no PR to raise"
    assert rig.dags.prs == []


def test_preview_says_what_it_prepares_and_reads(rig):
    preview, lines = _said(deploy._build_preview, _req(["job_a.py"]))
    assert "workflow_dispatch (df-deploy.yml)" in preview
    assert lines == [
        "Preparing the df-deploy.yml dispatch for job-a:2.0.0",
        "Reading 1 Composer DAG file(s) on main in acme/dags",
    ]


def test_preview_without_dag_files_reads_nothing_from_github(rig):
    _, lines = _said(deploy._build_preview, _req([]))
    assert lines == ["Preparing the df-deploy.yml dispatch for job-a:2.0.0"]


def _fake_clock(monkeypatch, module):
    clock = {"now": 1000.0}
    monkeypatch.setattr(module, "time", SimpleNamespace(
        time=lambda: clock["now"],
        sleep=lambda s: clock.__setitem__("now", clock["now"] + s)))


def test_waiting_for_the_run_id_repeats_every_fifteen_seconds(monkeypatch):
    _fake_clock(monkeypatch, dataflow)
    wf = SimpleNamespace(name="Deploy DF", get_runs=lambda: iter([]))

    (run, competing), lines = _said(dataflow._find_dispatched_run, wf, set(), tries=20, delay=2.0)
    assert (run, competing) == (None, 0)
    assert lines == [
        "Waiting for GitHub to start the run (0 s of up to 40 s)",
        "Waiting for GitHub to start the run (16 s of up to 40 s)",
        "Waiting for GitHub to start the run (32 s of up to 40 s)",
    ]


def test_a_run_found_by_polling_is_announced_after_the_wait(rig, monkeypatch):
    _fake_clock(monkeypatch, dataflow)
    run = SimpleNamespace(id=9, html_url="https://github.com/o/df/actions/runs/9",
                          status="queued", display_title="", event="workflow_dispatch",
                          created_at=None, actor=None)
    runs = []                        # the run exists only once the dispatch happened
    rig.workflow = SimpleNamespace(
        name="Deploy DF", path="p", url="", get_runs=lambda: iter(runs),
        create_dispatch=lambda ref, inputs, throw=False: runs.append(run))
    monkeypatch.setattr(dataflow, "_dispatch_login", lambda: "")
    _, lines = _said(deploy._apply_request, _req([]), "uat", "CONFIRM-ABC123")
    assert lines[3:] == [
        "Waiting for GitHub to start the run (0 s of up to 12 s)",
        "Run #9 started: https://github.com/o/df/actions/runs/9",
    ]


def test_no_line_carries_a_secret_or_a_typed_email(rig):
    _, lines = _said(deploy._apply_request, _req(["job_a.py"]), "uat", "CONFIRM-ABC123")
    assert not any("CONFIRM" in line or "@" in line or "ghp_" in line for line in lines)
    json.dumps(lines)
