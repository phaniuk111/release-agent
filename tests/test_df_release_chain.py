"""DF releases travel their own branch chain — e.g. RELEASE_UAT → RELEASE_PRD, no
SIT — configured by DF_RELEASE_BRANCHES; CARE keeps SIT → UAT → PRD/PRL1.

The release itself runs for real against a git repository on disk that has
ONLY the DF branches (a SIT-assuming step would fail on it); GitHub's API is
faked and records which repo every call went to.
"""
import json
from types import SimpleNamespace

import pytest
from dulwich import porcelain

from release_agent.tools import git_snapshot as G
from release_agent.tools import release_chain as C
from release_agent.tools import release_fileset as RF

DF_CHAIN = "uat:RELEASE_UAT,prd:RELEASE_PRD"

UPDATER = '''\
import argparse, json, os
ap = argparse.ArgumentParser(); ap.add_argument("--release-details-file"); a = ap.parse_args()
d = json.load(open(a.release_details_file))
os.makedirs("artefact-provider", exist_ok=True)
with open("artefact-provider/artefact.json", "w") as f:
    json.dump({"artefact": d["artefact"], "df_images": d["df_images"]}, f, indent=2)
'''


@pytest.fixture(autouse=True)
def chains(monkeypatch):
    s = C.settings
    for k, v in {"df_release_branches": DF_CHAIN, "df_release_repo": "o/df-release",
                 "deploy_repo": "o/care-deploy", "sit_branch": "SIT", "uat_branch": "UAT",
                 "prd_branch": "PRD", "prl1_branch": "PRL1",
                 "release_guard_branches": ["SIT", "UAT", "PRD", "PRL1"],
                 "artifactory_base_url": ""}.items():
        monkeypatch.setattr(s, k, v, raising=False)


# --- the chain itself ---------------------------------------------------------
def test_df_lands_on_its_uat_branch_and_promotes_to_prd_only():
    assert C.chain("df") == [("uat", "RELEASE_UAT"), ("prd", "RELEASE_PRD")]
    assert C.landing("df") == "RELEASE_UAT"
    assert C.targets("df") == {"prd": "RELEASE_PRD", "prod": "RELEASE_PRD"}
    assert C.guard_branches("df") == ["RELEASE_UAT", "RELEASE_PRD"]
    assert C.next_steps("df") == "Promote with 'promote the DF release to prd'."


def test_care_keeps_sit_uat_prd_prl1():
    assert C.landing("care") == "SIT"
    assert set(C.targets("care")) == {"uat", "prd", "prod", "prl1"}
    assert C.guard_branches("care") == ["SIT", "UAT", "PRD", "PRL1"]
    assert C.next_steps("care") == "Promote with 'promote release to uat' then 'promote release to prd/prl1'."


def test_no_df_chain_configured_means_one_chain_for_both(monkeypatch):
    monkeypatch.setattr(C.settings, "df_release_branches", "", raising=False)
    assert C.chain("df") == C.chain("care")


def test_parse_ignores_junk_and_keeps_order():
    assert C.parse(" uat : A , , prd:B, junk ,:C") == [("uat", "A"), ("prd", "B")]


def test_kind_is_df_only_when_every_artifact_is_a_df_image():
    assert C.kind_of({"artefact": ["df-a:1", "https://x/df-b:2"], "df_images": ["df-a", "df-b"]}) == "df"
    assert C.kind_of({"artefact": ["svc:1", "df-a:1"], "df_images": ["df-a"]}) == "care"
    assert C.kind_of({"artefact": ["svc:1"], "df_images": []}) == "care"
    assert C.kind_of({"artefact": ["svc:1"]}, explicit="df") == "df"


