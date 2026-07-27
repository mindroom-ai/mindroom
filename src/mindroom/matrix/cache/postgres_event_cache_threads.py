"""PostgreSQL binding for the shared thread-cache operations.

The operations themselves live in ``event_cache_thread_ops`` and the statements in
``event_cache_thread_statements``. What is left here is what PostgreSQL genuinely does
differently: its cursor handling, its array-valued bulk delete, and a write-sequence column that
defaults from ``nextval`` rather than being allocated in Python.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final, cast

from .event_cache_sql_dialect import POSTGRES_DIALECT
from .event_cache_thread_statements import ThreadStatements
from .postgres_event_cache_events import (
    delete_cached_events,
    delete_event_edit_rows,
    delete_event_thread_rows,
    event_or_original_is_redacted,
    filter_cacheable_events,
    write_lookup_index_rows,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from typing import LiteralString

    from psycopg import AsyncConnection

    from .event_cache_events import SerializedCachedEvent
    from .event_cache_thread_ops import ThreadBackend

_STATEMENTS: Final = ThreadStatements.build(POSTGRES_DIALECT)

_DELETE_THREAD_MEMBERSHIP_BY_EVENT_IDS: Final = """
DELETE FROM mindroom_event_cache_thread_events
WHERE namespace = %(scope)s AND room_id = %(room_id)s AND event_id = ANY(%(event_ids)s)
"""


def _query(sql: str) -> LiteralString:
    """Narrow a rendered statement to the query type psycopg accepts.

    Every statement reaching here is assembled by ``ThreadStatements`` from a closed set of literal
    table names, column names, and parameter markers. Caller values stay bound, so no runtime data
    reaches the text and the ``LiteralString`` contract holds even though the string is built at
    import time rather than written inline.
    """
    return cast("LiteralString", sql)


class _PostgresThreadBackend:
    """Run the shared thread-cache operations against a psycopg connection."""

    statements = _STATEMENTS

    async def execute(
        self,
        db: AsyncConnection,
        sql: str,
        params: Mapping[str, object],
    ) -> None:
        """Run one statement that returns no rows."""
        await db.execute(_query(sql), params)

    async def fetchone(
        self,
        db: AsyncConnection,
        sql: str,
        params: Mapping[str, object],
    ) -> tuple[Any, ...] | None:
        """Run one query and return its first row, if any."""
        cursor = await db.execute(_query(sql), params)
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        return None if row is None else tuple(row)

    async def fetchall(
        self,
        db: AsyncConnection,
        sql: str,
        params: Mapping[str, object],
    ) -> list[tuple[Any, ...]]:
        """Run one query and return every row."""
        cursor = await db.execute(_query(sql), params)
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        return [tuple(row) for row in rows]

    async def upsert_thread_membership_rows(
        self,
        db: AsyncConnection,
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Write thread-membership rows, letting the column default draw each write sequence."""
        for row in rows:
            await db.execute(_query(self.statements.upsert_thread_membership), row)

    async def delete_thread_membership_by_event_ids(
        self,
        db: AsyncConnection,
        scope: str,
        room_id: str,
        event_ids: Sequence[str],
    ) -> None:
        """Delete thread-membership rows for an explicit list of event IDs."""
        await db.execute(
            _query(_DELETE_THREAD_MEMBERSHIP_BY_EVENT_IDS),
            {"scope": scope, "room_id": room_id, "event_ids": list(event_ids)},
        )

    async def filter_cacheable_events(
        self,
        db: AsyncConnection,
        scope: str,
        room_id: str,
        candidates: list[tuple[str, dict[str, Any]]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Drop candidates this cache must refuse to store."""
        return await filter_cacheable_events(db, scope, room_id, candidates)

    async def write_lookup_index_rows(
        self,
        db: AsyncConnection,
        scope: str,
        room_id: str,
        *,
        serialized_events: list[SerializedCachedEvent],
        cached_at: float,
        thread_id: str | None = None,
    ) -> None:
        """Persist point-lookup, edit-index, and thread-index rows for cached events."""
        await write_lookup_index_rows(
            db,
            namespace=scope,
            room_id=room_id,
            serialized_events=serialized_events,
            cached_at=cached_at,
            thread_id=thread_id,
        )

    async def delete_cached_events(
        self,
        db: AsyncConnection,
        scope: str,
        room_id: str,
        event_ids: Sequence[str],
    ) -> None:
        """Delete point-lookup payload rows for these event IDs."""
        await delete_cached_events(db, namespace=scope, room_id=room_id, event_ids=list(event_ids))

    async def delete_event_edit_rows(
        self,
        db: AsyncConnection,
        scope: str,
        room_id: str,
        *,
        event_ids: Sequence[str],
        original_event_id: str | None,
    ) -> None:
        """Delete edit-index rows for these event IDs."""
        await delete_event_edit_rows(
            db,
            scope,
            room_id,
            event_ids=list(event_ids),
            original_event_id=original_event_id,
        )

    async def delete_event_thread_rows(
        self,
        db: AsyncConnection,
        scope: str,
        room_id: str,
        *,
        event_ids: Sequence[str],
        current_self_root_ids: Collection[str] = (),
    ) -> None:
        """Delete thread-index rows for these event IDs."""
        await delete_event_thread_rows(
            db,
            scope,
            room_id,
            event_ids=list(event_ids),
            current_self_root_ids=current_self_root_ids,
        )

    async def event_or_original_is_redacted(
        self,
        db: AsyncConnection,
        scope: str,
        room_id: str,
        *,
        event_id: str,
        event: dict[str, Any],
    ) -> bool:
        """Return whether this event, or the event it edits, is redacted."""
        return await event_or_original_is_redacted(db, scope, room_id, event_id=event_id, event=event)


BACKEND: Final[ThreadBackend[AsyncConnection]] = _PostgresThreadBackend()
