"""Redaction-safe PostgreSQL archival for superseded nonterminal streaming edits."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .cache_maintenance import (
    NONTERMINAL_STREAM_STATUSES,
    TERMINAL_STREAM_STATUSES,
    decompress_event_payload,
)
from .postgres_cursor import fetchall, fetchone, rowcount

_COMPACTION_BATCH_SIZE = 500

if TYPE_CHECKING:
    from psycopg import AsyncConnection


@dataclass(frozen=True, slots=True)
class _ArchivedPostgresStreamingEdit:
    """One compressed edit with ordering metadata loaded from its stable index."""

    event_id: str
    room_id: str
    sender: str
    origin_server_ts: int
    event_json_zlib: bytes
    cached_at: float
    event_order: int

    def event_payload(self) -> dict[str, Any]:
        """Return the archived JSON payload."""
        return _decompress_event(self.event_json_zlib)


type _ArchivedEditRow = tuple[
    str,
    str,
    str,
    int,
    bytes,
    float,
    int,
]


def _decompress_event(event_json_zlib: bytes) -> dict[str, Any]:
    return decompress_event_payload(event_json_zlib, backend="PostgreSQL")


def _archived_edit_from_row(row: object) -> _ArchivedPostgresStreamingEdit:
    values = cast("_ArchivedEditRow", row)
    return _ArchivedPostgresStreamingEdit(
        event_id=values[0],
        room_id=values[1],
        sender=values[2],
        origin_server_ts=values[3],
        event_json_zlib=values[4],
        cached_at=values[5],
        event_order=values[6],
    )


async def load_archived_event(
    db: AsyncConnection,
    *,
    namespace: str,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one compacted event for point lookup and late-redaction resolution."""
    row = await fetchone(
        db,
        """
        SELECT event_json_zlib
        FROM mindroom_event_cache_compacted_streaming_edits
        WHERE namespace = %s AND event_id = %s
        """,
        (namespace, event_id),
    )
    return None if row is None else _decompress_event(bytes(row[0]))


async def load_latest_archived_edit(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    original_event_id: str,
    sender: str | None,
) -> _ArchivedPostgresStreamingEdit | None:
    """Return the latest compacted edit in one visible replacement partition."""
    sender_predicate = "" if sender is None else "AND archived.sender = %s"
    parameters = (namespace, room_id, original_event_id, *((sender,) if sender is not None else ()))
    row = await fetchone(
        db,
        f"""
        SELECT
            archived.event_id,
            archived.room_id,
            archived.sender,
            edits.origin_server_ts,
            archived.event_json_zlib,
            archived.cached_at,
            archived.event_order
        FROM mindroom_event_cache_compacted_streaming_edits AS archived
        JOIN mindroom_event_cache_event_edits AS edits
            ON edits.namespace = archived.namespace
            AND edits.edit_event_id = archived.event_id
            AND edits.room_id = archived.room_id
        WHERE archived.namespace = %s
            AND archived.room_id = %s
            AND edits.original_event_id = %s
            {sender_predicate}
        ORDER BY edits.origin_server_ts DESC, archived.event_order DESC
        LIMIT 1
        """,  # noqa: S608
        parameters,
    )
    return None if row is None else _archived_edit_from_row(row)


async def load_archived_thread_events(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    thread_id: str,
) -> list[tuple[int, int, dict[str, Any]]]:
    """Return compacted snapshot members with their stable membership order."""
    rows = await fetchall(
        db,
        """
        SELECT membership.origin_server_ts, membership.write_seq, archived.event_json_zlib
        FROM mindroom_event_cache_thread_events AS membership
        JOIN mindroom_event_cache_compacted_streaming_edits AS archived
            ON archived.namespace = membership.namespace
            AND archived.event_id = membership.event_id
            AND archived.room_id = membership.room_id
        WHERE membership.namespace = %s AND membership.room_id = %s AND membership.thread_id = %s
        ORDER BY membership.origin_server_ts, membership.write_seq
        """,
        (namespace, room_id, thread_id),
    )
    return [(int(row[0]), int(row[1]), _decompress_event(bytes(row[2]))) for row in rows]


async def delete_archived_events(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
    event_ids: list[str],
) -> int:
    """Delete compacted fallback payloads for redacted or replaced event IDs."""
    if not event_ids:
        return 0
    return await rowcount(
        db,
        """
        DELETE FROM mindroom_event_cache_compacted_streaming_edits
        WHERE namespace = %s AND room_id = %s AND event_id = ANY(%s)
        """,
        (namespace, room_id, event_ids),
    )


