"""Public boundary plus runtime and lifecycle ownership for the Matrix event cache."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

import aiosqlite

from mindroom.logging_config import get_logger

from . import event_cache_events, event_cache_threads

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

EVENT_CACHE_SCHEMA_VERSION = 8
_EVENT_CACHE_TABLES = (
    "thread_events",
    "events",
    "event_edits",
    "event_threads",
    "redacted_events",
    "mxc_text_cache",
    "thread_cache_state",
    "room_cache_state",
)
_EVENT_CACHE_RESET_TABLES = _EVENT_CACHE_TABLES
_REQUIRED_EVENT_CACHE_TABLES = frozenset(_EVENT_CACHE_TABLES)
_LOCK_WAIT_LOG_THRESHOLD_SECONDS = 0.1
_MAX_CACHED_ROOM_LOCKS = 256
_EVENT_CACHE_SCHEMA_VERSION = EVENT_CACHE_SCHEMA_VERSION
T = TypeVar("T")

logger = get_logger(__name__)
ThreadCacheState = event_cache_threads.ThreadCacheState


async def initialize_event_cache_db(db_path: Path) -> aiosqlite.Connection:
    """Open the SQLite database and ensure the event-cache schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await reset_stale_cache_if_needed(db, db_path=db_path)
        await create_event_cache_schema(db)
        await db.commit()
    except Exception:
        await db.close()
        raise
    return db


