"""Redaction-safe SQLite archival for superseded nonterminal streaming edits."""

from __future__ import annotations

import json
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .cache_maintenance import (
    NONTERMINAL_STREAM_STATUSES,
    TERMINAL_STREAM_STATUSES,
    CorruptEventCachePayloadError,
)

_COMPACTION_BATCH_SIZE = 500

if TYPE_CHECKING:
    import aiosqlite


@dataclass(frozen=True, slots=True)
class ArchivedStreamingEdit:
    """One compressed edit plus the projections needed for exact cache semantics."""

    event_id: str
    room_id: str
    original_event_id: str
    sender: str
    origin_server_ts: int
    event_json_zlib: bytes
    cached_at: float
    event_order: int
    thread_id: str | None
    thread_origin_server_ts: int | None
    thread_order: int | None
    indexed_thread_id: str | None

    def event_payload(self) -> dict[str, Any]:
        """Return the archived JSON payload."""
        return _decompress_event(self.event_json_zlib)


type _ArchivedEditRow = tuple[
    str,
    str,
    str,
    str,
    int,
    bytes,
    float,
    int,
    str | None,
    int | None,
    int | None,
    str | None,
]


def _decompress_event(event_json_zlib: bytes) -> dict[str, Any]:
    try:
        return json.loads(zlib.decompress(event_json_zlib).decode())
    except (json.JSONDecodeError, UnicodeDecodeError, zlib.error) as exc:
        msg = "Compacted SQLite event payload is corrupt"
        raise CorruptEventCachePayloadError(msg) from exc


def _archived_edit_from_row(row: object) -> ArchivedStreamingEdit:
    values = cast("_ArchivedEditRow", row)
    return ArchivedStreamingEdit(
        event_id=values[0],
        room_id=values[1],
        original_event_id=values[2],
        sender=values[3],
        origin_server_ts=values[4],
        event_json_zlib=values[5],
        cached_at=values[6],
        event_order=values[7],
        thread_id=values[8],
        thread_origin_server_ts=values[9],
        thread_order=values[10],
        indexed_thread_id=values[11],
    )


