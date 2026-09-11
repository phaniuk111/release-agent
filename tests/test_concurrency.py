"""Many developers at once: one person's slow tool must not stall everyone's
chat, concurrent Dataflow dispatches must never swap run links, and two deploys
racing onto the same branch must both land rather than leave a conflicted PR."""
import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from github import GithubException

from adk_release_agent import agent as agent_module
from adk_release_agent import tools as T
from adk_release_agent.safety import ScopeGuardPlugin
from release_agent.config import settings
from release_agent.session_creds import SessionCredentials, active_credentials, get_store
from release_agent.tools import dataflow as DF
from release_agent.tools import promotion as P

SLOW = 0.4


async def _ticks_while(awaitable, every=0.02):
    """Run ``awaitable`` beside a ticker; how often the loop got to tick."""
    ticks = 0
    done = False

    async def tick():
        nonlocal ticks
        while not done:
            ticks += 1
            await asyncio.sleep(every)

    ticker = asyncio.create_task(tick())
    try:
        result = await awaitable
    finally:
        done = True
        await ticker
    return result, ticks


# --- 1. blocking work never runs on the shared event loop ----------------------

def test_every_chat_tool_keeps_its_declaration_when_moved_off_the_loop():
    from google.adk.tools import FunctionTool

    for fn in T.ADK_CHAT_TOOLS:
        moved = T.off_event_loop(fn)
        assert moved.__name__ == fn.__name__
        assert FunctionTool(moved)._get_declaration() == FunctionTool(fn)._get_declaration(), fn.__name__


@pytest.mark.parametrize("confirm", [True, False])
def test_every_chat_tool_the_agent_gets_is_async(monkeypatch, confirm):
    import inspect

    monkeypatch.setattr(settings, "adk_confirm_prod_ops", confirm)
    tools = agent_module._chat_additional_tools()
    assert len(tools) == len(T.ADK_CHAT_TOOLS)
    for t in tools:
        assert inspect.iscoroutinefunction(getattr(t, "func", t)), getattr(t, "name", t)


def test_a_slow_tool_does_not_stall_other_chats():
    from google.adk.tools import FunctionTool

    def slow(label: str = "") -> dict:
        """Waits on GitHub."""
        time.sleep(SLOW)
        return {"label": label}

    tool = FunctionTool(T.off_event_loop(slow))

    async def two_people():
        return await asyncio.gather(*(tool.run_async(args={"label": n}, tool_context=MagicMock())
                                      for n in ("a", "b")))

    t0 = time.monotonic()
    results, ticks = asyncio.run(_ticks_while(two_people()))
    assert [r["label"] for r in results] == ["a", "b"]
    assert time.monotonic() - t0 < SLOW * 1.75, "the two calls ran side by side"
    assert ticks >= 10, "the loop kept serving everyone else meanwhile"


def test_the_connected_pat_reaches_the_worker_thread():
    store = get_store()
    store.set("thread-x", SessionCredentials(pat_token="ghp_" + "x" * 36))

    def whose_token() -> dict:
        creds = active_credentials()
        return {"token": creds.pat_token if creds else None}

    async def call():
        with store.activate("thread-x"):
            return await T.off_event_loop(whose_token)()

    try:
        assert asyncio.run(call())["token"] == "ghp_" + "x" * 36
    finally:
        store.clear("thread-x")


def test_the_scope_classifier_does_not_block_the_loop(monkeypatch):
    asked = []

    def slow_classifier(text):
        asked.append(text)
        time.sleep(SLOW)
        return True

    monkeypatch.setattr(ScopeGuardPlugin, "_classify", staticmethod(slow_classifier))
    request = SimpleNamespace(contents=[SimpleNamespace(
        role="user", parts=[SimpleNamespace(text="write a poem about cats")])])
    plugin = ScopeGuardPlugin(mode="enforce")
    result, ticks = asyncio.run(_ticks_while(
        plugin.before_model_callback(callback_context=None, llm_request=request)))
    assert asked, "stage 1 let it through — this test needs the classifier to run"
    assert result is None
    assert ticks >= 10


# --- 2. a Dataflow dispatch gets ITS run, or no run — never someone else's ----

NOW = dt.datetime(2026, 9, 11, 12, 0, tzinfo=dt.timezone.utc)


def _run(id, title="UAT deploy", actor="dev-a", created=NOW, event="workflow_dispatch"):
    return SimpleNamespace(id=id, display_title=title, actor=SimpleNamespace(login=actor),
                           created_at=created, event=event, status="queued",
                           html_url=f"https://github.example/o/df/actions/runs/{id}")


