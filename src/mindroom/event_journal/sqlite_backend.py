"""SQLite backend: one writer task per process behind a queue, readers in WAL.

Concurrent writers are what produce ``database is locked`` under load, so
within a process there is exactly one. Every write is submitted to a queue
drained by a single task, which means that serialization is a property of the
structure rather than of a timeout being long enough.

Per process, and the qualifier is the whole of it. ``mindroom threads export``
opens this same file from its own process and hydrates through it, so a
deployment running the documented ``--watch`` pass alongside a bot has two
writers on one database and no shared queue to put them in. What serializes
*those* is the busy timeout, and nothing else -- exactly the arrangement the
queue exists to avoid, arrived at from outside where the queue cannot reach.

Which is survivable, and is not free. A write blocks the other process's next
write for as long as it runs, and an export installing one hydrated
conversation runs for as long as that conversation is large: a few hundred
messages is milliseconds, and the ceilings in ``thread_export`` permit far
more than the ten seconds another process is willing to wait. Past that the
loser is refused rather than delayed. On the bot that refusal reaches
``journal_ingress``, which declines the event instead of accepting one it did
not commit -- the checkpoint holds, nio redelivers, and no event is lost. The
cost is a stalled sync round trip, not a hole in the journal.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.logging_config import get_logger

from .offloading import ThreadOffload, settled
from .schema import SQLITE_DIALECT, render, schema_statements

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from .backend import Operation, Row


logger = get_logger(__name__)

_BUSY_TIMEOUT_MILLISECONDS = 10_000
_CLOSED_MESSAGE = "The event-journal store is closed"
# How long to wait between attempts at the one statement SQLite's own busy
# handler will not retry. Short enough that a contended open is not noticeably
# slower than an uncontended one, long enough not to spin.
_WAL_RETRY_SECONDS = 0.05


@dataclass(frozen=True, slots=True)
class _SqliteTransaction:
    """Statement execution against one open SQLite connection."""

    connection: sqlite3.Connection

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Run one statement."""
        self.connection.execute(render(sql, SQLITE_DIALECT), tuple(params))

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        """Run one query and return its first row, if any."""
        return self.connection.execute(render(sql, SQLITE_DIALECT), tuple(params)).fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> tuple[Row, ...]:
        """Run one query and return every row."""
        return tuple(self.connection.execute(render(sql, SQLITE_DIALECT), tuple(params)).fetchall())


def _enter_wal(connection: sqlite3.Connection) -> None:
    """Put one connection into WAL, waiting out a lock another process holds.

    ``busy_timeout`` bounds every contention this file can meet except this
    one. Entering WAL is a journal-mode change, and SQLite answers a mode
    change it cannot lock with ``SQLITE_BUSY`` straight away rather than
    calling the busy handler -- so the single statement here that can block on
    another process was the single statement with no timeout at all, and it
    failed on the spot instead of after ten seconds.

    Only ever reachable across processes, and only while the database was
    still being made: one already in WAL stays in it, and re-entering locks
    nothing. So a second opener met this exactly once, on the first open of a
    new journal -- an export pass starting while the bot was still creating the
    database it meant to read.

    Bounded by the same ten seconds as everything else, because a wait that
    cannot fail is not a bound. Past it the refusal is the caller's to see, and
    so is any refusal that was never about a lock: retrying one of those would
    turn an unopenable database into the same error ten seconds later.
    """
    deadline = time.monotonic() + _BUSY_TIMEOUT_MILLISECONDS / 1000
    while True:
        try:
            connection.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError as error:
            if error.sqlite_errorcode != sqlite3.SQLITE_BUSY or time.monotonic() >= deadline:
                raise
            time.sleep(_WAL_RETRY_SECONDS)
        else:
            return


def _configure(connection: sqlite3.Connection, *, synchronous: str) -> None:
    """Open one connection onto the journal, durable as far as its role needs.

    ``synchronous`` is asked for rather than assumed because the writer and the
    readers do not need the same thing, and the difference is expensive in one
    direction and unsafe in the other.

    ``busy_timeout`` is set before anything that can meet another process, so
    every statement below it waits rather than failing on the spot.
    """
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MILLISECONDS}")
    _enter_wal(connection)
    connection.execute(f"PRAGMA synchronous = {synchronous}")
    connection.execute("PRAGMA foreign_keys = ON")


