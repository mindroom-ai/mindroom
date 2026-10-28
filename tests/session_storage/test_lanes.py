"""Async callers run storage operations off the event loop, one at a time per database."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest

from mindroom.session_storage.lanes import _lane, run_session_storage_operation
from mindroom.session_storage.store import ConversationStore

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_REQUEST = contextvars.ContextVar[str]("request", default="none")


def _store(tmp_path: Path, name: str = "code") -> ConversationStore:
    return ConversationStore(tmp_path / f"{name}.db")


def _blocking(log: list[str], started: threading.Event, release: threading.Event) -> Callable[[ConversationStore], str]:
    def operation(_conversation: ConversationStore) -> str:
        log.append(f"start {threading.get_ident()}")
        started.set()
        assert release.wait(5)
        log.append("end")
        return "blocked"

    return operation


@pytest.mark.asyncio
async def test_operations_on_one_database_run_one_at_a_time_off_the_event_loop(tmp_path: Path) -> None:
    """An operation opens its store off the event loop and waits for the one before it, on the database's own thread."""
    store = _store(tmp_path)
    # A lane lives only while someone holds it. Hold it for the whole test, so the second operation joins the
    # first one's lane even if it reaches the lane only after the first operation has finished.
    held_lane = _lane(store)
    log: list[str] = []
    started, release = threading.Event(), threading.Event()
    first = asyncio.create_task(run_session_storage_operation(lambda: store, _blocking(log, started, release)))
    assert await asyncio.to_thread(started.wait, 5)

    opened_on: list[int] = []

    def open_second() -> ConversationStore:
        opened_on.append(threading.get_ident())
        return _store(tmp_path)

    def second_operation(_conversation: ConversationStore) -> str:
        log.append(f"second {threading.get_ident()}")
        return "second"

    second = asyncio.create_task(run_session_storage_operation(open_second, second_operation))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()

    assert await asyncio.gather(first, second) == ["blocked", "second"]
    lane_thread = log[0].removeprefix("start ")
    assert log == [f"start {lane_thread}", "end", f"second {lane_thread}"]
    assert lane_thread != str(threading.get_ident())
    (open_thread,) = opened_on
    assert open_thread != threading.get_ident()
    assert _lane(store) is held_lane


@pytest.mark.asyncio
async def test_a_running_operation_keeps_its_lane_for_the_next_one(tmp_path: Path) -> None:
    """With nothing else holding a busy database's lane, the next operation on it still queues behind the running one."""
    store = _store(tmp_path)
    log: list[str] = []
    started, release = threading.Event(), threading.Event()
    first = asyncio.create_task(run_session_storage_operation(lambda: store, _blocking(log, started, release)))
    assert await asyncio.to_thread(started.wait, 5)

    next_in_lane = asyncio.wrap_future(_lane(store).submit(threading.get_ident))
    release.set()

    assert await first == "blocked"
    assert log[0] == f"start {await next_in_lane}"


@pytest.mark.asyncio
async def test_operations_waiting_on_a_busy_lane_leave_the_default_executor_free(tmp_path: Path) -> None:
    """A burst of operations on one database waits on the loop, so tool calls and other offloads still get threads."""
    loop = asyncio.get_running_loop()
    default_executor = ThreadPoolExecutor(max_workers=2)
    loop.set_default_executor(default_executor)
    store = _store(tmp_path)
    started, queued_opened = asyncio.Event(), asyncio.Event()
    release = threading.Event()

    def hold_the_lane(_conversation: ConversationStore) -> str:
        loop.call_soon_threadsafe(started.set)
        assert release.wait(30)
        return "held"

    def open_queued() -> ConversationStore:
        loop.call_soon_threadsafe(queued_opened.set)
        return store

    operations = [asyncio.create_task(run_session_storage_operation(lambda: store, hold_the_lane))]
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        operations.append(
            asyncio.create_task(run_session_storage_operation(open_queued, lambda _conversation: "queued")),
        )
        await asyncio.wait_for(queued_opened.wait(), timeout=5)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "offloaded"), timeout=5) == "offloaded"
        assert not operations[1].done()
    finally:
        release.set()
        results = await asyncio.gather(*operations)
        default_executor.shutdown()
    assert results == ["held", "queued"]


@pytest.mark.asyncio
async def test_databases_do_not_wait_for_each_other(tmp_path: Path) -> None:
    """Each database has its own lane, so a slow operation on one never holds up another."""
    started, other_ran = threading.Event(), threading.Event()

    def wait_for_other(_conversation: ConversationStore) -> bool:
        started.set()
        return other_ran.wait(5)

    waiting = asyncio.create_task(run_session_storage_operation(lambda: _store(tmp_path, "code"), wait_for_other))
    assert await asyncio.to_thread(started.wait, 5)
    await run_session_storage_operation(lambda: _store(tmp_path, "helper"), lambda _conversation: other_ran.set())

    assert await waiting is True


@pytest.mark.asyncio
async def test_a_cancelled_caller_waits_for_its_started_operation(tmp_path: Path) -> None:
    """Cancelling the caller never abandons a started write halfway."""
    store = _store(tmp_path)
    log: list[str] = []
    started, release = threading.Event(), threading.Event()
    task = asyncio.create_task(run_session_storage_operation(lambda: store, _blocking(log, started, release)))
    assert await asyncio.to_thread(started.wait, 5)

    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert log[-1] == "end"


@pytest.mark.asyncio
async def test_operations_see_the_callers_context(tmp_path: Path) -> None:
    """Context variables such as request logging reach the operation."""
    _REQUEST.set("request-1")

    assert (
        await run_session_storage_operation(lambda: _store(tmp_path), lambda _conversation: _REQUEST.get())
        == "request-1"
    )
