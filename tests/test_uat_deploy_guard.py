"""A UAT deploy overwrites uat/deployment.json with exactly what the developer
gives — and so it goes ahead only when nothing else is changing that file.

Load test, 2026-09-29: ten developers confirming at once each overwrote the
others, and people were told "Deployed" whose charts were gone a minute later.
The rule since: an open PR into SIT or UAT that changes the file blocks a deploy
(merge it and what is in it deploys, close it and it is abandoned), and so does
another portal deploy that is still running.
"""
import contextvars
import json
import threading
from types import SimpleNamespace

import pytest

from adk_release_agent import deploy
from release_agent import identity
from release_agent.config import settings
from release_agent.tools import promotion as P
from test_concurrency import _chart, _lands_once, _RacingRepo

UAT_FILE = "uat/deployment.json"


def _open_pr(number, base, files=(UAT_FILE,), requested_by="alice@example.com"):
    return SimpleNamespace(
        number=number, base=base, title=f"Deploy reports-api:1.0.0 to uat (→ {base})",
        html_url=f"https://github.example/o/deploy/pull/{number}",
        body=f"Deploy reports-api:1.0.0 to uat\n\nRequested-by: {requested_by}\n",
        user=SimpleNamespace(login="portal-bot"),
        get_files=lambda: [SimpleNamespace(filename=f) for f in files])


class _Repo(_RacingRepo):
    """The racing fake, plus the open PRs a guard asks about."""

    def __init__(self, initial, open_prs=(), race=None):
        super().__init__(initial, race=race)
        self.open_prs = list(open_prs)
        self.full_name = "o/deploy"

    def get_pulls(self, state="open", base=None, head=None, **kw):
        branch = head.split(":", 1)[-1] if head else None
        return [p for p in self.open_prs if p.base == base
                and (branch is None or getattr(p, "head", None) == branch)]

    # A signed-in caller's deploy names them as commit author and merger.
    def create_file(self, path, msg, content, branch=None, **attribution):
        super().create_file(path, msg, content, branch)

    def update_file(self, path, msg, content, sha, branch=None, **attribution):
        super().update_file(path, msg, content, sha, branch)

    def create_pull(self, title, body, head, base):
        pr = super().create_pull(title, body, head, base)
        merge = pr.merge
        pr.merge = lambda merge_method="squash", **attribution: merge(merge_method)
        return pr


@pytest.fixture
def uat(monkeypatch):
    """open_release_pr against a fake repo whose SIT and UAT hold ``base:1.0``."""
    monkeypatch.setattr(settings, "sit_branch", "SIT")
    monkeypatch.setattr(settings, "uat_branch", "UAT")
    monkeypatch.setattr(P, "_find_deploy_run", lambda *a, **k: None)
    monkeypatch.setattr(P, "active_deploy_repo", lambda: "o/deploy")
    holder = {}

    def make(open_prs=(), race=None):
        initial = {b: {UAT_FILE: {"include": [_chart("base", "1.0")]}} for b in ("SIT", "UAT")}
        holder["repo"] = _Repo(initial, open_prs=open_prs, race=race)
        monkeypatch.setattr(P, "_get_github_client",
                            lambda: SimpleNamespace(get_repo=lambda name: holder["repo"]))
        return holder["repo"]
    return make


def _deploy(chart="ours", version="2.0"):
    return json.loads(P.open_release_pr("uat", deployment_json=json.dumps({"include": [_chart(chart, version)]})))


# --- an open PR on the file blocks a deploy ------------------------------------

@pytest.mark.parametrize("base", ["SIT", "UAT"])
def test_an_open_pr_on_the_file_blocks_the_deploy_and_says_whose_it_is(uat, base):
    repo = uat(open_prs=[_open_pr(7, base)])

    out = _deploy()

    assert out["ok"] is False and out["action"] == "blocked"
    assert "PR #7" in out["error"] and "alice@example.com" in out["error"]
    assert "Merge it" in out["error"] and "close it" in out["error"]
    assert repo.prs == [], "nothing was raised"
    assert repo.include("UAT") == ["base:1.0"] and repo.include("SIT") == ["base:1.0"]


def test_an_open_pr_on_other_files_does_not_block(uat):
    repo = uat(open_prs=[_open_pr(8, "SIT", files=("prd/deployment.json",))])

    out = _deploy()

    assert out["ok"] is True and out["action"] == "pending_review"
    assert repo.include("SIT") == ["ours:2.0"], "an overwrite with exactly what was given"
    assert repo.include("UAT") == ["base:1.0"], "UAT waits for the SIT -> UAT PR"


def test_once_the_pr_is_closed_the_next_deploy_overwrites_the_file(uat):
    blocking = _open_pr(7, "UAT")
    repo = uat(open_prs=[blocking])
    assert _deploy()["action"] == "blocked"

    repo.open_prs.remove(blocking)            # Alice's PR merged or closed

    assert _deploy("bob", "3.0")["action"] == "pending_review"
    assert repo.include("SIT") == ["bob:3.0"], "the next deploy overwrites SIT's file"


