"""Cache boundary and SQLite-backed implementation for Matrix event lookups."""

from __future__ import annotations

import asyncio
import json
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import aiosqlite

from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path


_RUNTIME_ONLY_EVENT_SOURCE_KEYS = frozenset({"com.mindroom.dispatch_pipeline_timing"})
_LOCK_WAIT_LOG_THRESHOLD_SECONDS = 0.1
_MAX_CACHED_ROOM_LOCKS = 256
_EVENT_CACHE_SCHEMA_VERSION = 1
logger = get_logger(__name__)


@dataclass
class _RoomLockEntry:
    """Track one room lock plus queued users that still rely on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_users: int = 0


class ConversationEventCache(Protocol):
    """Storage-agnostic cache API for Matrix event and thread lookups."""

    async def initialize(self) -> None:
        """Initialize any backing storage."""

    async def close(self) -> None:
        """Close any backing storage."""

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""

    async def get_latest_ts(self, room_id: str, thread_id: str) -> int | None:
        """Return the latest cached timestamp for one thread."""

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""

    async def get_latest_edit(self, room_id: str, original_event_id: str) -> dict[str, Any] | None:
        """Return the latest cached edit event for one original event."""

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        """Insert or replace one individually cached Matrix event."""

    async def store_events_batch(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Insert or replace a batch of individually cached Matrix events."""

    async def store_events(self, room_id: str, thread_id: str, events: list[dict[str, Any]]) -> None:
        """Insert or replace one batch of thread events."""

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""

    async def get_latest_timestamp(self, room_id: str, thread_id: str) -> int | None:
        """Compatibility wrapper for older cache call sites."""

    async def store_thread_events(self, room_id: str, thread_id: str, events: list[dict[str, Any]]) -> None:
        """Compatibility wrapper for older cache call sites."""

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""

    async def redact_event(
        self,
        room_id: str,
        event_id: str,
        *,
        thread_id: str | None = None,
        redaction_event: dict[str, Any] | None = None,
    ) -> bool:
        """Delete one cached event after a redaction."""