async def create_event_cache_schema(db: aiosqlite.Connection) -> None:
    """Create the current cache schema in one SQLite connection."""
    await db.execute(
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
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_thread_events_room_thread_ts
        ON thread_events(room_id, thread_id, origin_server_ts)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            event_json TEXT NOT NULL,
            cached_at REAL NOT NULL
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_edits (
            edit_event_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            original_event_id TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_edits_room_original_ts
        ON event_edits(room_id, original_event_id, origin_server_ts DESC, edit_event_id DESC)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_threads (
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            PRIMARY KEY (room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_threads_room_thread
        ON event_threads(room_id, thread_id, event_id)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS redacted_events (
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            PRIMARY KEY (room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mxc_text_cache (
            mxc_url TEXT PRIMARY KEY,
            text_content TEXT NOT NULL,
            cached_at REAL NOT NULL
        )
        """,
    )
    await db.execute(
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
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS room_cache_state (
            room_id TEXT PRIMARY KEY,
            invalidated_at REAL,
            invalidation_reason TEXT
        )
        """,
    )
    await db.execute(f"PRAGMA user_version = {EVENT_CACHE_SCHEMA_VERSION}")


async def schema_version(db: aiosqlite.Connection) -> int:
    """Return the current SQLite schema version for this cache."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    await cursor.close()
    return 0 if row is None else int(row[0])


async def existing_table_names(db: aiosqlite.Connection) -> set[str]:
    """Return the user-defined tables that currently exist in this SQLite DB."""
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


async def reset_stale_cache_if_needed(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
) -> None:
    """Drop stale cache contents instead of migrating old cache schemas forward."""
    current_schema_version = await schema_version(db)
    current_table_names = await existing_table_names(db)
    if not current_table_names:
        return
    if current_schema_version == EVENT_CACHE_SCHEMA_VERSION and _REQUIRED_EVENT_CACHE_TABLES.issubset(
        current_table_names,
    ):
        return

    logger.info(
        "Resetting stale Matrix event cache instead of migrating it",
        db_path=str(db_path),
        schema_version=current_schema_version,
        existing_tables=sorted(current_table_names),
    )
    await db.executescript(
        "\n".join(f"DROP TABLE IF EXISTS {table_name};" for table_name in _EVENT_CACHE_RESET_TABLES),
    )
    await db.execute("PRAGMA user_version = 0")
    await db.commit()


@dataclass
class _RoomLockEntry:
    """Track one room lock plus queued users that still rely on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_users: int = 0


class _EventCacheRuntime:
    """Own runtime-only lifecycle, locking, and disable state for one cache instance."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._disabled_reason: str | None = None
        self._db_lock = asyncio.Lock()
        self._room_locks: OrderedDict[str, _RoomLockEntry] = OrderedDict()

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path for this cache instance."""
        return self._db_path

    @property
    def db(self) -> aiosqlite.Connection | None:
        """Return the active SQLite connection, if initialized."""
        return self._db

    @property
    def room_locks(self) -> dict[str, _RoomLockEntry]:
        """Return the cached room-lock table for observability and tests."""
        return self._room_locks

    @property
    def is_initialized(self) -> bool:
        """Return whether the SQLite connection is currently open."""
        return self._db is not None

    @property
    def is_disabled(self) -> bool:
        """Return whether the advisory cache is disabled for this runtime."""
        return self._disabled_reason is not None

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

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        async with self._db_lock:
            if self._disabled_reason is not None or self._db is not None:
                return
            self._db = await initialize_event_cache_db(self._db_path)

    async def close(self) -> None:
        """Close the SQLite connection when the cache is no longer needed."""
        async with self._db_lock:
            if self._db is None:
                return
            await self._db.close()
            self._db = None
            self._room_locks.clear()

    def room_lock_entry(self, room_id: str, *, active_user_increment: int = 0) -> _RoomLockEntry:
        """Return the cached room lock entry, creating it on demand."""
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
    async def acquire_room_lock(self, room_id: str, *, operation: str) -> AsyncIterator[None]:
        """Serialize runtime-visible work for one room."""
        entry = self.room_lock_entry(room_id, active_user_increment=1)
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
    async def acquire_db_operation(
        self,
        room_id: str,
        *,
        operation: str,
    ) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize one DB operation with lifecycle changes and room ordering."""
        if self._db is None:
            await self.initialize()
        async with self._db_lock, self.acquire_room_lock(room_id, operation=operation):
            yield self.require_db()

    def require_db(self) -> aiosqlite.Connection:
        """Return the active SQLite connection or raise if uninitialized."""
        if self._db is None:
            msg = "_EventCache has not been initialized"
            raise RuntimeError(msg)
        return self._db

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


RoomLockEntry = _RoomLockEntry
EventCacheRuntime = _EventCacheRuntime


class ConversationEventCache(Protocol):
    """Storage-agnostic cache API for Matrix event and thread lookups."""

    async def initialize(self) -> None:
        """Initialize any backing storage."""

    async def close(self) -> None:
        """Close any backing storage."""

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""

    async def get_recent_room_thread_ids(self, room_id: str, *, limit: int) -> list[str]:
        """Return locally known thread IDs for one room ordered by newest cached activity."""

    async def get_thread_cache_state(self, room_id: str, thread_id: str) -> ThreadCacheState | None:
        """Return durable freshness metadata for one cached thread."""

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""

    async def get_latest_edit(self, room_id: str, original_event_id: str) -> dict[str, Any] | None:
        """Return the latest cached edit event for one original event."""

    async def get_mxc_text(self, room_id: str, mxc_url: str) -> str | None:
        """Return one durably cached MXC text payload when present."""

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        """Insert or replace one individually cached Matrix event."""

    async def store_events_batch(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Insert or replace a batch of individually cached Matrix events."""

    async def store_mxc_text(self, room_id: str, mxc_url: str, text: str) -> None:
        """Insert or replace one durably cached MXC text payload."""

    async def replace_thread(
        self,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        *,
        validated_at: float | None = None,
    ) -> None:
        """Atomically replace one cached thread snapshot."""

    async def replace_thread_if_not_newer(
        self,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        *,
        fetch_started_at: float,
        validated_at: float | None = None,
    ) -> bool:
        """Replace one cached thread snapshot only when nothing newer touched it after fetch start."""

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

    async def revalidate_thread_after_incremental_update(
        self,
        room_id: str,
        thread_id: str,
        *,
        runtime_started_at: float,
    ) -> bool:
        """Refresh thread validation after a safe incremental update in the current runtime."""

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
        self._runtime = _EventCacheRuntime(db_path)

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path for this cache instance."""
        return self._runtime.db_path

    @property
    def is_initialized(self) -> bool:
        """Return whether the SQLite connection is currently open."""
        return self._runtime.is_initialized

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        await self._runtime.initialize()

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""
        self._runtime.disable(reason)

    async def close(self) -> None:
        """Close the SQLite connection when the cache is no longer needed."""
        await self._runtime.close()

    async def _read_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: T,
        reader: Callable[[aiosqlite.Connection], Awaitable[T]],
    ) -> T:
        if self._runtime.is_disabled:
            return disabled_result
        async with self._runtime.acquire_db_operation(room_id, operation=operation) as db:
            return await reader(db)

    async def _write_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: T,
        writer: Callable[[aiosqlite.Connection], Awaitable[T]],
    ) -> T:
        if self._runtime.is_disabled:
            return disabled_result
        async with self._runtime.acquire_db_operation(room_id, operation=operation) as db:
            try:
                result = await writer(db)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        return result

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""
        return await self._read_operation(
            room_id,
            operation="get_thread_events",
            disabled_result=None,
            reader=lambda db: event_cache_threads.load_thread_events(
                db,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def get_recent_room_thread_ids(self, room_id: str, *, limit: int) -> list[str]:
        """Return locally known thread IDs for one room ordered by newest cached activity."""
        return await self._read_operation(
            room_id,
            operation="get_recent_room_thread_ids",
            disabled_result=[],
            reader=lambda db: event_cache_threads.load_recent_room_thread_ids(
                db,
                room_id=room_id,
                limit=limit,
            ),
        )

    async def get_thread_cache_state(self, room_id: str, thread_id: str) -> ThreadCacheState | None:
        """Return durable freshness metadata for one cached thread."""
        return await self._read_operation(
            room_id,
            operation="get_thread_cache_state",
            disabled_result=None,
            reader=lambda db: event_cache_threads.load_thread_cache_state(
                db,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""
        return await self._read_operation(
            room_id,
            operation="get_event",
            disabled_result=None,
            reader=lambda db: event_cache_events.load_event(db, event_id=event_id),
        )

    async def get_latest_edit(self, room_id: str, original_event_id: str) -> dict[str, Any] | None:
        """Return the latest cached edit event for one original event."""
        return await self._read_operation(
            room_id,
            operation="get_latest_edit",
            disabled_result=None,
            reader=lambda db: event_cache_events.load_latest_edit(
                db,
                room_id=room_id,
                original_event_id=original_event_id,
            ),
        )

    async def get_mxc_text(self, room_id: str, mxc_url: str) -> str | None:
        """Return one durably cached MXC text payload when present."""
        return await self._read_operation(
            room_id,
            operation="get_mxc_text",
            disabled_result=None,
            reader=lambda db: event_cache_events.load_mxc_text(
                db,
                mxc_url=mxc_url,
            ),
        )

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        """Insert or replace one individually cached Matrix event."""
        await self.store_events_batch([(event_id, room_id, event_data)])

    async def store_events_batch(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Insert or replace one batch of individually cached Matrix events."""
        if self._runtime.is_disabled or not events:
            return

        cached_at = time.time()
        events_by_room: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        for event_id, room_id, event_data in events:
            normalized_event = event_cache_events.normalize_event_source_for_cache(event_data, event_id=event_id)
            events_by_room.setdefault(room_id, []).append((event_id, normalized_event))

        for room_id, room_events in events_by_room.items():
            await self._write_operation(
                room_id,
                operation="store_events_batch",
                disabled_result=None,
                writer=lambda db,
                room_id=room_id,
                room_events=room_events,
                cached_at=cached_at: event_cache_events.persist_lookup_events(
                    db,
                    room_id=room_id,
                    room_events=room_events,
                    cached_at=cached_at,
                ),
            )

    async def store_mxc_text(self, room_id: str, mxc_url: str, text: str) -> None:
        """Insert or replace one durably cached MXC text payload."""
        await self._write_operation(
            room_id,
            operation="store_mxc_text",
            disabled_result=None,
            writer=lambda db: event_cache_events.persist_mxc_text(
                db,
                mxc_url=mxc_url,
                text=text,
                cached_at=time.time(),
            ),
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
        await self._write_operation(
            room_id,
            operation="replace_thread",
            disabled_result=None,
            writer=lambda db: event_cache_threads.replace_thread_locked(
                db,
                room_id=room_id,
                thread_id=thread_id,
                events=events,
                validated_at=time.time() if validated_at is None else validated_at,
            ),
        )

    async def replace_thread_if_not_newer(
        self,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        *,
        fetch_started_at: float,
        validated_at: float | None = None,
    ) -> bool:
        """Replace one cached thread snapshot only when nothing newer touched it after fetch start."""
        return bool(
            await self._write_operation(
                room_id,
                operation="replace_thread_if_not_newer",
                disabled_result=False,
                writer=lambda db: event_cache_threads.replace_thread_locked_if_not_newer(
                    db,
                    room_id=room_id,
                    thread_id=thread_id,
                    events=events,
                    fetch_started_at=fetch_started_at,
                    validated_at=time.time() if validated_at is None else validated_at,
                ),
            ),
        )

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""
        await self._write_operation(
            room_id,
            operation="invalidate_thread",
            disabled_result=None,
            writer=lambda db: event_cache_threads.invalidate_thread_locked(
                db,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def invalidate_room_threads(self, room_id: str) -> None:
        """Delete every cached thread snapshot for one room."""
        await self._write_operation(
            room_id,
            operation="invalidate_room_threads",
            disabled_result=None,
            writer=lambda db: event_cache_threads.invalidate_room_threads_locked(
                db,
                room_id=room_id,
            ),
        )

    async def mark_thread_stale(self, room_id: str, thread_id: str, *, reason: str) -> None:
        """Persist one durable thread invalidation marker."""
        await self._write_operation(
            room_id,
            operation="mark_thread_stale",
            disabled_result=None,
            writer=lambda db: event_cache_threads.mark_thread_stale_locked(
                db,
                room_id=room_id,
                thread_id=thread_id,
                reason=reason,
            ),
        )

    async def mark_room_threads_stale(self, room_id: str, *, reason: str) -> None:
        """Persist a durable invalidate-and-refetch marker for every cached thread in one room."""
        await self._write_operation(
            room_id,
            operation="mark_room_threads_stale",
            disabled_result=None,
            writer=lambda db: event_cache_threads.mark_room_stale_locked(
                db,
                room_id=room_id,
                reason=reason,
            ),
        )

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""
        normalized_event = event_cache_events.normalize_event_source_for_cache(event)
        return bool(
            await self._write_operation(
                room_id,
                operation="append_event",
                disabled_result=False,
                writer=lambda db: event_cache_threads.append_existing_thread_event(
                    db,
                    room_id=room_id,
                    thread_id=thread_id,
                    normalized_event=normalized_event,
                ),
            ),
        )

    async def revalidate_thread_after_incremental_update(
        self,
        room_id: str,
        thread_id: str,
        *,
        runtime_started_at: float,
    ) -> bool:
        """Refresh one thread's validated timestamp after a safe incremental update."""
        return bool(
            await self._write_operation(
                room_id,
                operation="revalidate_thread_after_incremental_update",
                disabled_result=False,
                writer=lambda db: event_cache_threads.revalidate_thread_after_incremental_update_locked(
                    db,
                    room_id=room_id,
                    thread_id=thread_id,
                    runtime_started_at=runtime_started_at,
                ),
            ),
        )

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""
        return await self._read_operation(
            room_id,
            operation="get_thread_id_for_event",
            disabled_result=None,
            reader=lambda db: event_cache_events.load_thread_id_for_event(
                db,
                room_id=room_id,
                event_id=event_id,
            ),
        )

    async def redact_event(
        self,
        room_id: str,
        event_id: str,
    ) -> bool:
        """Delete one cached event after a redaction."""
        return bool(
            await self._write_operation(
                room_id,
                operation="redact_event",
                disabled_result=False,
                writer=lambda db: event_cache_events.redact_event_locked(
                    db,
                    room_id=room_id,
                    event_id=event_id,
                ),
            ),
        )