@dataclass
class SqliteBackend:
    """A single-writer SQLite store."""

    database_path: Path
    _writer: sqlite3.Connection = field(init=False, repr=False)
    _readers: threading.local = field(init=False, repr=False)
    # Absent until the first write, because the queue belongs to the loop that
    # drains it and `open()` runs before there is one. Typed as such rather
    # than declared non-optional and probed for, which is the same fiction with
    # the type checker on the wrong side of it.
    _queue: asyncio.Queue[_QueuedWrite] | None = field(default=None, init=False, repr=False)
    _writer_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _open_readers: list[sqlite3.Connection] = field(default_factory=list, init=False, repr=False)
    _reader_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _offload: ThreadOffload = field(default_factory=ThreadOffload, init=False, repr=False)

    @classmethod
    def open(cls, database_path: Path) -> SqliteBackend:
        """Create the schema and connect.

        Synchronous, because a bot builds its collaborators before it has an
        event loop. The writer task is created on the first write instead, so
        the store can be constructed anywhere and still own a single writer.
        """
        backend = cls(database_path=database_path)
        backend.database_path.parent.mkdir(parents=True, exist_ok=True)
        backend._readers = threading.local()
        backend._writer = backend._connect_writer()
        return backend

    def _ensure_writer_task(self) -> asyncio.Queue[_QueuedWrite]:
        """Start the single writer task on the loop that first writes."""
        queue = self._queue
        if queue is None or self._writer_task is None or self._writer_task.done():
            queue = asyncio.Queue()
            self._queue = queue
            self._writer_task = asyncio.create_task(
                # Handed the queue it drains rather than reading the field,
                # so a task can only ever settle writes that were admitted to
                # its own queue.
                self._drain_writes(queue),
                name=f"event_journal_sqlite_writer_{self.database_path.name}",
            )
        return queue

    def _connect_writer(self) -> sqlite3.Connection:
        # The writer runs on whichever pool thread `asyncio.to_thread` picks, so
        # the connection has to outlive its creating thread. Only ever one write
        # is in flight, because a single task drains the queue.
        connection = sqlite3.connect(
            self.database_path,
            isolation_level=None,
            check_same_thread=False,
        )
        # The only connection that commits, and the only one whose commits have
        # to reach the disk before they are reported as landed. A Matrix sync
        # checkpoint is written through `write_json_file_durable`, so it is
        # fsynced; under `synchronous = NORMAL` the WAL frames it certifies are
        # not, and a host reset can leave the checkpoint pointing past events
        # the journal no longer holds. Nothing re-delivers those: the token says
        # they were consumed, and history debt is only recorded for a recovery
        # gap the certifier can see. Readers commit nothing, so this is the
        # writer's cost alone.
        _configure(connection, synchronous="FULL")
        connection.execute("BEGIN IMMEDIATE")
        for statement in schema_statements(SQLITE_DIALECT):
            connection.execute(statement)
        connection.execute("COMMIT")
        return connection

    def _reader(self) -> sqlite3.Connection:
        connection = getattr(self._readers, "connection", None)
        if connection is None:
            # Used only by its owning pool thread while open. `close()` drains
            # every offloaded read before closing these, so no statement is
            # ever executing on one when the closing thread reaches it.
            connection = sqlite3.connect(
                self.database_path,
                isolation_level=None,
                check_same_thread=False,
            )
            _configure(connection, synchronous="NORMAL")
            self._readers.connection = connection
            with self._reader_lock:
                self._open_readers.append(connection)
        return connection

    async def _drain_writes(self, queue: asyncio.Queue[_QueuedWrite]) -> None:
        while True:
            queued = await queue.get()
            try:
                await self._settle(queued)
            finally:
                queue.task_done()

    async def _settle(self, queued: _QueuedWrite) -> None:
        """Run one queued write and hand its caller what the statement did.

        The outcome is reported from the worker's own future rather than from
        how this await ended, because those are different questions: a
        cancellation reaches the await and never reaches the thread.
        """
        work = self._offload.submit(lambda: self._apply(queued.operation))
        try:
            # A failed write belongs to its caller's future, not to the writer
            # task, which has to survive it to run the write after it.
            with contextlib.suppress(Exception):
                await settled(work)
        finally:
            _report(queued.future, work)

    def _apply[T](self, operation: Operation[T]) -> T:
        self._writer.execute("BEGIN IMMEDIATE")
        try:
            result = operation(_SqliteTransaction(self._writer))
        except BaseException:
            self._writer.execute("ROLLBACK")
            raise
        self._writer.execute("COMMIT")
        return result

    async def write[T](self, operation: Operation[T]) -> T:
        """Queue one operation for the writer task and await its commit.

        Admission is decided once, here, and the decision cannot go stale:
        nothing suspends between reading ``_closed`` and enqueuing, so a write
        this accepted is in the queue before any ``close()`` can begin, and
        ``close()`` therefore sees every write it will ever have to refuse.

        That is what the queue being unbounded buys. A bounded one parked the
        producer in ``put`` instead, and ``close()`` frees a slot per entry it
        drains: parked producers woke afterwards, enqueued onto a queue whose
        consumer was already cancelled, and waited on it forever -- and any
        producer past the queue's size was never woken at all. Re-checking
        ``_closed`` after the ``put`` narrows that window without closing it,
        because it cannot reach a producer that is still parked.

        The bound was not paying for itself either. Every caller awaits its own
        write, so an entry exists only while a caller is suspended on it, and
        suspending that caller one await earlier holds the same operation in
        memory plus the machinery to park it.
        """
        if self._closed:
            raise RuntimeError(_CLOSED_MESSAGE)
        queue = self._ensure_writer_task()
        future: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        queue.put_nowait(_QueuedWrite(operation=operation, future=future))
        # Cancelling this await must not report an outcome the writer has not
        # reached yet: the statement runs on a thread regardless, so the caller
        # learns how it ended before its cancellation propagates.
        return await settled(future)

    async def read[T](self, operation: Operation[T]) -> T:
        """Run one read on a WAL reader, concurrently with the writer."""
        if self._closed:
            raise RuntimeError(_CLOSED_MESSAGE)

        def apply() -> T:
            return self._apply_read(operation)

        return await self._offload.run(apply)

    def _apply_read[T](self, operation: Operation[T]) -> T:
        return operation(_SqliteTransaction(self._reader()))

    async def close(self) -> None:
        """Stop the writer task and close every connection.

        Cancelling the writer task is safe only because ``_settle`` refuses to
        return while its worker thread is still executing: the cancellation
        ends the task after that statement, not during it. Awaiting the task is
        therefore also how this waits for the write in flight, and closing the
        connection cannot land underneath a live ``BEGIN IMMEDIATE`` -- which
        SQLite answers with a segmentation fault rather than an exception.

        Reads are not the writer task's to finish, so they are drained
        separately before the connections they run on are closed.

        Draining the queue once is enough because ``_closed`` is raised before
        it and ``write`` enqueues without suspending: every write that will
        ever be admitted is already in the queue by the time this reaches it,
        and every write after is refused where it starts.
        """
        if self._closed:
            return
        self._closed = True
        writer_task = self._writer_task
        self._writer_task = None
        if writer_task is not None:
            writer_task.cancel()
            try:  # noqa: SIM105 - the task may already be finished
                await writer_task
            except asyncio.CancelledError:
                pass
        queue = self._queue
        while queue is not None and not queue.empty():
            queued = queue.get_nowait()
            if not queued.future.done():
                queued.future.set_exception(RuntimeError(_CLOSED_MESSAGE))
            queue.task_done()
        await self._offload.drain()
        await asyncio.to_thread(self._writer.close)
        with self._reader_lock:
            readers = tuple(self._open_readers)
            self._open_readers.clear()
        for reader in readers:
            await asyncio.to_thread(reader.close)


def _report(future: asyncio.Future[Any], work: asyncio.Future[Any]) -> None:
    """Give the waiting caller the worker's own outcome."""
    if future.done():
        return
    if work.cancelled():
        future.cancel()
    elif (error := work.exception()) is not None:
        future.set_exception(error)
    else:
        future.set_result(work.result())


@dataclass(slots=True)
class _QueuedWrite:
    operation: Operation[Any]
    future: asyncio.Future[Any]
