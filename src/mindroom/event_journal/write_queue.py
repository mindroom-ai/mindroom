"""The one writer task a backend's writes queue for, from whichever loop issues them.

Each backend owns one writer connection, so its writes are serialized by one
task that drains a queue. Agno runs a synchronous tool's hooks on a loop of its
own, so a write can arrive from a loop that is not the writer's, and its caller
has to be admitted and woken across that boundary deliberately.

Every commit waits for the disk, and under load the writes queued behind one
commit outnumber what a commit apiece can keep up with. The task therefore
hands the backend every write queued at once, which it commits in one
transaction with each write atomic on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .offloading import ThreadOffload, settled

if TYPE_CHECKING:
    from collections.abc import Callable

    from .backend import Operation


CLOSED_MESSAGE = "The event-journal store is closed"
_WRITER_STOPPED_MESSAGE = "The event-journal writer stopped before running this write"
# The most queued writes one transaction commits together, so a backlog never
# makes the first of them wait for a long batch.
_GROUP_COMMIT_LIMIT = 64


@dataclass(frozen=True, slots=True)
class WriteOutcome:
    """How one write ended: its result, its error, or its cancellation."""

    result: Any = None
    error: BaseException | None = None
    cancelled: bool = False


type _ApplyBatch = Callable[[list[Operation[Any]]], list[WriteOutcome]]


@dataclass(slots=True)
class _QueuedWrite:
    operation: Operation[Any]
    future: asyncio.Future[Any]


@dataclass
class WriteQueue:
    """Serialize a backend's writes through one task, committing those queued at once together."""

    # Runs on a worker thread and commits every operation it is handed, or none.
    apply: _ApplyBatch
    offload: ThreadOffload
    task_name: str
    closed: bool = field(default=False, init=False)
    # Absent until the first write, because the queue belongs to the loop that
    # drains it and the backend opens before there is one. Typed as such rather
    # than declared non-optional and probed for, which is the same fiction with
    # the type checker on the wrong side of it.
    _queue: asyncio.Queue[_QueuedWrite] | None = field(default=None, init=False, repr=False)
    _writer_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    # The loop the writer task drains on, remembered because a caller on any
    # other loop can neither enqueue onto that queue nor be woken by it
    # without being handed across deliberately.
    _writer_loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _admission_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _pending_admissions: dict[asyncio.Future[Any], _QueuedWrite] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )

    def _ensure_writer_task(self) -> asyncio.Queue[_QueuedWrite]:
        """Start the single writer task on the loop that first writes."""
        queue = self._queue
        if queue is None or self._writer_task is None or self._writer_task.done():
            queue = asyncio.Queue()
            self._queue = queue
            self._writer_loop = asyncio.get_running_loop()
            self._writer_task = asyncio.create_task(
                # Handed the queue it drains rather than reading the field,
                # so a task can only ever settle writes that were admitted to
                # its own queue.
                self._drain_writes(queue),
                name=self.task_name,
            )
            # Nothing else will run what the task leaves queued, and ``close()``
            # is not its only canceller: a loop shutting down cancels every task
            # at once. A write left queued holds its caller forever, because
            # ``settled`` outlives the caller's own cancellation. A callback
            # rather than a ``finally``, since a task cancelled before its first
            # step never enters its coroutine.
            self._writer_task.add_done_callback(lambda _task: self._refuse_queued_writes(queue))
        return queue

    def _refuse_queued_writes(self, queue: asyncio.Queue[_QueuedWrite]) -> None:
        """Answer every write a stopped writer task will never run."""
        message = CLOSED_MESSAGE if self.closed else _WRITER_STOPPED_MESSAGE
        while not queue.empty():
            _deliver(queue.get_nowait().future, WriteOutcome(error=RuntimeError(message)))
            queue.task_done()

    async def _drain_writes(self, queue: asyncio.Queue[_QueuedWrite]) -> None:
        while True:
            batch = [await queue.get()]
            while len(batch) < _GROUP_COMMIT_LIMIT and not queue.empty():
                batch.append(queue.get_nowait())
            try:
                await self._settle(batch)
            finally:
                for _ in batch:
                    queue.task_done()

    async def _settle(self, batch: list[_QueuedWrite]) -> None:
        """Run queued writes and hand each caller what its operation did.

        The outcomes are reported from the worker's own future rather than from
        how this await ended, because those are different questions: a
        cancellation reaches the await and never reaches the thread.
        """
        work = self.offload.submit(lambda: self.apply([queued.operation for queued in batch]))
        try:
            # A failed write belongs to its caller's future, not to the writer
            # task, which has to survive it to run the write after it.
            with contextlib.suppress(Exception):
                await settled(work)
        finally:
            for queued, outcome in zip(batch, _outcomes(work, len(batch)), strict=True):
                _deliver(queued.future, outcome)

    async def write[T](self, operation: Operation[T]) -> T:
        """Queue one operation for the writer task and await its commit.

        Admission is coordinated with ``close()`` under the admission lock. A
        caller already on the writer's loop enqueues synchronously; one on
        another loop registers its handoff before scheduling the admission
        callback. The callback enqueues only while it still owns that handoff.

        That is what the queue being unbounded buys. A bounded one parked the
        producer in ``put`` instead, and ``close()`` frees a slot per entry it
        drains: parked producers woke afterwards, enqueued onto a queue whose
        consumer was already cancelled, and waited on it forever -- and any
        producer past the queue's size was never woken at all. Re-checking
        ``closed`` after the ``put`` narrows that window without closing it,
        because it cannot reach a producer that is still parked.

        The bound was not paying for itself either. Every caller awaits its own
        write, so an entry exists only while a caller is suspended on it, and
        suspending that caller one await earlier holds the same operation in
        memory plus the machinery to park it.
        """
        if self.closed:
            raise RuntimeError(CLOSED_MESSAGE)
        caller_loop = asyncio.get_running_loop()
        future: asyncio.Future[T] = caller_loop.create_future()
        queued = _QueuedWrite(operation=operation, future=future)
        # A queue belongs to the loop that drains it. Putting to one from
        # another loop wakes its consumer through a callback scheduled on the
        # wrong loop, which arrives whenever that loop happens to run next and
        # not because anything told it to.
        with self._admission_lock:
            if self.closed:
                raise RuntimeError(CLOSED_MESSAGE)
            writer_loop = self._writer_loop
            if writer_loop is None or writer_loop is caller_loop:
                queue = self._ensure_writer_task()
                queue.put_nowait(queued)
            else:
                # Shutdown must own this handoff before it is scheduled. A
                # stopped-but-open loop accepts call_soon_threadsafe() without
                # ever running its callback, so the callback itself cannot be
                # the first place the write becomes visible to close().
                self._pending_admissions[future] = queued
        if writer_loop is not None and writer_loop is not caller_loop:
            try:
                writer_loop.call_soon_threadsafe(self._admit, queued)
            except RuntimeError:
                with self._admission_lock:
                    still_pending = self._pending_admissions.pop(future, None) is not None
                if still_pending and not writer_loop.is_closed():
                    raise
                if still_pending:
                    _deliver(future, WriteOutcome(error=RuntimeError(CLOSED_MESSAGE)))
        # Cancelling this await must not report an outcome the writer has not
        # reached yet: the statement runs on a thread regardless, so the caller
        # learns how it ended before its cancellation propagates.
        return await settled(future)

    def _admit(self, queued: _QueuedWrite) -> None:
        """Put one still-pending handed-across write in the writer queue.

        A write from another loop cannot be admitted or inspect the writer task
        where it is decided, so both happen here, on the writer's own loop,
        under the lock that lets ``close()`` claim pending handoffs first.
        """
        with self._admission_lock:
            if self._pending_admissions.pop(queued.future, None) is None:
                return
            queue = self._ensure_writer_task()
            queue.put_nowait(queued)

    def close(self) -> None:
        """Refuse every later write and every handoff not yet admitted.

        Raising ``closed`` under the admission lock also claims every pending
        handoff, so every write already admitted is in the queue, which the
        writer task refuses as it stops; callbacks for claimed handoffs later
        see that they no longer own an admission and do nothing.
        """
        with self._admission_lock:
            self.closed = True
            pending_admissions = tuple(self._pending_admissions.values())
            self._pending_admissions.clear()
        for queued in pending_admissions:
            _deliver(queued.future, WriteOutcome(error=RuntimeError(CLOSED_MESSAGE)))

    async def stop(self) -> None:
        """Stop the writer task once the write in flight has finished, refusing what is still queued.

        Cancelling the task is safe only because ``_settle`` refuses to return
        while its worker thread is still executing: the cancellation ends the
        task after that statement, not during it. Awaiting the task is
        therefore also how this waits for the write in flight.
        """
        writer_task = self._writer_task
        self._writer_task = None
        if writer_task is not None:
            writer_task.cancel()
            try:  # noqa: SIM105 - the task may already be finished
                await writer_task
            except asyncio.CancelledError:
                pass


