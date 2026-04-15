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

    import nio


_RUNTIME_ONLY_EVENT_SOURCE_KEYS = frozenset({"com.mindroom.dispatch_pipeline_timing"})
_LOCK_WAIT_LOG_THRESHOLD_SECONDS = 0.1
_MAX_CACHED_ROOM_LOCKS = 256
_EVENT_CACHE_SCHEMA_VERSION = 7
_EVENT_CACHE_TABLES = (
    "thread_events",
    "events",
    "event_edits",
    "event_threads",
    "redacted_events",
    "thread_cache_state",
    "room_cache_state",
)
_EVENT_CACHE_RESET_TABLES = _EVENT_CACHE_TABLES
_REQUIRED_EVENT_CACHE_TABLES = frozenset(_EVENT_CACHE_TABLES)
logger = get_logger(__name__)


@dataclass
class _RoomLockEntry:
    """Track one room lock plus queued users that still rely on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_users: int = 0


@dataclass(frozen=True, slots=True)
class ThreadCacheState:
    """Durable freshness and invalidation metadata for one cached thread."""

    validated_at: float | None
    invalidated_at: float | None
    invalidation_reason: str | None
    room_invalidated_at: float | None
    room_invalidation_reason: str | None


@dataclass(frozen=True, slots=True)
class _SerializedCachedEvent:
    """One normalized cached event plus its serialized storage row."""

    event_id: str
    origin_server_ts: int
    event_json: str
    event: dict[str, Any]


class ConversationEventCache(Protocol):
    """Storage-agnostic cache API for Matrix event and thread lookups."""

    async def initialize(self) -> None:
        """Initialize any backing storage."""

    async def close(self) -> None:
        """Close any backing storage."""

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""

    async def get_thread_cache_state(self, room_id: str, thread_id: str) -> ThreadCacheState | None:
        """Return durable freshness metadata for one cached thread."""

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""

    async def get_latest_edit(self, room_id: str, original_event_id: str) -> dict[str, Any] | None:
        """Return the latest cached edit event for one original event."""

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        """Insert or replace one individually cached Matrix event."""

    async def store_events_batch(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Insert or replace a batch of individually cached Matrix events."""

    async def replace_thread(
        self,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        *,
        validated_at: float | None = None,
    ) -> None:
        """Atomically replace one cached thread snapshot."""

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""

    async def invalidate_room_threads(self, room_id: str) -> None:
        """Delete every cached thread snapshot for one room."""

    async def mark_thread_stale(self, room_id: str, thread_id: str, *, reason: str) -> None:
        """Persist one durable thread invalidation marker."""

    async def mark_room_threads_stale(self, room_id: str, *, reason: str) -> None:
        """Persist a durable invalidate-and-refetch marker for every cached thread in one room."""

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""

    async def redact_event(
        self,
        room_id: str,
        event_id: str,
    ) -> bool:
        """Delete one cached event after a redaction."""

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""