async def load_archived_event(
    db: aiosqlite.Connection,
    *,
    room_id: str | None,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one compacted event for point lookup and late-redaction resolution."""
    if room_id is None:
        cursor = await db.execute(
            "SELECT event_json_zlib FROM compacted_streaming_edits WHERE event_id = ?",
            (event_id,),
        )
    else:
        cursor = await db.execute(
            """
            SELECT event_json_zlib
            FROM compacted_streaming_edits
            WHERE room_id = ? AND event_id = ?
            """,
            (room_id, event_id),
        )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else _decompress_event(bytes(row[0]))


async def load_archived_thread_id(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return the learned thread mapping retained with one compacted edit."""
    cursor = await db.execute(
        """
        SELECT indexed_thread_id
        FROM compacted_streaming_edits
        WHERE room_id = ? AND event_id = ?
        """,
        (room_id, event_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None or row[0] is None else str(row[0])


async def load_latest_archived_edit(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    original_event_id: str,
    sender: str | None,
) -> ArchivedStreamingEdit | None:
    """Return the latest compacted edit in one visible replacement partition."""
    sender_predicate = "" if sender is None else "AND sender = ?"
    parameters = (room_id, original_event_id, *((sender,) if sender is not None else ()))
    cursor = await db.execute(
        f"""
        SELECT
            event_id,
            room_id,
            original_event_id,
            sender,
            origin_server_ts,
            event_json_zlib,
            cached_at,
            event_order,
            thread_id,
            thread_origin_server_ts,
            thread_order,
            indexed_thread_id
        FROM compacted_streaming_edits
        WHERE room_id = ?
            AND original_event_id = ?
            {sender_predicate}
        ORDER BY origin_server_ts DESC, event_order DESC
        LIMIT 1
        """,  # noqa: S608
        parameters,
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else _archived_edit_from_row(row)


async def load_archived_thread_events(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> list[tuple[int, int, dict[str, Any]]]:
    """Return compacted snapshot members with their stable membership order."""
    cursor = await db.execute(
        """
        SELECT thread_origin_server_ts, thread_order, event_json_zlib
        FROM compacted_streaming_edits
        WHERE room_id = ? AND thread_id = ?
        ORDER BY thread_origin_server_ts, thread_order
        """,
        (room_id, thread_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [(int(row[0]), int(row[1]), _decompress_event(bytes(row[2]))) for row in rows]


async def archived_thread_event_ids(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str | None = None,
) -> list[str]:
    """Return compacted snapshot-member IDs in one thread or room."""
    if thread_id is None:
        cursor = await db.execute(
            "SELECT event_id FROM compacted_streaming_edits WHERE room_id = ? AND thread_id IS NOT NULL",
            (room_id,),
        )
    else:
        cursor = await db.execute(
            "SELECT event_id FROM compacted_streaming_edits WHERE room_id = ? AND thread_id = ?",
            (room_id, thread_id),
        )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def archived_dependent_edit_ids(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    original_event_id: str,
) -> list[str]:
    """Return compacted edit IDs that must follow an original-event redaction."""
    cursor = await db.execute(
        """
        SELECT event_id
        FROM compacted_streaming_edits
        WHERE room_id = ? AND original_event_id = ?
        ORDER BY origin_server_ts, event_id
        """,
        (room_id, original_event_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def delete_archived_events(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    event_ids: list[str],
) -> int:
    """Delete compacted fallback payloads for redacted event IDs."""
    if not event_ids:
        return 0
    cursor = await db.executemany(
        """
        DELETE FROM compacted_streaming_edits
        WHERE room_id = ? AND event_id = ?
        """,
        [(room_id, event_id) for event_id in event_ids],
    )
    return 0 if cursor.rowcount is None else int(cursor.rowcount)


async def _compaction_candidates(
    db: aiosqlite.Connection,
    *,
    room_id: str | None,
    limit: int,
) -> list[ArchivedStreamingEdit]:
    nonterminal_placeholders = ",".join("?" for _ in NONTERMINAL_STREAM_STATUSES)
    terminal_placeholders = ",".join("?" for _ in TERMINAL_STREAM_STATUSES)
    room_predicate = "" if room_id is None else "AND nonterminal_index.room_id = ?"
    parameters: tuple[object, ...] = (
        *sorted(NONTERMINAL_STREAM_STATUSES),
        *sorted(TERMINAL_STREAM_STATUSES),
        *((room_id,) if room_id is not None else ()),
        limit,
    )
    cursor = await db.execute(
        f"""
        SELECT
            nonterminal_event.event_id,
            nonterminal_event.room_id,
            nonterminal_index.original_event_id,
            json_extract(nonterminal_event.event_json, '$.sender'),
            nonterminal_event.origin_server_ts,
            nonterminal_event.event_json,
            nonterminal_event.cached_at,
            nonterminal_event.write_seq,
            thread_events.thread_id,
            thread_events.origin_server_ts,
            thread_events.write_seq,
            event_threads.thread_id
        FROM event_edits AS nonterminal_index
        JOIN events AS nonterminal_event
            ON nonterminal_event.event_id = nonterminal_index.edit_event_id
            AND nonterminal_event.room_id = nonterminal_index.room_id
        LEFT JOIN thread_events
            ON thread_events.room_id = nonterminal_event.room_id
            AND thread_events.event_id = nonterminal_event.event_id
        LEFT JOIN event_threads
            ON event_threads.room_id = nonterminal_event.room_id
            AND event_threads.event_id = nonterminal_event.event_id
        WHERE nonterminal_event.event_json IS NOT NULL
            AND json_extract(nonterminal_event.event_json, '$.type') = 'm.room.message'
            AND json_type(nonterminal_event.event_json, '$.sender') = 'text'
            AND json_extract(
                nonterminal_event.event_json,
                '$.content."m.new_content"."io.mindroom.stream_status"'
            ) IN ({nonterminal_placeholders})
            AND EXISTS (
                SELECT 1
                FROM event_edits AS terminal_index
                JOIN events AS terminal_event
                    ON terminal_event.event_id = terminal_index.edit_event_id
                    AND terminal_event.room_id = terminal_index.room_id
                WHERE terminal_index.room_id = nonterminal_index.room_id
                    AND terminal_index.original_event_id = nonterminal_index.original_event_id
                    AND json_extract(terminal_event.event_json, '$.type') = 'm.room.message'
                    AND json_extract(terminal_event.event_json, '$.sender')
                        = json_extract(nonterminal_event.event_json, '$.sender')
                    AND terminal_event.origin_server_ts > nonterminal_event.origin_server_ts
                    AND json_extract(
                        terminal_event.event_json,
                        '$.content."m.new_content"."io.mindroom.stream_status"'
                    ) IN ({terminal_placeholders})
            )
            {room_predicate}
        ORDER BY nonterminal_event.origin_server_ts, nonterminal_event.write_seq
        LIMIT ?
        """,  # noqa: S608
        parameters,
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [
        ArchivedStreamingEdit(
            event_id=str(row[0]),
            room_id=str(row[1]),
            original_event_id=str(row[2]),
            sender=str(row[3]),
            origin_server_ts=int(row[4]),
            event_json_zlib=zlib.compress(str(row[5]).encode()),
            cached_at=float(row[6]),
            event_order=int(row[7]),
            thread_id=None if row[8] is None else str(row[8]),
            thread_origin_server_ts=None if row[9] is None else int(row[9]),
            thread_order=None if row[10] is None else int(row[10]),
            indexed_thread_id=None if row[11] is None else str(row[11]),
        )
        for row in rows
    ]


async def compact_superseded_streaming_edits(
    db: aiosqlite.Connection,
    *,
    room_id: str | None = None,
) -> int:
    """Archive superseded nonterminal edits and remove their active projections."""
    compacted = 0
    while candidates := await _compaction_candidates(
        db,
        room_id=room_id,
        limit=_COMPACTION_BATCH_SIZE,
    ):
        await _archive_candidate_batch(db, candidates)
        compacted += len(candidates)
    return compacted


async def _archive_candidate_batch(
    db: aiosqlite.Connection,
    candidates: list[ArchivedStreamingEdit],
) -> None:
    """Move one bounded candidate batch into cold storage."""
    if not candidates:
        return
    await db.executemany(
        """
        INSERT INTO compacted_streaming_edits(
            event_id,
            room_id,
            original_event_id,
            sender,
            origin_server_ts,
            event_json_zlib,
            cached_at,
            event_order,
            thread_id,
            thread_origin_server_ts,
            thread_order,
            indexed_thread_id
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            room_id = excluded.room_id,
            original_event_id = excluded.original_event_id,
            sender = excluded.sender,
            origin_server_ts = excluded.origin_server_ts,
            event_json_zlib = excluded.event_json_zlib,
            cached_at = excluded.cached_at,
            event_order = excluded.event_order,
            thread_id = excluded.thread_id,
            thread_origin_server_ts = excluded.thread_origin_server_ts,
            thread_order = excluded.thread_order,
            indexed_thread_id = excluded.indexed_thread_id
        """,
        [
            (
                event.event_id,
                event.room_id,
                event.original_event_id,
                event.sender,
                event.origin_server_ts,
                event.event_json_zlib,
                event.cached_at,
                event.event_order,
                event.thread_id,
                event.thread_origin_server_ts,
                event.thread_order,
                event.indexed_thread_id,
            )
            for event in candidates
        ],
    )
    event_ids = [event.event_id for event in candidates]
    await db.executemany("DELETE FROM thread_events WHERE event_id = ?", [(event_id,) for event_id in event_ids])
    await db.executemany("DELETE FROM event_threads WHERE event_id = ?", [(event_id,) for event_id in event_ids])
    await db.executemany(
        "DELETE FROM event_edits WHERE edit_event_id = ?",
        [(event_id,) for event_id in event_ids],
    )
    await db.executemany("DELETE FROM events WHERE event_id = ?", [(event_id,) for event_id in event_ids])
