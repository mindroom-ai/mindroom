"""Async callers run storage operations off the event loop, one at a time per database."""

from __future__ import annotations

import asyncio
import contextvars
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.schema import TableNames
from mindroom.session_storage.store import ConversationStore, open_conversation_store

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_REQUEST = contextvars.ContextVar[str]("request", default="none")


def _store(tmp_path: Path, name: str = "code") -> ConversationStore:
    return open_conversation_store(tmp_path / f"{name}.db", TableNames(f"{name}_sessions"))


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
    """An operation on a database waits for the one before it, on the database's own thread."""
    store = _store(tmp_path)
    log: list[str] = []
    started, release = threading.Event(), threading.Event()
    first = asyncio.create_task(run_session_storage_operation(lambda: store, _blocking(log, started, release)))
    assert await asyncio.to_thread(started.wait, 5)

    def second_operation(_conversation: ConversationStore) -> str:
        log.append(f"second {threading.get_ident()}")
        return "second"

    second = asyncio.create_task(run_session_storage_operation(lambda: _store(tmp_path), second_operation))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()

    assert await asyncio.gather(first, second) == ["blocked", "second"]
    lane_thread = log[0].removeprefix("start ")
    assert log == [f"start {lane_thread}", "end", f"second {lane_thread}"]
    assert lane_thread != str(threading.get_ident())


@pytest.mark.asyncio
async def test_databases_do_not_wait_for_each_other(tmp_path: Path) -> None:
    """Each database has its own lane, so a slow operation on one never holds up another."""
    other_ran = threading.Event()

    def wait_for_other(_conversation: ConversationStore) -> bool:
        return other_ran.wait(5)

    waiting = asyncio.create_task(run_session_storage_operation(lambda: _store(tmp_path, "code"), wait_for_other))
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
