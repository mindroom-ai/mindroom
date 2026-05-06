"""Small PostgreSQL cursor helpers for Matrix cache queries."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, LiteralString

if TYPE_CHECKING:
    from psycopg import AsyncConnection


async def fetchone(
    db: AsyncConnection,
    query: LiteralString,
    params: tuple[object, ...],
) -> tuple[Any, ...] | None:
    """Execute one query, fetch one row, and close the cursor."""
    cursor = await db.execute(query, params)
    try:
        return await cursor.fetchone()
    finally:
        await cursor.close()


async def fetchall(
    db: AsyncConnection,
    query: LiteralString,
    params: tuple[object, ...],
) -> list[tuple[Any, ...]]:
    """Execute one query, fetch all rows as tuples, and close the cursor."""
    cursor = await db.execute(query, params)
    try:
        rows = await cursor.fetchall()
        return [tuple(row) for row in rows]
    finally:
        await cursor.close()


async def rowcount(
    db: AsyncConnection,
    query: LiteralString,
    params: tuple[object, ...],
) -> int:
    """Execute one query, return its row count, and close the cursor."""
    cursor = await db.execute(query, params)
    try:
        return 0 if cursor.rowcount is None else int(cursor.rowcount)
    finally:
        await cursor.close()
