"""Who asked for this: every commit and PR the portal makes names the VERIFIED
caller — a Requested-by trailer, and the commit author — so GitHub's history
answers "on whose behalf" even when the server token did the writing."""
from types import SimpleNamespace

from release_agent import identity
from release_agent.tools import attribution as A
from release_agent.tools import dataflow as D
from release_agent.tools import promotion as P
from release_agent.tools._common import _upsert_json_file
from tests.fakes import FakeRepo

ALICE = identity.Caller(email="alice@example.com", name="Alice Example")


def test_nothing_is_written_without_a_verified_caller():
    assert A.trailer() == "" and A.with_trailer("chore: x") == "chore: x"
    assert A.author() is None and A.author_kwargs() == {} and A.merge_kwargs() == {}


def test_the_trailer_is_the_last_paragraph_and_the_author_is_the_person():
    with identity.activate(ALICE):
        assert A.with_trailer("chore: x\n") == "chore: x\n\nRequested-by: alice@example.com\n"
        assert A.author() == ("Alice Example", "alice@example.com")
        assert A.author_kwargs()["author"]._identity == {"name": "Alice Example", "email": "alice@example.com"}
        assert A.merge_kwargs() == {"commit_message": "Requested-by: alice@example.com"}
    with identity.activate(identity.Caller(email="bob@example.com")):
        assert A.author() == ("bob", "bob@example.com"), "no display name: the local part"


def test_the_release_files_marker_still_parses_after_the_trailer():
    from release_agent.tools import release_fileset as RF

    body = 'Release X\n\nRELEASE-FILES-JSON: ["a.json", "b.yaml"]\n'
    with identity.activate(ALICE):
        pr = SimpleNamespace(body=A.with_trailer(body))
    assert RF._release_files_from_pr(pr) == ["a.json", "b.yaml"]
    assert pr.body.endswith("Requested-by: alice@example.com\n")


def test_a_deploy_pr_carries_the_person_in_its_commit_and_its_body():
    repo = FakeRepo({"UAT": {}})
    with identity.activate(ALICE):
        _upsert_json_file(repo, "UAT", "uat/deployment.json", {"include": []})
        pr, _work = P._raise_hop_pr(repo, "UAT", [], {"change-request.json": {"summary": "x"}}, "Deploy payments-api:1.2.3 to UAT")
    assert repo.writes[0]["msg"].endswith("\n\nRequested-by: alice@example.com\n")
    assert repo.writes[0]["author"]._identity["email"] == "alice@example.com", "git author = the person, committer = the token"
    assert pr.body.endswith("Requested-by: alice@example.com\n")
    assert pr.title == "Deploy payments-api:1.2.3 to UAT (→ UAT)", "the title stays clean"


def test_the_prod_merge_commit_says_who_asked():
    repo = FakeRepo({"PRD": {}, "work": {"x": "1"}})
    pr = repo.create_pull("t", "b", "work", "PRD")
    with identity.activate(ALICE):
        merged, _ = P._merge_pr(pr, "merge")
    assert merged and pr.merge_message == "Requested-by: alice@example.com"
    anon = repo.create_pull("t", "b", "work", "PRD")
    P._merge_pr(anon, "merge")
    assert anon.merge_message is None, "identity off: GitHub's own default message, nothing invented"


# --- DF: the {requested_by} placeholder -------------------------------------

def test_requested_by_carries_the_verified_caller_never_a_typed_name(monkeypatch):
    monkeypatch.setattr(D.settings, "df_dispatch_inputs",
                        '{"module": "{image}", "binary_version": "{tag}", "requested_by": "{requested_by}"}', raising=False)
    with identity.activate(ALICE):
        assert D._dispatch_inputs("payments-api", "0.0.1", "uat") == {
            "module": "payments-api", "binary_version": "0.0.1", "requested_by": "alice@example.com"}
    assert D._dispatch_inputs("payments-api", "0.0.1", "uat")["requested_by"] == "", "identity off: empty"


def test_the_default_template_sends_nothing_new(monkeypatch):
    """Opt-in: teams that never mapped {requested_by} dispatch exactly what they did before."""
    monkeypatch.setattr(D.settings, "df_dispatch_inputs", "", raising=False)
    with identity.activate(ALICE):
        assert set(D._dispatch_inputs("payments-api", "0.0.1", "uat")) == {"image", "tag", "environment"}


def test_an_undeclared_requested_by_input_is_dropped_but_a_mismapped_image_is_not(monkeypatch):
    monkeypatch.setattr(D.settings, "df_dispatch_inputs",
                        '{"module": "{image}", "binary_version": "{tag}", "requested_by": "{requested_by}"}', raising=False)
    declared = {"module": {"type": "choice"}, "binary_version": {"type": "string"}}   # pipeline not updated yet
    inputs = {"module": "payments-api", "binary_version": "0.0.1", "requested_by": "alice@example.com"}
    kept, dropped = D._without_undeclared_requester(inputs, declared)
    assert dropped == ["requested_by"] and "requested_by" not in kept and kept["module"] == "payments-api"
    kept, dropped = D._without_undeclared_requester(inputs, {**declared, "requested_by": {"type": "string"}})
    assert dropped == [] and kept == inputs, "declared: sent"
    assert D._without_undeclared_requester(inputs, {}) == (inputs, []), "workflow unreadable: drop nothing"
    wrong = {"modul": "payments-api", "requested_by": "alice@example.com"}
    kept, dropped = D._without_undeclared_requester(wrong, declared)
    assert "modul" in kept and dropped == ["requested_by"], "only the attribution key is ever dropped"