# --- a DF release end to end --------------------------------------------------
@pytest.fixture
def df_remote(tmp_path):
    """A DF release repo with ONLY RELEASE_UAT and RELEASE_PRD — no SIT at all."""
    path = tmp_path / "df-remote"
    repo = porcelain.init(str(path))
    (path / "scripts/release").mkdir(parents=True)
    (path / "scripts/release/update_release_files.py").write_text(UPDATER)
    porcelain.add(repo, [str(path / "scripts/release/update_release_files.py")])
    porcelain.commit(repo, message=b"base", author=b"a <a@x>", committer=b"a <a@x>")
    for b in ("RELEASE_UAT", "RELEASE_PRD"):
        porcelain.branch_create(repo, b)
    repo.close()
    return str(path)


class _Gh:
    """GitHub, recording which repo each call targeted."""

    def __init__(self):
        self.repos_asked, self.pulls, self.refs, self.guarded = [], [], [], []

    def get_repo(self, full):
        self.repos_asked.append(full)
        gh = self

        class _R:
            full_name = full

            def create_git_blob(self, content, encoding):
                return SimpleNamespace(sha="b")

            def get_git_commit(self, sha):
                return SimpleNamespace(sha=sha, tree="t")

            def create_git_tree(self, elements, base_tree=None):
                return "nt"

            def create_git_commit(self, message, tree, parents, author=None):
                return SimpleNamespace(sha="c0ffee")

            def create_git_ref(self, ref, sha):
                gh.refs.append((full, ref))

            def create_pull(self, **kw):
                gh.pulls.append((full, kw))
                return SimpleNamespace(number=5, html_url="https://github.example/pull/5")
        return _R()


@pytest.fixture
def wired(monkeypatch, df_remote):
    gh = _Gh()
    monkeypatch.setattr(G, "repo_url", lambda repo_full: df_remote)
    monkeypatch.setattr(RF, "_resolve_github_token", lambda: "")
    monkeypatch.setattr(RF, "_get_github_client", lambda: gh)
    monkeypatch.setattr(RF, "_open_prd_pr_blocker",
                        lambda repo, **kw: gh.guarded.append(kw.get("branches")) or None)
    monkeypatch.setattr(RF, "_merge_pr", lambda pr, method: (True, "merged"))
    return gh


DF_PAYLOAD = {
    "release_name": "DF Release 7", "start_date": "2026-09-20 18:00:00",
    "end_date": "2026-09-20 20:00:00", "change_initiator": "dev@example.com",
    "change_summary": "df weekly", "artefact": ["df-orders:2.0.0"], "df_images": ["df-orders"],
    "deployment_repo": "o/df-release", "release_kind": "df",
}


def test_a_df_release_is_built_on_and_raised_to_release_uat(wired):
    prep = RF.prepare_release_fileset(dict(DF_PAYLOAD))
    assert prep["ok"], prep
    assert prep["kind"] == "df" and prep["landing_branch"] == "RELEASE_UAT"
    assert prep["preview"]["lands_on"] == "RELEASE_UAT"
    assert wired.guarded == [["RELEASE_UAT", "RELEASE_PRD"]], "guarded on the DF chain, not SIT/UAT/PRD"

    out = RF.apply_release_fileset(prep)
    assert out["ok"], out
    [(repo, pull)] = wired.pulls
    assert repo == "o/df-release" and pull["base"] == "RELEASE_UAT"
    assert "promote the DF release to prd" in out["note"]


def test_confirming_regenerates_in_the_df_repo_not_the_care_one(wired):
    """Found while building this: regeneration rebuilt from release_details.json
    alone — which has no repo — so a DF release re-ran against the CARE repo."""
    prep = RF.prepare_release_fileset(dict(DF_PAYLOAD))
    wired.repos_asked.clear()
    assert RF.apply_release_fileset(prep)["ok"]
    assert set(wired.repos_asked) == {"o/df-release"}, wired.repos_asked


def test_without_an_explicit_kind_a_df_only_release_is_still_df(wired):
    payload = {k: v for k, v in DF_PAYLOAD.items() if k != "release_kind"}
    assert RF.prepare_release_fileset(payload)["landing_branch"] == "RELEASE_UAT"


