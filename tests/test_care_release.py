"""CARE release in mono mode: ONE committed file, and a PR a person merges.

The portal edits CARE_RELEASE_FILE in CARE_RELEASE_REPO on a release branch
and raises a PR against the base branch — it never merges it. The CONFIRM
token flow, DF releases and CARE in the default fileset mode are unchanged.
"""
import json
from types import SimpleNamespace

import pytest

from adk_release_agent import deploy as D
from adk_release_agent import deploy_workflow as W
from release_agent import identity
from release_agent.tools import care_release as CR
from release_agent.tools import git_snapshot as G
from release_agent.tools import json_splice
from release_agent.tools import pr_reconcile
from release_agent.tools import release_fileset as RF
from tests.fakes import FakeRepo

MONO = "example-org/mono-repo"
FILE = ".github/release/release_details.json"
ART = "https://artifactory.example.com/docker/example-ds"
BRANCH = "release/team-care-release-2026-10-01"
ALICE = identity.Caller(email="alice@example.com", name="Alice Example")

COMMITTED = f"""{{
  "release_name": "<TEAM> CARE Release - 2026.09.24",
  "start_date": "2026-09-24 10:00:00",
  "end_date": "2026-09-25 23:00:00",
  "change_initiator": "someone@example.com",
  "change_summary": "<TEAM> CARE Release - 2026.09.24",
  "change_description": "Swagger fix and memory heap improvements.",
  "change_reason": "Fixes the swagger page.",
  "associated_risk": "Low risk. Changes include swagger fix and memory heap improvements.",
  "consequence": "The change introduces minor changes and bug fixes across <TEAM> services.",
  "user_service_impact": "No user impact is expected. The change improves memory management and API reliability across <TEAM> services.",
  "prl1_only" : [


  ],
  "artefact": [
    "{ART}/svc-a:5.0.463",
    "{ART}/svc-b:5.0.458"
  ]
}}
"""


