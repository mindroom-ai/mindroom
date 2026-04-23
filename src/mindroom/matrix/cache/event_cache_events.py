"""Event lookup, normalization, index, and redaction storage for the Matrix event cache."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from collections.abc import Mapping

    import aiosqlite
    import nio


_RUNTIME_ONLY_EVENT_SOURCE_KEYS = frozenset({"com.mindroom.dispatch_pipeline_timing"})


@dataclass(frozen=True, slots=True)
class SerializedCachedEvent:
    """One normalized cached event plus its serialized storage row."""

    event_id: str
    origin_server_ts: int
    event_json: str
    event: dict[str, Any]


@dataclass(frozen=True, slots=True)
class CachedEventRow:
    """One cached event payload plus the time its visible row was written."""

    event: dict[str, Any]
    cached_at: float | None


def event_id_for_cache(event: dict[str, Any]) -> str:
    """Return the required event ID from one normalized cached event."""
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    msg = "Cached Matrix event is missing event_id"
    raise ValueError(msg)


def event_timestamp_for_cache(event: dict[str, Any]) -> int:
    """Return the required origin-server timestamp from one normalized cached event."""
    timestamp = event.get("origin_server_ts")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        return timestamp
    msg = f"Cached Matrix event {event_id_for_cache(event)} is missing origin_server_ts"
    raise ValueError(msg)


def serialize_cached_event(event_id: str, event: dict[str, Any]) -> SerializedCachedEvent:
    """Serialize one normalized cached event for SQLite writes."""
    return SerializedCachedEvent(
        event_id=event_id,
        origin_server_ts=event_timestamp_for_cache(event),
        event_json=json.dumps(event, separators=(",", ":")),
        event=event,
    )


def serialize_cacheable_events(
    cacheable_events: list[tuple[str, dict[str, Any]]],
) -> list[SerializedCachedEvent]:
    """Serialize one batch of normalized cacheable events."""
    return [serialize_cached_event(event_id, event) for event_id, event in cacheable_events]


def normalize_event_source_for_cache(
    event_source: Mapping[str, Any],
    *,
    event_id: str | None = None,
    sender: str | None = None,
    origin_server_ts: int | None = None,
) -> dict[str, Any]:
    """Normalize one raw Matrix event payload for persistent cache storage."""
    source = {key: value for key, value in event_source.items() if key not in _RUNTIME_ONLY_EVENT_SOURCE_KEYS}
    if "event_id" not in source and isinstance(event_id, str):
        source["event_id"] = event_id
    if "sender" not in source and isinstance(sender, str):
        source["sender"] = sender
    if (
        "origin_server_ts" not in source
        and isinstance(origin_server_ts, int)
        and not isinstance(origin_server_ts, bool)
    ):
        source["origin_server_ts"] = origin_server_ts
    return source


def normalize_nio_event_for_cache(
    event: nio.Event,
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    """Normalize one nio event for persistent cache storage."""
    event_source = event.source if isinstance(event.source, dict) else {}
    server_timestamp = event.server_timestamp
    return normalize_event_source_for_cache(
        event_source,
        event_id=event.event_id if isinstance(event.event_id, str) else event_id,
        sender=event.sender if isinstance(event.sender, str) else None,
        origin_server_ts=server_timestamp
        if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
        else None,
    )


async def load_event(
    db: aiosqlite.Connection,
    *,
    event_id: str,
) -> dict[str, Any] | None:
    """Return one cached event payload by event ID."""
    cursor = await db.execute(
        """
        SELECT event_json
        FROM events
        WHERE event_id = ?
        """,
        (event_id,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else json.loads(row[0])


async def load_latest_edit(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    original_event_id: str,
) -> dict[str, Any] | None:
    """Return the latest cached edit event for one original event."""
    cursor = await db.execute(
        """
        SELECT events.event_json
        FROM event_edits
        JOIN events ON events.event_id = event_edits.edit_event_id
        WHERE event_edits.room_id = ? AND event_edits.original_event_id = ?
        ORDER BY event_edits.origin_server_ts DESC, event_edits.edit_event_id DESC
        LIMIT 1
        """,
        (room_id, original_event_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else json.loads(row[0])


async def load_latest_edit_row(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    original_event_id: str,
) -> CachedEventRow | None:
    """Return the latest cached edit event plus its lookup-row write time."""
    cursor = await db.execute(
        """
        SELECT events.event_json, events.cached_at
        FROM event_edits
        JOIN events ON events.event_id = event_edits.edit_event_id
        WHERE event_edits.room_id = ? AND event_edits.original_event_id = ?
        ORDER BY event_edits.origin_server_ts DESC, event_edits.edit_event_id DESC
        LIMIT 1
        """,
        (room_id, original_event_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    if row is None:
        return None
    return CachedEventRow(
        event=json.loads(row[0]),
        cached_at=None if row[1] is None else float(row[1]),
    )


async def load_mxc_text(
    db: aiosqlite.Connection,
    *,
    mxc_url: str,
) -> str | None:
    """Return one durably cached MXC text payload when present."""
    cursor = await db.execute(
        """
        SELECT text_content
        FROM mxc_text_cache
        WHERE mxc_url = ?
        """,
        (mxc_url,),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else str(row[0])


async def persist_mxc_text(
    db: aiosqlite.Connection,
    *,
    mxc_url: str,
    text: str,
    cached_at: float,
) -> None:
    """Insert or replace one durably cached MXC text payload."""
    await db.execute(
        """
        INSERT OR REPLACE INTO mxc_text_cache(mxc_url, text_content, cached_at)
        VALUES (?, ?, ?)
        """,
        (mxc_url, text, cached_at),
    )


async def persist_lookup_events(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist point-lookups and derived indexes for one room-scoped event batch."""
    cacheable_events = await filter_cacheable_events(db, room_id, room_events)
    await write_lookup_index_rows(
        db,
        room_id=room_id,
        serialized_events=serialize_cacheable_events(cacheable_events),
        cached_at=cached_at,
        thread_id=thread_id,
    )


