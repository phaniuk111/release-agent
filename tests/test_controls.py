"""Unit tests for the live build-report tool (controls.py).

All dependency-free: PyGithub is faked, no network. Covers URL parsing, the
built-from-main compare mapping, failed-step extraction, and get_build_report over
both entry points (workflow_url and image+tag).
"""

import base64
import json

import pytest

from release_agent.tools import controls as C


# --- fakes ------------------------------------------------------------------
class _Step:
    def __init__(self, name, conclusion, number=1, status="completed"):
        self.name, self.conclusion, self.number, self.status = name, conclusion, number, status


class _Job:
    def __init__(self, name, steps):
        self.name, self.steps = name, steps


class _Run:
    def __init__(self, jobs, *, run_id=123, conclusion="failure", head_sha="abc123", head_branch="v1.2.3"):
        self._jobs = jobs
        self.id = run_id
        self.html_url = f"https://github.com/org/build-repo/actions/runs/{run_id}"
        self.name = "build.yml"
        self.status = "completed"
        self.conclusion = conclusion
        self.head_sha = head_sha
        self.head_branch = head_branch
        self.created_at = 0  # _find_build_run sorts candidate runs by this

    def jobs(self):
        return self._jobs


class _Cmp:
    def __init__(self, status):
        self.status = status


class _Repo:
    def __init__(self, run=None, compare_status="behind", default_branch="main", raise_compare=False):
        self._run = run
        self._compare_status = compare_status
        self.default_branch = default_branch
        self._raise_compare = raise_compare

    def get_workflow_run(self, run_id):
        if self._run is None:
            raise RuntimeError("no such run")
        return self._run

    def compare(self, base, head):
        if self._raise_compare:
            raise RuntimeError("commit not found")
        return _Cmp(self._compare_status)


class _Client:
    def __init__(self, repo):
        self._repo = repo

    def get_repo(self, full):
        return self._repo


def _run_with_a_failed_step():
    jobs = [
        _Job(
            "build",
            [
                _Step("Checkout", "success", 1),
                _Step("Run tests", "failure", 2),
                _Step("RLFT approval gate", "success", 3),
                _Step("RFTL deploy control", "failure", 4),
            ],
        )
    ]
    return _Run(jobs)


# --- _parse_run_url ---------------------------------------------------------
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/org/build-repo/actions/runs/123", ("org/build-repo", 123)),
        ("https://github.com/org/build-repo/actions/runs/123/job/456", ("org/build-repo", 123)),
        ("https://github.com/org/build-repo/actions/runs/123/attempts/2", ("org/build-repo", 123)),
        ("https://example.com/nothing/here", (None, None)),
        ("not a url at all", (None, None)),
    ],
)
def test_parse_run_url(url, expected):
    assert C._parse_run_url(url) == expected


# --- _check_built_from_main -------------------------------------------------
@pytest.mark.parametrize(
    "status,expected",
    [("identical", True), ("behind", True), ("ahead", False), ("diverged", False)],
)
def test_built_from_main_mapping(status, expected):
    repo = _Repo(compare_status=status)
    out = C._check_built_from_main(repo, "abc123")
    assert out["result"] is expected
    assert out["default_branch"] == "main"
    assert out["status"] == status


def test_built_from_main_error_degrades_gracefully():
    repo = _Repo(raise_compare=True)
    out = C._check_built_from_main(repo, "deadbeef")
    assert out["result"] is None
    assert "reason" in out


# --- _failed_steps ----------------------------------------------------------
def test_failed_steps_extracts_only_failures():
    failed = C._failed_steps(_run_with_a_failed_step())
    names = {s["name"] for s in failed}
    # success steps excluded; control steps (RFTL...) excluded — they're in `controls`.
    assert names == {"Run tests"}
    assert all(s["job"] == "build" for s in failed)


# --- get_build_report via workflow_url --------------------------------------
def test_get_build_report_via_workflow_url(monkeypatch):
    repo = _Repo(run=_run_with_a_failed_step(), compare_status="ahead")
    monkeypatch.setattr(C, "_get_github_client", lambda: _Client(repo))

    out = json.loads(
        C.get_build_report.invoke(
            {"workflow_url": "https://github.com/org/build-repo/actions/runs/123"}
        )
    )
    assert out["found"] is True
    assert out["repo"] == "org/build-repo"
    assert out["run"]["url"].endswith("/runs/123")
    assert out["run"]["conclusion"] == "failure"
    # failed non-control step only; the failed RFTL control is in controls, not failed_steps
    assert {s["name"] for s in out["failed_steps"]} == {"Run tests"}
    assert any(c["control"] == "RFTL deploy control" and c["failed"] for c in out["controls"])
    # one RLFT control passed, one RFTL failed -> gate FAIL
    assert out["gate"] == "FAIL"
    assert out["built_from_main"]["result"] is False  # 'ahead' => not from main


