"""The storage contract the journal, projection, and outbox are written against.

Every durable operation is a plain synchronous function of one transaction.
Admission needs its journal insert, membership check, and projection update to
commit together, so the unit of durability is the transaction rather than the
individual statement, and both backends implement exactly that.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence


type _Params = Sequence[Any]
type Row = Mapping[str, Any]


class Transaction(Protocol):
    """Statement execution inside one committed unit of work."""

    def execute(self, sql: str, params: _Params = ()) -> None:
        """Run one statement."""
        ...

    def fetchone(self, sql: str, params: _Params = ()) -> Row | None:
        """Run one query and return its first row, if any."""
        ...

    def fetchall(self, sql: str, params: _Params = ()) -> tuple[Row, ...]:
        """Run one query and return every row."""
        ...


type Operation[T] = Callable[[Transaction], T]


class Backend(Protocol):
    """A durable store that can run read and write transactions."""

    async def write[T](self, operation: Operation[T]) -> T:
        """Run one operation in a serialized write transaction and commit it."""
        ...

    async def read[T](self, operation: Operation[T]) -> T:
        """Run one operation in a read transaction."""
        ...

    async def close(self) -> None:
        """Release every connection this backend owns."""
        ...
