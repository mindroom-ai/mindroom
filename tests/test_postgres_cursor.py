"""Tests for shared PostgreSQL cursor helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, LiteralString, cast

import pytest

from mindroom.matrix.cache import postgres_cursor

if TYPE_CHECKING:
    from psycopg import AsyncConnection


@dataclass(slots=True)
class _FakeCursor:
    fetchone_result: tuple[Any, ...] | None = None
    fetchall_result: list[list[Any] | tuple[Any, ...]] | None = None
    rowcount: int | None = None
    fetchall_error: Exception | None = None
    closed: bool = False

    async def fetchone(self) -> tuple[Any, ...] | None:
        return self.fetchone_result

    async def fetchall(self) -> list[list[Any] | tuple[Any, ...]]:
        if self.fetchall_error is not None:
            raise self.fetchall_error
        return [] if self.fetchall_result is None else self.fetchall_result

    async def close(self) -> None:
        self.closed = True


@dataclass(slots=True)
class _FakeConnection:
    cursor: _FakeCursor

    async def execute(self, query: LiteralString, params: tuple[object, ...]) -> _FakeCursor:
        assert query == "SELECT %s"
        assert params == ("value",)
        return self.cursor


def _db(cursor: _FakeCursor) -> AsyncConnection:
    return cast("AsyncConnection", _FakeConnection(cursor))


@pytest.mark.asyncio
async def test_postgres_cursor_fetchone_closes_cursor() -> None:
    """Fetchone should close its cursor after returning one row."""
    cursor = _FakeCursor(fetchone_result=("row",))

    row = await postgres_cursor.fetchone(_db(cursor), "SELECT %s", ("value",))

    assert row == ("row",)
    assert cursor.closed is True


@pytest.mark.asyncio
async def test_postgres_cursor_fetchall_normalizes_rows_and_closes_cursor() -> None:
    """Fetchall should return tuple rows and close its cursor."""
    cursor = _FakeCursor(fetchall_result=[["a", 1], ("b", 2)])

    rows = await postgres_cursor.fetchall(_db(cursor), "SELECT %s", ("value",))

    assert rows == [("a", 1), ("b", 2)]
    assert cursor.closed is True


@pytest.mark.asyncio
async def test_postgres_cursor_fetchall_closes_cursor_after_error() -> None:
    """Fetchall should close its cursor when fetching raises."""
    cursor = _FakeCursor(fetchall_error=RuntimeError("fetch failed"))

    with pytest.raises(RuntimeError, match="fetch failed"):
        await postgres_cursor.fetchall(_db(cursor), "SELECT %s", ("value",))

    assert cursor.closed is True


@pytest.mark.asyncio
async def test_postgres_cursor_rowcount_normalizes_none_and_closes_cursor() -> None:
    """Rowcount should return zero for missing row counts and close its cursor."""
    cursor = _FakeCursor(rowcount=None)

    count = await postgres_cursor.rowcount(_db(cursor), "SELECT %s", ("value",))

    assert count == 0
    assert cursor.closed is True
