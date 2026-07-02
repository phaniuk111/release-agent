"""Tests for remove_from_release: unstaging from today's PRD release PR vs live removal."""
import json
from types import SimpleNamespace

import pytest

from release_agent.tools import promotion as P
from release_agent.tools.release_window import _prd_release_branch

UAT_PATH = P._deployment_path("uat")
PRD_PATH = P._deployment_path("prd")


# --- minimal PyGithub stand-in (files[branch][path] = json string, PRs tracked) ---

class _FakeContent:
    def __init__(self, text):
        self.decoded_content = text.encode()
        self.sha = "sha"


class _FakeRef:
    def __init__(self, sha):
        self.object = SimpleNamespace(sha=sha)

    def delete(self):
        pass


class _FakePR:
    def __init__(self, repo, head, base):
        self.repo = repo
        self.head = SimpleNamespace(ref=head)
        self.base = SimpleNamespace(ref=base)
        self.state = "open"
        repo._pr += 1
        self.number = repo._pr
        self.html_url = f"http://pr/{self.number}"
        self.mergeable, self.mergeable_state, self.merge_commit_sha = True, "clean", "msha"

    def update(self):
        pass

    def edit(self, state=None, **kwargs):
        if state:
            self.state = state

    def merge(self, merge_method="squash"):
        self.repo.files.setdefault(self.base.ref, {}).update(
            self.repo.files.get(self.head.ref, {})
        )
        self.state = "closed"


class _FakeRepo:
    def __init__(self, initial):
        self.files = {b: {p: json.dumps(d) for p, d in fs.items()} for b, fs in initial.items()}
        self.prs = []
        self._pr = 0

    def get_git_ref(self, name):
        return _FakeRef(name.split("heads/", 1)[1])  # sha == branch name

    def create_git_ref(self, ref, sha):
        work = ref.split("heads/", 1)[1]
        self.files[work] = dict(self.files.get(sha, {}))

    def get_contents(self, path, ref=None):
        fs = self.files.get(ref, {})
        if path not in fs:
            raise Exception("404")
        return _FakeContent(fs[path])

    def create_file(self, path, msg, content, branch=None):
        self.files.setdefault(branch, {})[path] = content

    def update_file(self, path, msg, content, sha, branch=None):
        self.files.setdefault(branch, {})[path] = content

    def create_pull(self, title, body, head, base):
        pr = _FakePR(self, head, base)
        self.prs.append(pr)
        return pr

    def get_pulls(self, state="open", base=None, sort=None, direction=None):
        return [p for p in self.prs if p.state == state and (base is None or p.base.ref == base)]


def _entry(name, version, env="prd"):
    return P.assemble_entry(name, version, env)


def _include(repo, branch, path):
    return {e["helm_chart_name"] for e in P._read_include(repo, branch, path)}


@pytest.fixture
def make_repo(monkeypatch):
    """Build a _FakeRepo, wire it in as the GitHub client, and skip run polling."""

    def _make(initial, staging_prs=()):
        repo = _FakeRepo(initial)
        for head, base in staging_prs:
            repo.create_pull("PRD release", "staging", head, base)
        gh = SimpleNamespace(get_repo=lambda name: repo)
        monkeypatch.setattr(P, "_get_github_client", lambda: gh)
        monkeypatch.setattr(P, "_find_deploy_run", lambda *a, **k: None)
        return repo

    return _make


def _live_branches(with_targeted_on_uat=True):
    """SIT/UAT carry targeted-svc (already released to UAT); PRD holds only base-svc."""
    uat_inc = [_entry("base-svc", "1.0.0", "uat")]
    if with_targeted_on_uat:
        uat_inc = uat_inc + [_entry("targeted-svc", "1.2.0", "uat")]
    return {
        b: {
            PRD_PATH: {"include": [_entry("base-svc", "1.0.0", "prd")]},
            UAT_PATH: {"include": list(uat_inc)},
        }
        for b in ("SIT", "UAT", "PRD")
    }


def _staged(*charts):
    """A release/prd/<date> branch: PRD's live state plus today's staged charts."""
    return {
        PRD_PATH: {
            "include": [_entry("base-svc", "1.0.0", "prd")]
            + [_entry(n, v, "prd") for n, v in charts]
        },
        UAT_PATH: {
            "include": [_entry("base-svc", "1.0.0", "uat")]
            + [_entry(n, v, "uat") for n, v in charts]
        },
    }


# --- unstage from an open staging PR (default environment) -----------------------

