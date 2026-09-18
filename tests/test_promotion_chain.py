"""Tests for the generic SIT->UAT->PRD targeted-PR promotion chain (_promote_targeted
and its helpers): protected-branch refusals, conflict handling, dedupe, and the
UAT-deploy-drops-a-chart reporting. None of this is specific to any one caller —
open_release_pr (uat), remove_from_release and the CARE/DF release promotion all
share it.
"""
import json

from release_agent.tools.promotion import _deployment_path, plan_deploy
from tests.fakes import FakeRepo as _FakeRepo


def test_uat_deploy_plans_only_uat_file():
    plan = plan_deploy("uat", [{"helm_chart_name": "svc", "helm_chart_version": "1.0.0"}])
    assert set(plan) == {_deployment_path("uat")}


def test_doc_changed_ignores_updated_at():
    from release_agent.tools.promotion import _doc_changed

    a = {"chg_summary": "S", "updated_at": "t1"}
    b = {"chg_summary": "S", "updated_at": "t2"}
    assert _doc_changed(a, b) is False  # only updated_at differs
    assert _doc_changed(a, {"chg_summary": "X", "updated_at": "t2"}) is True
    assert _doc_changed({}, {"chg_summary": "S"}) is True


def test_promote_targeted_dedupes_stale_duplicate_entries():
    """A whole-branch SIT->UAT git merge can leave the same chart twice in include[]
    (3-way merge artifact — both sides had it at different positions). A later targeted
    promotion must self-heal: include[] starts with the duplicated chart and ends with
    exactly one entry for it, at the promoted version."""
    from release_agent.tools import promotion as P

    def entry(version):
        return {
            "helm_chart_name": "targeted-svc",
            "helm_chart_version": version,
            "helm_chart_dir": "d",
            "helm_values_file_name": "uat/values_uat.yaml",
            "gke_namespace": "ns",
        }

    initial = {
        "SIT": {"uat/deployment.json": {"include": [entry("1.1.0")]}},
        # include[] starts with the duplicated chart: current 1.1.0 plus a stale 1.0.0.
        "UAT": {"uat/deployment.json": {"include": [entry("1.1.0"), entry("1.0.0")]}},
        "PRD": {"uat/deployment.json": {"include": []}},
    }
    repo = _FakeRepo(initial)
    res = P._promote_targeted(
        repo,
        [("uat/deployment.json", P._upsert_each([entry("1.2.0")]))],
        "promote targeted-svc",
    )

    assert res["delivered"] is True
    for br in ("SIT", "UAT", "PRD"):
        inc = json.loads(repo.files[br]["uat/deployment.json"])["include"]
        versions = [e["helm_chart_version"] for e in inc if e["helm_chart_name"] == "targeted-svc"]
        assert versions == ["1.2.0"], f"{br}: expected one deduped entry, got {versions}"


def test_promote_targeted_writes_extra_files_verbatim_to_all_branches():
    """extra_files (e.g. a flat, non-include[] doc) travels alongside the targeted
    include[] edits, to every branch in the chain."""
    from release_agent.tools import promotion as P

    doc = {"summary": "S", "updated_by": "release-copilot", "updated_at": "t"}
    initial = {
        b: {"prd/deployment.json": {"include": []}, "uat/deployment.json": {"include": []}}
        for b in ("SIT", "UAT", "PRD")
    }
    repo = _FakeRepo(initial)
    res = P._promote_targeted(repo, [], "test release", extra_files={"extra.json": doc})

    assert res["delivered"] is True
    for br in ("SIT", "UAT", "PRD"):
        assert "extra.json" in repo.files[br], f"extra.json missing on {br}"
        assert json.loads(repo.files[br]["extra.json"]) == doc


# --- a protected branch refuses the merge ------------------------------------

class _ProtectedRefusal(Exception):
    """Shaped like PyGithub's GithubException for a protected-branch 405."""

    def __init__(self):
        super().__init__('405 {"message": "...", "documentation_url": "https://docs.github.com/..."}')
        self.status = 405
        self.data = {
            "message": "Waiting on code owner review from example-org/owners. "
                       'Required status check "ci / checks" is queued.',
            "documentation_url": "https://docs.github.com/articles/about-protected-branches",
        }


