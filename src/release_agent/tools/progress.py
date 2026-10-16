"""Step lines from tool code to the person waiting on it.

A UAT deploy or a release runs for a minute or two — a clone and the updater,
a merge, then up to UAT_PR_WAIT_SECONDS for the repository's own SIT → UAT PR —
while the page showed only typing dots, and people gave up on it. The chat
already draws ``{"type": "progress"}`` lines (Investigate uses them), so tool
code reports each step with :func:`say` and the deploy lane forwards the lines
as they arrive.

A sink is bound to the current context with :func:`capture`; ``say`` outside
one does nothing, so the tools stay callable from the chat agent, the commands
Workflow and the tests unchanged. Tool work runs on worker threads
(``asyncio.to_thread`` copies the context, so the sink is visible there) and
the sink hands each line back to its event loop with ``call_soon_threadsafe``.
``say`` never raises and never blocks: a line nobody can read is dropped, and
a deploy is never held up or failed by its own commentary.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import logging
from typing import Any, AsyncIterator, Iterator

logger = logging.getLogger(__name__)

_SINK: contextvars.ContextVar["Steps | None"] = contextvars.ContextVar("progress_sink", default=None)

# Marks the end of a captured stream in the queue.
END = object()


class Steps:
    """The lines of one turn, in order, readable from the event loop."""

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.queue: asyncio.Queue = asyncio.Queue()

    def put(self, line: object) -> None:
        """Hand a line to the loop from any thread. On the loop's own thread
        it goes straight in, so a line said just before an item yielded in
        the same step stays ahead of it; from a worker thread it is scheduled
        onto the loop. Silent when the loop is gone: the deploy must outlive
        its reader (AGENTS.md invariant 1)."""
        try:
            if self.loop.is_closed():
                return
            try:
                on_loop = asyncio.get_running_loop() is self.loop
            except RuntimeError:                  # no loop here: a worker thread
                on_loop = False
            if on_loop:
                self.queue.put_nowait(line)
            else:
                self.loop.call_soon_threadsafe(self.queue.put_nowait, line)
        except RuntimeError:          # the loop closed between the check and the call
            pass

    def close(self) -> None:
        self.put(END)


def say(line: str) -> None:
    """Report a step. Costs nothing outside :func:`capture`."""
    sink = _SINK.get()
    if sink is None:
        return
    text = str(line or "").strip()
    if not text:
        return
    try:
        sink.put(text)
    except Exception:  # noqa: BLE001 — commentary never fails the work
        logger.debug("progress line dropped", exc_info=True)


def waiting(what: str, elapsed: float, limit: float) -> None:
    """The one wording for a long wait, so every flow counts the same way:
    ``Waiting for the SIT → UAT PR (45 s of up to 90 s)``."""
    say(f"{what} ({int(elapsed)} s of up to {int(limit)} s)")


@contextlib.contextmanager
def capture() -> Iterator[Steps]:
    """Bind a sink for the code run inside — including tasks created and
    threads started inside, which copy this context. Must be entered on the
    event loop that will read the lines."""
    steps = Steps(asyncio.get_running_loop())
    token = _SINK.set(steps)
    try:
        yield steps
    finally:
        _SINK.reset(token)


# Strong references to the tasks below: a pending task held by nothing else
# can be garbage-collected mid-flight, and these carry GitHub work.
_running: set[asyncio.Task] = set()


async def stream(source: AsyncIterator[Any]) -> AsyncIterator[dict[str, Any] | Any]:
    """``source``'s items, with the lines said while it runs interleaved as
    ``{"type": "progress", "content": line}`` in arrival order.

    ``source`` is driven to its natural end on a task of its own: a Workflow
    run left half-consumed is finalised later in another context, so the task
    is never cancelled — if the consumer walks away, the work completes
    unread (the chat endpoint already runs every turn detached).
    """
    with capture() as steps:
        queue = steps.queue

        async def drive() -> None:
            try:
                async for item in source:
                    queue.put_nowait(("item", item))
            finally:
                queue.put_nowait(END)

        task = asyncio.create_task(drive())
        _running.add(task)
        task.add_done_callback(_running.discard)
        while True:
            got = await queue.get()
            if got is END:
                break
            if isinstance(got, tuple) and len(got) == 2 and got[0] == "item":
                yield got[1]
            else:
                yield {"type": "progress", "content": got}
        await task      # re-raises what the source raised, after its lines
