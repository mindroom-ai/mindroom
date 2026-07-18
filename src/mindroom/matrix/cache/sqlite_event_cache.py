"""SQLite runtime and lifecycle ownership for the Matrix event cache."""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

import aiosqlite

from mindroom.logging_config import get_logger
from mindroom.timing import milliseconds

from . import sqlite_event_cache_events, sqlite_event_cache_threads
from .cache_maintenance import CacheMetricsState, CorruptEventCachePayloadError
from .event_batching import group_lookup_events_by_room
from .event_normalization import normalize_event_source_for_cache
from .sqlite_agent_message_snapshot import load_sqlite_agent_message_snapshot
from .sqlite_cache_maintenance import (
    migrate_version_10_thread_events,
    refresh_runtime_metrics,
    run_startup_maintenance,
    sqlite_storage_bytes,
    with_sqlite_storage_bytes,
)
from .thread_cache_state import replacement_validated_at

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from .agent_message_snapshot import AgentMessageSnapshot
    from .cache_maintenance import CacheMaintenanceReport
    from .event_cache import ThreadCacheState

_EVENT_CACHE_SCHEMA_VERSION = 11
_MIGRATABLE_EVENT_CACHE_SCHEMA_VERSION = 10
_EVENT_CACHE_TABLES = (
    "cache_metadata",
    "thread_events",
    "events",
    "event_edits",
    "event_threads",
    "redacted_events",
    "mxc_text_cache",
    "thread_cache_state",
    "room_cache_state",
    "compacted_streaming_edits",
)
_REQUIRED_EVENT_CACHE_TABLES = frozenset(_EVENT_CACHE_TABLES)
_LOCK_WAIT_LOG_THRESHOLD_SECONDS = 0.1
_MAX_CACHED_ROOM_LOCKS = 256
_T = TypeVar("_T")

logger = get_logger(__name__)


async def _close_sqlite_connection_best_effort(db: aiosqlite.Connection, *, operation: str) -> None:
    """Close one SQLite connection without masking the original failure."""
    try:
        await db.close()
    except Exception as exc:
        logger.debug(
            "Ignoring error while closing SQLite event cache connection",
            operation=operation,
            error_type=type(exc).__name__,
            error=str(exc),
        )


async def _rollback_sqlite_connection_best_effort(db: aiosqlite.Connection, *, operation: str) -> None:
    """Roll back one SQLite connection without masking the original failure."""
    try:
        await db.rollback()
    except Exception as exc:
        logger.debug(
            "Ignoring SQLite event cache rollback failure",
            operation=operation,
            error_type=type(exc).__name__,
            error=str(exc),
        )


