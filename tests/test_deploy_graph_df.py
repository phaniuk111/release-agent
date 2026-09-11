"""The deploy graph's Dataflow branch, run through the REAL ADK Workflow.

A DF deploy that names Composer DAGs dispatches the run and raises the DAG PR
in the same confirm turn — not after the run finishes. The reply links the run
beside the PR: whether it is green enough to merge on is the person's call.
GitHub is faked at the tool seams.
"""
import asyncio
import json
import warnings

import pytest

from adk_release_agent import deploy, deploy_workflow as W

pytest.importorskip("google.adk")

from google.adk.apps import App, ResumabilityConfig  # noqa: E402
from google.adk.runners import InMemoryRunner  # noqa: E402
from google.genai import types  # noqa: E402

from release_agent.config import settings  # noqa: E402
from release_agent.tools import composer  # noqa: E402

RUN = {"id": 7, "url": "https://github.com/o/df/actions/runs/7", "status": "queued"}
RUNS_PAGE = "https://github.com/o/df/actions/workflows/df-deploy.yml"


def _payload():
    return json.dumps({
        "deployment_type": "dataflow", "environment": "uat", "image": "job-a",
        "tag": "2.0.0", "dag_files": ["job_a_dag.py"], "composer_repo": "o/dags",
    })


@pytest.fixture
def github(monkeypatch):
    """Fake every GitHub seam the DF branch touches; record what happened."""
    rec = {"dispatches": 0, "bumps": [], "run": dict(RUN)}
    deploy._PENDING_PREVIEWS.clear()

    def fake_invoke(name, args):
        assert name == "deploy_dataflow", name
        rec["dispatches"] += 1
        return {"ok": True, "action": "df_workflow_dispatched", "run": rec["run"],
                "run_url": (rec["run"] or {}).get("url", ""), "runs_page": RUNS_PAGE,
                "note": "Dispatched job-a:2.0.0 → uat."}

    def fake_bump(dag_files, version, environment="uat", image="", run_url="", repo=""):
        rec["bumps"].append({"files": dag_files, "version": version, "run_url": run_url, "repo": repo})
        return {"ok": True, "action": "dag_bump_pr_opened", "pr_number": 41,
                "pr_url": "https://github.com/o/dags/pull/41"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)
    monkeypatch.setattr(composer, "apply_dag_bump", fake_bump)
    monkeypatch.setattr(composer, "preview_dag_bump", lambda *a, **k: {
        "repo": "o/dags", "branch": "main", "dir": "uat", "problems": [],
        "changes": [{"file": "uat/job_a_dag.py", "from": ["1.9.0"], "to": "2.0.0", "unchanged": False}]})
    return rec


class _Session:
    def __init__(self):
        app = App(name=W.DEPLOY_APP_NAME, root_agent=W.build_deploy_workflow(),
                  resumability_config=ResumabilityConfig(is_resumable=True))
        self.runner = InMemoryRunner(app=app)
        self.id = None

    async def _turn(self, message):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if self.id is None:
                s = await self.runner.session_service.create_session(
                    app_name=W.DEPLOY_APP_NAME, user_id="u")
                self.id = s.id
            pause, output, text, progress = None, None, "", []
            async for ev in self.runner.run_async(user_id="u", session_id=self.id, new_message=message):
                lro = getattr(ev, "long_running_tool_ids", None) or set()
                for fc in ev.get_function_calls() or []:
                    if fc.id in lro:
                        pause = fc.id
                if ev.content and ev.content.parts:
                    text += "".join(p.text for p in ev.content.parts if getattr(p, "text", None))
                if (getattr(ev, "custom_metadata", None) or {}).get("progress"):
                    progress.append(ev.custom_metadata["progress"])
                if getattr(ev, "output", None) is not None:
                    output = ev.output
            return {"pause": pause, "output": output, "text": text, "progress": progress}

    def say(self, text):
        return asyncio.run(self._turn(types.Content(role="user", parts=[types.Part.from_text(text=text)])))

    def resume(self, token, confirmed=True):
        return asyncio.run(self._turn(types.Content(role="user", parts=[types.Part(
            function_response=types.FunctionResponse(
                id=token, name="adk_request_input", response={"confirmed": confirmed}))])))

    def state(self):
        async def get():
            s = await self.runner.session_service.get_session(
                app_name=W.DEPLOY_APP_NAME, user_id="u", session_id=self.id)
            return dict(s.state)
        return asyncio.run(get())


