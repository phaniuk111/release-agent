"""The Deploy to CARE UAT flow says each step while it runs (tools/progress.py).

The lines are the page's only sign of life between the CONFIRM token and the
result, so the exact list is pinned: what is said, in order, and that a step
which did not happen is never claimed.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

from adk_release_agent import deploy
from release_agent import identity
from release_agent.config import settings
from release_agent.tools import _common, progress
from release_agent.tools import promotion as P
from tests.fakes import FakeRepo

UAT_FILE = "uat/deployment.json"

# conftest stubs the preview's GitHub check out for every test; taken at import,
# before any fixture runs, this is the real one.
_REAL_BLOCKED = deploy._uat_deploy_blocked


class _Repo(FakeRepo):
    """What the deployment repo's own workflow does: a merge into SIT raises SIT -> UAT."""

    def __init__(self, initial, workflow=True):
        super().__init__(initial)
        self.workflow = workflow
        self.blocking = []              # open PRs on the file that were not raised here
        self.full_name, self.html_url = "o/deploy", "https://github.example/o/deploy"

    def get_pulls(self, state="open", base=None, head=None, **kw):
        if head is None:
            return [p for p in self.blocking if p.base == base]
        return super().get_pulls(state=state, base=base, head=head, **kw)

    def raise_sit_to_uat(self):
        if not self.get_pulls(state="open", base="UAT", head="o:SIT"):
            FakeRepo.create_pull(self, "Promote SIT to UAT", "auto", "SIT", "UAT")

    def create_pull(self, title, body, head, base):
        pr = super().create_pull(title, body, head, base)
        if base == "SIT" and self.workflow:
            merge = pr.merge

            def merge_then_raise(*a, **k):
                merge(*a, **k)
                self.raise_sit_to_uat()
            pr.merge = merge_then_raise
        return pr