async def _compaction_candidates(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str | None,
    limit: int,
) -> list[_ArchivedPostgresStreamingEdit]:
    room_predicate = "" if room_id is None else "AND nonterminal_index.room_id = %s"
    parameters = (
        namespace,
        sorted(NONTERMINAL_STREAM_STATUSES),
        sorted(TERMINAL_STREAM_STATUSES),
        *((room_id,) if room_id is not None else ()),
        limit,
    )
    rows = await fetchall(
        db,
        f"""
        SELECT
            nonterminal_event.event_id,
            nonterminal_event.room_id,
            nonterminal_event.event_json::jsonb ->> 'sender',
            nonterminal_event.origin_server_ts,
            nonterminal_event.event_json,
            nonterminal_event.cached_at,
            nonterminal_event.write_seq
        FROM mindroom_event_cache_event_edits AS nonterminal_index
        JOIN mindroom_event_cache_events AS nonterminal_event
            ON nonterminal_event.namespace = nonterminal_index.namespace
            AND nonterminal_event.event_id = nonterminal_index.edit_event_id
            AND nonterminal_event.room_id = nonterminal_index.room_id
        WHERE nonterminal_index.namespace = %s
            AND nonterminal_event.event_json::jsonb ->> 'type' = 'm.room.message'
            AND jsonb_typeof(nonterminal_event.event_json::jsonb -> 'sender') = 'string'
            AND nonterminal_event.event_json::jsonb
                -> 'content' -> 'm.new_content' ->> 'io.mindroom.stream_status' = ANY(%s)
            AND EXISTS (
                SELECT 1
                FROM mindroom_event_cache_event_edits AS terminal_index
                JOIN mindroom_event_cache_events AS terminal_event
                    ON terminal_event.namespace = terminal_index.namespace
                    AND terminal_event.event_id = terminal_index.edit_event_id
                    AND terminal_event.room_id = terminal_index.room_id
                WHERE terminal_index.namespace = nonterminal_index.namespace
                    AND terminal_index.room_id = nonterminal_index.room_id
                    AND terminal_index.original_event_id = nonterminal_index.original_event_id
                    AND terminal_event.event_json::jsonb ->> 'type' = 'm.room.message'
                    AND terminal_event.event_json::jsonb ->> 'sender'
                        = nonterminal_event.event_json::jsonb ->> 'sender'
                    AND terminal_event.origin_server_ts > nonterminal_event.origin_server_ts
                    AND terminal_event.event_json::jsonb
                        -> 'content' -> 'm.new_content' ->> 'io.mindroom.stream_status' = ANY(%s)
            )
            {room_predicate}
        ORDER BY nonterminal_event.origin_server_ts, nonterminal_event.write_seq
        LIMIT %s
        """,  # noqa: S608
        parameters,
    )
    return [
        _ArchivedPostgresStreamingEdit(
            event_id=str(row[0]),
            room_id=str(row[1]),
            sender=str(row[2]),
            origin_server_ts=int(row[3]),
            event_json_zlib=zlib.compress(str(row[4]).encode()),
            cached_at=float(row[5]),
            event_order=int(row[6]),
        )
        for row in rows
    ]


async def compact_superseded_streaming_edits(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str | None = None,
) -> int:
    """Compress superseded nonterminal edit payloads outside active storage."""
    if room_id is None:
        return await _compact_startup_candidates_by_locked_room(db, namespace=namespace)
    return await _compact_locked_room(db, namespace=namespace, room_id=room_id)


async def _compact_startup_candidates_by_locked_room(
    db: AsyncConnection,
    *,
    namespace: str,
) -> int:
    """Discover candidate rooms, then reselect edits under the runtime room lock."""
    compacted = 0
    while discovered := await _compaction_candidates(
        db,
        namespace=namespace,
        room_id=None,
        limit=_COMPACTION_BATCH_SIZE,
    ):
        room_ids = tuple(dict.fromkeys(candidate.room_id for candidate in discovered))
        for candidate_room_id in room_ids:
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (namespace, candidate_room_id),
            )
            compacted += await _compact_locked_room(
                db,
                namespace=namespace,
                room_id=candidate_room_id,
            )
    return compacted


async def _compact_locked_room(
    db: AsyncConnection,
    *,
    namespace: str,
    room_id: str,
) -> int:
    """Compact one room after its transaction-scoped advisory lock is held."""
    compacted = 0
    while candidates := await _compaction_candidates(
        db,
        namespace=namespace,
        room_id=room_id,
        limit=_COMPACTION_BATCH_SIZE,
    ):
        await _archive_candidate_batch(db, namespace=namespace, candidates=candidates)
        compacted += len(candidates)
    return compacted


async def _archive_candidate_batch(
    db: AsyncConnection,
    *,
    namespace: str,
    candidates: list[_ArchivedPostgresStreamingEdit],
) -> None:
    """Move one bounded candidate batch into cold storage."""
    async with db.cursor() as cursor:
        await cursor.executemany(
            """
            INSERT INTO mindroom_event_cache_compacted_streaming_edits(
                namespace,
                event_id,
                room_id,
                sender,
                event_json_zlib,
                cached_at,
                event_order
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            [
                (
                    namespace,
                    event.event_id,
                    event.room_id,
                    event.sender,
                    event.event_json_zlib,
                    event.cached_at,
                    event.event_order,
                )
                for event in candidates
            ],
        )
    event_ids = [event.event_id for event in candidates]
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_events
        WHERE namespace = %s AND event_id = ANY(%s)
        """,
        (namespace, event_ids),
    )