# --- promotion ----------------------------------------------------------------
def _promote_repo():
    from tests.test_release_fileset import _FakeGhRepo

    files = ["artefact-provider/artefact.json"]
    repo = _FakeGhRepo({
        "release/df-release-7": {files[0]: '{"artefact": ["df-orders:2.0.0"]}'},
        "RELEASE_UAT": {files[0]: '{"artefact": ["df-orders:2.0.0"]}'},
        "RELEASE_PRD": {files[0]: '{"artefact": []}'},
    })
    repo.create_pull(title="DF Release 7", body=f"{RF._FILES_MARKER} {json.dumps(files)}",
                     head="release/df-release-7", base="RELEASE_UAT")
    return repo, files


def test_the_df_release_promotes_to_release_prd(monkeypatch):
    repo, files = _promote_repo()
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    out = json.loads(RF.promote_release.invoke({"target": "prd", "kind": "df"}))
    assert out["ok"] and out["target"] == "RELEASE_PRD", out
    assert repo.files["RELEASE_PRD"][files[0]] == '{"artefact": ["df-orders:2.0.0"]}'


def test_the_df_repo_is_recognised_without_being_told(monkeypatch):
    repo, _ = _promote_repo()
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    out = json.loads(RF.promote_release.invoke({"target": "prd", "deployment_repo": "o/df-release"}))
    assert out["ok"] and out["target"] == "RELEASE_PRD", out


def test_promoting_a_df_release_to_uat_explains_it_already_lands_there(monkeypatch):
    repo, _ = _promote_repo()
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    out = json.loads(RF.promote_release.invoke({"target": "uat", "kind": "df"}))
    assert not out["ok"] and "lands on RELEASE_UAT directly" in out["error"] and "prd" in out["error"]


def test_a_shared_repo_promotes_whichever_release_is_newest(monkeypatch):
    """One repo, both kinds: the newest release PR (on either landing branch) decides."""
    repo, files = _promote_repo()
    repo.files.update({"SIT": {files[0]: "{}"}, "UAT": {files[0]: "{}"},
                       "release/care-9": {files[0]: '{"artefact": ["svc:9"]}'}})
    repo.create_pull(title="CARE 9", body=f"{RF._FILES_MARKER} {json.dumps(files)}",
                     head="release/care-9", base="SIT")
    monkeypatch.setattr(C.settings, "df_release_repo", "", raising=False)
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    out = json.loads(RF.promote_release.invoke({"target": "uat"}))
    assert out["ok"] and out["target"] == "UAT", "CARE 9 is newer, so the CARE chain applies"


# --- status + guard -------------------------------------------------------------
def test_the_df_status_watches_the_df_chain(monkeypatch):
    from release_agent.tools import release_window as W

    seen = {}
    monkeypatch.setattr(W, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: object()))
    monkeypatch.setattr(W, "_charts", lambda repo, env: {})
    monkeypatch.setattr(W, "_open_prd_pr_blocker",
                        lambda repo, branches=None: seen.update(branches=branches))
    W.get_release_status("o/df-release", kind="df")
    assert seen["branches"] == ["RELEASE_UAT", "RELEASE_PRD"]


# --- the chat's tools ----------------------------------------------------------
def test_the_chat_has_a_df_promotion_tool_that_cannot_hit_the_care_release(monkeypatch):
    """Found live: 'promote the DF release to uat' reached the CARE release — the
    chat's promote tool had no way to say which release. Now each has its own."""
    from adk_release_agent import tools as T

    calls = []
    monkeypatch.setattr(T, "_invoke_tool", lambda name, args=None: calls.append((name, args)) or {"ok": True})
    T.promote_df_release("prd")
    T.promote_release("uat")
    assert calls[0] == ("promote_release", {"target": "prd", "release_branch": "",
                                            "deployment_repo": "", "kind": "df"})
    assert calls[1][1]["kind"] == "care"
    assert T.promote_df_release in T.ADK_CHAT_TOOLS


