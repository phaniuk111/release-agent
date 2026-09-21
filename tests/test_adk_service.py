import json
import asyncio

from adk_release_agent import deploy
from release_agent.adk_service import AdkChatService, _looks_like_deploy_request, _user_id


def _collect(service: AdkChatService, message: str, thread_id: str = "t-adk"):
    async def _run():
        return [event async for event in service.stream_chat(message, thread_id)]

    return asyncio.run(_run())


def _token_events(events):
    return [event for event in events if event.get("type") == "token"]


def _types(events):
    """Event types EXCLUDING progress — progress is advisory UI chatter that may
    appear anywhere in a turn, so contract assertions ignore it."""
    return [e["type"] for e in events if e.get("type") != "progress"]


def _interrupt(events):
    return next(e["data"] for e in events if e.get("type") == "interrupt")


def test_adk_service_streams_deterministic_deploy_preview():
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()

    events = _collect(service, "deploy abc-client-api-svc:1.1.1230 to uat")

    assert _types(events) == ["token", "interrupt", "done"]
    assert "uat/deployment.json" in _token_events(events)[0]["content"]
    interrupt = _interrupt(events)
    assert interrupt["type"] == "confirmation"
    assert interrupt["token"].startswith("CONFIRM-")
    assert interrupt["token"] in deploy._PENDING_PREVIEWS


def test_adk_service_confirmation_applies_pending_deploy(monkeypatch):
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    preview_events = _collect(service, "deploy abc-client-api-svc:1.1.1230 to uat")
    token = _interrupt(preview_events)["token"]
    calls = []

    def fake_invoke(name, args):
        calls.append((name, args))
        return {"ok": True, "note": "deployed via test"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)

    events = _collect(service, token)

    # an applied deploy reports mutated=True so the UI refreshes the banner
    # (a plain question does not — that's what keeps the banner off GitHub)
    assert [e for e in events if e.get("type") != "progress"] == [
        {"type": "token", "content": "deployed via test"},
        {"type": "done", "mutated": True},
    ]
    assert calls == [
        (
            "open_release_pr",
            {"environment": "uat", "image_tags": "abc-client-api-svc:1.1.1230"},
        )
    ]


def test_query_with_chart_tag_is_not_treated_as_deploy():
    deploy._PENDING_PREVIEWS.clear()

    assert not _looks_like_deploy_request("find the PR for abc-client-api-svc:1.1.1230")
    assert deploy._PENDING_PREVIEWS == {}


def test_adk_service_accepts_ui_deploy_json_payload():
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    payload = json.dumps(
        {
            "environment": "uat",
            "include": [
                {
                    "helm_chart_name": "abc-client-api-svc",
                    "helm_chart_version": "1.1.1230",
                    "gke_namespace": "default",
                }
            ],
        }
    )

    events = _collect(service, payload)

    interrupt = _interrupt(events)
    assert interrupt["environment"] == "uat"
    assert {"uat/deployment.json"} == set(interrupt["proposed"])


def test_adk_service_refuses_ui_deploy_json_payload_targeting_prod():
    """PROD is reached only through a release now — a chart-deploy JSON payload
    that names prod is refused, no CONFIRM interrupt raised."""
    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    payload = json.dumps(
        {
            "environment": "prod",
            "include": [
                {
                    "helm_chart_name": "abc-client-api-svc",
                    "helm_chart_version": "1.1.1230",
                    "gke_namespace": "default",
                }
            ],
        }
    )

    events = _collect(service, payload)

    assert not any(e.get("type") == "interrupt" for e in events)
    tokens = "".join(e["content"] for e in _token_events(events))
    assert "release" in tokens.lower()
    assert deploy._PENDING_PREVIEWS == {}


def test_progress_events_describe_tool_calls():
    """Long turns stream `progress` labels so the UI shows what the agent is
    doing instead of silent dots. Labels are human text, never raw tool names
    for known tools, and the HITL confirmation call is excluded (it becomes an
    interrupt of its own)."""
    from types import SimpleNamespace

    from release_agent import adk_service as S

    def call(name, args=None):
        return SimpleNamespace(name=name, args=args or {})

    event = SimpleNamespace(get_function_calls=lambda: [
        call("release_stats"),
        call("load_skill", {"skill_name": "release-queue"}),
        call("some_new_tool"),
        call(S._REQUEST_CONFIRMATION),
    ])
    assert S._progress_events(event) == [
        "Reading the release history",
        "Loading release-queue guidance",
        "Some new tool",
    ]
    # no function calls -> no progress
    assert S._progress_events(SimpleNamespace(get_function_calls=lambda: [])) == []