def test_unstage_removes_chart_from_staging_pr_only(make_repo):
    branch = _prd_release_branch()
    initial = _live_branches()
    initial[branch] = _staged(("targeted-svc", "1.2.0"), ("other-svc", "2.0.0"))
    repo = make_repo(initial, staging_prs=[(branch, "PRD")])

    res = json.loads(P.remove_from_release("targeted-svc"))

    assert res["ok"] is True and res["action"] == "unstaged"
    assert res["environment"] == "staging" and res["removed"] == ["targeted-svc"]
    # Dropped from BOTH deployment files on the staging branch.
    assert _include(repo, branch, PRD_PATH) == {"base-svc", "other-svc"}
    assert _include(repo, branch, UAT_PATH) == {"base-svc", "other-svc"}
    # other-svc still pending, so the PR stays open.
    assert res["staging_pr"]["retired"] is False
    assert repo.prs[0].state == "open"
    assert res["staging_pr"]["still_pending"] == ["other-svc:2.0.0"]
    # Live environments untouched — the legitimately-released UAT version survives.
    assert "targeted-svc" in _include(repo, "UAT", UAT_PATH)
    assert "targeted-svc" in _include(repo, "SIT", UAT_PATH)
    assert _include(repo, "PRD", PRD_PATH) == {"base-svc"}


def test_unstaging_only_differing_chart_retires_staging_pr(make_repo):
    branch = _prd_release_branch()
    initial = _live_branches()
    initial[branch] = _staged(("targeted-svc", "1.2.0"))
    repo = make_repo(initial, staging_prs=[(branch, "PRD")])

    res = json.loads(P.remove_from_release("targeted-svc"))

    assert res["action"] == "unstaged" and res["staging_pr"]["retired"] is True
    assert res["staging_pr"]["still_pending"] == []
    assert repo.prs[0].state == "closed"
    # Staging branch reduced back to PRD's live state before retiring.
    assert _include(repo, branch, PRD_PATH) == {"base-svc"}


def test_unstage_with_no_staging_pr_is_a_safe_no_op(make_repo):
    repo = make_repo(_live_branches())

    res = json.loads(P.remove_from_release("targeted-svc"))

    assert res["ok"] is True and res["action"] == "no_change"
    assert res["environment"] == "staging"
    assert "not staged" in res["note"]
    # Nothing anywhere was touched — in particular not live UAT.
    assert "targeted-svc" in _include(repo, "UAT", UAT_PATH)
    assert repo.prs == []


def test_unstage_chart_not_in_open_staging_pr_is_no_change(make_repo):
    branch = _prd_release_branch()
    initial = _live_branches()
    initial[branch] = _staged(("other-svc", "2.0.0"))
    repo = make_repo(initial, staging_prs=[(branch, "PRD")])

    res = json.loads(P.remove_from_release("targeted-svc"))

    assert res["action"] == "no_change"
    assert repo.prs[0].state == "open"
    assert _include(repo, branch, PRD_PATH) == {"base-svc", "other-svc"}


# --- explicit live removal --------------------------------------------------------

def test_uat_removal_uses_targeted_per_branch_edits(make_repo):
    repo = make_repo(_live_branches())

    res = json.loads(P.remove_from_release("targeted-svc", environment="uat"))

    assert res["action"] == "removed" and res["environment"] == "uat"
    # Targeted edits landed on SIT and UAT; PRD was never part of the chain.
    assert "targeted-svc" not in _include(repo, "SIT", UAT_PATH)
    assert "targeted-svc" not in _include(repo, "UAT", UAT_PATH)
    assert _include(repo, "PRD", PRD_PATH) == {"base-svc"}
    stages = [p["stage"] for p in res["prs"]]
    assert stages == ["→SIT", "→UAT"]  # per-branch working PRs, no SIT→UAT branch merge


def test_prod_removal_also_unstages_from_open_staging_pr(make_repo):
    branch = _prd_release_branch()
    initial = _live_branches()
    # targeted-svc is live in PRD too, and a newer version is staged for tonight.
    for b in ("SIT", "UAT", "PRD"):
        initial[b][PRD_PATH]["include"].append(_entry("targeted-svc", "1.1.0", "prd"))
    initial[branch] = _staged(("targeted-svc", "1.2.0"), ("other-svc", "2.0.0"))
    repo = make_repo(initial, staging_prs=[(branch, "PRD")])

    res = json.loads(P.remove_from_release("targeted-svc", environment="prod"))

    assert res["action"] == "removed" and res["environment"] == "prod"
    # Unstaged from the release PR (so it can't ship again at the cutoff) ...
    assert "targeted-svc" not in _include(repo, branch, PRD_PATH)
    assert res["staging_pr"]["removed"] == ["targeted-svc"]
    # ... and removed from both files on all live branches.
    for b in ("SIT", "UAT", "PRD"):
        assert "targeted-svc" not in _include(repo, b, PRD_PATH)
        assert "targeted-svc" not in _include(repo, b, UAT_PATH)


def test_unsupported_environment_is_rejected(make_repo):
    make_repo(_live_branches())
    out = P.remove_from_release("targeted-svc", environment="qa")
    assert out.startswith("ERROR") and "staging, uat or prod" in out


def test_input_schema_defaults_to_staging():
    assert P.RemoveFromReleaseInput(image_names="x").environment == "staging"
