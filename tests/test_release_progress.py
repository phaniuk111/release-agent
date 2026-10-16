"""The step lines CARE Release, DF Release and CARE mono say while they run.

Each case runs the real prepare/apply against stubs of GitHub (and, for the
file-sets, a real git repository on disk with a real updater script) and
asserts the exact ordered lines a person would watch go by.
"""
import asyncio
from types import SimpleNamespace

import pytest
from dulwich import porcelain

from release_agent.tools import care_release as CR
from release_agent.tools import git_snapshot as G
from release_agent.tools import progress
from release_agent.tools import release_chain as C
from release_agent.tools import release_fileset as RF
from release_agent.tools import release_queue
from tests.fakes import FakeRepo
from tests.test_care_release import COMMITTED, FILE, MONO, PREV, PREV_COMMITTED, _payload as _mono_payload

UPDATER = '''\
import argparse, json, os
ap = argparse.ArgumentParser(); ap.add_argument("--release-details-file"); a = ap.parse_args()
d = json.load(open(a.release_details_file))
os.makedirs("artefact-provider", exist_ok=True)
with open("artefact-provider/artefact.json", "w") as f:
    json.dump({"artefact": d["artefact"]}, f, indent=2)
'''


def said(fn, *args, **kwargs):
    """(result, the lines fn said) — fn runs on a worker thread, as tools do."""
    async def run():
        with progress.capture() as steps:
            result = await asyncio.to_thread(fn, *args, **kwargs)
            lines = []
            while not steps.queue.empty():
                lines.append(steps.queue.get_nowait())
            return result, lines

    return asyncio.run(run())


@pytest.fixture
def queue_on(monkeypatch):
    """The queue is configured, so moving charts is a real step."""
    moved = []
    monkeypatch.setattr(release_queue, "queue_enabled", lambda: True)
    monkeypatch.setattr(release_queue, "mark_released",
                        lambda *a, **k: moved.append(a) or {"ok": True})
    return moved


# --- file-set mode: CARE and DF --------------------------------------------------
@pytest.fixture
def remote(tmp_path):
    """A deploy repo carrying the updater on every branch either kind lands on."""
    path = tmp_path / "remote"
    repo = porcelain.init(str(path))
    script = path / "scripts/release/update_release_files.py"
    script.parent.mkdir(parents=True)
    script.write_text(UPDATER)
    porcelain.add(repo, [str(script)])
    porcelain.commit(repo, message=b"base", author=b"a <a@x>", committer=b"a <a@x>")
    for branch in ("sit", "RELEASE_UAT", "RELEASE_PRD"):
        porcelain.branch_create(repo, branch)
    repo.close()
    return str(path)


class _Gh:
    def __init__(self):
        self.pulls = []

    def get_repo(self, full):
        gh = self

        class _R:
            def create_git_blob(self, content, encoding):
                return SimpleNamespace(sha="b")

            def get_git_commit(self, sha):
                return SimpleNamespace(sha=sha, tree="t")

            def create_git_tree(self, elements, base_tree=None):
                return "nt"

            def create_git_commit(self, message, tree, parents, author=None):
                return SimpleNamespace(sha="c0ffee")

            def create_git_ref(self, ref, sha):
                pass

            def create_pull(self, **kw):
                gh.pulls.append(kw)
                return SimpleNamespace(number=5, html_url="https://github.example/pull/5")

        return _R()


@pytest.fixture
def fileset(monkeypatch, remote):
    gh = _Gh()
    for k, v in {"care_release_mode": "fileset", "df_release_branches": "uat:RELEASE_UAT,prd:RELEASE_PRD",
                 "df_release_repo": "o/df-release", "deploy_repo": "o/care-deploy", "sit_branch": "sit",
                 "uat_branch": "UAT", "prd_branch": "PRD", "prl1_branch": "PRL1",
                 "release_guard_branches": ["sit", "UAT", "PRD", "PRL1"],
                 "artifactory_base_url": ""}.items():
        monkeypatch.setattr(C.settings, k, v, raising=False)
    monkeypatch.setattr(G, "repo_url", lambda repo_full: remote)
    monkeypatch.setattr(RF, "_resolve_github_token", lambda: "")
    monkeypatch.setattr(RF, "_get_github_client", lambda: gh)
    monkeypatch.setattr(RF, "_open_prd_pr_blocker", lambda repo, **kw: None)
    monkeypatch.setattr(RF, "_merge_pr", lambda pr, method: (True, "merged"))
    return gh


