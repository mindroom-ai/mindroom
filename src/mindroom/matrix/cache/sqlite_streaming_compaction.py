"""Redaction-safe SQLite archival for superseded nonterminal streaming edits."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from .cache_maintenance import (
    NONTERMINAL_STREAM_STATUSES,
    TERMINAL_STREAM_STATUSES,
    decompress_event_payload,
)

_COMPACTION_BATCH_SIZE = 500

if TYPE_CHECKING:
    import aiosqlite


@dataclass(frozen=True, slots=True)
class _ArchivedStreamingEdit:
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
    return decompress_event_payload(event_json_zlib, backend="SQLite")


def _archived_edit_from_row(row: object) -> _ArchivedStreamingEdit:
    values = cast("_ArchivedEditRow", row)
    return _ArchivedStreamingEdit(
        event_id=values[0],
        room_id=values[1],
        sender=values[2],
        origin_server_ts=values[3],
        event_json_zlib=values[4],
        cached_at=values[5],
        event_order=values[6],
    )


async def load_archived_event(
    db: aiosqlite.Connection,
    *,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one compacted event for point lookup and late-redaction resolution."""
    cursor = await db.execute(
        "SELECT event_json_zlib FROM compacted_streaming_edits WHERE event_id = ?",
        (event_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else _decompress_event(bytes(row[0]))


async def load_latest_archived_edit(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    original_event_id: str,
    sender: str | None,
) -> _ArchivedStreamingEdit | None:
    """Return the latest compacted edit in one visible replacement partition."""
    sender_predicate = "" if sender is None else "AND archived.sender = ?"
    parameters = (room_id, original_event_id, *((sender,) if sender is not None else ()))
    cursor = await db.execute(
        f"""
        SELECT
            archived.event_id,
            archived.room_id,
            archived.sender,
            edits.origin_server_ts,
            archived.event_json_zlib,
            archived.cached_at,
            archived.event_order
        FROM compacted_streaming_edits AS archived
        JOIN event_edits AS edits
            ON edits.edit_event_id = archived.event_id
            AND edits.room_id = archived.room_id
        WHERE archived.room_id = ?
            AND edits.original_event_id = ?
            {sender_predicate}
        ORDER BY edits.origin_server_ts DESC, archived.event_order DESC
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
        SELECT membership.origin_server_ts, membership.write_seq, archived.event_json_zlib
        FROM thread_events AS membership
        JOIN compacted_streaming_edits AS archived
            ON archived.event_id = membership.event_id
            AND archived.room_id = membership.room_id
        WHERE membership.room_id = ? AND membership.thread_id = ?
        ORDER BY membership.origin_server_ts, membership.write_seq
        """,
        (room_id, thread_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [(int(row[0]), int(row[1]), _decompress_event(bytes(row[2]))) for row in rows]


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
) -> list[_ArchivedStreamingEdit]:
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
            json_extract(nonterminal_event.event_json, '$.sender'),
            nonterminal_event.origin_server_ts,
            nonterminal_event.event_json,
            nonterminal_event.cached_at,
            nonterminal_event.write_seq
        FROM event_edits AS nonterminal_index
        JOIN events AS nonterminal_event
            ON nonterminal_event.event_id = nonterminal_index.edit_event_id
            AND nonterminal_event.room_id = nonterminal_index.room_id
        WHERE json_extract(nonterminal_event.event_json, '$.type') = 'm.room.message'
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
        _ArchivedStreamingEdit(
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
    db: aiosqlite.Connection,
    *,
    room_id: str | None = None,
) -> int:
    """Compress superseded nonterminal edit payloads outside active storage."""
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
    candidates: list[_ArchivedStreamingEdit],
) -> None:
    """Move one bounded candidate batch into cold storage."""
    await db.executemany(
        """
        INSERT INTO compacted_streaming_edits(
            event_id,
            room_id,
            sender,
            event_json_zlib,
            cached_at,
            event_order
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
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
    await db.executemany("DELETE FROM events WHERE event_id = ?", [(event_id,) for event_id in event_ids])