def test_only_state_changing_tools_mark_a_turn_mutated():
    """The banner is refreshed only when a turn actually changed release state —
    that is what keeps read-only questions off GitHub's rate limit."""
    from types import SimpleNamespace

    from release_agent import adk_service as S

    def ev(*names):
        return SimpleNamespace(
            get_function_calls=lambda: [SimpleNamespace(name=n, args={}) for n in names]
        )

    assert S._changes_release_state(ev("promote_release")) is True
    assert S._changes_release_state(ev("promote_df_release")) is True
    assert S._changes_release_state(ev("remove_from_release")) is True
    # reads must NOT trigger a refresh
    assert S._changes_release_state(ev("release_stats", "find_prs")) is False
    assert S._changes_release_state(ev("list_release_queue")) is False
    assert S._changes_release_state(ev()) is False


# --- a repeat of an operation the person just approved --------------------------
class _Ev:
    """Just enough of an ADK Event for the chat lane."""

    def __init__(self, text="", calls=(), responses=(), long_running=(), invocation_id="inv-1"):
        from google.genai import types as _t

        self.content = _t.Content(role="model", parts=[_t.Part(text=text)]) if text else None
        self._calls, self._responses = list(calls), list(responses)
        self.long_running_tool_ids = set(long_running)
        self.invocation_id = invocation_id

    def get_function_calls(self):
        return self._calls

    def get_function_responses(self):
        return self._responses


def _confirmation_call(call_id, target="prd"):
    from types import SimpleNamespace

    return SimpleNamespace(id=call_id, name="adk_request_confirmation", args={
        "originalFunctionCall": {"id": "orig-" + call_id, "name": "promote_release", "args": {"target": target}},
        "toolConfirmation": {"hint": "?"}})


def test_a_repeat_of_the_just_approved_operation_is_declined_not_asked_again(monkeypatch):
    """Seen live: after 'yes' ran the PRD promotion (#150 merged), the model asked
    to run the SAME promotion again — a second approval for something done."""
    from types import SimpleNamespace

    from release_agent.adk_service import PendingAdkCall

    service = AdkChatService()
    runs = []

    class _Runner:
        app_name = "adk_release_agent"

        async def run_async(self, **kw):
            runs.append(kw["new_message"].parts[0].function_response.response)
            if len(runs) == 1:          # the approved run: the tool result, then a repeat request
                yield _Ev(responses=[SimpleNamespace(name="promote_release", response={
                    "ok": True, "note": "Release file-set promoted to PRD via PR #150 (merged)."})])
                yield _Ev(calls=[_confirmation_call("c2")], long_running=["c2"])
            else:                       # the portal's "no" to the repeat
                yield _Ev(text="Okay, I have cancelled the promotion.")

    service.chat_runner = _Runner()
    service._pending_adk_calls[(_user_id(), "t-rep")] = PendingAdkCall(
        invocation_id="inv-1", function_call_id="c1", function_name="adk_request_confirmation",
        args=_confirmation_call("c1").args)
    events = _collect(service, "yes", thread_id="t-rep")

    assert runs == [{"confirmed": True}, {"confirmed": False}], "the repeat was answered 'no'"
    assert "interrupt" not in _types(events), "no second approval for the person"
    text = "".join(e["content"] for e in _token_events(events))
    assert "promoted to PRD via PR #150" in text and "cancelled" not in text
    assert "t-rep" not in service._pending_adk_calls


def test_a_different_operation_after_an_approval_still_asks(monkeypatch):
    from types import SimpleNamespace

    from release_agent.adk_service import PendingAdkCall

    service = AdkChatService()

    class _Runner:
        app_name = "adk_release_agent"

        async def run_async(self, **kw):
            yield _Ev(responses=[SimpleNamespace(name="promote_release", response={"note": "done"})])
            yield _Ev(calls=[_confirmation_call("c2", target="prl1")], long_running=["c2"])

    service.chat_runner = _Runner()
    service._pending_adk_calls[(_user_id(), "t-diff")] = PendingAdkCall(
        invocation_id="inv-1", function_call_id="c1", function_name="adk_request_confirmation",
        args=_confirmation_call("c1").args)
    events = _collect(service, "yes", thread_id="t-diff")
    assert "interrupt" in _types(events), "PRL1 is a different operation — the person decides"