class _Workflow:
    name = "UAT deploy"

    def __init__(self, runs=(), answer=None, error=None):
        self.runs, self.answer, self.error = list(runs), answer, error
        self.url = "https://api.github.example/repos/o/df/actions/workflows/UAT.yaml"
        self.posted, self.plain_dispatches = [], 0
        self._requester = self

    def get_runs(self):
        return list(self.runs)

    def requestJsonAndCheck(self, verb, url, input=None):
        self.posted.append(input)
        if self.error is not None:
            raise self.error
        return {}, self.answer

    def create_dispatch(self, ref, inputs, throw=False):
        self.plain_dispatches += 1
        return True


def _find(workflow, **kw):
    kw.setdefault("delay", 0)
    return DF._find_dispatched_run(workflow, set(), since=NOW, **kw)


def test_github_naming_the_run_is_used_as_is_without_searching(monkeypatch):
    wf = _Workflow(answer={"workflow_run_id": 777, "html_url": "https://github.example/o/df/actions/runs/777",
                           "run_url": "https://api.github.example/…/777"})
    assert DF._dispatch(wf, "sit", {"module": "a"}) == {
        "id": 777, "url": "https://github.example/o/df/actions/runs/777", "status": "queued"}
    assert wf.posted[0]["return_run_details"] is True


def test_a_server_that_answers_204_falls_back_to_matching():
    wf = _Workflow(answer=None)
    assert DF._dispatch(wf, "sit", {}) is None
    assert wf.plain_dispatches == 0, "dispatched exactly once"


def test_a_server_that_rejects_the_flag_is_dispatched_again_without_it():
    wf = _Workflow(error=GithubException(422, {"message": "Invalid request. \"return_run_details\" is not a permitted key."}))
    assert DF._dispatch(wf, "sit", {}) is None
    assert wf.plain_dispatches == 1


def test_any_other_rejection_raises_and_is_not_retried():
    wf = _Workflow(error=GithubException(422, {"message": "Unexpected inputs provided: [\"image\"]"}))
    with pytest.raises(GithubException):
        DF._dispatch(wf, "sit", {})
    assert wf.plain_dispatches == 0


def test_two_runs_started_together_are_not_guessed_between():
    assert _find(_Workflow([_run(2), _run(1)])) == (None, 2)


def test_a_run_title_naming_the_tag_picks_ours_out_of_several():
    wf = _Workflow([_run(3, "Deploy svc 1.0.1"), _run(2, "Deploy svc 1.0.2"), _run(1, "Deploy svc 9.9")])
    run, _ = _find(wf, match="1.0.2")
    assert run["id"] == 2


def test_a_lone_run_naming_another_deploy_is_never_ours():
    run, competing = _find(_Workflow([_run(5, "Deploy other 3.0.0")]), match="1.0.2", tries=3)
    assert run is None and competing == 0


def test_a_lone_plain_run_is_taken_only_once_it_stays_alone():
    wf = _Workflow([_run(9)])
    assert _find(wf, tries=1) == (None, 0), "one sighting is not enough"
    assert _find(wf, tries=2)[0]["id"] == 9


def test_runs_by_someone_else_or_from_before_the_dispatch_are_ignored():
    wf = _Workflow([_run(8, actor="dev-b"), _run(7, created=NOW - dt.timedelta(minutes=5)), _run(6)])
    assert _find(wf, actor="dev-a", tries=2)[0]["id"] == 6


def test_the_deploy_says_it_will_not_guess(monkeypatch):
    wf = _Workflow(answer=None)
    repo = SimpleNamespace(get_workflow=lambda n: wf, default_branch="sit",
                           html_url="https://github.example/o/df")
    monkeypatch.setattr(DF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda r: repo))
    monkeypatch.setattr(settings, "df_deploy_repo", "o/df")
    monkeypatch.setattr(settings, "df_deploy_workflow", "UAT.yaml")
    monkeypatch.setattr(settings, "df_dispatch_inputs", "")
    monkeypatch.setattr(DF, "_find_dispatched_run", lambda *a, **k: (None, 2))
    out = json.loads(DF.deploy_dataflow.func(environment="uat", image="svc", tag="1.0.2"))
    assert out["run"] is None and out["runs_page"].endswith("/actions/workflows/UAT.yaml")
    assert "won't guess which is yours" in out["note"] and "svc:1.0.2" in out["note"]


