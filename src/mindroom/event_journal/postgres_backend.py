"""PostgreSQL backend implementing the same contract as SQLite.

The same SQL, the same transactions, the same operations. There is no second
application protocol here: if the two backends could diverge in behavior, the
parity tests would have nothing meaningful to compare.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, LiteralString, cast

import psycopg
from psycopg.rows import dict_row

from .offloading import ThreadOffload
from .schema import POSTGRES_DIALECT, render, schema_statements

# An arbitrary constant that only this schema setup uses, so the lock it
# takes cannot collide with an application advisory lock.
_SCHEMA_LOCK_KEY = 7_403_118_615_233_901

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .backend import Operation, Row


_POOL_SIZE = 4


def _statement(sql: str) -> LiteralString:
    """Return one rendered statement as the literal it actually is.

    Every statement originates in a module constant and is transformed only by
    swapping the parameter marker, so no caller-supplied text can reach it.
    The type checker cannot see through that transformation, and building the
    SQL any other way would mean giving up parameter binding.
    """
    return cast("LiteralString", render(sql, POSTGRES_DIALECT))


@dataclass(frozen=True, slots=True)
class _PostgresTransaction:
    """Statement execution against one open PostgreSQL connection."""

    cursor: psycopg.Cursor[dict[str, Any]]

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Run one statement."""
        self.cursor.execute(_statement(sql), tuple(params))

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        """Run one query and return its first row, if any."""
        self.cursor.execute(_statement(sql), tuple(params))
        return None if self.cursor.rowcount == 0 else self.cursor.fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> tuple[Row, ...]:
        """Run one query and return every row."""
        self.cursor.execute(_statement(sql), tuple(params))
        return tuple(self.cursor.fetchall())


@dataclass
class PostgresBackend:
    """A PostgreSQL store with a serialized writer and pooled readers."""

    # Kept out of the repr because a DSN carries a password, and a dataclass
    # repr reaches logs and tracebacks without anyone choosing to print it.
    database_url: str = field(repr=False)
    _writer: psycopg.Connection[tuple[Any, ...]] = field(init=False, repr=False)
    _writer_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _readers: asyncio.Queue[psycopg.Connection[tuple[Any, ...]]] | None = field(default=None, init=False, repr=False)
    _pool: list[psycopg.Connection[tuple[Any, ...]]] = field(default_factory=list, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _offload: ThreadOffload = field(default_factory=ThreadOffload, init=False, repr=False)

    @classmethod
    def open(cls, database_url: str) -> PostgresBackend:
        """Create the schema and open the connections.

        Synchronous for the same reason as the SQLite backend: a bot builds
        its collaborators before it has an event loop.
        """
        backend = cls(database_url=database_url)
        backend._writer = backend._connect()
        backend._create_schema()
        backend._pool = [backend._connect() for _ in range(_POOL_SIZE)]
        return backend

    def _readers_queue(self) -> asyncio.Queue[psycopg.Connection[tuple[Any, ...]]]:
        """Bind the reader pool to the loop that first reads."""
        if self._readers is None:
            self._readers = asyncio.Queue()
            for connection in self._pool:
                self._readers.put_nowait(connection)
        return self._readers

    def _connect(self) -> psycopg.Connection[tuple[Any, ...]]:
        # The row factory is chosen per cursor rather than per connection: it
        # is the only place both psycopg and the type checker agree on the
        # resulting row type.
        return psycopg.connect(self.database_url, autocommit=False)

    def _create_schema(self) -> None:
        with self._writer.cursor(row_factory=dict_row) as cursor:
            # `IF NOT EXISTS` is not itself race-free: two processes creating
            # the same table or index at once can still collide in the system
            # catalogue, and PostgreSQL resolves that by failing one of them.
            # Serializing the whole of schema setup on a transaction-scoped
            # advisory lock costs nothing at steady state, because the
            # statements are all no-ops once the schema exists.
            cursor.execute(cast("LiteralString", f"SELECT pg_advisory_xact_lock({_SCHEMA_LOCK_KEY})"))
            for statement in schema_statements(POSTGRES_DIALECT):
                cursor.execute(cast("LiteralString", statement))
        self._writer.commit()

    async def write[T](self, operation: Operation[T]) -> T:
        """Run one operation in a serialized write transaction and commit it.

        The lock is what makes the shared writer connection safe, so it may not
        be released while a statement is still executing on that connection.
        A bare ``await asyncio.to_thread`` released it on cancellation: the next
        write then ran inside the cancelled one's still-open transaction, and
        whichever of the two reached ``commit`` or ``rollback`` first decided
        the fate of both.
        """
        if self._closed:
            msg = "The event-journal store is closed"
            raise RuntimeError(msg)

        def apply() -> T:
            return self._apply_write(operation)

        async with self._writer_lock:
            # Waiting for the lock is a suspension, so the store may have been
            # closed since the check above and this connection may be gone.
            if self._closed:
                msg = "The event-journal store is closed"
                raise RuntimeError(msg)
            return await self._offload.run(apply)

    def _apply_write[T](self, operation: Operation[T]) -> T:
        try:
            with self._writer.cursor(row_factory=dict_row) as cursor:
                result = operation(_PostgresTransaction(cursor))
        except BaseException:
            self._writer.rollback()
            raise
        self._writer.commit()
        return result

    async def read[T](self, operation: Operation[T]) -> T:
        """Run one operation on a pooled reader.

        The connection goes back to the pool only once its statement has
        stopped. Returning it while a cancelled read's thread was still using
        it handed a live connection to the next reader.
        """
        if self._closed:
            msg = "The event-journal store is closed"
            raise RuntimeError(msg)
        connection = await self._readers_queue().get()

        def apply() -> T:
            return self._apply_read(connection, operation)

        try:
            # An exhausted pool makes the line above a suspension, so the store
            # may have been closed while this read waited for a connection.
            if self._closed:
                msg = "The event-journal store is closed"
                raise RuntimeError(msg)
            return await self._offload.run(apply)
        finally:
            self._readers_queue().put_nowait(connection)

    @staticmethod
    def _apply_read[T](
        connection: psycopg.Connection[tuple[Any, ...]],
        operation: Operation[T],
    ) -> T:
        try:
            with connection.cursor(row_factory=dict_row) as cursor:
                return operation(_PostgresTransaction(cursor))
        finally:
            connection.rollback()

    async def close(self) -> None:
        """Close every connection this backend owns, once nothing is using one.

        Setting the closed flag first stops any further statement from
        starting; draining then waits for the ones already on worker threads,
        because closing a connection under a running statement is how psycopg
        reports someone else's cancellation as this caller's broken connection.
        """
        if self._closed:
            return
        self._closed = True
        await self._offload.drain()
        for connection in (self._writer, *self._pool):
            await asyncio.to_thread(connection.close)
        self._pool.clear()