def _outcomes(work: asyncio.Future[list[WriteOutcome]], count: int) -> list[WriteOutcome]:
    """Snapshot each write's outcome from the worker running its batch.

    A batch that did not commit fails every write in it, whatever each
    operation returned.
    """
    if work.cancelled():
        return [WriteOutcome(cancelled=True)] * count
    if (error := work.exception()) is not None:
        return [WriteOutcome(error=error)] * count
    return work.result()


def _deliver(future: asyncio.Future[Any], outcome: WriteOutcome) -> None:
    """Apply a plain write outcome on the caller future's own loop.

    Completing a future belonging to another loop sets its result but schedules
    its callbacks with a plain ``call_soon``, which does not wake that loop. A
    loop with nothing else pending -- the synchronous tool bridge's own loop,
    between the write it issued and the answer it is waiting for -- then sleeps
    in its selector with the result already sitting there, and the caller never
    resumes. Handing the completion across deliberately is what wakes it.
    """
    caller_loop = future.get_loop()
    if caller_loop is not _running_loop():
        if not caller_loop.is_closed():
            with contextlib.suppress(RuntimeError):
                caller_loop.call_soon_threadsafe(_deliver, future, outcome)
        return
    if future.done():
        return
    if outcome.cancelled:
        future.cancel()
    elif outcome.error is not None:
        future.set_exception(outcome.error)
    else:
        future.set_result(outcome.result)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """Return the loop this call is running on, if it is running on one."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