class _ProtectedRepo(_FakeRepo):
    """A repo whose named branches refuse merges, the way branch protection does."""

    def __init__(self, initial, protected=("UAT",)):
        super().__init__(initial)
        self.protected = set(protected)

    def create_pull(self, title, body, head, base):
        pr = super().create_pull(title, body, head, base)
        if base in self.protected:
            def refuse(merge_method="squash"):
                raise _ProtectedRefusal()
            pr.merge = refuse
        return pr


def _chart(version):
    return {"helm_chart_name": "svc-a", "helm_chart_version": version, "helm_chart_dir": "d",
            "helm_values_file_name": "uat/values_uat.yaml", "gke_namespace": "ns"}


def test_merge_refusal_reason_is_githubs_own_sentence():
    """The raw exception is a status code, the JSON body and a docs URL; the
    sentence the person must act on is buried in the middle of it."""
    from release_agent.tools.promotion import _merge_refusal_reason

    reason = _merge_refusal_reason(_ProtectedRefusal())
    assert reason.startswith("Waiting on code owner review")
    assert "405" not in reason and "documentation_url" not in reason


def test_chain_stops_at_the_protected_branch_and_is_not_delivered():
    from release_agent.tools import promotion as P

    initial = {b: {"uat/deployment.json": {"include": [_chart("1.0.0")]}} for b in ("SIT", "UAT")}
    repo = _ProtectedRepo(initial)
    res = P._promote_targeted(
        repo, [("uat/deployment.json", P._replace_with([_chart("2.0.0")]))],
        "deploy svc-a", branches=("SIT", "UAT"),
    )

    assert res["delivered"] is False
    sit, uat = res["prs"]
    assert sit["merged"] is True and uat["merged"] is False
    assert uat["url"], "the open PR must carry its link"
    assert "Waiting on code owner review" in uat["detail"]
    # Nothing reached UAT.
    live = json.loads(repo.files["UAT"]["uat/deployment.json"])["include"]
    assert [e["helm_chart_version"] for e in live] == ["1.0.0"]


def test_a_last_hop_already_in_state_counts_as_delivered():
    """SIT needed the change, UAT already had it: UAT is in the desired state,
    so the deploy HAS landed — calling that 'pending' would be the mirror lie."""
    from release_agent.tools import promotion as P

    initial = {"SIT": {"uat/deployment.json": {"include": [_chart("1.0.0")]}},
               "UAT": {"uat/deployment.json": {"include": [_chart("2.0.0")]}}}
    repo = _ProtectedRepo(initial)          # UAT protected, but never needs a PR
    res = P._promote_targeted(
        repo, [("uat/deployment.json", P._replace_with([_chart("2.0.0")]))],
        "deploy svc-a", branches=("SIT", "UAT"),
    )
    assert res["delivered"] is True


