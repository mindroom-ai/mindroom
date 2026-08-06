"""PostgreSQL backend implementing the same contract as SQLite.

The same SQL, the same transactions, the same operations. There is no second
application protocol here: if the two backends could diverge in behavior, the
parity tests would have nothing meaningful to compare.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

import psycopg
from psycopg.rows import dict_row

from .schema import POSTGRES_DIALECT, Dialect, render, schema_statements

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .backend import Operation, Row

T = TypeVar("T")

_POOL_SIZE = 4


@dataclass(frozen=True, slots=True)
class _PostgresTransaction:
    """Statement execution against one open PostgreSQL connection."""

    cursor: psycopg.Cursor[dict[str, Any]]

    @property
    def dialect(self) -> Dialect:
        """Return the PostgreSQL spelling."""
        return POSTGRES_DIALECT

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        """Run one statement."""
        self.cursor.execute(render(sql, POSTGRES_DIALECT), tuple(params))

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Row | None:
        """Run one query and return its first row, if any."""
        self.cursor.execute(render(sql, POSTGRES_DIALECT), tuple(params))
        return None if self.cursor.rowcount == 0 else self.cursor.fetchone()

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> tuple[Row, ...]:
        """Run one query and return every row."""
        self.cursor.execute(render(sql, POSTGRES_DIALECT), tuple(params))
        return tuple(self.cursor.fetchall())


@dataclass
class PostgresBackend:
    """A PostgreSQL store with a serialized writer and pooled readers."""

    database_url: str
    _writer: psycopg.Connection[dict[str, Any]] = field(init=False, repr=False)
    _writer_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _readers: asyncio.Queue[psycopg.Connection[dict[str, Any]]] = field(init=False, repr=False)
    _all_readers: list[psycopg.Connection[dict[str, Any]]] = field(default_factory=list, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def dialect(self) -> Dialect:
        """Return the PostgreSQL spelling."""
        return POSTGRES_DIALECT

    @classmethod
    async def open(cls, database_url: str) -> PostgresBackend:
        """Create the schema and open the connections."""
        backend = cls(database_url=database_url)
        await backend._start()
        return backend

    async def _start(self) -> None:
        self._writer = await asyncio.to_thread(self._connect)
        await asyncio.to_thread(self._create_schema)
        self._readers = asyncio.Queue()
        for _ in range(_POOL_SIZE):
            connection = await asyncio.to_thread(self._connect)
            self._all_readers.append(connection)
            self._readers.put_nowait(connection)

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.database_url, row_factory=dict_row, autocommit=False)

    def _create_schema(self) -> None:
        with self._writer.cursor() as cursor:
            for statement in schema_statements(POSTGRES_DIALECT):
                cursor.execute(statement)
        self._writer.commit()

    async def write(self, operation: Operation[T]) -> T:
        """Run one operation in a serialized write transaction and commit it."""
        if self._closed:
            msg = "The event-journal store is closed"
            raise RuntimeError(msg)
        async with self._writer_lock:
            return await asyncio.to_thread(self._apply_write, operation)

    def _apply_write(self, operation: Operation[T]) -> T:
        try:
            with self._writer.cursor() as cursor:
                result = operation(_PostgresTransaction(cursor))
        except BaseException:
            self._writer.rollback()
            raise
        self._writer.commit()
        return result

    async def read(self, operation: Operation[T]) -> T:
        """Run one operation on a pooled reader."""
        if self._closed:
            msg = "The event-journal store is closed"
            raise RuntimeError(msg)
        connection = await self._readers.get()
        try:
            return await asyncio.to_thread(self._apply_read, connection, operation)
        finally:
            self._readers.put_nowait(connection)

    @staticmethod
    def _apply_read(
        connection: psycopg.Connection[dict[str, Any]],
        operation: Operation[T],
    ) -> T:
        try:
            with connection.cursor() as cursor:
                return operation(_PostgresTransaction(cursor))
        finally:
            connection.rollback()

    async def close(self) -> None:
        """Close every connection this backend owns."""
        if self._closed:
            return
        self._closed = True
        for connection in (self._writer, *self._all_readers):
            await asyncio.to_thread(connection.close)
        self._all_readers.clear()