class _DeployEvent:
    """The shape _stream_deploy_preview reads off a workflow event."""

    def __init__(self, output):
        self.output = output
        self.content = None
        self.actions = None
        self.author = "deploy"
        self.invocation_id = "i"
        self.long_running_tool_ids = set()

    def get_function_calls(self):
        return []


def test_a_preview_that_cannot_be_built_says_why_instead_of_nothing():
    """Found live: a release payload with a malformed date produced an empty
    turn — the form was submitted and the user was told nothing at all. The
    gate routes an unbuildable preview to cancel, which carries its reason in
    the node OUTPUT rather than as text, so the streamer has to render it."""
    from release_agent import adk_service as S

    svc = S.AdkChatService.__new__(S.AdkChatService)
    svc._pending_deploy = {}
    cancelled = _DeployEvent(
        output={"ok": False, "status": "cancelled", "token": "",
                "error": "'start_date' must be 'YYYY-MM-DD HH:MM:SS'"})

    class _Runner:
        async def run_async(self, **kw):
            # The gate's rejecting Event and the cancel node both carry it.
            yield cancelled
            yield cancelled

    svc.deploy_runner = _Runner()

    async def drain():
        return [e async for e in svc._stream_deploy_preview("{}", "t")]

    events = asyncio.run(drain())
    said = " ".join(e.get("content", "") for e in events if e.get("type") == "token")
    assert "start_date" in said, "the reason the preview failed must reach the user"
    assert said.count("start_date") == 1, "said once, not once per node that carries it"
    assert events[-1]["type"] == "done"


def test_a_silent_deploy_graph_still_answers():
    """Even with no text and no output, the lane must not answer with silence."""
    from release_agent import adk_service as S

    svc = S.AdkChatService.__new__(S.AdkChatService)
    svc._pending_deploy = {}

    class _Runner:
        async def run_async(self, **kw):
            yield _DeployEvent(output=None)

    svc.deploy_runner = _Runner()

    async def drain():
        return [e async for e in svc._stream_deploy_preview("{}", "t")]

    events = asyncio.run(drain())
    assert any(e.get("type") == "token" and e.get("content") for e in events)


def _confirmation_pending(function, args):
    from release_agent.adk_service import PendingAdkCall

    return PendingAdkCall(
        invocation_id="i", function_call_id="c", function_name="adk_request_confirmation",
        args={"originalFunctionCall": {"name": function, "args": args},
              "toolConfirmation": {"hint": "Please approve or reject the tool call "
                                           f"{function}() by responding with a FunctionResponse "
                                           "with an expected ToolConfirmation payload."}})


def test_a_prod_removal_is_described_in_words_a_person_can_act_on():
    """Found live: remove_from_release had no hint of its own, so the highest-
    impact scoped op showed ADK's FunctionResponse boilerplate."""
    from release_agent.adk_service import _confirmation_interrupt_payload

    p = _confirmation_interrupt_payload(
        _confirmation_pending("remove_from_release",
                              {"image_names": "targeted-svc", "environment": "prod"}))
    assert "targeted-svc" in p["message"] and "PROD" in p["message"]
    assert "FunctionResponse" not in p["message"]


def test_adk_protocol_boilerplate_is_never_shown_as_the_question():
    from release_agent.adk_service import _confirmation_interrupt_payload

    p = _confirmation_interrupt_payload(_confirmation_pending("some_other_tool", {}))
    assert "FunctionResponse" not in p["message"]
    assert p["message"] == "Confirm some_other_tool?"