def test_the_dag_pr_is_raised_in_the_confirm_turn_citing_the_run(github):
    s = _Session()
    confirm = s.say(_payload())["pause"]
    assert confirm.startswith("CONFIRM-")

    out = s.resume(confirm)
    assert out["pause"] is None, "no waiting on the run — the PR is raised now"
    result = out["output"]
    assert result["ok"] is True and result["dag_bump"]["pr_number"] == 41
    assert github["dispatches"] == 1
    [bump] = github["bumps"]
    assert bump["run_url"] == RUN["url"], "the PR body links the run to check"
    assert bump["files"] == ["job_a_dag.py"] and bump["repo"] == "o/dags"


def test_a_run_github_has_not_registered_yet_links_the_runs_page(github):
    github["run"] = None
    s = _Session()
    s.resume(s.say(_payload())["pause"])
    [bump] = github["bumps"]
    assert bump["run_url"] == RUNS_PAGE, "still somewhere to check before merging"


def test_the_confirm_token_is_spent_by_the_apply(github):
    """Single-use: after the apply, neither the token nor the preview can be
    found in session state — so a re-sent CONFIRM finds nothing to resume."""
    s = _Session()
    s.resume(s.say(_payload())["pause"])
    state = s.state()
    assert not state.get("deploy_confirm_token") and not state.get("deploy_pending")
    assert deploy._PENDING_PREVIEWS == {}


def test_an_apply_that_raises_becomes_an_honest_outcome_not_a_failed_node(github, monkeypatch):
    """On ADK 2.9 a failed node re-runs on resume. The apply must therefore
    never fail: it reports what happened, and the workflow completes."""
    def boom(name, args):
        github["dispatches"] += 1
        raise RuntimeError("GitHub 502 half-way through")
    monkeypatch.setattr(deploy, "_invoke_tool", boom)

    s = _Session()
    confirm = s.say(_payload())["pause"]
    result = s.resume(confirm)["output"]
    assert result["ok"] is False and result["status"] == "apply_error"
    assert "may already have been applied" in result["note"] and confirm in result["note"]
    assert github["dispatches"] == 1
    assert not s.state().get("deploy_confirm_token"), "a failed apply still spends the token"


def test_a_slow_preview_is_abandoned_with_an_explanation(monkeypatch):
    import time as _time

    deploy._PENDING_PREVIEWS.clear()
    monkeypatch.setattr(settings, "deploy_preview_timeout_seconds", 0.2)
    monkeypatch.setattr(deploy, "prepare_deploy_preview", lambda **kw: _time.sleep(1) or {"ok": True})
    out = _Session().say(_payload())
    assert out["pause"] is None
    assert out["output"]["status"] == "cancelled"
    assert "took longer than" in (out["output"].get("error") or "")


# --- through the chat service: CONFIRM → one reply with the PR and the run -------

def _stream(service, message, thread="t-df"):
    async def go():
        return [e async for e in service.stream_chat(message, thread)]
    return asyncio.run(go())


def test_the_reply_hands_the_merge_call_to_the_person_with_the_run(github):
    from release_agent.adk_service import AdkChatService

    service = AdkChatService()
    [confirm] = [e["data"]["token"] for e in _stream(service, _payload()) if e["type"] == "interrupt"]
    done = _stream(service, confirm)
    assert not [e for e in done if e["type"] == "interrupt"], "nothing left to wait on"
    reply = "".join(e.get("content", "") for e in done if e["type"] == "token")
    assert "[PR #41](https://github.com/o/dags/pull/41)" in reply
    assert f"[Run #7]({RUN['url']})" in reply, "the run to check sits beside the PR"
    assert "only once it is green" in reply and "call is yours" in reply


# --- composer: a retry after a half-finished attempt still opens the PR -------

class _Blob:
    def __init__(self, text):
        self.decoded_content, self.sha = text.encode(), "sha"


class _PR:
    def __init__(self, n):
        self.number, self.html_url = n, f"https://github.com/o/dags/pull/{n}"


