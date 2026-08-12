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
import queue
import threading
from typing import TYPE_CHECKING

import pytest

from mindroom.event_journal.sqlite_backend import SqliteBackend, _report

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.event_journal.backend import Transaction

_RETURN_TIMEOUT_SECONDS = 5.0


class _LoopOwnedFuture(asyncio.Future[int]):
    """A future that rejects reads away from its owning loop."""

    def _assert_owning_loop(self) -> None:
        assert asyncio.get_running_loop() is self.get_loop()

    def cancelled(self) -> bool:
        self._assert_owning_loop()
        return super().cancelled()

    def exception(self) -> BaseException | None:
        self._assert_owning_loop()
        return super().exception()

    def result(self) -> int:
        self._assert_owning_loop()
        return super().result()


def _assert_closed_refusal(outcome: list[BaseException | None], operation_ran: threading.Event) -> None:
    assert len(outcome) == 1
    error = outcome[0]
    assert isinstance(error, RuntimeError)
    assert str(error) == "The event-journal store is closed"
    assert not operation_ran.is_set()


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
async def test_cross_loop_report_reads_work_only_on_the_writer_loop() -> None:
    """The caller-loop handoff must contain a plain snapshot, not the writer's future."""
    caller_future: queue.Queue[asyncio.Future[int]] = queue.Queue()
    outcome: list[int] = []

    def wait_on_its_own_loop() -> None:
        async def wait() -> None:
            future = asyncio.get_running_loop().create_future()
            caller_future.put(future)
            outcome.append(await future)

        asyncio.run(wait())

    caller = threading.Thread(target=wait_on_its_own_loop, name="second-loop-caller", daemon=True)
    caller.start()
    future = await asyncio.to_thread(caller_future.get)
    work = _LoopOwnedFuture()
    work.set_result(7)

    _report(future, work)

    await asyncio.to_thread(caller.join, _RETURN_TIMEOUT_SECONDS)
    assert outcome == [7]


@pytest.mark.asyncio
async def test_a_cross_loop_write_racing_close_is_refused_not_stranded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A write admitted across loops must end, even if close() beats it to the queue."""
    backend = SqliteBackend.open(tmp_path / "journal.db")
    await backend.write(lambda transaction: transaction.execute("CREATE TABLE claim (value INTEGER)"))

    outcome: list[BaseException | None] = []
    operation_ran = threading.Event()

    def insert_claim(transaction: Transaction) -> None:
        operation_ran.set()
        transaction.execute("INSERT INTO claim VALUES (1)")

    def write_from_its_own_loop() -> None:
        async def claim() -> None:
            await backend.write(insert_claim)

        try:
            asyncio.run(claim())
        except BaseException as error:  # the point is only that something ends the wait
            outcome.append(error)
        else:
            outcome.append(None)

    bridge = threading.Thread(target=write_from_its_own_loop, name="second-loop-writer", daemon=True)
    admission_scheduled = threading.Event()
    writer_loop = asyncio.get_running_loop()
    call_soon_threadsafe = writer_loop.call_soon_threadsafe

    def record_admission(callback: Callable[..., object], *args: object) -> asyncio.Handle:
        handle = call_soon_threadsafe(callback, *args)
        if callback == backend._admit:
            admission_scheduled.set()
        return handle

    with monkeypatch.context() as patch:
        patch.setattr(writer_loop, "call_soon_threadsafe", record_admission)
        bridge.start()
        # Block this loop until the bridge has passed the first closed check and
        # scheduled admission here. The queued callback cannot run before close.
        assert admission_scheduled.wait(_RETURN_TIMEOUT_SECONDS), "the bridge never scheduled admission"
    await backend.close()

    await asyncio.to_thread(bridge.join, _RETURN_TIMEOUT_SECONDS)
    _assert_closed_refusal(outcome, operation_ran)


@pytest.mark.asyncio
async def test_close_wakes_a_cross_loop_write_already_waiting_in_the_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing must wake a foreign caller whose write was queued behind another write."""
    backend = SqliteBackend.open(tmp_path / "journal.db")
    await backend.write(lambda transaction: transaction.execute("CREATE TABLE claim (value INTEGER)"))

    blocking_write_started = threading.Event()
    release_blocking_write = threading.Event()

    def block_writer(transaction: Transaction) -> None:
        del transaction
        blocking_write_started.set()
        release_blocking_write.wait()

    blocking_write = asyncio.create_task(backend.write(block_writer))
    await asyncio.to_thread(blocking_write_started.wait)

    outcome: list[BaseException | None] = []
    operation_ran = threading.Event()

    def insert_claim(transaction: Transaction) -> None:
        operation_ran.set()
        transaction.execute("INSERT INTO claim VALUES (1)")

    def write_from_its_own_loop() -> None:
        async def claim() -> None:
            await backend.write(insert_claim)

        try:
            asyncio.run(claim())
        except BaseException as error:  # the point is only that something ends the wait
            outcome.append(error)
        else:
            outcome.append(None)

    bridge = threading.Thread(target=write_from_its_own_loop, name="queued-second-loop-writer", daemon=True)
    admission_completed = asyncio.Event()
    writer_loop = asyncio.get_running_loop()
    call_soon_threadsafe = writer_loop.call_soon_threadsafe

    def record_admission(callback: Callable[..., object], *args: object) -> asyncio.Handle:
        if callback != backend._admit:
            return call_soon_threadsafe(callback, *args)

        def admit_and_record() -> None:
            callback(*args)
            admission_completed.set()

        return call_soon_threadsafe(admit_and_record)

    with monkeypatch.context() as patch:
        patch.setattr(writer_loop, "call_soon_threadsafe", record_admission)
        bridge.start()
        await admission_completed.wait()

    close = asyncio.create_task(backend.close())
    await asyncio.sleep(0)
    release_blocking_write.set()
    await close

    await asyncio.to_thread(bridge.join, _RETURN_TIMEOUT_SECONDS)
    _assert_closed_refusal(outcome, operation_ran)
    await blocking_write


