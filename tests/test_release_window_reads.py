"""Deploy status reads each branch's open PRs once, all at the same time."""
import threading
from types import SimpleNamespace as _NS

from release_agent.tools import release_window as RW


def _pr(number, base, head="UAT"):
    return _NS(number=number, html_url=f"https://github.com/example-org/deploy/pull/{number}",
               head=_NS(ref=head), base=_NS(ref=base))


class _CountingRepo:
    def __init__(self, open_by_base, fail=()):
        self.open_by_base, self.fail, self.asked = open_by_base, set(fail), []
        self._lock = threading.Lock()
        self.full_name = "example-org/deploy"

    def get_pulls(self, state="open", base=None, sort="created", direction="desc", **kw):
        with self._lock:
            self.asked.append((base, state, sort, direction, tuple(sorted(kw))))
        if base in self.fail:
            raise RuntimeError(f"{base}: 403")
        return iter(self.open_by_base.get(base, []))


def test_each_branch_is_read_once_however_often_it_is_asked():
    repo = _CountingRepo({"PRD": [_pr(312, "PRD")], "PRL1": [_pr(313, "PRL1")]})
    pulls = RW._OpenPullsReadOnce(repo, ["PRD", "PRL1", "PRD", "PRL1", ""])
    for _ in range(3):
        assert [p.number for p in pulls.get_pulls(state="open", base="PRD", sort="created", direction="desc")] == [312]
    assert [p.number for p in pulls.get_pulls(base="PRL1")] == [313]
    assert sorted(b for b, *_ in repo.asked) == ["PRD", "PRL1"]


def test_a_branch_that_failed_raises_when_asked_and_the_callers_skip_it():
    repo = _CountingRepo({"PRL1": [_pr(313, "PRL1")]}, fail={"PRD"})
    pulls = RW._OpenPullsReadOnce(repo, ["PRD", "PRL1"])
    try:
        pulls.get_pulls(base="PRD")
        raise AssertionError("expected the stored error")
    except RuntimeError as e:
        assert "403" in str(e)
    assert [p.number for p in RW._open_release_prs(pulls, ["PRD", "PRL1"])] == [313]
    assert RW._open_prd_pr_blocker(pulls, branches=["PRD"]) is None


def test_any_other_question_goes_to_github_as_before():
    repo = _CountingRepo({"PRD": [_pr(312, "PRD")]})
    pulls = RW._OpenPullsReadOnce(repo, ["PRD"])
    repo.asked.clear()
    list(pulls.get_pulls(state="closed", base="PRD"))
    list(pulls.get_pulls(base="PRD", head="org:x"))
    list(pulls.get_pulls(base="SIT"))
    assert [(b, s) for b, s, *_ in repo.asked] == [("PRD", "closed"), ("PRD", "open"), ("SIT", "open")]
    assert pulls.full_name == "example-org/deploy", "everything else is the repository's own"


def test_only_the_first_depth_prs_are_kept():
    repo = _CountingRepo({"PRD": [_pr(n, "PRD") for n in range(100)]})
    assert len(list(RW._OpenPullsReadOnce(repo, ["PRD"], depth=30).get_pulls(base="PRD"))) == 30


def test_status_still_blocks_and_lists_with_one_read_per_branch(monkeypatch):
    repo = _CountingRepo({"PRD": [_pr(312, "PRD")], "PRL1": [_pr(313, "PRL1")]})
    monkeypatch.setattr(RW, "_get_github_client", lambda: _NS(get_repo=lambda full: repo))
    monkeypatch.setattr(RW, "_charts", lambda repo, env: {"svc": "1.0.0"} if env == "prd" else {})
    monkeypatch.setattr("release_agent.tools.release_chain.guard_branches", lambda kind: ["PRD"])
    monkeypatch.setattr("release_agent.tools.release_chain.production_branches", lambda: ["PRL1", "PRD"])
    s = RW.get_release_status("example-org/deploy")
    assert s["blocking_pr"]["number"] == 312 and "PR #312" in s["reason"]
    assert [p["number"] for p in s["release_prs"]] == [313, 312]
    assert s["prd_charts"] == [{"helm_chart_name": "svc", "helm_chart_version": "1.0.0"}]
    assert sorted(b for b, *_ in repo.asked) == ["PRD", "PRL1"], "was PRD twice and PRL1 once"


def test_a_repo_that_cannot_be_opened_is_an_error_dict(monkeypatch):
    def boom(full):
        raise RuntimeError("bad credentials")
    monkeypatch.setattr(RW, "_get_github_client", lambda: _NS(get_repo=boom))
    s = RW.get_release_status("example-org/deploy")
    assert "bad credentials" in s["error"] and s["release_prs"] == [] and s["blocking_pr"] is None
