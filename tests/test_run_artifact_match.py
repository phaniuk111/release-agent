"""One run, one artifact: a queued chart:version must come with the run that
built exactly it — a passing run of another image vouches for nothing."""
import json
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
    log = ("2026-06-26T10:00:00Z \x1b[36;1mecho \"New tag is: ${GITHUB_REF_NAME}\"\x1b[0m\n"
           "2026-06-26T10:00:01Z New tag is: orders-api-1.0.0-pass\n")
    assert C._tags_from_log(log, "New tag is:") == ["orders-api-1.0.0-pass"]


def test_every_tag_a_matrix_run_logged_is_read():
    log = ("2026-09-12T10:00:00Z New tag is: orders-api-1.2.3\n"
           "noise\n2026-09-12T10:00:01Z echo \"New tag is: 'payments-api-4.0.0'\"\n"
           "New tag is: orders-api-1.2.3\n")
    assert C._tags_from_log(log, "New tag is:") == ["orders-api-1.2.3", "payments-api-4.0.0"]


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
    job = SimpleNamespace(id=7, steps=[_step("Checkout"), _step("Create new tag")])
    github(_run(event="workflow_dispatch", jobs=[job]), logs={7: "New tag is: orders-api-1.2.3"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out == {"ok": True, "built": "orders-api-1.2.3", "source": "log"}


def test_a_failed_tag_step_proves_nothing(github):
    job = SimpleNamespace(id=7, steps=[_step("Create new tag", "failure")])
    github(_run(event="workflow_dispatch", jobs=[job]), logs={7: "New tag is: orders-api-1.2.3"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "Can't tell which image and tag" in out["reason"]


def test_a_push_to_a_branch_is_not_mistaken_for_a_tag(github):
    github(_run(head="orders-api-1.2.3"), tags=set())         # a BRANCH named like a tag
    assert C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")["ok"] is False


def test_a_bare_version_is_tied_to_the_image_by_its_own_workflow(github):
    github(_run(head="1.2.3"), tags={"1.2.3"})
    ok = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert ok["ok"] is True and ok["via_workflow"] == "build-orders-api.yml"


def test_a_bare_version_from_another_images_workflow_is_always_refused(github):
    """The catalogue CONTRADICTS the run — that is evidence, not a gap."""
    github(_run(head="1.2.3", path=".github/workflows/build-payments-api.yml"), tags={"1.2.3"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "is built by build-orders-api.yml" in out["reason"]


def test_an_image_outside_the_catalogue_is_carried_by_the_version_alone(github):
    """A monorepo tags per service; listing every one in image-workflows.json
    goes stale, so a matching version is enough unless the setting demands more."""
    github(_run(head="1.2.3"), tags={"1.2.3"}, workflow=None)
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is True and out["via_version_only"] is True


def test_the_catalogue_can_be_made_mandatory(github, monkeypatch):
    monkeypatch.setattr(C.settings, "queue_require_image_workflow", True, raising=False)
    github(_run(head="1.2.3"), tags={"1.2.3"}, workflow=None)
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "no build workflow in" in out["reason"]


def test_a_bare_version_that_is_the_wrong_version_is_still_refused(github):
    """Relaxing the image tie must not relax the version check."""
    github(_run(head="1.2.30"), tags={"1.2.30"}, workflow=None)
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "not orders-api:1.2.3" in out["reason"]


def test_a_matrix_run_that_built_ours_among_others_counts(github):
    job = SimpleNamespace(id=7, steps=[_step("Create new tag")])
    github(_run(event="workflow_dispatch", jobs=[job]),
           logs={7: "New tag is: payments-api-4.0.0\nNew tag is: orders-api-1.2.3"})
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


# --- the older provenance tool reads the same log ---------------------------------

def _vitb_repo(monkeypatch, step_name, log, tag="orders-api-1.2.3"):
    """verify_image_tag_build against one run whose tag step logged `log`."""
    job = SimpleNamespace(id=7, name="build", steps=[
        SimpleNamespace(name=step_name, number=3, status="completed", conclusion="success")])
    run = SimpleNamespace(id=99, name="build", html_url="u", head_sha="abc", status="completed",
                          conclusion="success", created_at=1, jobs=lambda: [job])
    wf = SimpleNamespace(get_runs=lambda head_sha: [run])
    repo = SimpleNamespace(get_workflow=lambda w: wf)
    monkeypatch.setattr(C, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda r: repo))
    monkeypatch.setattr(C, "_image_build_workflow", lambda repo_obj, image: "build-orders-api.yml")
    monkeypatch.setattr(C, "_resolve_tag_commit", lambda repo_obj, t: "abcdef1234")
    monkeypatch.setattr(C, "_fetch_job_log", lambda repo_full, job_id: log)
    return json.loads(C.verify_image_tag_build("orders-api", tag, repo="o/build"))


def test_the_tag_is_found_when_the_log_puts_a_space_after_the_marker(monkeypatch):
    """The step logs "New tag is: <tag>"; a plain "<marker><tag>" substring
    search would never match it, and every build would read as unverified."""
    out = _vitb_repo(monkeypatch, C.settings.build_tag_step,
                     "2026-09-17T10:00:00Z New tag is: orders-api-1.2.3\n")
    assert out["verified"] is True and out["tag_generation"]["log_marker_found"] is True


def test_a_longer_tag_sharing_our_prefix_does_not_verify_it(monkeypatch):
    out = _vitb_repo(monkeypatch, C.settings.build_tag_step,
                     "New tag is: orders-api-1.2.30\n")
    assert out["verified"] is False


def test_the_step_name_and_marker_follow_the_configured_ones(monkeypatch):
    monkeypatch.setattr(C.settings, "build_tag_step", "Tag it")
    monkeypatch.setattr(C.settings, "build_tag_marker", "tag=")
    out = _vitb_repo(monkeypatch, "Tag it", "tag=orders-api-1.2.3\n")
    assert out["verified"] is True and out["tag_generation"]["step"] == "Tag it"


# --- the tag step may BE a job -----------------------------------------------------
# Found live (2026-09-17): the pipeline generates the tag in a reusable-workflow
# job called "Create new tag", whose own steps are named after the actions they
# run ("Tag this new commit"). Matching step names alone found nothing, and the
# refusal blamed the step for logging nothing while its log held the tag.

def _reusable_run(job_name, step_names, job_conclusion="success"):
    job = SimpleNamespace(
        id=7, name=job_name, conclusion=job_conclusion, status="completed",
        steps=[SimpleNamespace(name=n, number=i + 1, status="completed", conclusion="success")
               for i, n in enumerate(step_names)])
    return _run(event="push", head="main", jobs=[job])


def test_the_tag_job_of_a_reusable_workflow_is_found_by_its_own_name(github):
    """The API names it "<caller> / <job>"; the run page shows only the last part."""
    github(_reusable_run("build-deploy-publish / Create new tag",
                         ["Set up job", "Tag this new commit", "Complete job"]),
           logs={7: "2026-09-17T09:00:00Z New tag is: orders-api-1.2.3\n"})
    assert C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3") == {
        "ok": True, "built": "orders-api-1.2.3", "source": "log"}


def test_a_tag_job_that_did_not_succeed_vouches_for_nothing(github):
    github(_reusable_run("build-deploy-publish / Create new tag", ["Set up job"],
                         job_conclusion="failure"),
           logs={7: "New tag is: orders-api-1.2.3\n"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "did not succeed" in out["reason"]


@pytest.mark.parametrize("configured, name, ok", [
    ("Create new tag", "build-deploy-publish / Create new tag", True),
    ("Create new tag", "Create new tag", True),
    ("Create new tag", "  create NEW tag ", True),
    ("Create new tag", "Create new tag v2", False),
    ("Create new tag", "Tag this new commit", False),
    ("", "Create new tag", False),
])
def test_which_names_answer_to_the_configured_one(configured, name, ok):
    assert C.tag_name_matches(configured, name) is ok


def test_an_unreadable_log_does_not_read_as_a_pipeline_that_logged_nothing(github):
    """A token without actions:read, or a blocked log download, must say so."""
    github(_reusable_run("Create new tag", ["Set up job"]), logs={})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "log could not be read" in out["reason"]


def test_a_name_that_is_nowhere_in_the_run_says_exactly_that(github):
    github(_reusable_run("build", ["Set up job", "Build"]), logs={7: "nothing here"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "no step or job in that run is named" in out["reason"]


def test_a_matching_step_whose_log_has_no_marker_is_reported_as_such(github):
    github(_reusable_run("build", ["Set up job", C.settings.build_tag_step]),
           logs={7: "built fine, said nothing about a tag"})
    out = C.match_run_to_artifact("o/build", 1, "orders-api", "1.2.3")
    assert out["ok"] is False and "has no" in out["reason"]