@pytest.mark.asyncio
async def test_a_closed_recorded_writer_loop_refuses_a_cross_loop_write(tmp_path: Path) -> None:
    """A stale closed writer loop must produce the store's closure error, not leak a loop error."""
    backend = SqliteBackend.open(tmp_path / "journal.db")
    await backend.write(lambda transaction: transaction.execute("CREATE TABLE claim (value INTEGER)"))
    writer_loop = backend._writer_loop
    closed_loop = asyncio.new_event_loop()
    closed_loop.close()
    operation_ran = threading.Event()

    def insert_claim(transaction: Transaction) -> None:
        operation_ran.set()
        transaction.execute("INSERT INTO claim VALUES (1)")

    backend._writer_loop = closed_loop
    try:
        with pytest.raises(RuntimeError, match=r"^The event-journal store is closed$"):
            await backend.write(insert_claim)
    finally:
        backend._writer_loop = writer_loop
        await backend.close()

    assert not operation_ran.is_set()


@pytest.mark.asyncio
async def test_cancelling_a_cross_loop_write_waits_for_its_statement_to_finish(tmp_path: Path) -> None:
    """Cancellation must wake only after the cross-loop statement releases the writer."""
    backend = SqliteBackend.open(tmp_path / "journal.db")
    await backend.write(lambda transaction: transaction.execute("CREATE TABLE claim (value INTEGER)"))
    statement_started = threading.Event()
    release_statement = threading.Event()
    statement_finished = threading.Event()
    cancellation_sent = threading.Event()
    cancellation_returned = threading.Event()
    outcome: list[BaseException] = []

    def insert_claim(transaction: Transaction) -> None:
        transaction.execute("INSERT INTO claim VALUES (1)")
        statement_started.set()
        assert release_statement.wait(_RETURN_TIMEOUT_SECONDS), "the test never released the statement"
        statement_finished.set()

    def write_from_its_own_loop() -> None:
        async def claim() -> None:
            writing = asyncio.create_task(backend.write(insert_claim))
            assert await asyncio.to_thread(statement_started.wait, _RETURN_TIMEOUT_SECONDS), (
                "the cross-loop statement never started"
            )
            writing.cancel()
            cancellation_sent.set()
            try:
                await writing
            except asyncio.CancelledError as error:
                outcome.append(error)
            finally:
                cancellation_returned.set()

        asyncio.run(claim())

    bridge = threading.Thread(target=write_from_its_own_loop, name="cancelled-second-loop-writer", daemon=True)
    bridge.start()
    try:
        assert await asyncio.to_thread(cancellation_sent.wait, _RETURN_TIMEOUT_SECONDS)
        assert not cancellation_returned.is_set(), "cancellation escaped while the statement still held the writer"
    finally:
        release_statement.set()

    await asyncio.to_thread(bridge.join, _RETURN_TIMEOUT_SECONDS)
    assert cancellation_returned.is_set()
    assert len(outcome) == 1
    assert isinstance(outcome[0], asyncio.CancelledError)
    assert statement_finished.is_set()
    await backend.close()