CARE = {
    "release_name": "Release 42", "start_date": "2026-09-20 18:00:00",
    "end_date": "2026-09-20 20:00:00", "change_initiator": "dev@example.com", "jira": "ABC-1234",
    "change_summary": "weekly", "artefact": ["payments-api:1.2.0", "ledger:3.4.5"],
    "deployment_repo": "o/care-deploy", "release_kind": "care",
}
DF = {
    "release_name": "DF Release 7", "start_date": "2026-09-20 18:00:00",
    "end_date": "2026-09-20 20:00:00", "change_initiator": "dev@example.com", "jira": "ABC-1234",
    "change_summary": "df weekly", "artefact": ["df-orders:2.0.0"], "df_images": ["df-orders"],
    "deployment_repo": "o/df-release", "release_kind": "df",
}

# What a confirm re-runs: apply regenerates the file-set to prove it is unchanged.
REGENERATED = ["Checking the files have not moved since the preview"]


def test_care_fileset_says_each_step_at_preview_and_apply(fileset, queue_on):
    prep, at_preview = said(RF.prepare_release_fileset, dict(CARE))
    assert prep["ok"], prep
    assert at_preview == [
        "Checking for an open release PR on sit, UAT, PRD, PRL1",
        "Cloning o/care-deploy at sit",
        "Running the deployment repo's release updater",
        "Collecting the changed files",
    ]

    out, at_apply = said(RF.apply_release_fileset, prep)
    assert out["ok"], out
    assert at_apply == REGENERATED + at_preview + [
        "Committing 1 file to release/release-42 through the GitHub API",
        "Opening the CARE release PR into sit",
        "Merging release PR #5 into sit",
        "Moving 2 charts from the queue to Release history",
    ]
    assert len(queue_on) == 1


def test_df_fileset_says_each_step_at_preview_and_apply(fileset, queue_on):
    prep, at_preview = said(RF.prepare_release_fileset, dict(DF))
    assert prep["ok"], prep
    assert at_preview == [
        "Checking for an open release PR on RELEASE_UAT, RELEASE_PRD",
        "Cloning o/df-release at RELEASE_UAT",
        "Running the deployment repo's release updater",
        "Collecting the changed files",
    ]

    out, at_apply = said(RF.apply_release_fileset, prep)
    assert out["ok"], out
    assert at_apply == REGENERATED + at_preview + [
        "Committing 1 file to release/df-release-7 through the GitHub API",
        "Opening the DF release PR into RELEASE_UAT",
        "Merging release PR #5 into RELEASE_UAT",
        "Moving 1 chart from the queue to Release history",
    ]


def test_a_release_pr_held_for_review_says_so(fileset, queue_on, monkeypatch):
    monkeypatch.setattr(RF, "_merge_pr", lambda pr, method: (False, "awaiting review/checks (blocked)"))
    prep, _ = said(RF.prepare_release_fileset, dict(CARE))
    out, at_apply = said(RF.apply_release_fileset, prep)
    assert out["ok"] and out["merged_to_sit"] is False
    assert at_apply[-4:] == [
        "Opening the CARE release PR into sit",
        "Merging release PR #5 into sit",
        "Release PR #5 is open, not merged — awaiting review/checks (blocked)",
        "Moving 2 charts from the queue to Release history",
    ]


def test_no_queue_means_no_line_about_moving_charts(fileset):
    """Without BigQuery nothing moves, so nothing is claimed."""
    prep, _ = said(RF.prepare_release_fileset, dict(CARE))
    out, at_apply = said(RF.apply_release_fileset, prep)
    assert out["ok"]
    assert not [line for line in at_apply if "Release history" in line]


def test_a_failed_queue_write_says_the_charts_still_show(fileset, queue_on, monkeypatch):
    monkeypatch.setattr(release_queue, "mark_released", lambda *a, **k: {"ok": False, "error": "BigQuery down"})
    prep, _ = said(RF.prepare_release_fileset, dict(CARE))
    out, at_apply = said(RF.apply_release_fileset, prep)
    assert out["ok"]
    assert at_apply[-2:] == [
        "Moving 2 charts from the queue to Release history",
        "The release queue could not be updated — its charts still show there",
    ]


def test_a_refused_preview_stops_after_the_guard_line(fileset, monkeypatch):
    blocker = SimpleNamespace(number=9, html_url="https://github.example/pull/9",
                              head=SimpleNamespace(ref="release/other"), base=SimpleNamespace(ref="UAT"))
    monkeypatch.setattr(RF, "_open_prd_pr_blocker", lambda repo, **kw: blocker)
    out, lines = said(RF.prepare_release_fileset, dict(CARE))
    assert out["ok"] is False and "PR #9" in out["errors"][0]
    assert lines == ["Checking for an open release PR on sit, UAT, PRD, PRL1"], \
        "no clone and no updater line for work that never started"