def test_the_deploy_links_the_run_github_named(monkeypatch):
    wf = _Workflow(answer={"workflow_run_id": 4242})       # no html_url in the answer
    repo = SimpleNamespace(get_workflow=lambda n: wf, default_branch="sit",
                           html_url="https://github.example/o/df")
    monkeypatch.setattr(DF, "_get_github_client", lambda: SimpleNamespace(get_repo=lambda r: repo))
    monkeypatch.setattr(settings, "df_deploy_repo", "o/df")
    monkeypatch.setattr(settings, "df_deploy_workflow", "UAT.yaml")
    monkeypatch.setattr(settings, "df_dispatch_inputs", "")
    monkeypatch.setattr(DF, "_find_dispatched_run", lambda *a, **k: pytest.fail("searched anyway"))
    out = json.loads(DF.deploy_dataflow.func(environment="uat", image="svc", tag="1.0.2"))
    assert out["run_url"] == "https://github.example/o/df/actions/runs/4242"
    assert "[Run #4242]" in out["note"]


# --- 3. two deploys racing onto one branch both land --------------------------

class _PR:
    def __init__(self, repo, head, base):
        repo.pr_count += 1
        self.repo, self.head, self.base, self.number = repo, head, base, repo.pr_count
        self.html_url = f"https://github.example/o/deploy/pull/{self.number}"
        self.merge_commit_sha, self.state, self.comments = None, "open", []
        # Conflicted when the base moved since this PR's branch was cut.
        dirty = repo.files[base] != repo.cut_from[head]
        self.mergeable, self.mergeable_state = (False, "dirty") if dirty else (True, "clean")

    def update(self):
        pass

    def merge(self, merge_method="squash"):
        self.repo.files[self.base] = dict(self.repo.files[self.head])
        self.state = "merged"

    def create_issue_comment(self, text):
        self.comments.append(text)

    def edit(self, state=None):
        self.state = state or self.state


class _RacingRepo:
    """files[branch][path] = json text. ``race(branch)`` lands another
    person's change on ``branch`` the moment our PR is raised against it."""

    def __init__(self, initial, race=None):
        self.files = {b: {p: json.dumps(d) for p, d in fs.items()} for b, fs in initial.items()}
        self.cut_from, self.prs, self.pr_count, self.race = {}, [], 0, race
        self.deleted = []

    def get_git_ref(self, name):
        branch = name.split("heads/", 1)[1]
        return SimpleNamespace(object=SimpleNamespace(sha=branch),
                               delete=lambda: self.deleted.append(branch))

    def create_git_ref(self, ref, sha):
        work = ref.split("heads/", 1)[1]
        self.files[work] = dict(self.files[sha])
        self.cut_from[work] = dict(self.files[sha])

    def get_contents(self, path, ref=None):
        if path not in self.files.get(ref, {}):
            raise Exception("404")
        return SimpleNamespace(decoded_content=self.files[ref][path].encode(), sha="s")

    def create_file(self, path, msg, content, branch=None):
        self.files[branch][path] = content

    def update_file(self, path, msg, content, sha, branch=None):
        self.create_file(path, msg, content, branch)

    def create_pull(self, title, body, head, base):
        if self.race:
            self.race(self, base)
        pr = _PR(self, head, base)
        self.prs.append(pr)
        return pr

    def include(self, branch, path="uat/deployment.json"):
        return [e["helm_chart_name"] + ":" + e["helm_chart_version"]
                for e in json.loads(self.files[branch][path])["include"]]


def _chart(name, version):
    return {"helm_chart_name": name, "helm_chart_version": version, "helm_chart_dir": "d",
            "helm_values_file_name": "uat/values_uat.yaml", "gke_namespace": "ns"}


def _lands_once(branch, change):
    """Someone else's merge lands on ``branch`` once, just as we raise ours."""
    fired = []

    def race(repo, base):
        if base == branch and not fired:
            fired.append(True)
            doc = json.loads(repo.files[base]["uat/deployment.json"])
            change(doc["include"])
            repo.files[base]["uat/deployment.json"] = json.dumps(doc)
    return race


@pytest.fixture(autouse=True)
def _no_run_polling(monkeypatch):
    monkeypatch.setattr(P, "_find_deploy_run", lambda *a, **k: None)
    monkeypatch.setattr(settings, "sit_branch", "SIT")
    monkeypatch.setattr(settings, "uat_branch", "UAT")
    monkeypatch.setattr(settings, "prd_branch", "PRD")