# --- get_build_report via image + tag ---------------------------------------
def test_get_build_report_via_image_tag(monkeypatch):
    run = _run_with_a_failed_step()
    repo = _Repo(compare_status="behind")
    monkeypatch.setattr(C, "_get_github_client", lambda: _Client(repo))
    # Skip the real tag->commit->run discovery; return our fake run directly.
    monkeypatch.setattr(C, "_find_build_run", lambda repo_obj, image, tag: (run, None))

    out = json.loads(C.get_build_report.invoke({"image": "payments-api", "tag": "v1.2.3"}))
    assert out["found"] is True
    assert out["image"] == "payments-api"
    assert out["tag"] == "v1.2.3"
    assert out["built_from_main"]["result"] is True  # 'behind' => ancestor of main


def test_get_build_report_needs_input():
    out = json.loads(C.get_build_report.invoke({}))
    assert out["found"] is False
    assert "workflow_url" in out["reason"] or "image" in out["reason"]


def test_get_build_report_bad_url(monkeypatch):
    monkeypatch.setattr(C, "_get_github_client", lambda: _Client(_Repo()))
    out = json.loads(C.get_build_report.invoke({"workflow_url": "https://github.com/org/repo/tree/main"}))
    assert out["found"] is False


# --- image+tag path exercising the REAL _find_build_run (no monkeypatch) -----
class _Contents:
    def __init__(self, doc):
        self.content = base64.b64encode(json.dumps(doc).encode()).decode()


class _RefObj:
    def __init__(self, sha, type_="commit"):
        self.sha, self.type = sha, type_


class _Ref:
    def __init__(self, obj):
        self.object = obj


class _Workflow:
    def __init__(self, runs):
        self._runs = runs
        self.head_sha_seen = "__unset__"

    def get_runs(self, head_sha=None):
        # Record that the caller used the server-side head_sha filter, and emulate it.
        self.head_sha_seen = head_sha
        return [r for r in self._runs if getattr(r, "head_sha", None) == head_sha]


class _FullRepo(_Repo):
    """Fake supporting the real tag->commit->run resolution in _find_build_run."""

    def __init__(self, run, config, tag_sha, **kw):
        super().__init__(run=run, **kw)
        self._config = config
        self._tag_sha = tag_sha
        self.wf = _Workflow([run])

    def get_contents(self, path, ref=None):
        return _Contents(self._config)

    def get_git_ref(self, ref):
        return _Ref(_RefObj(self._tag_sha))

    def get_workflow(self, name):
        return self.wf


def test_get_build_report_image_tag_real_resolution(monkeypatch):
    # No monkeypatch of _find_build_run: exercises _image_build_workflow (reads config),
    # _resolve_tag_commit (git ref), and the get_runs(head_sha=commit) server-side filter.
    run = _run_with_a_failed_step()  # head_sha="abc123", head_branch="v1.2.3"
    repo = _FullRepo(
        run=run,
        config={"images": {"payments-api": {"build_workflow": "build.yml"}}},
        tag_sha="abc123",
        compare_status="behind",
    )
    monkeypatch.setattr(C, "_get_github_client", lambda: _Client(repo))

    out = json.loads(C.get_build_report.invoke({"image": "payments-api", "tag": "v1.2.3"}))
    assert out["found"] is True
    assert repo.wf.head_sha_seen == "abc123"  # the head_sha filter was actually used
    assert {s["name"] for s in out["failed_steps"]} == {"Run tests"}
    assert out["built_from_main"]["result"] is True


# --- note-branch logic (success vs workflow-level failure with no step detail) ----
def test_get_build_report_success_note(monkeypatch):
    run = _Run(
        [_Job("build", [_Step("Checkout", "success", 1), _Step("Build", "success", 2)])],
        conclusion="success",
    )
    monkeypatch.setattr(C, "_get_github_client", lambda: _Client(_Repo(run=run, compare_status="behind")))
    out = json.loads(C.get_build_report.invoke({"workflow_url": "https://github.com/o/r/actions/runs/1"}))
    assert out["run_succeeded"] is True
    assert out["failed_steps"] == []
    assert "succeed" in out["note"].lower()


def test_get_build_report_startup_failure_note(monkeypatch):
    run = _Run([], conclusion="failure")  # 0 jobs — workflow/startup-level failure
    monkeypatch.setattr(C, "_get_github_client", lambda: _Client(_Repo(run=run, compare_status="behind")))
    out = json.loads(C.get_build_report.invoke({"workflow_url": "https://github.com/o/r/actions/runs/1"}))
    assert out["run_succeeded"] is False
    assert out["failed_steps"] == []
    assert "workflow-level" in out["note"].lower()