def test_every_lane_of_a_turn_is_traced_under_a_named_route(monkeypatch):
    """Each branch of stream_chat must run under a `turn` span carrying the
    route and the ids Langfuse groups by. The two RESUME branches — the
    approval and the CONFIRM token — are the ones worth auditing most, and
    they were the ones that had no span at all."""
    import release_agent.adk_service as S

    seen = []

    def fake_traced_stream(source, route, *, thread_id, user_id, session_id, detail=""):
        seen.append({"route": route, "thread_id": thread_id, "session_id": session_id})
        return source

    monkeypatch.setattr(S, "traced_stream", fake_traced_stream)
    service = AdkChatService.__new__(AdkChatService)      # no runners needed
    service._pending_adk_calls = {}
    service._pending_deploy = {}

    async def fake_stream(*a, **k):
        yield {"type": "done"}

    monkeypatch.setattr(service, "_stream_deploy_preview", fake_stream, raising=False)
    monkeypatch.setattr(service, "_run_chat_agent", fake_stream, raising=False)
    monkeypatch.setattr(service, "_stream_deploy_resume", fake_stream, raising=False)
    monkeypatch.setattr(S, "_looks_like_deploy_request", lambda m: m.startswith("deploy "))
    monkeypatch.setattr(S.adk_parsing, "is_queue_intent", lambda m: False)
    monkeypatch.setattr(S.adk_intent, "deploy_payload_from_freeform", lambda m: None)

    async def no_pending(*a, **k):
        return None

    monkeypatch.setattr(service, "_pending_call_from_session", no_pending, raising=False)
    monkeypatch.setattr(service, "_pending_token_from_session", no_pending, raising=False)

    _collect(service, "deploy payments-api:1.2.3 to uat", "t1")
    _collect(service, "what is deployed in uat?", "t2")

    # the CONFIRM-token resume branch
    service._pending_deploy["t3"] = "CONFIRM-ABC123"
    _collect(service, "CONFIRM-ABC123", "t3")

    assert [s["route"] for s in seen] == [
        "deploy_workflow:deterministic", "chat", "deploy_workflow:resume",
    ]
    assert [s["thread_id"] for s in seen] == ["t1", "t2", "t3"]
    assert seen[0]["session_id"].endswith(":deploy") or "deploy" in seen[0]["session_id"]
    assert "chat" in seen[1]["session_id"]


def test_one_persons_yes_cannot_answer_another_persons_paused_approval(monkeypatch):
    """A thread id is not a secret — the portal prints it in its header. The
    paused prod-ops approval is therefore keyed by (owner, thread), like the
    PAT store and the CONFIRM preview: bob typing "yes" against alice's thread
    id must not approve the operation she is being asked about."""
    import release_agent.adk_service as S
    from release_agent import identity
    from release_agent.adk_service import PendingAdkCall

    alice, bob = identity.Caller(email="alice@example.com"), identity.Caller(email="bob@example.com")
    service = AdkChatService.__new__(AdkChatService)
    service._pending_adk_calls = {}
    service._pending_deploy = {}

    async def no_pending(*a, **k):
        return None

    monkeypatch.setattr(service, "_pending_call_from_session", no_pending, raising=False)
    monkeypatch.setattr(service, "_pending_token_from_session", no_pending, raising=False)
    monkeypatch.setattr(S, "_looks_like_deploy_request", lambda m: False)
    monkeypatch.setattr(S.adk_parsing, "is_queue_intent", lambda m: False)
    monkeypatch.setattr(S.adk_intent, "deploy_payload_from_freeform", lambda m: None)

    answered = []

    async def fake_chat(content, thread_id, **kw):
        answered.append(kw.get("approved"))
        yield {"type": "done"}

    monkeypatch.setattr(service, "_run_chat_agent", fake_chat, raising=False)

    with identity.activate(alice):
        service._pending_adk_calls[(S._user_id(), "t-shared")] = PendingAdkCall(
            invocation_id="inv-1", function_call_id="fc-1", function_name="adk_request_confirmation",
            args={"originalFunctionCall": {"name": "merge_prod_release", "args": {"env": "prd"}}},
        )

    with identity.activate(bob):
        _collect(service, "yes", "t-shared")
    assert answered == [None], "bob's turn must not carry an approval"
    assert (("alice@example.com", "t-shared") in service._pending_adk_calls), \
        "alice's pending approval must still be waiting for HER"

    with identity.activate(alice):
        _collect(service, "yes", "t-shared")
    assert answered[-1] == ("merge_prod_release", '{"env": "prd"}'), "the owner's yes still approves"
    assert not service._pending_adk_calls, "and consumes it"