def test_uat_deploy_blocked_by_review_says_so_and_links_the_pr(monkeypatch):
    """The reported bug: 'Deployed … to UAT' and a chart count read from the
    UNCHANGED live file, while the UAT PR sat in review."""
    from types import SimpleNamespace

    from release_agent.tools import promotion as P

    initial = {b: {"uat/deployment.json": {"include": [_chart("1.0.0")]}} for b in ("SIT", "UAT")}
    repo = _ProtectedRepo(initial)

    monkeypatch.setattr(P, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    monkeypatch.setattr(P, "active_deploy_repo", lambda: "example-org/deploy")
    out = json.loads(P.open_release_pr.invoke({"environment": "uat", "image_tags": "svc-a:2.0.0"}))

    assert out["action"] == "pending_review"
    assert out["uat_charts"] is None, "must not quote the pre-deploy file as the result"
    assert "NOT deployed yet" in out["note"] and "Deployed svc-a" not in out["note"]
    [pending] = out["pending_prs"]
    assert pending["url"] and f"[PR #{pending['number']}]({pending['url']})" in out["note"]
    assert pending["reason"].startswith("Waiting on code owner review")


def test_pending_deploy_is_not_recorded_as_deployed(monkeypatch):
    """Recording it made per-environment state claim the version was live
    while it sat in review — and kept claiming so if the PR was closed."""
    from adk_release_agent import deploy as D
    from release_agent.tools import release_queue as RQ

    written = []
    monkeypatch.setattr(RQ, "record_deployment", lambda **kw: written.append(kw))

    D._record_deploy_event({"images": [{"name": "svc-a", "tag": "2.0.0"}]}, "uat",
                           {"ok": True, "action": "pending_review", "prs": [{"number": 7}]})
    assert written == []

    D._record_deploy_event({"images": [{"name": "svc-a", "tag": "2.0.0"}]}, "uat",
                           {"ok": True, "action": "deployed", "prs": [{"number": 8}]})
    assert len(written) == 1


def test_uat_deploy_blocked_at_sit_says_to_run_it_again(monkeypatch):
    """When the FIRST hop is blocked the UAT PR is never raised, so "approve
    and merge it" would leave UAT unchanged after they did exactly that."""
    from types import SimpleNamespace

    from release_agent.tools import promotion as P

    initial = {b: {"uat/deployment.json": {"include": [_chart("1.0.0")]}} for b in ("SIT", "UAT")}
    repo = _ProtectedRepo(initial, protected=("SIT",))

    monkeypatch.setattr(P, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    monkeypatch.setattr(P, "active_deploy_repo", lambda: "example-org/deploy")
    out = json.loads(P.open_release_pr.invoke({"environment": "uat", "image_tags": "svc-a:2.0.0"}))

    assert out["action"] == "pending_review"
    [pending] = out["pending_prs"]
    assert pending["stage"] == "→SIT" and pending["final"] is False
    assert "then run this again" in out["note"]


def _other(version):
    return {**_chart(version), "helm_chart_name": "svc-b"}


def test_a_uat_override_reports_the_charts_it_takes_off(monkeypatch):
    """Found in E2E: the override REPLACES uat/deployment.json, so a chart left
    out leaves UAT — and was never logged, leaving it 'deployed' forever."""
    from types import SimpleNamespace

    from release_agent.tools import promotion as P

    initial = {b: {"uat/deployment.json": {"include": [_chart("1.0.0"), _other("3.0.0")]}}
               for b in ("SIT", "UAT")}
    repo = _FakeRepo(initial)

    monkeypatch.setattr(P, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    monkeypatch.setattr(P, "active_deploy_repo", lambda: "example-org/deploy")
    out = json.loads(P.open_release_pr.invoke({"environment": "uat", "image_tags": "svc-a:2.0.0"}))
    assert out["action"] == "deployed"
    assert out["dropped"] == [{"name": "svc-b", "tag": "3.0.0"}]


def test_dropped_charts_are_logged_removed_or_pending_removal(monkeypatch):
    from adk_release_agent import deploy as D
    from release_agent.tools import pr_reconcile as R
    from release_agent.tools import release_queue as RQ

    written = []
    monkeypatch.setattr(RQ, "record_deployment", lambda **kw: written.append(kw))
    req = {"images": [{"name": "svc-a", "tag": "2.0.0"}], "deployment_repo": "o/d"}
    D._record_deploy_event(req, "uat", {"ok": True, "action": "deployed", "prs": [{"number": 8}],
                                        "dropped": [{"name": "svc-b", "tag": "3.0.0"}]})
    assert [(w["event_type"] if "event_type" in w else "deployed", w["artifacts"][0]["name"])
            for w in written] == [("deployed", "svc-a"), ("removed", "svc-b")]
    assert written[1]["pr_number"] == 8

    written.clear()
    D._record_deploy_event(req, "uat", {"ok": True, "action": "pending_review",
                                        "pending_prs": [{"number": 9, "final": True}],
                                        "dropped": [{"name": "svc-b", "tag": "3.0.0"}]})
    notes = [R.parse_pending_note(w["note"]) for w in written]
    assert [n[0] for n in notes] == ["deployed", "removed"], "settles as removed when the PR merges"