def test_promoting_the_df_release_to_prd_asks_first():
    from adk_release_agent import agent as A

    tools = {getattr(t, "name", getattr(t, "__name__", "")): t for t in A._chat_additional_tools()}
    df = tools["promote_df_release"]
    confirm = getattr(df, "_require_confirmation", None) or getattr(df, "require_confirmation", None)
    assert callable(confirm) and confirm(target="prd") and not confirm(target="uat")


def test_a_df_promotion_logs_its_df_images_not_a_stale_helm_workflow(monkeypatch):
    """Found live: the DF promotion logged 'orders-api deployed to PRD' — read from
    a CARE helm workflow the DF release never wrote."""
    from release_agent.tools import release_queue as RQ

    repo, files = _promote_repo()
    repo.files["release/df-release-7"][".github/workflows/deploy_with_sdlc_governance_prd.yaml"] = (
        '"helm_chart_name": "orders-api",\n"helm_chart_version": "1.0.0",\n')      # stale, not in the release
    written = []
    monkeypatch.setattr(RQ, "record_deployment", lambda **kw: written.append(kw))
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
    assert json.loads(RF.promote_release.invoke({"target": "prd", "kind": "df"}))["ok"]
    [event] = written
    assert event["artifacts"] == [{"name": "df-orders", "tag": "2.0.0"}] and event["environment"] == "prd"


def test_a_care_promotion_only_trusts_a_workflow_the_release_wrote(monkeypatch):
    from release_agent.tools import release_queue as RQ

    wf = ".github/workflows/deploy_with_sdlc_governance_uat.yaml"
    body = '"helm_chart_name": "svc-a",\n"helm_chart_version": "2.0.0",\n'
    from tests.test_release_fileset import _FakeGhRepo

    for in_release, expected in ((True, [{"name": "svc-a", "tag": "2.0.0"}]), (False, None)):
        files = ["artefact-provider/artefact.json"] + ([wf] if in_release else [])
        repo = _FakeGhRepo({"release/r": {files[0]: '{"artefact": ["svc-a:2.0.0"]}', wf: body},
                            "SIT": {}, "UAT": {files[0]: "{}"}})
        repo.create_pull(title="R", body=f"{RF._FILES_MARKER} {json.dumps(files)}", head="release/r", base="SIT")
        written = []
        monkeypatch.setattr(RQ, "record_deployment", lambda **kw: written.append(kw))
        monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: repo))
        assert json.loads(RF.promote_release.invoke({"target": "uat", "kind": "care"}))["ok"]
        assert (written[0]["artifacts"] if written else None) == expected


def test_a_df_release_held_for_review_still_moves_its_charts_to_release_history(wired, monkeypatch):
    """Asked for: DF behaves like CARE mono mode — raising the release PR moves
    its charts from the queue to Release history, merged by the portal or held
    for review; a release that does not go through is put back from there."""
    from release_agent.tools import release_queue as RQ

    released = []
    monkeypatch.setattr(RQ, "mark_released",
                        lambda name, pr, artifacts, deployment_repo="":
                        released.append((name, pr, deployment_repo)) or {"ok": True})
    monkeypatch.setattr(RF, "_merge_pr", lambda pr, method: (False, "awaiting review/checks (blocked)"))
    out = RF.apply_release_fileset(RF.prepare_release_fileset(dict(DF_PAYLOAD)))
    assert out["ok"] and "opened against RELEASE_UAT" in out["note"]
    assert released == [("DF Release 7", 5, "o/df-release")], "released on raise, not on merge"
    assert "moved from the release queue to Release history" in out["note"]


def test_a_queue_outage_never_fails_a_df_release_and_says_so(wired, monkeypatch):
    from release_agent.tools import release_queue as RQ

    monkeypatch.setattr(RQ, "mark_released", lambda *a, **k: {"ok": False, "error": "BigQuery down"})
    out = RF.apply_release_fileset(RF.prepare_release_fileset(dict(DF_PAYLOAD)))
    assert out["ok"] and "remove them by hand" in out["note"]
