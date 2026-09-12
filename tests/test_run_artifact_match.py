"""One run, one artifact: a queued chart:version must come with the run that
built exactly it — a passing run of another image vouches for nothing."""
from types import SimpleNamespace

import pytest

from adk_release_agent import tools as T
from release_agent.tools import controls as C
from release_agent.tools import release_queue as RQ


# --- the pure rules ------------------------------------------------------------

@pytest.mark.parametrize("built,names_image,version", [
    ("orders-api-1.2.3", True, "1.2.3"),
    ("orders-api/1.2.3", True, "1.2.3"),
    ("orders-api:1.2.3", True, "1.2.3"),
    ("refs/tags/orders-api-1.2.3-rc1", True, "1.2.3-rc1"),
    ("1.2.3", False, "1.2.3"),
    ("orders-apix-1.2.3", False, "orders-apix-1.2.3"),     # a longer name is not ours
    ("orders-1.2.3", False, "orders-1.2.3"),
])
def test_a_tag_is_split_into_image_and_version(built, names_image, version):
    assert C.split_built_tag(built, "orders-api") == (names_image, version)


@pytest.mark.parametrize("built,wanted,ok", [
    ("1.0.1", "1.0.1", True), ("v1.0.1", "1.0.1", True), ("1.0.1", "v1.0.1", True),
    ("1.0.10", "1.0.1", False), ("1.0.1", "1.0.10", False), ("1.0.1-pass", "1.0.1", False),
    ("", "1.0.1", False), ("1.0.1", "", False),
])
def test_versions_match_exactly(built, wanted, ok):
    assert C.version_matches(built, wanted) is ok


def test_the_echoed_script_line_is_not_a_tag():
    """Seen in a real run's log: GitHub prints the step's script, colour codes
    and all, before its output — the unexpanded variable must not count."""
    log = ("2026-06-26T10:00:00Z \x1b[36;1mecho \"TAG_GENERATED=${GITHUB_REF_NAME}\"\x1b[0m\n"
           "2026-06-26T10:00:01Z TAG_GENERATED=orders-api-1.0.0-pass\n")
    assert C._tags_from_log(log, "TAG_GENERATED=") == ["orders-api-1.0.0-pass"]


def test_every_tag_a_matrix_run_logged_is_read():
    log = ("2026-09-12T10:00:00Z TAG_GENERATED=orders-api-1.2.3\n"
           "noise\n2026-09-12T10:00:01Z echo \"TAG_GENERATED='payments-api-4.0.0'\"\n"
           "TAG_GENERATED=orders-api-1.2.3\n")
    assert C._tags_from_log(log, "TAG_GENERATED=") == ["orders-api-1.2.3", "payments-api-4.0.0"]


# --- reading a real run's shape ---------------------------------------------------

class _Repo:
    def __init__(self, run, tags=()):
        self.run, self.tags = run, set(tags)

    def get_workflow_run(self, run_id):
        return self.run

    def get_git_ref(self, ref):
        if ref.split("tags/", 1)[1] not in self.tags:
            raise Exception("404")
        return SimpleNamespace()


def _step(name, conclusion="success"):
    return SimpleNamespace(name=name, conclusion=conclusion)


def _run(event="push", head="main", path=".github/workflows/build-orders-api.yml", jobs=()):
    return SimpleNamespace(event=event, head_branch=head, path=path, jobs=lambda: list(jobs))


@pytest.fixture
def github(monkeypatch):
    def _set(run, tags=(), logs=None, workflow="build-orders-api.yml"):
        repo = _Repo(run, tags)
        monkeypatch.setattr(C, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda r: repo))
        monkeypatch.setattr(C, "_image_build_workflow", lambda repo_obj, image: workflow)
        monkeypatch.setattr(C, "_fetch_job_log", lambda repo_full, job_id: (logs or {}).get(job_id, ""))
    return _set


def test_a_tag_push_run_is_read_from_its_trigger(github):
    github(_run(head="orders-api-1.2.3"), tags={"orders-api-1.2.3"})
    assert C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3") == {
        "ok": True, "built": "orders-api-1.2.3", "source": "trigger"}


