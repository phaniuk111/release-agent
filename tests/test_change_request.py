"""Tests for the PROD change-request feature and the prod-updates-both-files guarantee."""
import json

from adk_release_agent import deploy
from adk_release_agent.deploy_workflow import _preview_text
from release_agent.agent.parsing import _try_parse_json_payload
from release_agent.tools.promotion import _deployment_path, change_request_doc, plan_deploy


# --- requirement 1: a prod deploy plans BOTH deployment.json files ---------------

def test_prod_deploy_plans_both_deployment_files():
    entries = [{"helm_chart_name": "svc", "helm_chart_version": "1.0.0"}]
    plan = plan_deploy("prod", entries)
    assert set(plan) == {_deployment_path("uat"), _deployment_path("prd")}
    # UAT copy repoints the values file to the uat one; prd keeps the prod entries.
    assert plan[_deployment_path("prd")] == entries


def test_uat_deploy_plans_only_uat_file():
    plan = plan_deploy("uat", [{"helm_chart_name": "svc", "helm_chart_version": "1.0.0"}])
    assert set(plan) == {_deployment_path("uat")}


# --- change_request_doc mapping (pure) ------------------------------------------

def test_change_request_doc_maps_semantic_and_chg_keys():
    d = change_request_doc(
        {"summary": "S", "change_description": "D", "start_time": "a", "end_time": "b"}, "NOW"
    )
    assert d == {
        "chg_summary": "S",
        "description": "D",
        "start_date": "a",
        "end_date": "b",
        "updated_by": "release-copilot",
        "updated_at": "NOW",
    }
    # Canonical CHG keys pass through unchanged.
    d2 = change_request_doc({"chg_summary": "X", "start_date": "s", "end_date": "e"}, "NOW")
    assert d2["chg_summary"] == "X" and d2["start_date"] == "s" and d2["description"] == ""
    # JSON string is accepted.
    assert change_request_doc('{"summary": "J"}', "NOW")["chg_summary"] == "J"


def test_change_request_doc_returns_none_when_empty():
    assert change_request_doc(None, "NOW") is None
    assert change_request_doc({}, "NOW") is None
    assert change_request_doc("not json", "NOW") is None
    assert change_request_doc({"unrelated": "x"}, "NOW") is None


# --- flow: change_request carried through parse -> preview -> apply args ----------

def test_json_payload_carries_change_request():
    req = _try_parse_json_payload(
        json.dumps(
            {
                "environment": "prod",
                "include": [{"helm_chart_name": "a", "helm_chart_version": "1"}],
                "change_request": {"chg_summary": "S"},
            }
        )
    )
    assert req["change_request"]["chg_summary"] == "S"
    # No change_request key -> None (uat/CLI paths unaffected).
    req2 = _try_parse_json_payload(
        json.dumps({"include": [{"helm_chart_name": "a", "helm_chart_version": "1"}]})
    )
    assert req2["change_request"] is None