class _EventCache:
    """SQLite-backed ConversationEventCache implementation."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._disabled_reason: str | None = None
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

    @property
    def is_initialized(self) -> bool:
        """Return whether the SQLite connection is currently open."""
        return self._db is not None

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
                    "Waited for _EventCache room lock",
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
        if self._db is None:
            await self.initialize()
        async with self._db_lock, self._acquire_room_lock(room_id, operation=operation):
            yield self._require_db()

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        async with self._db_lock:
            if self._disabled_reason is not None or self._db is not None:
                return
            try:
                self._db_path.parent.mkdir(parents=True, exist_ok=True)
                self._db = await aiosqlite.connect(self._db_path)
                await self._db.execute("PRAGMA journal_mode=WAL")
                await self._db.execute("PRAGMA busy_timeout=5000")
                await self._reset_stale_cache_if_needed()
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
                await self._db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS event_threads (
                        room_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        PRIMARY KEY (room_id, event_id)
                    )
                    """,
                )
                await self._db.execute(
                    """
                    CREATE INDEX IF NOT EXISTS idx_event_threads_room_thread
                    ON event_threads(room_id, thread_id, event_id)
                    """,
                )
                await self._db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS redacted_events (
                        room_id TEXT NOT NULL,
                        event_id TEXT NOT NULL,
                        PRIMARY KEY (room_id, event_id)
                    )
                    """,
                )
                await self._db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS thread_cache_state (
                        room_id TEXT NOT NULL,
                        thread_id TEXT NOT NULL,
                        validated_at REAL,
                        invalidated_at REAL,
                        invalidation_reason TEXT,
                        PRIMARY KEY (room_id, thread_id)
                    )
                    """,
                )
                await self._db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS room_cache_state (
                        room_id TEXT PRIMARY KEY,
                        invalidated_at REAL,
                        invalidation_reason TEXT
                    )
                    """,
                )
                await self._db.execute(f"PRAGMA user_version = {_EVENT_CACHE_SCHEMA_VERSION}")
                await self._db.commit()
            except Exception:
                if self._db is not None:
                    try:
                        await self._db.close()
                    finally:
                        self._db = None
                raise

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""
        if self._disabled_reason is not None:
            return
        self._disabled_reason = reason
        logger.warning(
            "Disabling advisory Matrix event cache",
            db_path=str(self._db_path),
            reason=reason,
        )

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
        if self._disabled_reason is not None:
            return None
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

    async def get_thread_cache_state(self, room_id: str, thread_id: str) -> ThreadCacheState | None:
        """Return durable freshness metadata for one cached thread."""
        if self._disabled_reason is not None:
            return None
        async with self._acquire_db_operation(room_id, operation="get_thread_cache_state") as db:
            cursor = await db.execute(
                """
                SELECT
                    thread_cache_state.validated_at,
                    thread_cache_state.invalidated_at,
                    thread_cache_state.invalidation_reason,
                    room_cache_state.invalidated_at,
                    room_cache_state.invalidation_reason
                FROM (SELECT ? AS requested_room_id, ? AS requested_thread_id) AS requested
                LEFT JOIN thread_cache_state
                    ON thread_cache_state.room_id = requested.requested_room_id
                    AND thread_cache_state.thread_id = requested.requested_thread_id
                LEFT JOIN room_cache_state
                    ON room_cache_state.room_id = requested.requested_room_id
                """,
                (room_id, thread_id),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None or all(value is None for value in row):
                return None
            return ThreadCacheState(
                validated_at=None if row[0] is None else float(row[0]),
                invalidated_at=None if row[1] is None else float(row[1]),
                invalidation_reason=row[2] if isinstance(row[2], str) else None,
                room_invalidated_at=None if row[3] is None else float(row[3]),
                room_invalidation_reason=row[4] if isinstance(row[4], str) else None,
            )

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""
        if self._disabled_reason is not None:
            return None
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
        if self._disabled_reason is not None:
            return None
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
        if self._disabled_reason is not None:
            return
        if not events:
            return

        cached_at = time.time()
        events_by_room: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for event_id, room_id, event_data in events:
            normalized_event = normalize_event_source_for_cache(event_data, event_id=event_id)
            events_by_room.setdefault(room_id, []).append((event_id, normalized_event))

        for room_id, room_events in events_by_room.items():
            async with self._acquire_db_operation(room_id, operation="store_events_batch") as db:
                try:
                    cacheable_events = await _filter_cacheable_events(
                        db,
                        room_id,
                        room_events,
                    )
                    await _write_lookup_index_rows(
                        db,
                        room_id=room_id,
                        serialized_events=_serialize_cacheable_events(cacheable_events),
                        cached_at=cached_at,
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise

    async def _store_thread_events_locked(
        self,
        db: aiosqlite.Connection,
        *,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        validated_at: float,
    ) -> None:
        """Persist one authoritative thread snapshot within an existing DB transaction."""
        if not events:
            await db.execute(
                """
                INSERT INTO thread_cache_state(
                    room_id,
                    thread_id,
                    validated_at,
                    invalidated_at,
                    invalidation_reason
                )
                VALUES (?, ?, ?, NULL, NULL)
                ON CONFLICT(room_id, thread_id) DO UPDATE SET
                    validated_at = excluded.validated_at,
                    invalidated_at = NULL,
                    invalidation_reason = NULL
                """,
                (room_id, thread_id, validated_at),
            )
            return

        cached_at = validated_at
        normalized_events = [normalize_event_source_for_cache(event) for event in events]
        cacheable_events = await _filter_cacheable_events(
            db,
            room_id,
            [(_event_id(event), event) for event in normalized_events],
        )
        serialized_events = _serialize_cacheable_events(cacheable_events)
        if serialized_events:
            await db.executemany(
                """
                INSERT OR REPLACE INTO thread_events(room_id, thread_id, event_id, origin_server_ts, event_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        room_id,
                        thread_id,
                        event.event_id,
                        event.origin_server_ts,
                        event.event_json,
                    )
                    for event in serialized_events
                ],
            )
            await _write_lookup_index_rows(
                db,
                room_id=room_id,
                serialized_events=serialized_events,
                cached_at=cached_at,
                thread_id=thread_id,
            )
        await db.execute(
            """
            INSERT INTO thread_cache_state(
                room_id,
                thread_id,
                validated_at,
                invalidated_at,
                invalidation_reason
            )
            VALUES (?, ?, ?, NULL, NULL)
            ON CONFLICT(room_id, thread_id) DO UPDATE SET
                validated_at = excluded.validated_at,
                invalidated_at = NULL,
                invalidation_reason = NULL
            """,
            (room_id, thread_id, validated_at),
        )

    async def _replace_thread_locked(
        self,
        db: aiosqlite.Connection,
        *,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        validated_at: float,
    ) -> None:
        """Replace one thread snapshot atomically within an existing DB transaction."""
        existing_event_ids = await _thread_event_ids_for_thread(db, room_id=room_id, thread_id=thread_id)
        await db.execute(
            """
            DELETE FROM thread_events
            WHERE room_id = ? AND thread_id = ?
            """,
            (room_id, thread_id),
        )
        if existing_event_ids:
            await _delete_cached_events(db, event_ids=existing_event_ids)
            await _delete_event_edit_rows(
                db,
                room_id,
                event_ids=existing_event_ids,
                original_event_id=None,
            )
            await _delete_event_thread_rows(
                db,
                room_id,
                event_ids=existing_event_ids,
            )
        await self._store_thread_events_locked(
            db,
            room_id=room_id,
            thread_id=thread_id,
            events=events,
            validated_at=validated_at,
        )

    async def replace_thread(
        self,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        *,
        validated_at: float | None = None,
    ) -> None:
        """Atomically replace one cached thread snapshot."""
        if self._disabled_reason is not None:
            return
        async with self._acquire_db_operation(room_id, operation="replace_thread") as db:
            try:
                await self._replace_thread_locked(
                    db,
                    room_id=room_id,
                    thread_id=thread_id,
                    events=events,
                    validated_at=time.time() if validated_at is None else validated_at,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""
        if self._disabled_reason is not None:
            return
        async with self._acquire_db_operation(room_id, operation="invalidate_thread") as db:
            try:
                event_ids = await _thread_event_ids_for_thread(db, room_id=room_id, thread_id=thread_id)
                await db.execute(
                    """
                    DELETE FROM thread_events
                    WHERE room_id = ? AND thread_id = ?
                    """,
                    (room_id, thread_id),
                )
                if event_ids:
                    await _delete_cached_events(db, event_ids=event_ids)
                    await _delete_event_edit_rows(
                        db,
                        room_id,
                        event_ids=event_ids,
                        original_event_id=None,
                    )
                    await _delete_event_thread_rows(
                        db,
                        room_id,
                        event_ids=event_ids,
                    )
                await db.execute(
                    """
                    DELETE FROM thread_cache_state
                    WHERE room_id = ? AND thread_id = ?
                    """,
                    (room_id, thread_id),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def invalidate_room_threads(self, room_id: str) -> None:
        """Delete every cached thread snapshot for one room."""
        if self._disabled_reason is not None:
            return
        async with self._acquire_db_operation(room_id, operation="invalidate_room_threads") as db:
            try:
                event_ids = await _thread_event_ids_for_room(db, room_id=room_id)
                await db.execute(
                    """
                    DELETE FROM thread_events
                    WHERE room_id = ?
                    """,
                    (room_id,),
                )
                if event_ids:
                    await _delete_cached_events(db, event_ids=event_ids)
                    await _delete_event_edit_rows(
                        db,
                        room_id,
                        event_ids=event_ids,
                        original_event_id=None,
                    )
                    await _delete_event_thread_rows(
                        db,
                        room_id,
                        event_ids=event_ids,
                    )
                await db.execute(
                    """
                    DELETE FROM thread_cache_state
                    WHERE room_id = ?
                    """,
                    (room_id,),
                )
                await db.execute(
                    """
                    DELETE FROM room_cache_state
                    WHERE room_id = ?
                    """,
                    (room_id,),
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def mark_thread_stale(self, room_id: str, thread_id: str, *, reason: str) -> None:
        """Persist one durable thread invalidation marker."""
        if self._disabled_reason is not None:
            return
        async with self._acquire_db_operation(room_id, operation="mark_thread_stale") as db:
            try:
                await _mark_thread_stale_locked(
                    db,
                    room_id=room_id,
                    thread_id=thread_id,
                    reason=reason,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def mark_room_threads_stale(self, room_id: str, *, reason: str) -> None:
        """Persist a durable invalidate-and-refetch marker for every cached thread in one room."""
        if self._disabled_reason is not None:
            return
        async with self._acquire_db_operation(room_id, operation="mark_room_threads_stale") as db:
            try:
                await _mark_room_stale_locked(
                    db,
                    room_id=room_id,
                    invalidated_at=time.time(),
                    reason=reason,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    async def _append_existing_thread_event(
        self,
        db: aiosqlite.Connection,
        *,
        room_id: str,
        thread_id: str,
        normalized_event: dict[str, Any],
        write_lookup_row: bool,
    ) -> bool:
        """Append one event to an existing cached thread."""
        event_id = _event_id(normalized_event)
        if await _event_or_original_is_redacted(
            db,
            room_id,
            event_id=event_id,
            event=normalized_event,
        ):
            return False

        serialized_event = _serialize_cached_event(event_id, normalized_event)
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
            if write_lookup_row:
                await _write_lookup_index_rows(
                    db,
                    room_id=room_id,
                    serialized_events=[serialized_event],
                    cached_at=time.time(),
                    thread_id=thread_id,
                )
            return False

        await db.execute(
            """
            INSERT OR REPLACE INTO thread_events(room_id, thread_id, event_id, origin_server_ts, event_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                room_id,
                thread_id,
                serialized_event.event_id,
                serialized_event.origin_server_ts,
                serialized_event.event_json,
            ),
        )
        if write_lookup_row:
            await _write_lookup_index_rows(
                db,
                room_id=room_id,
                serialized_events=[serialized_event],
                cached_at=time.time(),
                thread_id=thread_id,
            )
        else:
            await db.executemany(
                """
                INSERT OR REPLACE INTO event_threads(room_id, event_id, thread_id)
                VALUES (?, ?, ?)
                """,
                _with_thread_root_self_rows([(room_id, event_id, thread_id)]),
            )
        await db.commit()
        return True

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""
        if self._disabled_reason is not None:
            return False
        normalized_event = normalize_event_source_for_cache(event)
        async with self._acquire_db_operation(room_id, operation="append_event") as db:
            try:
                return await self._append_existing_thread_event(
                    db,
                    room_id=room_id,
                    thread_id=thread_id,
                    normalized_event=normalized_event,
                    write_lookup_row=True,
                )
            except Exception:
                await db.rollback()
                raise

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""
        if self._disabled_reason is not None:
            return None
        async with self._acquire_db_operation(room_id, operation="get_thread_id_for_event") as db:
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

    async def redact_event(
        self,
        room_id: str,
        event_id: str,
    ) -> bool:
        """Delete one cached event after a redaction."""
        if self._disabled_reason is not None:
            return False
        async with self._acquire_db_operation(room_id, operation="redact_event") as db:
            try:
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
                deleted_thread_index_rows = await _delete_event_thread_rows(
                    db,
                    room_id,
                    event_ids=removed_event_ids,
                )
                await _record_redacted_events(
                    db,
                    room_id,
                    event_ids=removed_event_ids,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return (
            deleted_thread_rows > 0 or deleted_event_rows > 0 or deleted_edit_rows > 0 or deleted_thread_index_rows > 0
        )

    def _require_db(self) -> aiosqlite.Connection:
        if self._db is None:
            msg = "_EventCache has not been initialized"
            raise RuntimeError(msg)
        return self._db

    async def _schema_version(self) -> int:
        """Return the current SQLite schema version for this cache."""
        db = self._require_db()
        cursor = await db.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        await cursor.close()
        return 0 if row is None else int(row[0])

    async def _existing_table_names(self) -> set[str]:
        """Return the user-defined tables that currently exist in this SQLite DB."""
        db = self._require_db()
        cursor = await db.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
            """,
        )
        rows = await cursor.fetchall()
        await cursor.close()
        return {str(row[0]) for row in rows}

    async def _reset_stale_cache_if_needed(self) -> None:
        """Drop stale cache contents instead of migrating old cache schemas forward."""
        db = self._require_db()
        schema_version = await self._schema_version()
        existing_tables = await self._existing_table_names()
        if not existing_tables:
            return
        if schema_version == _EVENT_CACHE_SCHEMA_VERSION and _REQUIRED_EVENT_CACHE_TABLES.issubset(existing_tables):
            return

        # This cache is advisory state. We intentionally do not support migration
        # of old cache schemas; stale cache contents are discarded and rebuilt
        # lazily through normal usage so the durable schema stays simple.
        logger.info(
            "Resetting stale Matrix event cache instead of migrating it",
            db_path=str(self._db_path),
            schema_version=schema_version,
            existing_tables=sorted(existing_tables),
        )
        await db.executescript(
            "\n".join(f"DROP TABLE IF EXISTS {table_name};" for table_name in _EVENT_CACHE_RESET_TABLES),
        )
        await db.execute("PRAGMA user_version = 0")
        await db.commit()


def _event_id(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    msg = "Cached Matrix event is missing event_id"
    raise ValueError(msg)


def _serialize_cached_event(event_id: str, event: dict[str, Any]) -> _SerializedCachedEvent:
    """Serialize one normalized cached event for SQLite writes."""
    return _SerializedCachedEvent(
        event_id=event_id,
        origin_server_ts=_event_timestamp(event),
        event_json=json.dumps(event, separators=(",", ":")),
        event=event,
    )


def _serialize_cacheable_events(
    cacheable_events: list[tuple[str, dict[str, Any]]],
) -> list[_SerializedCachedEvent]:
    """Serialize one batch of normalized cacheable events."""
    return [_serialize_cached_event(event_id, event) for event_id, event in cacheable_events]


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


async def _thread_event_ids_for_thread(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for one thread."""
    cursor = await db.execute(
        """
        SELECT event_id
        FROM thread_events
        WHERE room_id = ? AND thread_id = ?
        """,
        (room_id, thread_id),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def _thread_event_ids_for_room(
    db: aiosqlite.Connection,
    *,
    room_id: str,
) -> list[str]:
    """Return cached event IDs currently stored for every thread in one room."""
    cursor = await db.execute(
        """
        SELECT event_id
        FROM thread_events
        WHERE room_id = ?
        """,
        (room_id,),
    )
    rows = await cursor.fetchall()
    await cursor.close()
    return [str(row[0]) for row in rows]


async def _write_lookup_index_rows(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    serialized_events: list[_SerializedCachedEvent],
    cached_at: float,
    thread_id: str | None = None,
) -> None:
    """Persist point-lookup, edit-index, and thread-index rows for cached events."""
    if not serialized_events:
        return
    await db.executemany(
        """
        INSERT OR REPLACE INTO events(event_id, room_id, event_json, cached_at)
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                event.event_id,
                room_id,
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


async def _mark_thread_stale_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    thread_id: str,
    reason: str,
) -> None:
    """Persist a durable invalidate-and-refetch marker within an active transaction."""
    await db.execute(
        """
        INSERT INTO thread_cache_state(
            room_id,
            thread_id,
            validated_at,
            invalidated_at,
            invalidation_reason
        )
        VALUES (?, ?, NULL, ?, ?)
        ON CONFLICT(room_id, thread_id) DO UPDATE SET
            invalidated_at = excluded.invalidated_at,
            invalidation_reason = excluded.invalidation_reason
        """,
        (room_id, thread_id, time.time(), reason),
    )


async def _mark_room_stale_locked(
    db: aiosqlite.Connection,
    *,
    room_id: str,
    invalidated_at: float,
    reason: str,
) -> None:
    """Persist one durable room-scoped invalidate-and-refetch marker."""
    await db.execute(
        """
        INSERT INTO room_cache_state(
            room_id,
            invalidated_at,
            invalidation_reason
        )
        VALUES (?, ?, ?)
        ON CONFLICT(room_id) DO UPDATE SET
            invalidated_at = excluded.invalidated_at,
            invalidation_reason = excluded.invalidation_reason
        """,
        (room_id, invalidated_at, reason),
    )


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


async def _delete_event_thread_rows(
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


async def _delete_event_edit_rows(
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


async def _event_or_original_is_redacted(
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


async def _filter_cacheable_events(
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