def test_a_failed_clone_does_not_claim_the_later_steps(fileset, monkeypatch):
    def broken(*a, **k):
        raise G.SnapshotError("could not fetch sit")

    monkeypatch.setattr(G, "clone_branch", broken)
    out, lines = said(RF.prepare_release_fileset, dict(CARE))
    assert out == {"ok": False, "errors": ["could not fetch sit"]}
    assert lines == ["Checking for an open release PR on sit, UAT, PRD, PRL1", "Cloning o/care-deploy at sit"]


def test_the_lines_cost_nothing_outside_a_turn(fileset):
    prep = RF.prepare_release_fileset(dict(CARE))      # no sink bound
    assert prep["ok"]


# --- CARE mono --------------------------------------------------------------------
@pytest.fixture
def mono(monkeypatch):
    for k, v in {"care_release_mode": "mono", "care_release_repo": MONO,
                 "care_release_base_branch": "main", "care_release_file": FILE,
                 "care_release_branch_prefix": "release/", "artifactory_base_url": "",
                 "care_previous_tags_file": PREV}.items():
        monkeypatch.setattr(CR.settings, k, v, raising=False)
    repo = FakeRepo({"main": {FILE: COMMITTED, PREV: PREV_COMMITTED}})
    repo.strict_sha = True
    monkeypatch.setattr(CR, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    return repo


def test_care_mono_says_each_step_at_preview_and_apply(mono, queue_on):
    prep, at_preview = said(RF.prepare_release_fileset, _mono_payload())
    assert prep["ok"], prep
    assert at_preview == [
        "Checking for an open release PR on main",
        f"Reading {FILE} on main",
        "Working out the smallest change to the release file",
        f"Reading {PREV}",
    ]

    out, at_apply = said(RF.apply_release_fileset, prep)
    assert out["ok"], out
    assert at_apply == [
        "Checking main has not moved since the preview",
        "Checking for an open release PR on main",
        "Claiming branch release/team-care-release-2026-10-01",
        "Committing the previous release tags",
        "Committing the release file",
        "Raising the PR into main — a person merges it",
        "Moving 2 charts from the queue to Release history",
    ]


def test_care_mono_without_a_previous_tags_file_skips_those_lines(mono, queue_on, monkeypatch):
    monkeypatch.setattr(CR.settings, "care_previous_tags_file", "")
    prep, at_preview = said(RF.prepare_release_fileset, _mono_payload())
    assert at_preview == [
        "Checking for an open release PR on main",
        f"Reading {FILE} on main",
        "Working out the smallest change to the release file",
    ]
    out, at_apply = said(RF.apply_release_fileset, prep)
    assert out["ok"], out
    assert at_apply == [
        "Checking main has not moved since the preview",
        "Checking for an open release PR on main",
        "Claiming branch release/team-care-release-2026-10-01",
        "Committing the release file",
        "Raising the PR into main — a person merges it",
        "Moving 2 charts from the queue to Release history",
    ]


def test_care_mono_refused_for_an_open_release_pr_says_only_the_guard(mono):
    mono.create_pull("Last week", "", "release/team-care-release-2026-09-24", "main")
    out, lines = said(RF.prepare_release_fileset, _mono_payload())
    assert out["ok"] is False and "already awaiting review" in out["errors"][0]
    assert lines == ["Checking for an open release PR on main"]


def test_care_mono_apply_stopped_by_a_moved_file_does_not_claim_a_branch(mono, queue_on):
    prep, _ = said(RF.prepare_release_fileset, _mono_payload())
    mono.files["main"][FILE] = COMMITTED.replace("Fixes the swagger page.", "Edited on main.")
    out, lines = said(RF.apply_release_fileset, prep)
    assert out["ok"] is False and "changed since the preview" in out["error"]
    assert lines == ["Checking main has not moved since the preview"]


def test_mono_re_apply_on_a_branch_that_already_carries_the_release(mono, queue_on):
    prep, _ = said(RF.prepare_release_fileset, _mono_payload())
    first, _ = said(RF.apply_release_fileset, prep)
    assert first["ok"]
    again, lines = said(RF.apply_release_fileset, prep)
    assert again["ok"] and again["action"] == "release_pr_exists"
    assert lines == [
        "Checking main has not moved since the preview",
        "Checking for an open release PR on main",
        "Claiming branch release/team-care-release-2026-10-01",
        "release/team-care-release-2026-10-01 already carries this release",
    ]