async def load_thread_id_for_event(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    event_id: str,
) -> str | None:
    """Return the cached thread ID for one event."""
    cursor = await db.execute(
        """
        SELECT thread_id
        FROM event_threads
        WHERE room_id = ? AND event_id = ?
        """,
        (room_id, event_id),
    )
    row = await cursor.fetchone()
    await cursor.close()
    return None if row is None else str(row[0])


async def redact_event_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    event_id: str,
) -> bool:
    """Delete one cached event after a redaction within an existing transaction."""
    dependent_edit_ids = await dependent_edit_event_ids(db, room_id, original_event_id=event_id)
    removed_event_ids = list(dict.fromkeys([event_id, *dependent_edit_ids]))
    deleted_thread_rows = await _delete_room_thread_events(db, room_id, event_ids=removed_event_ids)
    deleted_event_rows = await delete_cached_events(db, event_ids=removed_event_ids)
    deleted_edit_rows = await delete_event_edit_rows(
        db,
        room_id,
        event_ids=removed_event_ids,
        original_event_id=event_id,
    )
    deleted_thread_index_rows = await delete_event_thread_rows(
        db,
        room_id,
        event_ids=removed_event_ids,
    )
    await _record_redacted_events(
        db,
        room_id,
        event_ids=removed_event_ids,
    )
    return deleted_thread_rows > 0 or deleted_event_rows > 0 or deleted_edit_rows > 0 or deleted_thread_index_rows > 0


async def event_or_original_is_redacted(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_id: str,
    event: dict[str, Any],
) -> bool:
    """Return whether this event or its edited original was durably redacted."""
    event_info = EventInfo.from_event(event)
    candidate_ids = {event_id}
    if event_info.is_edit and isinstance(event_info.original_event_id, str):
        candidate_ids.add(event_info.original_event_id)
    return bool(
        await _redacted_event_ids_for_candidates(
            db,
            room_id,
            event_ids=candidate_ids,
        ),
    )


