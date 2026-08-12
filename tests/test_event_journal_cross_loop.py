"""A journal write issued from a second event loop has to come back.

Agno runs a synchronous tool as ``await asyncio.to_thread(function_call.execute)``,
so its hook chain lands on a worker thread with no loop of its own, and
``_run_coroutine_from_sync`` (``tool_system/tool_hooks.py``) gives it one with
``asyncio.run``. Any journal write that chain triggers is therefore issued
from a loop that is not the one the store's writer task lives on. The runtime
loop is still running, so the write itself is fine -- the writer commits it --
but the caller has to be woken on its own loop, and a loop whose only pending
work is that write has no reason to wake unless something tells it to.

When it is not told, the caller never resumes: the row is committed and
visible to every reader, while the coroutine that wrote it waits forever,
holding whatever it was in the middle of publishing.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal.sqlite_backend import SqliteBackend

if TYPE_CHECKING:
    from pathlib import Path

_RETURN_TIMEOUT_SECONDS = 5.0


@pytest.mark.asyncio
async def test_a_write_from_a_second_event_loop_comes_back(tmp_path: Path) -> None:
    """A caller on another loop must resume once its write has committed."""
    backend = SqliteBackend.open(tmp_path / "journal.db")
    # The first write is what pins the writer task to this loop, which is the
    # arrangement the bridge then writes into from a loop of its own.
    await backend.write(lambda transaction: transaction.execute("CREATE TABLE claim (value INTEGER)"))

    returned = threading.Event()

    def write_from_its_own_loop() -> None:
        async def claim() -> None:
            # Deliberately unbounded: a timeout would put a timer on this loop
            # and wake it for reasons of its own, which is the very thing the
            # caller cannot rely on.
            await backend.write(lambda transaction: transaction.execute("INSERT INTO claim VALUES (1)"))

        asyncio.run(claim())
        returned.set()

    bridge = threading.Thread(target=write_from_its_own_loop, name="second-loop-writer", daemon=True)
    bridge.start()
    # Joined off this loop so the writer task stays free to drain the queue.
    await asyncio.to_thread(bridge.join, _RETURN_TIMEOUT_SECONDS)

    committed = await backend.read(lambda transaction: transaction.fetchone("SELECT value FROM claim"))
    assert committed is not None, "the write never reached the database, so this proves something else"
    assert returned.is_set(), "the write committed but its caller was never woken"

    await backend.close()


@pytest.mark.asyncio
async def test_a_cross_loop_write_racing_close_is_refused_not_stranded(tmp_path: Path) -> None:
    """A write admitted across loops must end, even if close() beats it to the queue."""
    backend = SqliteBackend.open(tmp_path / "journal.db")
    await backend.write(lambda transaction: transaction.execute("CREATE TABLE claim (value INTEGER)"))

    outcome: list[BaseException | None] = []

    def write_from_its_own_loop() -> None:
        async def claim() -> None:
            await backend.write(lambda transaction: transaction.execute("INSERT INTO claim VALUES (1)"))

        try:
            asyncio.run(claim())
        except BaseException as error:  # the point is only that something ends the wait
            outcome.append(error)
        else:
            outcome.append(None)

    bridge = threading.Thread(target=write_from_its_own_loop, name="second-loop-writer", daemon=True)
    bridge.start()
    # Blocking rather than awaiting, so this loop cannot run the admission the
    # bridge is scheduling on it: close() then reaches the queue first, which
    # is the race the caller must survive.
    time.sleep(0.2)  # noqa: ASYNC251 - blocking this loop is the point of the test
    await backend.close()

    await asyncio.to_thread(bridge.join, _RETURN_TIMEOUT_SECONDS)
    assert outcome, "the write was neither admitted nor refused, so its caller is stranded"