def test_a_conflict_is_rebuilt_on_the_new_head_and_both_changes_survive():
    initial = {b: {"uat/deployment.json": {"include": [_chart("base", "1.0")]}} for b in ("SIT", "UAT")}
    repo = _RacingRepo(initial, race=_lands_once(
        "UAT", lambda inc: inc.append(_chart("theirs", "2.0"))))

    res = P._promote_targeted(repo, [("uat/deployment.json", P._upsert_each([_chart("ours", "3.0")]))],
                              "deploy ours", branches=("SIT", "UAT"))

    assert res["delivered"] is True
    assert repo.include("UAT") == ["base:1.0", "theirs:2.0", "ours:3.0"], "nobody's change was lost"
    uat = res["prs"][-1]
    first = repo.prs[1]
    assert uat["rebuilt_after_conflict"] == [first.number] and uat["number"] != first.number
    assert first.state == "closed" and "Superseded" in first.comments[0]


def test_a_uat_override_racing_another_lands_as_the_latest():
    """UAT deploys are a full override of uat/deployment.json by design; the
    other person's run already fired on their merge. Ours must still land."""
    initial = {b: {"uat/deployment.json": {"include": [_chart("base", "1.0")]}} for b in ("SIT", "UAT")}
    repo = _RacingRepo(initial, race=_lands_once(
        "UAT", lambda inc: (inc.clear(), inc.append(_chart("theirs", "2.0")))))

    res = P._promote_targeted(repo, [("uat/deployment.json", P._replace_with([_chart("ours", "3.0")]))],
                              "deploy ours", branches=("SIT", "UAT"))

    assert res["delivered"] is True and repo.include("UAT") == ["ours:3.0"]


def test_a_branch_that_keeps_moving_leaves_one_open_pr_and_says_why():
    initial = {b: {"uat/deployment.json": {"include": []}} for b in ("SIT", "UAT")}
    counter = iter(range(100))

    def always(repo, base):
        if base == "UAT":
            doc = json.loads(repo.files[base]["uat/deployment.json"])
            doc["include"].append(_chart(f"other-{next(counter)}", "1"))
            repo.files[base]["uat/deployment.json"] = json.dumps(doc)

    repo = _RacingRepo(initial, race=always)
    res = P._promote_targeted(repo, [("uat/deployment.json", P._upsert_each([_chart("ours", "1")]))],
                              "deploy ours", branches=("SIT", "UAT"))

    assert res["delivered"] is False
    uat = res["prs"][-1]
    assert uat["detail"] == P.MERGE_CONFLICT and len(uat["rebuilt_after_conflict"]) == P._CONFLICT_REBUILDS
    open_prs = [p for p in repo.prs if p.base == "UAT" and p.state == "open"]
    assert [p.number for p in open_prs] == [uat["number"]], "exactly one PR is left, and it is linked"


def test_a_review_refusal_is_never_retried():
    initial = {b: {"uat/deployment.json": {"include": []}} for b in ("SIT", "UAT")}
    repo = _RacingRepo(initial)
    real_create = repo.create_pull

    def protected(title, body, head, base):
        pr = real_create(title, body, head, base)
        if base == "UAT":
            def refuse(merge_method="squash"):
                raise GithubException(405, {"message": "Waiting on code owner review from team-x."})
            pr.merge = refuse
        return pr
    repo.create_pull = protected

    res = P._promote_targeted(repo, [("uat/deployment.json", P._upsert_each([_chart("ours", "1")]))],
                              "deploy ours", branches=("SIT", "UAT"))

    assert [p.base for p in repo.prs] == ["SIT", "UAT"], "one PR per hop"
    assert res["prs"][-1]["detail"].startswith("Waiting on code owner review")


@pytest.mark.parametrize("state,mergeable,error,expected", [
    ("dirty", False, None, P.MERGE_CONFLICT),
    ("behind", False, None, "awaiting review/checks (behind)"),
    ("clean", True, GithubException(405, {"message": "Base branch was modified. Review and try the merge again."}),
     P.MERGE_CONFLICT),
    ("clean", True, GithubException(409, {"message": "Head branch was modified. Review and try the merge again."}),
     P.MERGE_CONFLICT),
    ("blocked", True, GithubException(405, {"message": "Required status check ci is expected."}),
     "Required status check ci is expected."),
])
def test_merge_tells_a_conflict_from_a_review_hold(state, mergeable, error, expected):
    def merge(merge_method="squash"):
        if error:
            raise error
    pr = SimpleNamespace(mergeable=mergeable, mergeable_state=state, update=lambda: None, merge=merge)
    ok, detail = P._merge_pr(pr)
    assert ok is (error is None and mergeable) and detail == ("merged" if ok else expected)