def test_the_run_of_another_image_is_refused_by_name(github):
    github(_run(head="payments-api-1.4.2"), tags={"payments-api-1.4.2"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "2.0")
    assert out["ok"] is False and "built payments-api-1.4.2, not orders-api:2.0" in out["reason"]


def test_the_right_image_at_another_version_is_refused(github):
    github(_run(head="orders-api-1.0.10"), tags={"orders-api-1.0.10"})
    assert C.match_run_to_artifact("o/build", 1, "orders-api", "1.0.1")["ok"] is False


def test_a_branch_run_is_read_from_its_tag_step_log(github):
    job = SimpleNamespace(id=7, steps=[_step("Checkout"), _step("Generate Git tag")])
    github(_run(event="workflow_dispatch", jobs=[job]), logs={7: "TAG_GENERATED=orders-api-1.2.3"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out == {"ok": True, "built": "orders-api-1.2.3", "source": "log"}


def test_a_failed_tag_step_proves_nothing(github):
    job = SimpleNamespace(id=7, steps=[_step("Generate Git tag", "failure")])
    github(_run(event="workflow_dispatch", jobs=[job]), logs={7: "TAG_GENERATED=orders-api-1.2.3"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "Can't tell which image and tag" in out["reason"]


def test_a_push_to_a_branch_is_not_mistaken_for_a_tag(github):
    github(_run(head="orders-api-1.2.3"), tags=set())         # a BRANCH named like a tag
    assert C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")["ok"] is False


def test_a_bare_version_counts_only_from_the_images_own_workflow(github):
    github(_run(head="1.2.3"), tags={"1.2.3"})
    ok = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert ok["ok"] is True and ok["via_workflow"] == "build-orders-api.yml"

    github(_run(head="1.2.3", path=".github/workflows/build-payments-api.yml"), tags={"1.2.3"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "build workflow is build-orders-api.yml" in out["reason"]

    github(_run(head="1.2.3"), tags={"1.2.3"}, workflow=None)
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "no build workflow in" in out["reason"]


def test_a_matrix_run_that_built_ours_among_others_counts(github):
    job = SimpleNamespace(id=7, steps=[_step("Generate Git tag")])
    github(_run(event="workflow_dispatch", jobs=[job]),
           logs={7: "TAG_GENERATED=payments-api-4.0.0\nTAG_GENERATED=orders-api-1.2.3"})
    assert C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")["ok"] is True


# --- the queue gate -------------------------------------------------------------

@pytest.fixture
def queue(monkeypatch):
    writes = []

    def _run(match, **report_over):
        report = {"found": True, "repo": "o/build", "tag": "x", "run_succeeded": True,
                  "run": {"id": 42, "url": "https://github.com/o/build/actions/runs/42"},
                  "controls": [{"control": "RLFT-a", "job": "b", "passed": True, "failed": False}],
                  "failed_controls": [], "open_controls": [], "failed_steps": [], "gate": "PASS",
                  **report_over}
        monkeypatch.setattr(T, "_invoke_tool", lambda name, args=None: report)
        monkeypatch.setattr(C, "match_run_to_artifact", lambda *a: match)
        monkeypatch.setattr(RQ, "add_intent", lambda **kw: writes.append(kw) or {"ok": True})
        monkeypatch.setattr(RQ, "_fetch_events", lambda: [])
        return T.queue_release_intent(
            artifact="orders-api:2.0", requested_by="dev@example.com",
            build_run_url="https://github.com/o/build/actions/runs/42",
            jira_ticket="ABC-1", change_details="d")
    _run.writes = writes
    return _run


def test_the_gate_refuses_a_run_that_built_something_else(queue):
    out = queue({"ok": False, "built": ["payments-api-1.4.2"],
                 "reason": "That run built payments-api-1.4.2, not orders-api:2.0."})
    assert out["ok"] is False and out["eligible"] is False
    assert out["built_tags"] == ["payments-api-1.4.2"] and "Nothing was queued" in out["error"]
    assert queue.writes == []


def test_the_gate_checks_identity_before_controls(queue):
    """A failing run of another image is the wrong run first, a failed build second."""
    out = queue({"ok": False, "built": ["x"], "reason": "wrong run"}, gate="FAIL", run_succeeded=False)
    assert out["error"].startswith("wrong run") and "failed_controls" not in out


def test_the_gate_queues_the_run_that_built_it(queue):
    out = queue({"ok": True, "built": "orders-api-2.0", "source": "trigger"})
    assert out["ok"] is True and queue.writes[0]["build_verified"] is True


def test_the_check_can_be_relaxed(queue, monkeypatch):
    from release_agent.config import settings
    monkeypatch.setattr(settings, "queue_require_run_match", False)
    out = queue({"ok": False, "built": ["x"], "reason": "wrong run"})
    assert out["ok"] is True
