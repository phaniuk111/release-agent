"""tools/progress.py — the step lines a deploy or release says while it runs."""
import asyncio
import threading

from release_agent.tools import progress


def test_say_outside_a_capture_does_nothing():
    progress.say("Opened the PR into SIT")      # no sink bound: silent, no error


def test_lines_said_from_a_worker_thread_arrive_in_order():
    """Tool code runs on asyncio.to_thread workers; the sink has to be visible
    there (the thread copies the context) and the lines must come back in the
    order they were said."""
    async def run():
        with progress.capture() as steps:
            def work():
                assert threading.current_thread() is not threading.main_thread()
                progress.say("one")
                progress.say("  ")              # blank: dropped
                progress.waiting("Waiting for the SIT → UAT PR", 45.2, 90)
                progress.say("three")
                return "done"

            result = await asyncio.to_thread(work)
            lines = []
            while not steps.queue.empty():
                lines.append(steps.queue.get_nowait())
            return result, lines

    assert asyncio.run(run()) == ("done", [
        "one", "Waiting for the SIT → UAT PR (45 s of up to 90 s)", "three"])


def test_stream_interleaves_lines_with_the_sources_items():
    async def source():
        await asyncio.to_thread(progress.say, "Reading SIT's deployment file")
        yield {"event": 1}
        await asyncio.to_thread(progress.say, "Merging into SIT")
        yield {"event": 2}

    async def run():
        return [item async for item in progress.stream(source())]

    assert asyncio.run(run()) == [
        {"type": "progress", "content": "Reading SIT's deployment file"},
        {"event": 1},
        {"type": "progress", "content": "Merging into SIT"},
        {"event": 2},
    ]


def test_a_failing_source_still_delivers_its_lines_then_raises():
    async def source():
        progress.say("Opening the PR into SIT")
        yield "first"
        raise RuntimeError("GitHub refused")

    async def run():
        got = []
        try:
            async for item in progress.stream(source()):
                got.append(item)
        except RuntimeError as e:
            got.append(f"raised: {e}")
        return got

    assert asyncio.run(run()) == [
        {"type": "progress", "content": "Opening the PR into SIT"}, "first", "raised: GitHub refused"]


def test_a_line_said_after_the_loop_is_gone_is_dropped_silently():
    """A deploy outlives its reader: a worker thread still running after the
    turn's loop closed must not fail on its own commentary."""
    holder = {}

    async def run():
        with progress.capture() as steps:
            holder["steps"] = steps
            holder["ctx"] = __import__("contextvars").copy_context()

    asyncio.run(run())                         # the loop is closed now
    holder["ctx"].run(progress.say, "Found the deploy run")
    holder["steps"].put("late")
    holder["steps"].close()


def test_a_confirmed_deploy_streams_its_steps_before_the_result(monkeypatch):
    """The whole point: after the CONFIRM token the page used to show dots
    until the apply ended. Lines said inside the apply (on its worker thread)
    now reach the page first, then the result as before."""
    from adk_release_agent import deploy
    from release_agent.adk_service import AdkChatService

    def collect(service, message, thread_id="t-progress"):
        async def _run():
            return [e async for e in service.stream_chat(message, thread_id)]
        return asyncio.run(_run())

    deploy._PENDING_PREVIEWS.clear()
    service = AdkChatService()
    token = next(e["data"] for e in collect(service, "deploy abc-client-api-svc:1.1.1230 to uat")
                 if e.get("type") == "interrupt")["token"]

    def fake_invoke(name, args):
        progress.say("Opened the PR into SIT")
        progress.say("Merged into SIT")
        return {"ok": True, "note": "deployed via test"}

    monkeypatch.setattr(deploy, "_invoke_tool", fake_invoke)
    events = collect(service, token)

    lines = [e["content"] for e in events if e.get("type") == "progress"]
    assert lines[0].startswith(f"Confirmed `{token}`")
    assert lines[1:] == ["Opened the PR into SIT", "Merged into SIT"]
    assert [e["type"] for e in events] == ["progress", "progress", "progress", "token", "done"]
    assert events[-2]["content"] == "deployed via test"