def test_prod_change_request_flows_into_open_release_pr(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    payload = json.dumps(
        {
            "environment": "prod",
            "include": [
                {"helm_chart_name": "svc-a", "helm_chart_version": "1.0.0", "gke_namespace": "ns"}
            ],
            "change_request": {
                "chg_summary": "Quarterly release",
                "description": "Deploy svc-a",
                "start_date": "2026-07-03T09:00:00Z",
                "end_date": "2026-07-03T11:00:00Z",
            },
        }
    )
    preview = deploy.prepare_deploy_preview(deployment_json=payload)
    assert preview["ok"] is True and preview["environment"] == "prod"
    assert preview["change_request"]["chg_summary"] == "Quarterly release"

    calls = []

    def fake_invoke(name, args):
        calls.append((name, args))
        return {"ok": True, "note": "staged"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)
    deploy.apply_confirmed_deploy(preview["token"])

    assert len(calls) == 1
    name, args = calls[0]
    assert name == "open_release_pr" and args["environment"] == "prod"
    assert args["change_request"]["chg_summary"] == "Quarterly release"
    assert args["change_request"]["end_date"] == "2026-07-03T11:00:00Z"


def test_uat_deploy_carries_no_change_request(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    preview = deploy.prepare_deploy_preview(image_tags="svc-a:1.0.0", environment="uat")
    calls = []
    monkeypatch.setattr(deploy, "_invoke_tool", lambda n, a: calls.append((n, a)) or {"ok": True})
    deploy.apply_confirmed_deploy(preview["token"])
    _, args = calls[0]
    assert "change_request" not in args


# --- preview rendering ----------------------------------------------------------

def test_preview_text_includes_change_request_for_prod():
    text = _preview_text(
        {"prd/deployment.json": []},
        "CONFIRM-X",
        "prod",
        "svc:1",
        {"chg_summary": "S", "start_date": "a", "end_date": "b", "description": "D"},
    )
    assert "Change request" in text and "S" in text and "a → b" in text


def test_preview_text_omits_change_request_when_none():
    text = _preview_text({"uat/deployment.json": []}, "CONFIRM-Y", "uat", "svc:1", None)
    assert "Change request" not in text


# --- cutoff promotion: change-request.json travels to all live branches ----------

def test_doc_changed_ignores_updated_at():
    from release_agent.tools.promotion import _doc_changed

    a = {"chg_summary": "S", "updated_at": "t1"}
    b = {"chg_summary": "S", "updated_at": "t2"}
    assert _doc_changed(a, b) is False  # only updated_at differs
    assert _doc_changed(a, {"chg_summary": "X", "updated_at": "t2"}) is True
    assert _doc_changed({}, {"chg_summary": "S"}) is True


class _FakeContent:
    def __init__(self, text):
        self.decoded_content = text.encode()
        self.sha = "sha"


class _FakeRef:
    def __init__(self, sha):
        self.object = type("O", (), {"sha": sha})()

    def delete(self):
        pass


class _FakePR:
    def __init__(self, repo, head, base):
        self.repo, self.head_b, self.base_b = repo, head, base
        repo._pr += 1
        self.number = repo._pr
        self.html_url = f"http://pr/{self.number}"
        self.mergeable, self.mergeable_state, self.merge_commit_sha = True, "clean", "msha"

    def update(self):
        pass

    def merge(self, merge_method="squash"):
        self.repo.files.setdefault(self.base_b, {}).update(self.repo.files.get(self.head_b, {}))


class _FakeRepo:
    """Minimal PyGithub stand-in: files[branch][path] = json string."""

    def __init__(self, initial):
        self.files = {b: {p: json.dumps(d) for p, d in fs.items()} for b, fs in initial.items()}
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
        return _FakePR(self, head, base)


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


def test_promote_targeted_promotes_change_request_to_all_branches():
    from release_agent.tools import promotion as P

    cr = {
        "chg_summary": "S", "description": "D", "start_date": "a", "end_date": "b",
        "updated_by": "release-copilot", "updated_at": "t",
    }
    initial = {
        b: {"prd/deployment.json": {"include": []}, "uat/deployment.json": {"include": []}}
        for b in ("SIT", "UAT", "PRD")
    }
    repo = _FakeRepo(initial)
    res = P._promote_targeted(repo, [], "test release", extra_files={"change-request.json": cr})

    assert res["delivered"] is True
    for br in ("SIT", "UAT", "PRD"):
        assert "change-request.json" in repo.files[br], f"CHG missing on {br}"
        assert json.loads(repo.files[br]["change-request.json"]) == cr


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
        pr = _FakePR(self, head, base)
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
    from release_agent.tools import promotion as P

    initial = {b: {"uat/deployment.json": {"include": [_chart("1.0.0")]}} for b in ("SIT", "UAT")}
    repo = _ProtectedRepo(initial)

    class _Github:
        def get_repo(self, full):
            return repo

    monkeypatch.setattr(P, "_get_github_client", lambda: _Github())
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
    from release_agent.tools import promotion as P

    initial = {b: {"uat/deployment.json": {"include": [_chart("1.0.0")]}} for b in ("SIT", "UAT")}
    repo = _ProtectedRepo(initial, protected=("SIT",))

    class _Github:
        def get_repo(self, full):
            return repo

    monkeypatch.setattr(P, "_get_github_client", lambda: _Github())
    monkeypatch.setattr(P, "active_deploy_repo", lambda: "example-org/deploy")
    out = json.loads(P.open_release_pr.invoke({"environment": "uat", "image_tags": "svc-a:2.0.0"}))

    assert out["action"] == "pending_review"
    [pending] = out["pending_prs"]
    assert pending["stage"] == "→SIT" and pending["final"] is False
    assert "then run this again" in out["note"]