class _DagRepo:
    DAG = "p = \"gs://b/t/{{dag_run.conf['version'] | default('1.9.0')}}/job.json\"\n"

    def __init__(self, branch_text=None, open_pr=None, fail_create=False):
        self.full_name = "o/dags"
        self.files = {"main": self.DAG, "branch": branch_text or self.DAG}
        self.open_pr, self.fail_create, self.created, self.writes = open_pr, fail_create, [], 0

    def get_branch(self, name):
        return type("B", (), {"commit": type("C", (), {"sha": "base"})()})()

    def create_git_ref(self, ref, sha):
        raise Exception("Reference already exists")          # an earlier attempt made it

    def get_contents(self, path, ref=None):
        return _Blob(self.files["main" if ref == "main" else "branch"])

    def update_file(self, path, msg, text, sha, branch=None):
        self.writes += 1
        self.files["branch"] = text

    def get_pulls(self, state="open", head=None):
        return [self.open_pr] if self.open_pr else []

    def get_git_ref(self, name):
        return type("R", (), {"object": type("O", (), {"sha": "moved"})(), "delete": lambda s: None})()

    def create_pull(self, **kw):
        if self.fail_create:
            raise Exception("422 A pull request already exists")
        self.created.append(kw)
        return _PR(42)


def _bump(monkeypatch, repo):
    monkeypatch.setattr(composer, "_repo", lambda r="": repo)
    monkeypatch.setattr(settings, "composer_branch", "main")
    return composer.apply_dag_bump(["job.py"], "2.0.0", "uat", image="job-a", repo="o/dags")


def test_retry_after_edits_were_pushed_but_no_pr_opens_the_pr(monkeypatch):
    """The bug this fixes: the old code compared against the WORK branch, found
    its own earlier edits, said "no change needed" and never opened the PR."""
    repo = _DagRepo(branch_text=_DagRepo.DAG.replace("1.9.0", "2.0.0"))
    out = _bump(monkeypatch, repo)
    assert out["action"] == "dag_bump_pr_opened" and out["pr_number"] == 42
    assert repo.writes == 0, "the branch already had the edit — nothing to rewrite"


def test_an_existing_pr_is_returned_not_duplicated(monkeypatch):
    repo = _DagRepo(branch_text=_DagRepo.DAG.replace("1.9.0", "2.0.0"), open_pr=_PR(40))
    out = _bump(monkeypatch, repo)
    assert out["action"] == "dag_bump_pr_exists" and out["pr_number"] == 40
    assert repo.created == []


def test_losing_the_race_to_open_the_pr_is_still_success(monkeypatch):
    repo = _DagRepo(fail_create=True)
    repo.get_pulls = lambda state="open", head=None: [_PR(43)] if repo.writes else []
    out = _bump(monkeypatch, repo)
    assert out["ok"] is True and out["pr_number"] == 43


# --- the message a user sees when the model is rate-limited -------------------

def test_a_wrapped_429_reads_as_model_busy_not_internal_error():
    from release_agent.app_fastapi import _chat_error_message

    try:
        try:
            raise RuntimeError("429 RESOURCE_EXHAUSTED. Resource exhausted.")
        except RuntimeError as inner:
            raise ValueError("model call failed") from inner
    except ValueError as outer:
        msg = _chat_error_message(outer)
    assert "busy" in msg and "Nothing was changed" in msg
    assert "internal" not in _chat_error_message(KeyError("x")).lower()


def test_a_spent_confirm_token_gets_a_plain_answer_and_applies_nothing(github):
    from release_agent.adk_service import AdkChatService

    service = AdkChatService()
    [confirm] = [e["data"]["token"] for e in _stream(service, _payload(), "t-s") if e["type"] == "interrupt"]
    _stream(service, confirm, "t-s")                       # applies once
    again = _stream(service, confirm, "t-s")               # re-sent
    reply = "".join(e.get("content", "") for e in again if e["type"] == "token")
    assert "already used" in reply and "Nothing was applied" in reply
    assert github["dispatches"] == 1 and len(github["bumps"]) == 1


def test_the_pr_body_says_merge_only_once_the_run_is_green(monkeypatch):
    """Raised at dispatch, before the build finishes — the body must not claim
    the run is green, and must say to check it first."""
    repo = _DagRepo()
    monkeypatch.setattr(composer, "_repo", lambda r="": repo)
    monkeypatch.setattr(settings, "composer_branch", "main")
    composer.apply_dag_bump(["job.py"], "2.0.0", "uat", image="job-a",
                            run_url="https://github.com/o/df/actions/runs/7", repo="o/dags")
    body = repo.created[0]["body"]
    assert "https://github.com/o/df/actions/runs/7" in body
    assert "Merge only once that run is green" in body and "(green)" not in body