async def _initialize_event_cache_db(
    db_path: Path,
) -> tuple[aiosqlite.Connection, CacheMaintenanceReport, str]:
    """Open the SQLite database and ensure the event-cache schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")
        (
            migrated_from_schema_version,
            destructive_reset,
            normalized_legacy_thread_payload_rows,
        ) = await _prepare_event_cache_schema(
            db,
            db_path=db_path,
        )
        await _create_event_cache_schema(db)
        certification_generation = await _initialize_cache_metadata(db)
        report = await run_startup_maintenance(
            db,
            schema_version=_EVENT_CACHE_SCHEMA_VERSION,
            migrated_from_schema_version=migrated_from_schema_version,
            destructive_reset=destructive_reset,
            normalized_legacy_thread_payload_rows=normalized_legacy_thread_payload_rows,
        )
        await db.commit()
    except BaseException:
        await _rollback_sqlite_connection_best_effort(db, operation="initialize")
        await _close_sqlite_connection_best_effort(db, operation="initialize")
        raise
    report = with_sqlite_storage_bytes(report, db_path)
    logger.info("Matrix event cache startup maintenance complete", backend="sqlite", **report.as_runtime_diagnostics())
    return db, report, certification_generation


async def _create_event_cache_schema(db: aiosqlite.Connection) -> None:
    """Create the current cache schema in one SQLite connection."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS cache_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_events (
            room_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL,
            write_seq INTEGER NOT NULL,
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
            origin_server_ts INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            cached_at REAL NOT NULL,
            write_seq INTEGER NOT NULL
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_room_origin_ts
        ON events(room_id, origin_server_ts DESC)
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
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS compacted_streaming_edits (
            event_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            original_event_id TEXT NOT NULL,
            sender TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL,
            event_json_zlib BLOB NOT NULL,
            cached_at REAL NOT NULL,
            event_order INTEGER NOT NULL,
            thread_id TEXT,
            thread_origin_server_ts INTEGER,
            thread_order INTEGER,
            indexed_thread_id TEXT
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_compacted_streaming_edits_original
        ON compacted_streaming_edits(room_id, original_event_id, sender, origin_server_ts)
        """,
    )
    await db.execute(f"PRAGMA user_version = {_EVENT_CACHE_SCHEMA_VERSION}")


async def _initialize_cache_metadata(db: aiosqlite.Connection) -> str:
    """Initialize durable ordering and sync-certification metadata."""
    cursor = await db.execute(
        """
        SELECT MAX(value)
        FROM (
            SELECT COALESCE(MAX(write_seq), 0) AS value FROM events
            UNION ALL
            SELECT COALESCE(MAX(write_seq), 0) AS value FROM thread_events
            UNION ALL
            SELECT COALESCE(MAX(event_order), 0) AS value FROM compacted_streaming_edits
            UNION ALL
            SELECT COALESCE(MAX(thread_order), 0) AS value FROM compacted_streaming_edits
        )
        """,
    )
    row = await cursor.fetchone()
    await cursor.close()
    maximum_write_sequence = 0 if row is None or row[0] is None else int(row[0])
    await db.execute(
        """
        INSERT INTO cache_metadata(key, value)
        VALUES ('write_sequence', ?)
        ON CONFLICT(key) DO UPDATE SET
            value = CAST(MAX(CAST(cache_metadata.value AS INTEGER), CAST(excluded.value AS INTEGER)) AS TEXT)
        """,
        (str(maximum_write_sequence),),
    )
    await db.execute(
        """
        INSERT INTO cache_metadata(key, value)
        VALUES ('certification_generation', ?)
        ON CONFLICT(key) DO NOTHING
        """,
        (uuid.uuid4().hex,),
    )
    generation_cursor = await db.execute(
        "SELECT value FROM cache_metadata WHERE key = 'certification_generation'",
    )
    generation_row = await generation_cursor.fetchone()
    await generation_cursor.close()
    if generation_row is None or not str(generation_row[0]):
        msg = "SQLite event cache certification generation was not initialized"
        raise RuntimeError(msg)
    return str(generation_row[0])


async def _schema_version(db: aiosqlite.Connection) -> int:
    """Return the current SQLite schema version for this cache."""
    cursor = await db.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    await cursor.close()
    return 0 if row is None else int(row[0])


async def _existing_table_names(db: aiosqlite.Connection) -> set[str]:
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


async def _prepare_event_cache_schema(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
) -> tuple[int | None, bool, int]:
    """Migrate version 10 or reset unsupported cache shapes in the active transaction."""
    current_schema_version = await _schema_version(db)
    current_table_names = await _existing_table_names(db)
    if not current_table_names:
        return None, False, 0
    if current_schema_version == _EVENT_CACHE_SCHEMA_VERSION and _REQUIRED_EVENT_CACHE_TABLES.issubset(
        current_table_names,
    ):
        return None, False, 0

    version_10_tables = _REQUIRED_EVENT_CACHE_TABLES - {"cache_metadata", "compacted_streaming_edits"}
    if current_schema_version == _MIGRATABLE_EVENT_CACHE_SCHEMA_VERSION and version_10_tables.issubset(
        current_table_names,
    ):
        logger.info(
            "Migrating Matrix event cache schema",
            db_path=str(db_path),
            from_schema_version=current_schema_version,
            to_schema_version=_EVENT_CACHE_SCHEMA_VERSION,
        )
        normalized_legacy_thread_payload_rows = await migrate_version_10_thread_events(db)
        return current_schema_version, False, normalized_legacy_thread_payload_rows

    logger.info(
        "Resetting unsupported Matrix event cache schema",
        db_path=str(db_path),
        _schema_version=current_schema_version,
        existing_tables=sorted(current_table_names),
    )
    for table_name in (*_EVENT_CACHE_TABLES, "thread_events_v10"):
        await db.execute(f"DROP TABLE IF EXISTS {table_name}")
    await db.execute("PRAGMA user_version = 0")
    return None, True, 0


@dataclass
class _RoomLockEntry:
    """Track one room lock plus queued users that still rely on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_users: int = 0


class _SqliteEventCacheRuntime:
    """Own runtime-only lifecycle, locking, and disable state for one cache instance."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._metrics = CacheMetricsState()
        self._certification_generation: str | None = None
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
    def is_initialized(self) -> bool:
        """Return whether the SQLite connection is currently open."""
        return self._db is not None

    @property
    def is_disabled(self) -> bool:
        """Return whether the advisory cache is disabled for this runtime."""
        return self._disabled_reason is not None

    @property
    def disabled_reason(self) -> str | None:
        """Return the log-safe reason this advisory cache was disabled."""
        return self._disabled_reason

    @property
    def maintenance_report(self) -> CacheMaintenanceReport | None:
        """Return current counts plus immutable startup maintenance outcomes."""
        return self._metrics.report

    @property
    def certification_generation(self) -> str | None:
        """Return the durable generation bound to certified sync checkpoints."""
        return self._certification_generation

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
            self._db, report, self._certification_generation = await _initialize_event_cache_db(self._db_path)
            self._metrics.initialize(report)

    async def close(self) -> None:
        """Close the SQLite connection when the cache is no longer needed."""
        async with self._db_lock:
            if self._db is None:
                return
            await self._db.close()
            self._db = None
            self._certification_generation = None
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
                    "Waited for SqliteEventCache room lock",
                    room_id=room_id,
                    operation=operation,
                    wait_time_ms=milliseconds(wait_time, ndigits=2),
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
            msg = "SqliteEventCache has not been initialized"
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


class SqliteEventCache:
    """SQLite-backed ConversationEventCache implementation."""

    def __init__(self, db_path: Path) -> None:
        self._runtime = _SqliteEventCacheRuntime(db_path)
        self._metrics_refresh_task: asyncio.Task[None] | None = None
        self._metrics_refresh_enabled = True

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path for this cache instance."""
        return self._runtime.db_path

    @property
    def is_initialized(self) -> bool:
        """Return whether the SQLite connection is currently open."""
        return self._runtime.is_initialized

    @property
    def durable_writes_available(self) -> bool:
        """Return whether cache writes can durably persist data."""
        return self._runtime.is_initialized and not self._runtime.is_disabled

    @property
    def startup_requires_sync_reset(self) -> bool:
        """Return whether startup destructively reset cache contents."""
        report = self._runtime.maintenance_report
        return report is not None and report.startup_requires_sync_reset

    @property
    def certification_generation(self) -> str | None:
        """Return the durable generation bound to certified sync checkpoints."""
        return self._runtime.certification_generation

    def runtime_diagnostics(self) -> dict[str, object]:
        """Return log-safe runtime state for sync certification diagnostics."""
        storage_bytes = sqlite_storage_bytes(self.db_path)
        diagnostics: dict[str, object] = {
            "cache_backend": "sqlite",
            "cache_sqlite_initialized": self._runtime.is_initialized,
            "cache_sqlite_disabled": self._runtime.is_disabled,
            "cache_certification_generation_present": self.certification_generation is not None,
            "cache_storage_bytes_available": storage_bytes is not None,
        }
        if storage_bytes is not None:
            diagnostics["cache_storage_bytes"] = storage_bytes
        if self._runtime.disabled_reason is not None:
            diagnostics["cache_sqlite_disabled_reason"] = self._runtime.disabled_reason
        diagnostics.update(self._runtime._metrics.as_runtime_diagnostics())
        diagnostics["cache_metrics_refresh_in_progress"] = self._metrics_refresh_task is not None
        return diagnostics

    async def refresh_runtime_diagnostics(self) -> dict[str, object]:
        """Force an exact metrics refresh without changing cache availability on telemetry failure."""
        try:
            await self._cancel_metrics_refresh(disable=False)
            return await self._collect_runtime_diagnostics()
        finally:
            if (
                self._metrics_refresh_enabled
                and self._runtime._metrics.dirty
                and self._runtime.is_initialized
                and not self._runtime.is_disabled
            ):
                self._schedule_metrics_refresh()

    async def _collect_runtime_diagnostics(self) -> dict[str, object]:
        """Collect one exact SQLite storage snapshot."""
        if self._runtime.is_disabled or not self._runtime.is_initialized:
            return self.runtime_diagnostics()
        async with self._runtime._db_lock:
            db = self._runtime.require_db()
            report = self._runtime.maintenance_report
            if report is None:
                return self.runtime_diagnostics()
            try:
                await db.execute("BEGIN")
                refreshed_report = await refresh_runtime_metrics(
                    db,
                    startup_report=report,
                    db_path=self.db_path,
                )
                await db.commit()
            except asyncio.CancelledError:
                await _rollback_sqlite_connection_best_effort(db, operation="refresh_runtime_diagnostics")
                raise
            except Exception as exc:
                await _rollback_sqlite_connection_best_effort(db, operation="refresh_runtime_diagnostics")
                self._runtime._metrics.record_refresh_failure()
                logger.warning(
                    "Matrix event cache metrics refresh failed",
                    backend="sqlite",
                    error_type=type(exc).__name__,
                )
            else:
                self._runtime._metrics.record_refresh(refreshed_report)
                logger.info(
                    "Matrix event cache runtime metrics refreshed",
                    backend="sqlite",
                    **refreshed_report.as_runtime_diagnostics(snapshot="runtime"),
                )
        return self.runtime_diagnostics()

    def pending_durable_write_room_ids(self) -> tuple[str, ...]:
        """Return rooms with runtime-only writes that must persist before certifying a sync token."""
        return ()

    async def flush_pending_durable_writes(self, room_id: str) -> None:
        """Persist runtime-only writes for one room before certifying a sync token."""
        _ = room_id

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        self._metrics_refresh_enabled = True
        await self._runtime.initialize()

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""
        self._runtime.disable(reason)

    async def close(self) -> None:
        """Close the SQLite connection when the cache is no longer needed."""
        await self._cancel_metrics_refresh()
        await self._runtime.close()

    async def _read_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: _T,
        reader: Callable[[aiosqlite.Connection], Awaitable[_T]],
    ) -> _T:
        if self._runtime.is_disabled:
            return disabled_result
        async with self._runtime.acquire_db_operation(room_id, operation=operation) as db:
            try:
                return await reader(db)
            except CorruptEventCachePayloadError:
                self._runtime.disable("corrupt_compacted_event_payload")
                return disabled_result

    async def _write_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: _T,
        writer: Callable[[aiosqlite.Connection], Awaitable[_T]],
    ) -> _T:
        if self._runtime.is_disabled:
            return disabled_result
        async with self._runtime.acquire_db_operation(room_id, operation=operation) as db:
            try:
                result = await writer(db)
                await db.commit()
            except BaseException:
                await _rollback_sqlite_connection_best_effort(db, operation=operation)
                raise
        self._runtime._metrics.mark_dirty()
        self._schedule_metrics_refresh()
        return result

    def _schedule_metrics_refresh(self) -> None:
        """Coalesce hot-path mutations into one bounded-staleness metrics refresh."""
        if not self._metrics_refresh_enabled or self._metrics_refresh_task is not None:
            return
        self._metrics_refresh_task = asyncio.create_task(
            self._refresh_metrics_after_delay(),
            name="sqlite_event_cache_metrics_refresh",
        )

    async def _refresh_metrics_after_delay(self) -> None:
        """Refresh metrics after the shared throttle interval."""
        refresh_task = asyncio.current_task()
        try:
            await asyncio.sleep(self._runtime._metrics.refresh_delay_seconds)
            await self._collect_runtime_diagnostics()
        finally:
            if self._metrics_refresh_task is refresh_task:
                self._metrics_refresh_task = None
            if (
                self._metrics_refresh_enabled
                and self._runtime._metrics.dirty
                and self._runtime.is_initialized
                and not self._runtime.is_disabled
            ):
                self._schedule_metrics_refresh()

    async def _cancel_metrics_refresh(self, *, disable: bool = True) -> None:
        """Cancel and await the owned metrics task during shutdown."""
        refresh_was_enabled = self._metrics_refresh_enabled
        self._metrics_refresh_enabled = False
        task = self._metrics_refresh_task
        try:
            if task is None:
                return
            task.cancel()
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                caller = asyncio.current_task()
                if caller is not None and caller.cancelling():
                    raise
        finally:
            if not disable:
                self._metrics_refresh_enabled = refresh_was_enabled

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""
        return await self._read_operation(
            room_id,
            operation="get_thread_events",
            disabled_result=None,
            reader=lambda db: sqlite_event_cache_threads.load_thread_events(
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
            reader=lambda db: sqlite_event_cache_threads.load_recent_room_thread_ids(
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
            reader=lambda db: sqlite_event_cache_threads.load_thread_cache_state(
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
            reader=lambda db: sqlite_event_cache_events.load_event(db, event_id=event_id),
        )

    async def get_recent_room_events(
        self,
        room_id: str,
        *,
        event_type: str,
        since_ts_ms: int,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        """Return recent cached room events of `event_type` since `since_ts_ms`, newest first."""
        return await self._read_operation(
            room_id,
            operation="get_recent_room_events",
            disabled_result=[],
            reader=lambda db: sqlite_event_cache_events.load_recent_room_events(
                db,
                room_id=room_id,
                event_type=event_type,
                since_ts_ms=since_ts_ms,
                limit=limit,
            ),
        )

    async def get_latest_edit(
        self,
        room_id: str,
        original_event_id: str,
        *,
        sender: str | None = None,
    ) -> dict[str, Any] | None:
        """Return the latest cached edit event for one original event."""
        return await self._read_operation(
            room_id,
            operation="get_latest_edit",
            disabled_result=None,
            reader=lambda db: sqlite_event_cache_events.load_latest_edit(
                db,
                room_id=room_id,
                original_event_id=original_event_id,
                sender=sender,
            ),
        )

    async def get_latest_agent_message_snapshot(
        self,
        room_id: str,
        thread_id: str | None,
        sender: str,
        *,
        runtime_started_at: float | None,
    ) -> AgentMessageSnapshot | None:
        """Return the latest visible cached message from one sender in the given scope."""
        return await self._read_operation(
            room_id,
            operation="get_latest_agent_message_snapshot",
            disabled_result=None,
            reader=lambda db: load_sqlite_agent_message_snapshot(
                db,
                room_id=room_id,
                thread_id=thread_id,
                sender=sender,
                runtime_started_at=runtime_started_at,
            ),
        )

    async def get_mxc_text(self, room_id: str, mxc_url: str) -> str | None:
        """Return one durably cached MXC text payload when present."""
        return await self._read_operation(
            room_id,
            operation="get_mxc_text",
            disabled_result=None,
            reader=lambda db: sqlite_event_cache_events.load_mxc_text(
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
        for room_id, room_events in group_lookup_events_by_room(events).items():
            await self._write_operation(
                room_id,
                operation="store_events_batch",
                disabled_result=None,
                writer=lambda db, room_id=room_id, room_events=room_events, cached_at=cached_at: (
                    sqlite_event_cache_events.persist_lookup_events(
                        db,
                        room_id=room_id,
                        room_events=room_events,
                        cached_at=cached_at,
                    )
                ),
            )

    async def store_mxc_text(self, room_id: str, mxc_url: str, text: str) -> None:
        """Insert or replace one durably cached MXC text payload."""
        await self._write_operation(
            room_id,
            operation="store_mxc_text",
            disabled_result=None,
            writer=lambda db: sqlite_event_cache_events.persist_mxc_text(
                db,
                mxc_url=mxc_url,
                text=text,
                cached_at=time.time(),
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
        replacement_timestamp = replacement_validated_at(
            fetch_started_at=fetch_started_at,
            validated_at=validated_at,
        )

        async def replace_if_still_safe(db: aiosqlite.Connection) -> bool:
            return await sqlite_event_cache_threads.replace_thread_locked_if_not_newer(
                db,
                room_id=room_id,
                thread_id=thread_id,
                events=events,
                fetch_started_at=fetch_started_at,
                validated_at=replacement_timestamp,
            )

        return bool(
            await self._write_operation(
                room_id,
                operation="replace_thread_if_not_newer",
                disabled_result=False,
                writer=replace_if_still_safe,
            ),
        )

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""
        await self._write_operation(
            room_id,
            operation="invalidate_thread",
            disabled_result=None,
            writer=lambda db: sqlite_event_cache_threads.invalidate_thread_locked(
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
            writer=lambda db: sqlite_event_cache_threads.invalidate_room_threads_locked(
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
            writer=lambda db: sqlite_event_cache_threads.mark_thread_stale_locked(
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
            writer=lambda db: sqlite_event_cache_threads.mark_room_stale_locked(
                db,
                room_id=room_id,
                reason=reason,
            ),
        )

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""
        normalized_event = normalize_event_source_for_cache(event)
        return bool(
            await self._write_operation(
                room_id,
                operation="append_event",
                disabled_result=False,
                writer=lambda db: sqlite_event_cache_threads.append_existing_thread_event(
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
    ) -> bool:
        """Refresh one thread's validated timestamp after a safe incremental update."""
        return bool(
            await self._write_operation(
                room_id,
                operation="revalidate_thread_after_incremental_update",
                disabled_result=False,
                writer=lambda db: sqlite_event_cache_threads.revalidate_thread_after_incremental_update_locked(
                    db,
                    room_id=room_id,
                    thread_id=thread_id,
                ),
            ),
        )

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""
        return await self._read_operation(
            room_id,
            operation="get_thread_id_for_event",
            disabled_result=None,
            reader=lambda db: sqlite_event_cache_events.load_thread_id_for_event(
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
                writer=lambda db: sqlite_event_cache_events.redact_event_locked(
                    db,
                    room_id=room_id,
                    event_id=event_id,
                ),
            ),
        )