async def filter_cacheable_events(
    db: aiosqlite.Connection,
    room_id: str,
    room_events: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Drop events that target durable redaction tombstones before persisting them."""
    candidate_ids: set[str] = set()
    for event_id, event_data in room_events:
        candidate_ids.add(event_id)
        event_info = EventInfo.from_event(event_data)
        if event_info.is_edit and isinstance(event_info.original_event_id, str):
            candidate_ids.add(event_info.original_event_id)
    redacted_event_ids = await _redacted_event_ids_for_candidates(
        db,
        room_id,
        event_ids=candidate_ids,
    )
    if not redacted_event_ids:
        return room_events

    cacheable_events: list[tuple[str, dict[str, Any]]] = []
    for event_id, event_data in room_events:
        event_info = EventInfo.from_event(event_data)
        original_event_id = event_info.original_event_id if event_info.is_edit else None
        if event_id in redacted_event_ids:
            continue
        if isinstance(original_event_id, str) and original_event_id in redacted_event_ids:
            continue
        cacheable_events.append((event_id, event_data))
    return cacheable_events


async def write_lookup_index_rows(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    serialized_events: list[SerializedCachedEvent],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist point-lookup, edit-index, and thread-index rows for cached events."""
    if not serialized_events:
        return
    await db.executemany(
        """
        INSERT OR REPLACE INTO events(event_id, room_id, origin_server_ts, event_json, cached_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                event.event_id,
                room_id,
                event.origin_server_ts,
                event.event_json,
                cached_at,
            )
            for event in serialized_events
        ],
    )
    edit_rows = [
        row for row in (_edit_cache_row(room_id, event.event) for event in serialized_events) if row is not None
    ]
    if edit_rows:
        await db.executemany(
            """
            INSERT OR REPLACE INTO event_edits(edit_event_id, room_id, original_event_id, origin_server_ts)
            VALUES (?, ?, ?, ?)
            """,
            edit_rows,
        )
    thread_rows = (
        [(room_id, event.event_id, thread_id) for event in serialized_events]
        if thread_id is not None
        else [
            row for row in (_event_thread_row(room_id, event.event) for event in serialized_events) if row is not None
        ]
    )
    if thread_rows:
        thread_rows = _with_thread_root_self_rows(thread_rows)
        await db.executemany(
            """
            INSERT OR REPLACE INTO event_threads(room_id, event_id, thread_id)
            VALUES (?, ?, ?)
            """,
            thread_rows,
        )


async def dependent_edit_event_ids(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    original_event_id: str,
) -> list[str]:
    """Return cached edit event IDs that target one original event."""
    cursor = await db.execute(
        """
        SELECT edit_event_id
        FROM event_edits
        WHERE room_id = ? AND original_event_id = ?
        """,
        (room_id, original_event_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def delete_cached_events(
    db: aiosqlite.Connection,
    *,
    event_ids: list[str],
) -> int:
    """Delete point-lookup cache rows for the provided event IDs."""
    if not event_ids:
        return 0
    cursor = await db.executemany(
        """
        DELETE FROM events
        WHERE event_id = ?
        """,
        [(event_id,) for event_id in event_ids],
    )
    return 0 if cursor.rowcount is None else int(cursor.rowcount)


async def delete_event_thread_rows(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
) -> int:
    """Delete durable event-to-thread rows for the provided event IDs."""
    if not event_ids:
        return 0
    cursor = await db.executemany(
        """
        DELETE FROM event_threads
        WHERE room_id = ? AND event_id = ?
        """,
        [(room_id, event_id) for event_id in event_ids],
    )
    return 0 if cursor.rowcount is None else int(cursor.rowcount)


async def delete_event_edit_rows(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
    original_event_id: str | None,
) -> int:
    """Delete derived edit-index rows affected by one event redaction."""
    deleted_rows = 0
    for event_id in event_ids:
        cursor = await db.execute(
            """
            DELETE FROM event_edits
            WHERE room_id = ? AND edit_event_id = ?
            """,
            (room_id, event_id),
        )
        deleted_rows += 0 if cursor.rowcount is None else int(cursor.rowcount)
        await cursor.close()
    if original_event_id is not None:
        cursor = await db.execute(
            """
            DELETE FROM event_edits
            WHERE room_id = ? AND original_event_id = ?
            """,
            (room_id, original_event_id),
        )
        deleted_rows += 0 if cursor.rowcount is None else int(cursor.rowcount)
        await cursor.close()
    return deleted_rows


def _event_thread_row(room_id: str, event: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return one durable event-to-thread mapping row when thread membership is explicit."""
    event_id = event.get("event_id")
    if not isinstance(event_id, str) or not event_id:
        return None
    event_info = EventInfo.from_event(event)
    thread_id = event_info.thread_id
    if not isinstance(thread_id, str):
        thread_id = event_info.thread_id_from_edit
    if not isinstance(thread_id, str) or not thread_id:
        return None
    return room_id, event_id, thread_id


def _with_thread_root_self_rows(
    thread_rows: list[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Ensure any learned thread membership also records the root's own lookup row."""
    if not thread_rows:
        return thread_rows
    return list(
        dict.fromkeys(
            [
                *thread_rows,
                *((room_id, thread_id, thread_id) for room_id, _event_id, thread_id in thread_rows),
            ],
        ),
    )


def _edit_cache_row(room_id: str, event: dict[str, Any]) -> tuple[str, str, str, int] | None:
    """Return one edit-index row for a cached event when it is an edit."""
    if event.get("type") != "m.room.message":
        return None

    event_info = EventInfo.from_event(event)
    if not event_info.is_edit or not isinstance(event_info.original_event_id, str):
        return None

    return (
        event_id_for_cache(event),
        room_id,
        event_info.original_event_id,
        event_timestamp_for_cache(event),
    )


async def _delete_room_thread_events(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
) -> int:
    """Delete cached thread rows for the provided event IDs within one room."""
    if not event_ids:
        return 0
    cursor = await db.executemany(
        """
        DELETE FROM thread_events
        WHERE room_id = ? AND event_id = ?
        """,
        [(room_id, event_id) for event_id in event_ids],
    )
    return 0 if cursor.rowcount is None else int(cursor.rowcount)


async def _record_redacted_events(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
) -> None:
    """Persist durable tombstones for redacted event IDs."""
    if not event_ids:
        return
    await db.executemany(
        """
        INSERT OR REPLACE INTO redacted_events(room_id, event_id)
        VALUES (?, ?)
        """,
        [(room_id, event_id) for event_id in event_ids],
    )


async def _redacted_event_ids_for_candidates(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: set[str],
) -> frozenset[str]:
    """Return the subset of candidate event IDs that are durably tombstoned."""
    if not event_ids:
        return frozenset()
    placeholders = ",".join("?" for _ in event_ids)
    query = f"""
        SELECT event_id
        FROM redacted_events
        WHERE room_id = ? AND event_id IN ({placeholders})
        """  # noqa: S608
    cursor = await db.execute(
        query,
        (room_id, *sorted(event_ids)),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return frozenset(str(row[0]) for row in rows)