class EventCache:
    """SQLite-backed ConversationEventCache implementation."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        # One shared SQLite connection must serialize lifecycle changes with all
        # in-flight DB operations so shutdown cannot close it mid-query.
        self._db_lock = asyncio.Lock()
        # These locks preserve logical room ordering for the advisory cache and
        # keep contention visible in logs even though DB operations are gated by
        # the shared connection lock above.
        self._room_locks: OrderedDict[str, _RoomLockEntry] = OrderedDict()

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path for this cache instance."""
        return self._db_path

    def _prune_room_locks(self) -> None:
        while len(self._room_locks) > _MAX_CACHED_ROOM_LOCKS:
            evicted_room_id: str | None = None
            for cached_room_id, cached_entry in self._room_locks.items():
                if cached_entry.active_users > 0:
                    continue
                evicted_room_id = cached_room_id
                break
            if evicted_room_id is None:
                return
            self._room_locks.pop(evicted_room_id, None)

    def _room_lock_entry(self, room_id: str, *, active_user_increment: int = 0) -> _RoomLockEntry:
        entry = self._room_locks.get(room_id)
        if entry is None:
            entry = _RoomLockEntry(active_users=active_user_increment)
        else:
            entry.active_users += active_user_increment
        self._room_locks[room_id] = entry
        self._room_locks.move_to_end(room_id)
        self._prune_room_locks()
        return entry

    def _room_lock(self, room_id: str) -> asyncio.Lock:
        return self._room_lock_entry(room_id).lock

    @asynccontextmanager
    async def _acquire_room_lock(self, room_id: str, *, operation: str) -> AsyncIterator[None]:
        entry = self._room_lock_entry(room_id, active_user_increment=1)
        wait_started = time.perf_counter()
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            wait_time = time.perf_counter() - wait_started
            if wait_time > _LOCK_WAIT_LOG_THRESHOLD_SECONDS:
                logger.debug(
                    "Waited for EventCache room lock",
                    room_id=room_id,
                    operation=operation,
                    wait_time_ms=round(wait_time * 1000, 2),
                )
            yield
        finally:
            if acquired:
                entry.lock.release()
            entry.active_users -= 1
            if entry.active_users == 0:
                self._prune_room_locks()

    @asynccontextmanager
    async def _acquire_db_operation(
        self,
        room_id: str,
        *,
        operation: str,
    ) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize one DB operation with lifecycle changes and room ordering."""
        async with self._db_lock, self._acquire_room_lock(room_id, operation=operation):
            yield self._require_db()

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        async with self._db_lock:
            if self._db is not None:
                return

            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            self._db = await aiosqlite.connect(self._db_path)
            await self._db.execute("PRAGMA journal_mode=WAL")
            await self._db.execute("PRAGMA busy_timeout=5000")
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS thread_events (
                    room_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    origin_server_ts INTEGER NOT NULL,
                    event_json TEXT NOT NULL,
                    PRIMARY KEY (room_id, event_id)
                )
                """,
            )
            await self._db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_thread_events_room_thread_ts
                ON thread_events(room_id, thread_id, origin_server_ts)
                """,
            )
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    event_json TEXT NOT NULL,
                    cached_at REAL NOT NULL
                )
                """,
            )
            await self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS event_edits (
                    edit_event_id TEXT PRIMARY KEY,
                    room_id TEXT NOT NULL,
                    original_event_id TEXT NOT NULL,
                    origin_server_ts INTEGER NOT NULL
                )
                """,
            )
            await self._db.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_event_edits_room_original_ts
                ON event_edits(room_id, original_event_id, origin_server_ts DESC, edit_event_id DESC)
                """,
            )
            schema_version = await self._schema_version()
            if schema_version < _EVENT_CACHE_SCHEMA_VERSION:
                await self._backfill_event_edits()
                await self._db.execute(f"PRAGMA user_version = {_EVENT_CACHE_SCHEMA_VERSION}")
            await self._db.commit()

    async def close(self) -> None:
        """Close the SQLite connection when the cache is no longer needed."""
        async with self._db_lock:
            if self._db is None:
                return
            await self._db.close()
            self._db = None
            self._room_locks.clear()

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""
        async with self._acquire_db_operation(room_id, operation="get_thread_events") as db:
            cursor = await db.execute(
                """
                SELECT event_json
                FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                ORDER BY origin_server_ts ASC, event_id ASC
                """,
                (room_id, thread_id),
            )
            rows = await cursor.fetchall()
            await cursor.close()
            if not rows:
                return None
            return [json.loads(row[0]) for row in rows]

    async def get_latest_ts(self, room_id: str, thread_id: str) -> int | None:
        """Return the latest cached server timestamp for one thread."""
        async with self._acquire_db_operation(room_id, operation="get_latest_ts") as db:
            cursor = await db.execute(
                """
                SELECT MAX(origin_server_ts)
                FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                """,
                (room_id, thread_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return None if row is None or row[0] is None else int(row[0])

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""
        async with self._acquire_db_operation(room_id, operation="get_event") as db:
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

    async def get_latest_edit(self, room_id: str, original_event_id: str) -> dict[str, Any] | None:
        """Return the latest cached edit event for one original event."""
        async with self._acquire_db_operation(room_id, operation="get_latest_edit") as db:
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

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        """Insert or replace one individually cached Matrix event."""
        await self.store_events_batch([(event_id, room_id, event_data)])

    async def store_events_batch(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Insert or replace one batch of individually cached Matrix events."""
        if not events:
            return

        cached_at = time.time()
        events_by_room: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for event_id, room_id, event_data in events:
            normalized_event = normalize_event_source_for_cache(event_data, event_id=event_id)
            events_by_room.setdefault(room_id, []).append((event_id, normalized_event))

        for room_id, room_events in events_by_room.items():
            async with self._acquire_db_operation(room_id, operation="store_events_batch") as db:
                await db.executemany(
                    """
                    INSERT OR REPLACE INTO events(event_id, room_id, event_json, cached_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    [
                        (
                            event_id,
                            room_id,
                            json.dumps(
                                normalize_event_source_for_cache(event_data, event_id=event_id),
                                separators=(",", ":"),
                            ),
                            cached_at,
                        )
                        for event_id, event_data in room_events
                    ],
                )
                edit_rows = [
                    row
                    for row in (_edit_cache_row(room_id, event_data) for _event_id, event_data in room_events)
                    if row is not None
                ]
                if edit_rows:
                    await db.executemany(
                        """
                        INSERT OR REPLACE INTO event_edits(edit_event_id, room_id, original_event_id, origin_server_ts)
                        VALUES (?, ?, ?, ?)
                        """,
                        edit_rows,
                    )
                await db.commit()

    async def store_events(self, room_id: str, thread_id: str, events: list[dict[str, Any]]) -> None:
        """Insert or replace one batch of thread events."""
        if not events:
            return

        cached_at = time.time()
        normalized_events = [normalize_event_source_for_cache(event) for event in events]
        async with self._acquire_db_operation(room_id, operation="store_events") as db:
            serialized_events = [
                (
                    _event_id(event),
                    _event_timestamp(event),
                    json.dumps(event, separators=(",", ":")),
                )
                for event in normalized_events
            ]
            await db.executemany(
                """
                INSERT OR REPLACE INTO thread_events(room_id, thread_id, event_id, origin_server_ts, event_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        room_id,
                        thread_id,
                        event_id,
                        origin_server_ts,
                        event_json,
                    )
                    for event_id, origin_server_ts, event_json in serialized_events
                ],
            )
            await db.executemany(
                """
                INSERT OR REPLACE INTO events(event_id, room_id, event_json, cached_at)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        event_id,
                        room_id,
                        event_json,
                        cached_at,
                    )
                    for event_id, _origin_server_ts, event_json in serialized_events
                ],
            )
            edit_rows = [
                row for row in (_edit_cache_row(room_id, event) for event in normalized_events) if row is not None
            ]
            if edit_rows:
                await db.executemany(
                    """
                    INSERT OR REPLACE INTO event_edits(edit_event_id, room_id, original_event_id, origin_server_ts)
                    VALUES (?, ?, ?, ?)
                    """,
                    edit_rows,
                )
            await db.commit()

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""
        async with self._acquire_db_operation(room_id, operation="invalidate_thread") as db:
            cursor = await db.execute(
                """
                SELECT event_id
                FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                """,
                (room_id, thread_id),
            )
            event_ids = [str(row[0]) for row in await cursor.fetchall()]
            await cursor.close()
            await db.execute(
                """
                DELETE FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                """,
                (room_id, thread_id),
            )
            if event_ids:
                await db.executemany(
                    """
                    DELETE FROM events
                    WHERE event_id = ?
                    """,
                    [(event_id,) for event_id in event_ids],
                )
                await db.executemany(
                    """
                    DELETE FROM event_edits
                    WHERE room_id = ? AND edit_event_id = ?
                    """,
                    [(room_id, event_id) for event_id in event_ids],
                )
            await db.commit()

    async def get_latest_timestamp(self, room_id: str, thread_id: str) -> int | None:
        """Compatibility wrapper for older cache call sites."""
        return await self.get_latest_ts(room_id, thread_id)

    async def store_thread_events(self, room_id: str, thread_id: str, events: list[dict[str, Any]]) -> None:
        """Compatibility wrapper for older cache call sites."""
        await self.store_events(room_id, thread_id, events)

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""
        normalized_event = normalize_event_source_for_cache(event)
        async with self._acquire_db_operation(room_id, operation="append_event") as db:
            cursor = await db.execute(
                """
                SELECT 1
                FROM thread_events
                WHERE room_id = ? AND thread_id = ?
                LIMIT 1
                """,
                (room_id, thread_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                return False

            event_id = _event_id(normalized_event)
            event_json = json.dumps(normalized_event, separators=(",", ":"))
            await db.execute(
                """
                INSERT OR REPLACE INTO thread_events(room_id, thread_id, event_id, origin_server_ts, event_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    room_id,
                    thread_id,
                    event_id,
                    _event_timestamp(normalized_event),
                    event_json,
                ),
            )
            await db.execute(
                """
                INSERT OR REPLACE INTO events(event_id, room_id, event_json, cached_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    event_id,
                    room_id,
                    event_json,
                    time.time(),
                ),
            )
            edit_row = _edit_cache_row(room_id, normalized_event)
            if edit_row is not None:
                await db.execute(
                    """
                    INSERT OR REPLACE INTO event_edits(edit_event_id, room_id, original_event_id, origin_server_ts)
                    VALUES (?, ?, ?, ?)
                    """,
                    edit_row,
                )
            await db.commit()
            return True

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""
        async with self._acquire_db_operation(room_id, operation="get_thread_id_for_event") as db:
            cursor = await db.execute(
                """
                SELECT thread_id
                FROM thread_events
                WHERE room_id = ? AND event_id = ?
                """,
                (room_id, event_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            return None if row is None else str(row[0])

    async def redact_event(
        self,
        room_id: str,
        event_id: str,
        *,
        thread_id: str | None = None,
        redaction_event: dict[str, Any] | None = None,
    ) -> bool:
        """Delete one cached event after a redaction."""
        del redaction_event, thread_id

        async with self._acquire_db_operation(room_id, operation="redact_event") as db:
            dependent_edit_ids = await _dependent_edit_event_ids(db, room_id, original_event_id=event_id)
            removed_event_ids = list(dict.fromkeys([event_id, *dependent_edit_ids]))
            deleted_thread_rows = await _delete_room_thread_events(db, room_id, event_ids=removed_event_ids)
            deleted_event_rows = await _delete_cached_events(db, event_ids=removed_event_ids)
            deleted_edit_rows = await _delete_event_edit_rows(
                db,
                room_id,
                event_ids=removed_event_ids,
                original_event_id=event_id,
            )
            await db.commit()
        return deleted_thread_rows > 0 or deleted_event_rows > 0 or deleted_edit_rows > 0

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "EventCache has not been initialized"
            raise RuntimeError(msg)
        return self._db

    async def _schema_version(self) -> int:
        """Return the current SQLite schema version for this cache."""
        db = self._require_db()
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        return 0 if row is None else int(row[0])

    async def _backfill_event_edits(self) -> None:
        """Populate the derived edit index from existing cached event payloads."""
        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT room_id, event_json
            FROM events
            """,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        edit_rows = [
            row
            for room_id, event_json in rows
            for row in [_edit_cache_row(str(room_id), json.loads(str(event_json)))]
            if row is not None
        ]
        if not edit_rows:
            return
        await db.executemany(
            """
            INSERT OR REPLACE INTO event_edits(edit_event_id, room_id, original_event_id, origin_server_ts)
            VALUES (?, ?, ?, ?)
            """,
            edit_rows,
        )


def _event_id(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    msg = "Cached Matrix event is missing event_id"
    raise ValueError(msg)


def _event_timestamp(event: dict[str, Any]) -> int:
    timestamp = event.get("origin_server_ts")
    if isinstance(timestamp, int) and not isinstance(timestamp, bool):
        return timestamp
    msg = f"Cached Matrix event {_event_id(event)} is missing origin_server_ts"
    raise ValueError(msg)


def _edit_cache_row(room_id: str, event: dict[str, Any]) -> tuple[str, str, str, int] | None:
    """Return one edit-index row for a cached event when it is an edit."""
    if event.get("type") != "m.room.message":
        return None

    event_info = EventInfo.from_event(event)
    if not event_info.is_edit or not isinstance(event_info.original_event_id, str):
        return None

    return (_event_id(event), room_id, event_info.original_event_id, _event_timestamp(event))


async def _dependent_edit_event_ids(
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


async def _delete_cached_events(
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


async def _delete_event_edit_rows(
    db: aiosqlite.Connection,
    room_id: str,
    *,
    event_ids: list[str],
    original_event_id: str,
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