def _payload(**over):
    base = {
        "release_name": "<TEAM> CARE Release - 2026.10.01",
        "start_date": "2026-10-01 10:00:00",
        "end_date": "2026-10-02 23:00:00",
        "change_initiator": "someone@example.com",
        "change_summary": "<TEAM> CARE Release - 2026.10.01",
        "change_description": "Swagger fix and memory heap improvements.",
        "change_reason": "Tunes the heap — fewer restarts.",
        "associated_risk": "Low risk. Changes include swagger fix and memory heap improvements.",
        "consequence": "The change introduces minor changes and bug fixes across <TEAM> services.",
        "user_service_impact": "No user impact is expected. The change improves memory "
                               "management and API reliability across <TEAM> services.",
        "prl1_only": [],
        "df_images": [],
        "artefact": [f"{ART}/svc-a:5.0.470", f"{ART}/svc-c:1.2.3"],
        "deployment_repo": MONO,
        "release_kind": "care",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def mono(monkeypatch):
    for k, v in {"care_release_mode": "mono", "care_release_repo": MONO,
                 "care_release_base_branch": "main", "care_release_file": FILE,
                 "care_release_branch_prefix": "release/", "artifactory_base_url": ""}.items():
        monkeypatch.setattr(CR.settings, k, v, raising=False)


@pytest.fixture
def repo(monkeypatch):
    r = FakeRepo({"main": {FILE: COMMITTED}})
    r.strict_sha = True            # a commit on a stale blob sha fails, as on GitHub

    def get_repo(full):
        assert full == MONO, f"mono mode must only ever touch {MONO}, asked for {full}"
        return r

    monkeypatch.setattr(CR, "_get_github_client", lambda: SimpleNamespace(get_repo=get_repo))
    monkeypatch.setattr(G, "clone_branch", lambda *a, **k: pytest.fail("mono mode never clones"))
    return r


@pytest.fixture
def pending(monkeypatch):
    calls = []
    monkeypatch.setattr(pr_reconcile, "record_pending",
                        lambda *a, **kw: calls.append((a, kw)) or {"ok": True})
    return calls


def _prepare(**over):
    return RF.prepare_release_fileset(_payload(**over))


def _raise_pr(**over):
    prep = _prepare(**over)
    assert prep["ok"], prep
    return prep, RF.apply_release_fileset(prep)


# --- preview ------------------------------------------------------------------

def test_the_preview_is_the_exact_file_diff_and_writes_nothing(repo):
    prep = _prepare()
    assert prep["ok"] and prep["mode"] == "mono" and prep["kind"] == "care"
    preview = prep["preview"]
    assert (preview["repo"], preview["base"], preview["file"], preview["branch"]) == (
        MONO, "main", FILE, BRANCH)
    assert preview["environments"]["prd"] == ["svc-a", "svc-c"]
    changed = [line for line in preview["file_diff"].splitlines()
               if line[:1] in "+-" and line[:3] not in ("+++", "---")]
    assert changed == [
        '-  "release_name": "<TEAM> CARE Release - 2026.09.24",',
        '-  "start_date": "2026-09-24 10:00:00",',
        '-  "end_date": "2026-09-25 23:00:00",',
        '+  "release_name": "<TEAM> CARE Release - 2026.10.01",',
        '+  "start_date": "2026-10-01 10:00:00",',
        '+  "end_date": "2026-10-02 23:00:00",',
        '-  "change_summary": "<TEAM> CARE Release - 2026.09.24",',
        '+  "change_summary": "<TEAM> CARE Release - 2026.10.01",',
        '-  "change_reason": "Fixes the swagger page.",',
        '+  "change_reason": "Tunes the heap — fewer restarts.",',
        f'-    "{ART}/svc-a:5.0.463",',
        f'-    "{ART}/svc-b:5.0.458"',
        f'+    "{ART}/svc-a:5.0.470",',
        f'+    "{ART}/svc-c:1.2.3"',
    ], "only what this release changes — no df_images, prl1_only's blank lines untouched"
    assert preview["file_diff"].startswith(f"--- a/{FILE}\n+++ b/{FILE}\n")
    assert repo.writes == [] and repo.prs == [] and set(repo.files) == {"main"}
    assert json.loads(json.dumps(prep)) == prep, "plain data: it is persisted in session state"


def test_no_mono_repo_configured_is_refused(repo, monkeypatch):
    monkeypatch.setattr(CR.settings, "care_release_repo", "")
    out = _prepare()
    assert out["ok"] is False and "CARE_RELEASE_REPO" in out["errors"][0]


def test_a_release_aimed_at_another_repo_is_refused(repo):
    out = _prepare(deployment_repo="example-org/deploy-repo")
    assert out["ok"] is False
    assert MONO in out["errors"][0] and "example-org/deploy-repo" in out["errors"][0]
    assert _prepare(deployment_repo="")["ok"], "empty means the configured mono repo"


def test_dataflow_images_are_refused(repo):
    out = _prepare(df_images=["svc-c"])
    assert out["ok"] is False and "cannot carry Dataflow images" in out["errors"][0]


def test_a_release_main_already_carries_is_refused(repo):
    details, _ = RF.validate_release(_payload())
    repo.files["main"][FILE] = json_splice.splice_top_level(COMMITTED, CR.file_doc(details))
    out = _prepare()
    assert out == {"ok": False, "errors": [
        f"main already carries exactly this release — {FILE} would not change."]}


def test_the_guard_ignores_other_prs_into_main_and_blocks_a_release_pr(repo):
    repo.files["feature/login"] = dict(repo.files["main"])
    repo.create_pull("Login page", "", "feature/login", "main")
    repo.create_pull("Unrelated release", "", "release/other-team", "develop")
    assert _prepare()["ok"], "a busy mono repo always has other PRs open into main"

    blocker = repo.create_pull("Last week", "", "release/team-care-release-2026-09-24", "main")
    out = _prepare()
    assert out["ok"] is False
    assert out["errors"] == [
        f"A CARE release is already awaiting review: PR #{blocker.number} ({blocker.html_url}). "
        "One release at a time — merge or close it first."]


# --- apply --------------------------------------------------------------------

def test_apply_refuses_when_the_file_on_main_moved(repo, pending):
    prep = _prepare()
    repo.files["main"][FILE] = COMMITTED.replace("Fixes the swagger page.", "Edited on main.")
    out = RF.apply_release_fileset(prep)
    assert out == {"ok": False, "error": "The release file on main changed since the preview — "
                                         "start the release again. Nothing was pushed."}
    assert repo.writes == [] and repo.prs == [] and set(repo.files) == {"main"} and pending == []


def test_apply_raises_the_pr_commits_one_file_and_never_merges(repo, pending):
    with identity.activate(ALICE):
        prep, out = _raise_pr()
    assert out["ok"] and out["action"] == "release_pr_opened" and out["merged"] is False
    [pr] = repo.prs
    assert out["pr_number"] == pr.number and out["pr_url"] == pr.html_url
    assert f"[PR #{pr.number}]({pr.html_url})" in out["note"] and "never merges" in out["note"]
    assert (pr.head.ref, pr.base.ref, pr.title, pr.state) == (
        BRANCH, "main", "<TEAM> CARE Release - 2026.10.01", "open")
    assert not hasattr(pr, "merge_message"), "the portal never merges the release PR"
    assert repo.files["main"][FILE] == COMMITTED, "main is untouched until a person merges"

    [write] = repo.writes
    assert (write["path"], write["branch"]) == (FILE, BRANCH), "one file, on the release branch"
    assert write["msg"] == "<TEAM> CARE Release - 2026.10.01\n\nRequested-by: alice@example.com\n"
    assert write["author"]._identity == {"name": "Alice Example", "email": "alice@example.com"}
    assert set(repo.files[BRANCH]) == {FILE}
    assert CR._content_hash(repo.files[BRANCH][FILE]) == prep["content_hash"], \
        "the branch carries exactly the previewed text"
    assert '"prl1_only" : [\n\n\n  ],' in repo.files[BRANCH][FILE]
    assert "df_images" not in repo.files[BRANCH][FILE]

    body = pr.body
    assert body.startswith("Release **<TEAM> CARE Release - 2026.10.01** — updates "
                           f"`{FILE}`.\n\nWindow: 2026-10-01 10:00:00 → 2026-10-02 23:00:00\n"
                           "Raised by: alice@example.com\n\n"
                           f"- {ART}/svc-a:5.0.470\n- {ART}/svc-c:1.2.3\n\n"
                           "Review and merge this PR; on merge the repository's workflow updates "
                           "the deployment repo. The portal never merges it.\n")
    assert body.endswith("\n\nRequested-by: alice@example.com\n")


def test_without_identity_the_pr_names_the_typed_email_as_unverified(repo, pending):
    _prep, out = _raise_pr(change_initiator="bob@example.com")
    assert out["ok"]
    [pr] = repo.prs
    assert "Raised by: bob@example.com (unverified — typed on the form)\n" in pr.body
    assert "Requested-by" not in pr.body, "no trailer for a claim"
    [write] = repo.writes
    assert write["author"] is None, "the token's owner authors it — never a typed name"
    assert _prepare(release_name="Other")["ok"] is False, "and the guard now sees this PR"


def test_the_preview_says_who_will_raise_it(repo):
    assert _prepare()["preview"]["raised_by"] == "someone@example.com (unverified, typed on the form)"
    with identity.activate(ALICE):
        assert _prepare()["preview"]["raised_by"] == "alice@example.com"


def test_applying_again_reuses_the_open_pr_without_a_second_commit(repo, pending):
    prep, first = _raise_pr()
    again = RF.apply_release_fileset(prep)
    assert again["ok"] and again["action"] == "release_pr_exists"
    assert again["pr_number"] == first["pr_number"] and again["committed"] is False
    assert len(repo.prs) == 1 and len(repo.writes) == 1


def test_a_second_release_confirmed_while_the_first_is_open_is_refused(repo, pending):
    """Both previews pass the guard — the first PR did not exist yet. The guard
    runs again at apply, so the second CONFIRM pushes nothing, whatever it is
    named (the name is a date: two people can raise on the same day)."""
    first = _prepare()
    same_name = _prepare(artefact=[f"{ART}/svc-d:2.0.0"])
    other_name = _prepare(release_name="<TEAM> CARE Release - 2026.10.02",
                          change_summary="<TEAM> CARE Release - 2026.10.02")
    assert first["ok"] and same_name["ok"] and other_name["ok"]
    out = RF.apply_release_fileset(first)
    [pr] = repo.prs
    approved = repo.files[BRANCH][FILE]

    for second in (same_name, other_name):
        refused = RF.apply_release_fileset(second)
        assert refused == {"ok": False, "error": (
            f"A CARE release is already awaiting review: PR #{pr.number} ({pr.html_url}). "
            "One release at a time — merge or close it first. Nothing was pushed.")}
    assert repo.prs == [pr] and len(repo.writes) == 1 and set(repo.files) == {"main", BRANCH}
    assert repo.files[BRANCH][FILE] == approved, "the first PR still carries what its person approved"
    [(args, _kw)] = pending
    assert args[1] == first["artifacts"] and args[3] == out["pr_number"]


def test_a_same_named_release_racing_past_the_guard_never_writes_over_the_open_pr(
        repo, pending, monkeypatch):
    """The second apply's guard ran just before the first PR opened: taking the
    branch name is where it then finds that PR, and it only ever reuses a PR
    that already carries exactly the approved file."""
    first, second = _prepare(), _prepare(artefact=[f"{ART}/svc-d:2.0.0"])
    RF.apply_release_fileset(first)
    [pr] = repo.prs
    approved = repo.files[BRANCH][FILE]
    monkeypatch.setattr(CR, "_open_prd_pr_blocker", lambda *a, **k: None)

    out = RF.apply_release_fileset(second)
    assert out == {"ok": False, "error": (
        f"PR #{pr.number} ({pr.html_url}) for this release name is open with different content — "
        "nothing was pushed. One release at a time: merge or close it first.")}
    assert repo.files[BRANCH][FILE] == approved and len(repo.writes) == 1 and repo.prs == [pr]
    assert len(pending) == 1, "the second release's charts are not recorded against the first PR"


def test_a_retry_never_writes_over_an_edit_made_on_the_open_pr(repo, pending):
    prep, first = _raise_pr()
    edited = repo.files[BRANCH][FILE].replace("Tunes the heap", "Reviewer's wording")
    repo.files[BRANCH][FILE] = edited
    out = RF.apply_release_fileset(prep)
    assert out["ok"] is False and f"PR #{first['pr_number']}" in out["error"]
    assert "Nothing was pushed." in out["error"]
    assert repo.files[BRANCH][FILE] == edited and len(repo.writes) == 1


def test_a_leftover_branch_without_a_pr_is_never_written_to(repo, pending):
    repo.files[BRANCH] = {FILE: "{}", "stale.txt": "left behind"}
    _prep, out = _raise_pr()
    assert out["ok"] and out["branch"] == BRANCH + "-2"
    assert repo.prs[0].head.ref == BRANCH + "-2"
    assert repo.files[BRANCH] == {FILE: "{}", "stale.txt": "left behind"}


def test_the_release_is_recorded_as_pending_against_its_pr(repo, pending):
    _prep, out = _raise_pr()
    [(args, kwargs)] = pending
    assert args == ("", [{"name": "svc-a", "tag": "5.0.470"}, {"name": "svc-c", "tag": "1.2.3"}],
                    MONO, out["pr_number"])
    assert kwargs == {"on_merge": "released", "tag": "<TEAM> CARE Release - 2026.10.01"}


def test_a_failed_commit_leaves_no_branch_behind(repo, pending, monkeypatch):
    prep = _prepare()

    def refused(*a, **k):
        raise Exception("403 Resource not accessible by integration")

    monkeypatch.setattr(repo, "update_file", refused)
    out = RF.apply_release_fileset(prep)
    assert out["ok"] is False and "Could not commit" in out["error"] and "403" in out["error"]
    assert set(repo.files) == {"main"} and repo.prs == [] and pending == []


def test_a_commit_that_landed_before_its_error_is_not_a_failure(repo, pending, monkeypatch):
    """urllib3 retries a PUT after a 5xx; if the first one had landed, the retry
    fails on the now-stale sha — the file is there, so carry on."""
    real = repo.update_file

    def landed_then_failed(*a, **k):
        real(*a, **k)
        raise Exception("409 does not match")

    monkeypatch.setattr(repo, "update_file", landed_then_failed)
    _prep, out = _raise_pr()
    assert out["ok"] and out["action"] == "release_pr_opened" and len(repo.prs) == 1


def test_a_pr_that_cannot_be_opened_says_where_the_commit_is(repo, pending, monkeypatch):
    prep = _prepare()
    monkeypatch.setattr(repo, "create_pull",
                        lambda **kw: (_ for _ in ()).throw(Exception("422 Validation Failed")))
    out = RF.apply_release_fileset(prep)
    assert out["ok"] is False and BRANCH in out["error"] and "by hand" in out["error"]
    assert pending == [], "no PR, nothing pending"


def test_apply_never_raises(repo, pending, monkeypatch):
    prep = _prepare()
    monkeypatch.setattr(CR, "_get_github_client", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    out = RF.apply_release_fileset(prep)
    assert out["ok"] is False and "boom" in out["error"] and "Nothing was merged" in out["error"]
    assert RF.apply_release_fileset({"mode": "mono"})["ok"] is False


# --- the CONFIRM flow and the chat preview ------------------------------------

def test_the_confirm_flow_raises_the_pr_from_the_session_copy(repo, pending):
    prep = D.prepare_deploy_preview(message=json.dumps(_payload()))
    assert prep["ok"], prep
    assert prep["heading"] == (f"Raise CARE release PR <TEAM> CARE Release - 2026.10.01 → "
                               f"{MONO}@main")
    D._PENDING_PREVIEWS.clear()                        # another replica
    out = D.apply_confirmed_deploy(prep["token"], pending=prep["pending"])
    assert out["ok"] and out["action"] == "release_pr_opened"
    assert out["confirmed_token"] == prep["token"] and len(repo.prs) == 1
    assert D.apply_confirmed_deploy(prep["token"])["status"] == "not_confirmed", "single use"


def test_the_chat_preview_shows_the_diff_after_the_json(repo):
    prep = _prepare()
    stored = prep["preview"]
    text = W._preview_text(stored, "CONFIRM-ABC123", "prod", "R", heading="Raise it")
    json_part = text.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert "file_diff" not in json.loads(json_part)
    assert "\n\n```diff\n--- a/" in text and text.index("```diff") > text.index("```json")
    assert '+  "change_reason": "Tunes the heap — fewer restarts.",' in text
    assert "file_diff" in stored, "the pending preview itself is not changed"
    assert text.endswith("Reply `CONFIRM-ABC123` to confirm.")


def test_prose_cannot_close_the_diff_block_early():
    diff = '+  "change_reason": "see ```` here",\n'
    block = W._diff_block(diff)
    assert block.startswith("\n\n`````diff\n") and block.endswith("\n`````")
    assert W._diff_block("+ plain\n") == "\n\n```diff\n+ plain\n```"


# --- the other paths stay as they were ----------------------------------------

def _stub_fileset_path(monkeypatch):
    """The file-set path up to the clone — reaching it is the proof."""
    cloned = []
    monkeypatch.setattr(RF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda full: object()))
    monkeypatch.setattr(RF, "_resolve_github_token", lambda: "")
    monkeypatch.setattr(RF, "_open_prd_pr_blocker", lambda repo, **kw: None)

    def clone(url, branch, dest, token):
        cloned.append(branch)
        raise RuntimeError("clone reached")

    monkeypatch.setattr(G, "clone_branch", clone)
    monkeypatch.setattr(CR, "prepare", lambda *a, **k: pytest.fail("not a mono release"))
    return cloned


def test_a_df_release_is_unaffected_by_mono_mode(monkeypatch):
    cloned = _stub_fileset_path(monkeypatch)
    monkeypatch.setattr(RF.settings, "df_release_branches", "uat:RELEASE_UAT,prd:RELEASE_PRD",
                        raising=False)
    out = RF.prepare_release_fileset(_payload(
        release_kind="df", deployment_repo="example-org/df-release",
        df_images=["svc-a", "svc-c"]))
    assert out == {"ok": False, "errors": ["clone reached"]} and cloned == ["RELEASE_UAT"]


def test_fileset_mode_keeps_care_on_the_fileset_path(monkeypatch):
    cloned = _stub_fileset_path(monkeypatch)
    monkeypatch.setattr(RF.settings, "care_release_mode", "fileset")
    monkeypatch.setattr(RF.settings, "sit_branch", "SIT", raising=False)
    out = RF.prepare_release_fileset(_payload(deployment_repo="example-org/deploy-repo"))
    assert out == {"ok": False, "errors": ["clone reached"]} and cloned == ["SIT"]