class _Clock:
    """time.time / time.sleep for promotion: sleeping moves the clock, and
    ``on_sleep`` lets a test make the repository's workflow act meanwhile."""

    def __init__(self, on_sleep=None):
        self.now, self.sleeps, self.on_sleep = 1000.0, [], on_sleep

    def time(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(len(self.sleeps))


def _chart(name, version):
    return {"helm_chart_name": name, "helm_chart_version": version, "helm_chart_dir": "d",
            "helm_values_file_name": "uat/values_uat.yaml", "gke_namespace": "ns"}


@pytest.fixture
def uat(monkeypatch):
    monkeypatch.setattr(settings, "uat_pr_wait_seconds", 0)
    monkeypatch.setattr(settings, "sit_branch", "SIT")
    monkeypatch.setattr(settings, "uat_branch", "UAT")
    monkeypatch.setattr(deploy, "_uat_deploy_blocked", _REAL_BLOCKED)
    monkeypatch.setattr(P, "_find_deploy_run", lambda *a, **k: None)
    monkeypatch.setattr(P, "active_deploy_repo", lambda: "o/deploy")
    monkeypatch.setattr(_common, "active_deploy_repo", lambda: "o/deploy")

    def make(workflow=True, open_prs=()):
        repo = _Repo({"SIT": {UAT_FILE: {"include": []}}, "UAT": {UAT_FILE: {"include": []}}},
                     workflow=workflow)
        repo.blocking = list(open_prs)
        client = SimpleNamespace(get_repo=lambda name: repo)
        monkeypatch.setattr(P, "_get_github_client", lambda: client)
        monkeypatch.setattr(_common, "_get_github_client", lambda: client)
        return repo
    return make


def _said(call):
    """Run ``call`` the way the app does (a worker thread under a bound sink)
    and return (its result, the lines it said)."""
    async def run():
        with progress.capture() as steps:
            result = await asyncio.to_thread(call)
            lines = []
            while not steps.queue.empty():
                lines.append(steps.queue.get_nowait())
            return result, lines
    return asyncio.run(run())


def _apply():
    return json.loads(P.open_release_pr(
        "uat", deployment_json=json.dumps({"include": [_chart("orders-api", "1.2.3")]})))


def _blocking_pr(number=7, base="SIT"):
    return SimpleNamespace(
        number=number, base=base, title="Deploy x:1 to uat", html_url=f"http://pr/{number}",
        body="Requested-by: alice@example.com\n", user=SimpleNamespace(login="bot"),
        get_files=lambda: [SimpleNamespace(filename=UAT_FILE)])


# --- apply ------------------------------------------------------------------------

def test_a_uat_deploy_says_each_step_in_order(uat):
    repo = uat()

    with identity.activate(identity.Caller(email="alice@example.com")):
        out, lines = _said(_apply)

    number = out["pending_prs"][0]["number"]
    assert out["action"] == "pending_review"
    assert lines == [
        "Checking nothing else is deploying to UAT",
        "Reading UAT's deployment file",
        "Writing uat/deployment.json on a working branch",
        "Opening the PR into SIT",
        "Merging into SIT",
        f"Found the SIT → UAT PR #{number}",
    ]
    assert not any("@" in line or "CONFIRM" in line for line in lines), "no email or token in a line"
    assert repo.prs, "the lines were commentary on real work"


def test_the_lines_use_the_configured_branch_names(uat, monkeypatch):
    repo = uat(workflow=False)
    for old, new in (("SIT", "RELEASE_SIT"), ("UAT", "RELEASE_UAT")):
        repo.files[new] = repo.files.pop(old)
    monkeypatch.setattr(settings, "sit_branch", "RELEASE_SIT")
    monkeypatch.setattr(settings, "uat_branch", "RELEASE_UAT")

    _, lines = _said(_apply)

    assert lines[0] == "Checking nothing else is deploying to RELEASE_UAT"
    assert "Opening the PR into RELEASE_SIT" in lines and "Merging into RELEASE_SIT" in lines
    assert lines[-1] == "The RELEASE_SIT → RELEASE_UAT PR has not appeared yet"


@pytest.mark.parametrize("base", ["SIT", "UAT"])
def test_a_refused_deploy_says_only_that_it_checked(uat, base):
    repo = uat(open_prs=[_blocking_pr(base=base)])

    out, lines = _said(_apply)

    assert out["action"] == "blocked" and repo.prs == []
    assert lines == ["Checking nothing else is deploying to UAT"]


def test_a_deploy_running_elsewhere_is_refused_after_the_same_single_line(uat):
    uat()
    P._UAT_DEPLOYS_RUNNING["o/deploy"] = {"by": "bob@example.com", "charts": "x:1", "since": 0}
    try:
        out, lines = _said(_apply)
    finally:
        P._UAT_DEPLOYS_RUNNING.clear()
    assert out["action"] == "blocked"
    assert lines == ["Checking nothing else is deploying to UAT"]


def test_a_wait_that_runs_out_counts_every_fifteen_seconds_then_says_so(uat, monkeypatch):
    uat(workflow=False)
    monkeypatch.setattr(settings, "uat_pr_wait_seconds", 40)
    clock = _Clock()
    monkeypatch.setattr(P, "time", clock)

    out, lines = _said(_apply)

    assert out["pending_prs"] == [] and "has not appeared yet" in out["note"]
    assert lines[:5] == [
        "Checking nothing else is deploying to UAT",
        "Reading UAT's deployment file",
        "Writing uat/deployment.json on a working branch",
        "Opening the PR into SIT",
        "Merging into SIT",
    ]
    assert lines[5:] == [
        "Waiting for the SIT → UAT PR (0 s of up to 40 s)",
        "Waiting for the SIT → UAT PR (15 s of up to 40 s)",
        "Waiting for the SIT → UAT PR (30 s of up to 40 s)",
        "The SIT → UAT PR has not appeared yet",
    ]
    assert clock.sleeps == [5] * 8, "polled every 5 s until the 40 s were up"


def test_a_pr_that_appears_during_the_wait_ends_the_count(uat, monkeypatch):
    repo = uat(workflow=False)
    monkeypatch.setattr(settings, "uat_pr_wait_seconds", 90)
    clock = _Clock(on_sleep=lambda n: repo.raise_sit_to_uat() if n == 4 else None)   # at 20 s
    monkeypatch.setattr(P, "time", clock)

    out, lines = _said(_apply)

    number = out["pending_prs"][0]["number"]
    assert lines[5:] == [
        "Waiting for the SIT → UAT PR (0 s of up to 90 s)",
        "Waiting for the SIT → UAT PR (15 s of up to 90 s)",
        f"Found the SIT → UAT PR #{number}",
    ]


def test_no_wait_configured_says_nothing_about_waiting(uat):
    uat(workflow=False)                                  # uat_pr_wait_seconds is 0

    _, lines = _said(_apply)

    assert lines[-1] == "The SIT → UAT PR has not appeared yet"
    assert not any(line.startswith("Waiting") for line in lines)


def test_a_no_change_deploy_does_not_claim_the_steps_it_never_took(uat):
    repo = uat()
    repo.files["SIT"][UAT_FILE] = json.dumps({"include": [_chart("orders-api", "1.2.3")]})

    out, lines = _said(_apply)

    assert out["action"] == "no_change"
    assert lines == [
        "Checking nothing else is deploying to UAT",
        "Reading UAT's deployment file",
    ]


def test_the_deploy_is_the_same_with_nobody_listening(uat):
    uat()
    assert _apply()["action"] == "pending_review"        # no sink bound: say() is a no-op


# --- preview ----------------------------------------------------------------------

def test_the_preview_says_what_it_checks_then_that_it_is_building(uat):
    uat()

    out, lines = _said(lambda: deploy.prepare_deploy_preview(
        image_tags="orders-api:1.2.3", environment="uat"))

    assert out["ok"] is True
    assert lines == ["Checking nothing else is deploying to UAT", "Building the preview"]


def test_a_refused_preview_never_claims_to_build(uat):
    uat(open_prs=[_blocking_pr()])

    out, lines = _said(lambda: deploy.prepare_deploy_preview(
        image_tags="orders-api:1.2.3", environment="uat"))

    assert out["ok"] is False and "Not deployed" in out["error"]
    assert lines == ["Checking nothing else is deploying to UAT"]
