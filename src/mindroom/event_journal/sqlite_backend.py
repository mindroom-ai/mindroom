"""SQLite backend: one queued writer per backend, readers in WAL.

The runtime shares its backend across bots and thread exports, so their writes
are serialized by one writer task, which commits the writes queued at once in
one transaction. ``mindroom threads export`` calls the running API and uses
that same writer. Separate processes have separate queues and rely on SQLite's
busy timeout to wait for the database write lock.

If batch admission fails, its writes roll back and the Nio batch remains
unacknowledged for retry. A successful application commit must reach disk before
Nio is told it can release the batch.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.file_locks import release_file_lock, try_exclusive_file_lock
from mindroom.logging_config import get_logger

from .legacy_response_attempts import upgrade_continuation_identity
from .legacy_schema import (
    upgrade_legacy_journal,
    upgrade_outbox_reply_rows,
)
from .offloading import ThreadOffload, settled
from .schema import OUTBOX_TABLE, SQLITE_DIALECT, render, schema_statements
from .write_queue import CLOSED_MESSAGE, WriteOutcome, WriteQueue

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import TextIO

    from .backend import Operation, Row


logger = get_logger(__name__)

_BUSY_TIMEOUT_MILLISECONDS = 10_000
# How long to wait between attempts at the one statement SQLite's own busy
# handler will not retry. Short enough that a contended open is not noticeably
# slower than an uncontended one, long enough not to spin.
_WAL_RETRY_SECONDS = 0.05
# The first SQLite release with a built-in ``octet_length``.
_OCTET_LENGTH_VERSION = (3, 43, 0)


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


def _octet_length(value: str | bytes | None) -> int | None:
    """Return a stored value's size in bytes, as SQLite 3.43's ``octet_length`` does."""
    if value is None:
        return None
    return len(value.encode() if isinstance(value, str) else value)


def _configure(connection: sqlite3.Connection, *, synchronous: str) -> None:
    """Open one connection onto the journal, durable as far as its role needs.

    ``synchronous`` is asked for rather than assumed because the writer and the
    readers do not need the same thing, and the difference is expensive in one
    direction and unsafe in the other.

    ``busy_timeout`` is set before anything that can meet another process, so
    every statement below it waits rather than failing on the spot.
    """
    connection.row_factory = sqlite3.Row
    if sqlite3.sqlite_version_info < _OCTET_LENGTH_VERSION:
        # Reads size rows with ``octet_length``, which older libraries lack.
        # This stand-in loads each value to measure it, as ``length`` would.
        connection.create_function("octet_length", 1, _octet_length, deterministic=True)
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
    _writes: WriteQueue = field(init=False, repr=False)
    _close_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _open_readers: list[sqlite3.Connection] = field(default_factory=list, init=False, repr=False)
    # The claim of one runtime on this database file, held by an open descriptor.
    _hold: TextIO | None = field(default=None, init=False, repr=False)
    _reader_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _offload: ThreadOffload = field(default_factory=ThreadOffload, init=False, repr=False)
    _recovery_offload: ThreadOffload = field(
        default_factory=lambda: ThreadOffload.serial(
            thread_name_prefix="mindroom-event-journal-recovery",
        ),
        init=False,
        repr=False,
    )

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
        backend._writes = WriteQueue(
            apply=backend._apply,
            offload=backend._offload,
            task_name=f"event_journal_sqlite_writer_{database_path.name}",
        )
        return backend

    def _connect_writer(self) -> sqlite3.Connection:
        # The writer runs on whichever owned-pool thread is free, so the
        # connection has to outlive its creating thread. Only ever one write is
        # in flight, because a single task drains the queue.
        connection = sqlite3.connect(
            self.database_path,
            isolation_level=None,
            check_same_thread=False,
        )
        # Application effects must reach disk before the ingestion pump
        # acknowledges their Nio batch. Under synchronous=NORMAL, a host reset
        # could lose committed WAL frames after Nio has released that batch.
        # FULL makes the application commit durable before acknowledgement.
        # Readers commit nothing, so this is the writer's cost alone.
        _configure(connection, synchronous="FULL")
        connection.execute("BEGIN IMMEDIATE")
        try:
            existing_tables = frozenset(
                str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            )
            upgrade_legacy_journal(_SqliteTransaction(connection), existing_tables)
            outbox_columns = frozenset(
                str(row[1]) for row in connection.execute("PRAGMA table_info(matrix_delivery_outbox)")
            )
            upgrade_outbox_reply_rows(
                _SqliteTransaction(connection),
                outbox_columns,
                outbox_table_ddl=OUTBOX_TABLE,
                sqlite=True,
            )
            continuation_columns = frozenset(
                str(row[1]) for row in connection.execute("PRAGMA table_info(approval_continuations)")
            )
            upgrade_continuation_identity(_SqliteTransaction(connection), existing_tables, continuation_columns)
            for statement in schema_statements(SQLITE_DIALECT):
                connection.execute(statement)
            connection.execute("COMMIT")
        except BaseException:
            connection.close()
            raise
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

    def _apply(self, operations: list[Operation[Any]]) -> list[WriteOutcome]:
        """Commit queued writes in one transaction, each in a savepoint so one that fails rolls back alone."""
        self._writer.execute("BEGIN IMMEDIATE")
        try:
            outcomes = [self._apply_one(operation) for operation in operations]
            self._writer.execute("COMMIT")
        except BaseException:
            # SQLite rolls the whole transaction back itself on some errors.
            if self._writer.in_transaction:
                self._writer.execute("ROLLBACK")
            raise
        return outcomes

    def _apply_one(self, operation: Operation[Any]) -> WriteOutcome:
        self._writer.execute("SAVEPOINT journal_write")
        try:
            result = operation(_SqliteTransaction(self._writer))
        except Exception as error:
            if not self._writer.in_transaction:
                # The error took the whole transaction, and the batch with it.
                raise
            self._writer.execute("ROLLBACK TO journal_write")
            self._writer.execute("RELEASE journal_write")
            return WriteOutcome(error=error)
        self._writer.execute("RELEASE journal_write")
        return WriteOutcome(result=result)

    async def write[T](self, operation: Operation[T]) -> T:
        """Queue one operation for the writer task and await its commit."""
        return await self._writes.write(operation)

    async def read[T](self, operation: Operation[T]) -> T:
        """Run one read on a WAL reader, concurrently with the writer."""
        if self._writes.closed:
            raise RuntimeError(CLOSED_MESSAGE)

        def apply() -> T:
            return self._apply_read(operation)

        return await self._offload.run(apply)

    async def recovery_read[T](self, operation: Operation[T]) -> T:
        """Run a committed-state handoff proof on its reserved WAL reader."""
        if self._writes.closed:
            raise RuntimeError(CLOSED_MESSAGE)

        def apply() -> T:
            connection = self._reader()
            connection.execute("BEGIN")
            try:
                return operation(_SqliteTransaction(connection))
            finally:
                connection.execute("ROLLBACK")

        return await self._recovery_offload.run(apply)

    def _apply_read[T](self, operation: Operation[T]) -> T:
        return operation(_SqliteTransaction(self._reader()))

    async def close(self) -> None:
        """Close admission once and await the one owned connection teardown.

        Stopping the writer task waits for the write in flight, so closing the
        connection cannot land underneath a live ``BEGIN IMMEDIATE`` -- which
        SQLite answers with a segmentation fault rather than an exception.

        Reads are not the writer task's to finish, so they are drained
        separately before the connections they run on are closed.
        """
        close_task = self._close_task
        if close_task is None:
            self._writes.close()
            close_task = asyncio.create_task(
                self._finish_close(),
                name="event_journal_sqlite_close",
            )
            self._close_task = close_task
        await settled(close_task)

    async def hold_exclusively(self, identity: str) -> bool:
        """Claim this database file for one runtime; the operating system withdraws it if the process dies."""
        del identity
        if self._hold is None:
            self._hold = try_exclusive_file_lock(
                self.database_path.with_name(f"{self.database_path.name}.runtime.lock"),
            )
        return self._hold is not None

    async def still_held(self) -> bool:
        """Return whether this runtime holds the database; an open descriptor holds it until close."""
        return self._hold is not None

    async def _finish_close(self) -> None:
        """Finish the teardown every close waiter shares; the hold goes last, once nothing can write."""
        await self._writes.stop()
        try:
            await asyncio.gather(
                self._offload.drain(),
                self._recovery_offload.drain(),
            )
            await self._offload.run(self._writer.close)
            with self._reader_lock:
                readers = tuple(self._open_readers)
                self._open_readers.clear()
            for reader in readers:
                await self._offload.run(reader.close)
        finally:
            self._offload.shutdown()
            self._recovery_offload.shutdown()
            hold, self._hold = self._hold, None
            if hold is not None:
                release_file_lock(hold)