# --- another deploy still running blocks a deploy ------------------------------

def test_two_deploys_at_once_one_runs_and_the_other_is_told_who_is_deploying(uat):
    repo = uat()
    inside, release = threading.Event(), threading.Event()
    real_create = repo.create_pull

    def slow_create(title, body, head, base):     # Alice's deploy, mid-chain, no PR open yet
        inside.set()
        release.wait(5)
        return real_create(title, body, head, base)
    repo.create_pull = slow_create

    results = {}
    with identity.activate(identity.Caller(email="alice@example.com")):
        # The app runs a confirm via asyncio.to_thread, which carries the caller along.
        ctx = contextvars.copy_context()
    alice = threading.Thread(target=lambda: ctx.run(lambda: results.setdefault("alice", _deploy("alice", "1.0"))))
    alice.start()
    assert inside.wait(5)
    with identity.activate(identity.Caller(email="bob@example.com")):
        results["bob"] = _deploy("bob", "2.0")
    release.set()
    alice.join(5)

    assert results["bob"]["action"] == "blocked"
    assert "alice@example.com is deploying to UAT right now" in results["bob"]["error"]
    assert results["alice"]["action"] == "pending_review"
    assert repo.include("SIT") == ["alice:1.0"], "only alice's change is on SIT"


def test_the_running_marker_is_cleared_even_when_a_deploy_fails(uat):
    repo = uat()

    def boom(*a, **k):
        raise RuntimeError("GitHub is down")
    repo.create_git_ref = boom

    with pytest.raises(RuntimeError):
        _deploy()
    assert P._UAT_DEPLOYS_RUNNING == {}, "a failed deploy must not block everyone after it"


# --- a conflict is reported, not rebuilt over someone else's change -------------

def test_a_uat_deploy_that_conflicts_is_left_open_and_said_to_conflict(uat):
    repo = uat(race=_lands_once("SIT", lambda inc: inc.append(_chart("theirs", "9.0"))))

    out = _deploy()

    assert out["ok"] is True and out["action"] == "pending_review"
    assert "conflicts" in out["note"] and "awaiting approval" not in out["note"]
    assert len([p for p in repo.prs if p.base == "SIT"]) == 1, "not rebuilt over their change"
    assert "theirs:9.0" in repo.include("SIT")


def test_github_saying_merge_conflicts_is_read_as_a_conflict():
    from github import GithubException

    def merge(merge_method="squash"):
        raise GithubException(405, {"message": "Pull Request has merge conflicts"})
    pr = SimpleNamespace(mergeable=True, mergeable_state="clean", update=lambda: None, merge=merge)

    assert P._merge_pr(pr) == (False, P.MERGE_CONFLICT)


# --- said before a token is minted, and as the form opens -----------------------

def test_a_blocked_deploy_gets_no_preview_and_no_token(monkeypatch):
    monkeypatch.setattr(deploy, "_uat_deploy_blocked", lambda req: "uat/deployment.json has an open PR: #7")
    deploy._PENDING_PREVIEWS.clear()

    out = deploy.prepare_deploy_preview(message=json.dumps({"environment": "uat", "include": [
        {"helm_chart_name": "ours", "helm_chart_version": "2.0", "gke_namespace": "ns"}]}))

    assert out["ok"] is False and "#7" in out["error"] and "token" not in out
    assert deploy._PENDING_PREVIEWS == {}


def test_the_deploy_form_opens_with_the_reason_it_would_be_refused(uat, monkeypatch):
    from fastapi.testclient import TestClient

    from release_agent import app_fastapi as A
    from release_agent.tools import _common

    repo = uat(open_prs=[_open_pr(7, "UAT")])
    monkeypatch.setattr(_common, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda name: repo))

    body = TestClient(A.app).get("/api/deploy-template", params={"env": "uat"}).json()

    assert body["from_repo"] is True and "PR #7" in body["blocked"]


# --- the file carries the change and nothing else --------------------------------

def test_a_deploy_writes_no_stamp_and_drops_the_old_one(uat):
    repo = uat()
    for b in ("SIT", "UAT"):
        doc = json.loads(repo.files[b][UAT_FILE])
        repo.files[b][UAT_FILE] = json.dumps({**doc, "updated_by": "release-copilot"}, indent=2) + "\n"

    _deploy()

    text = repo.files["SIT"][UAT_FILE]
    assert "updated_by" not in text, "no stamp added, and the old one is gone"
    assert text.endswith("}\n"), "the file keeps its trailing newline"
    assert list(json.loads(text)) == ["include"]
    assert "updated_by" in repo.files["UAT"][UAT_FILE], "UAT is the SIT -> UAT PR's to change"


def test_a_file_without_a_trailing_newline_is_left_without_one(uat):
    repo = uat()
    _deploy()
    assert not repo.files["SIT"][UAT_FILE].endswith("\n")
