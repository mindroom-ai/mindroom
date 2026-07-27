"""SQLite binding for the shared thread-cache operations.

The operations themselves live in ``event_cache_thread_ops`` and the statements in
``event_cache_thread_statements``. What is left here is what SQLite genuinely does differently:
its cursor handling, and its write-sequence allocation, which reads a counter row because SQLite
has no sequence object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from .event_cache_sql_dialect import SQLITE_DIALECT
from .event_cache_thread_statements import ThreadStatements
from .sqlite_event_cache_events import (
    allocate_write_sequences,
    delete_cached_events,
    delete_event_edit_rows,
    delete_event_thread_rows,
    event_or_original_is_redacted,
    filter_cacheable_events,
    write_lookup_index_rows,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence

    import aiosqlite

    from .event_cache_events import SerializedCachedEvent
    from .event_cache_thread_ops import ThreadBackend

_STATEMENTS: Final = ThreadStatements.build(SQLITE_DIALECT)

# SQLite has no array parameter, so a bulk membership delete is one statement per event ID.
_DELETE_THREAD_MEMBERSHIP_BY_EVENT_ID: Final = """
DELETE FROM thread_events
WHERE principal_id = :scope AND room_id = :room_id AND event_id = :event_id
"""


class _SqliteThreadBackend:
    """Run the shared thread-cache operations against an aiosqlite connection."""

    statements = _STATEMENTS

    async def execute(
        self,
        db: aiosqlite.Connection,
        sql: str,
        params: Mapping[str, object],
    ) -> None:
        """Run one statement that returns no rows."""
        await db.execute(sql, params)

    async def fetchone(
        self,
        db: aiosqlite.Connection,
        sql: str,
        params: Mapping[str, object],
    ) -> tuple[Any, ...] | None:
        """Run one query and return its first row, if any."""
        cursor = await db.execute(sql, params)
        try:
            row = await cursor.fetchone()
        finally:
            await cursor.close()
        return None if row is None else tuple(row)

    async def fetchall(
        self,
        db: aiosqlite.Connection,
        sql: str,
        params: Mapping[str, object],
    ) -> list[tuple[Any, ...]]:
        """Run one query and return every row."""
        cursor = await db.execute(sql, params)
        try:
            rows = await cursor.fetchall()
        finally:
            await cursor.close()
        return [tuple(row) for row in rows]

    async def upsert_thread_membership_rows(
        self,
        db: aiosqlite.Connection,
        rows: Sequence[Mapping[str, object]],
    ) -> None:
        """Write thread-membership rows, drawing each row's write sequence from the counter row."""
        if not rows:
            return
        write_sequences = await allocate_write_sequences(db, len(rows))
        await db.executemany(
            self.statements.upsert_thread_membership,
            [{**row, "write_seq": write_sequence} for row, write_sequence in zip(rows, write_sequences, strict=True)],
        )

    async def delete_thread_membership_by_event_ids(
        self,
        db: aiosqlite.Connection,
        scope: str,
        room_id: str,
        event_ids: Sequence[str],
    ) -> None:
        """Delete thread-membership rows for an explicit list of event IDs."""
        await db.executemany(
            _DELETE_THREAD_MEMBERSHIP_BY_EVENT_ID,
            [{"scope": scope, "room_id": room_id, "event_id": event_id} for event_id in event_ids],
        )

    async def filter_cacheable_events(
        self,
        db: aiosqlite.Connection,
        scope: str,
        room_id: str,
        candidates: list[tuple[str, dict[str, Any]]],
    ) -> list[tuple[str, dict[str, Any]]]:
        """Drop candidates this cache must refuse to store."""
        return await filter_cacheable_events(db, scope, room_id, candidates)

    async def write_lookup_index_rows(
        self,
        db: aiosqlite.Connection,
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
            principal_id=scope,
            room_id=room_id,
            serialized_events=serialized_events,
            cached_at=cached_at,
            thread_id=thread_id,
        )

    async def delete_cached_events(
        self,
        db: aiosqlite.Connection,
        scope: str,
        room_id: str,
        event_ids: Sequence[str],
    ) -> None:
        """Delete point-lookup payload rows for these event IDs."""
        await delete_cached_events(db, principal_id=scope, room_id=room_id, event_ids=list(event_ids))

    async def delete_event_edit_rows(
        self,
        db: aiosqlite.Connection,
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
        db: aiosqlite.Connection,
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
        db: aiosqlite.Connection,
        scope: str,
        room_id: str,
        *,
        event_id: str,
        event: dict[str, Any],
    ) -> bool:
        """Return whether this event, or the event it edits, is redacted."""
        return await event_or_original_is_redacted(db, scope, room_id, event_id=event_id, event=event)


BACKEND: Final[ThreadBackend[aiosqlite.Connection]] = _SqliteThreadBackend()
