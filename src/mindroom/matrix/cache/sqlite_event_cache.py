"""SQLite runtime and lifecycle ownership for the Matrix event cache."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, TypeVar

import aiosqlite

from mindroom.logging_config import get_logger

from . import sqlite_event_cache_events, sqlite_event_cache_threads
from .event_batching import group_lookup_events_by_room
from .event_normalization import normalize_event_source_for_cache
from .sqlite_agent_message_snapshot import load_sqlite_agent_message_snapshot
from .thread_cache_state import replacement_validated_at

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from .agent_message_snapshot import AgentMessageSnapshot
    from .event_cache import ThreadCacheState

_EVENT_CACHE_SCHEMA_VERSION = 11
_EVENT_CACHE_TABLES = (
    "thread_events",
    "events",
    "event_edits",
    "event_threads",
    "redacted_events",
    "event_mxc_references",
    "mxc_text_cache",
    "thread_cache_state",
    "room_cache_state",
    "event_cache_metadata",
)
_REQUIRED_EVENT_CACHE_TABLES = frozenset(_EVENT_CACHE_TABLES)
_DEFAULT_PRINCIPAL_ID = "__mindroom_default_principal__"
_PRINCIPAL_PURGE_LOCK_SCOPE = "__mindroom_principal_purge__"
_T = TypeVar("_T")

logger = get_logger(__name__)


async def _noop_write(_db: aiosqlite.Connection) -> None:
    """Complete an operation after its pending runtime writes are flushed."""


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


async def _initialize_event_cache_db(db_path: Path) -> aiosqlite.Connection:
    """Open the SQLite database and ensure the event-cache schema exists."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(db_path)
    try:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.execute("BEGIN IMMEDIATE")
        try:
            await _reset_stale_cache_if_needed(db, db_path=db_path)
            await _create_event_cache_schema(db)
            await db.commit()
        except BaseException:
            await _rollback_sqlite_connection_best_effort(db, operation="initialize")
            raise
    except BaseException:
        await _close_sqlite_connection_best_effort(db, operation="initialize")
        raise
    return db


async def _create_event_cache_schema(db: aiosqlite.Connection) -> None:
    """Create the current cache schema in one SQLite connection."""
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_events (
            principal_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            PRIMARY KEY (principal_id, room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_thread_events_room_thread_ts
        ON thread_events(principal_id, room_id, thread_id, origin_server_ts)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
            principal_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            cached_at REAL NOT NULL,
            PRIMARY KEY (principal_id, room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_events_room_origin_ts
        ON events(principal_id, room_id, origin_server_ts DESC)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_edits (
            principal_id TEXT NOT NULL,
            edit_event_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            original_event_id TEXT NOT NULL,
            origin_server_ts INTEGER NOT NULL,
            PRIMARY KEY (principal_id, room_id, edit_event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_edits_room_original_ts
        ON event_edits(principal_id, room_id, original_event_id, origin_server_ts DESC, edit_event_id DESC)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_threads (
            principal_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            PRIMARY KEY (principal_id, room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_threads_room_thread
        ON event_threads(principal_id, room_id, thread_id, event_id)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS redacted_events (
            principal_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            PRIMARY KEY (principal_id, room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mxc_text_cache (
            principal_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            mxc_url TEXT NOT NULL,
            text_content TEXT NOT NULL,
            cached_at REAL NOT NULL,
            PRIMARY KEY (principal_id, room_id, mxc_url)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_mxc_references (
            principal_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            mxc_url TEXT NOT NULL,
            PRIMARY KEY (principal_id, room_id, event_id, mxc_url)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_event_mxc_references_plaintext
        ON event_mxc_references(principal_id, room_id, mxc_url, event_id)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS thread_cache_state (
            principal_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            validated_at REAL,
            invalidated_at REAL,
            invalidation_reason TEXT,
            PRIMARY KEY (principal_id, room_id, thread_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS room_cache_state (
            principal_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            invalidated_at REAL,
            invalidation_reason TEXT,
            PRIMARY KEY (principal_id, room_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS event_cache_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    )
    await db.execute(
        """
        INSERT OR IGNORE INTO event_cache_metadata(key, value)
        VALUES ('generation', ?)
        """,
        (uuid.uuid4().hex,),
    )
    await db.execute(f"PRAGMA user_version = {_EVENT_CACHE_SCHEMA_VERSION}")


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


async def _reset_stale_cache_if_needed(
    db: aiosqlite.Connection,
    *,
    db_path: Path,
) -> None:
    """Drop stale cache contents instead of migrating old cache schemas forward."""
    current_schema_version = await _schema_version(db)
    current_table_names = await _existing_table_names(db)
    if not current_table_names:
        return
    if current_schema_version == _EVENT_CACHE_SCHEMA_VERSION and _REQUIRED_EVENT_CACHE_TABLES.issubset(
        current_table_names,
    ):
        return

    logger.info(
        "Resetting stale Matrix event cache instead of migrating it",
        db_path=str(db_path),
        _schema_version=current_schema_version,
        existing_tables=sorted(current_table_names),
    )
    for table_name in _EVENT_CACHE_TABLES:
        await db.execute(f"DROP TABLE IF EXISTS {table_name}")
    await db.execute("PRAGMA user_version = 0")


class _SqliteEventCacheRuntime:
    """Own runtime-only lifecycle, locking, and disable state for one cache instance."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: aiosqlite.Connection | None = None
        self._disabled_reason: str | None = None
        self._disabled_principal_reasons: dict[str, str] = {}
        self._db_lock = asyncio.Lock()
        self._generation: str | None = None
        self._pending_room_purges: set[tuple[str, str]] = set()
        self._pending_principal_purges: set[str] = set()
        self._departed_rooms: set[tuple[str, str]] = set()
        self._room_departure_epochs: dict[tuple[str, str], int] = {}

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
    def generation(self) -> str | None:
        """Return the durable cache generation when the database is initialized."""
        return self._generation

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

    def disable_principal(self, principal_id: str, reason: str) -> None:
        """Disable one principal view without affecting other Matrix accounts."""
        if principal_id in self._disabled_principal_reasons:
            return
        self._disabled_principal_reasons[principal_id] = reason
        logger.warning(
            "Disabling principal Matrix event cache view",
            db_path=str(self._db_path),
            principal_id=principal_id,
            reason=reason,
        )

    def is_principal_disabled(self, principal_id: str) -> bool:
        """Return whether one principal view is disabled for this runtime."""
        return principal_id in self._disabled_principal_reasons

    def mark_room_departed(self, principal_id: str, room_id: str) -> int:
        """Fence one departed principal-room, queue its deletion, and return its new epoch."""
        key = (principal_id, room_id)
        epoch = self._room_departure_epochs.get(key, 0) + 1
        self._room_departure_epochs[key] = epoch
        self._departed_rooms.add(key)
        self._pending_room_purges.add(key)
        return epoch

    def mark_room_joined(self, principal_id: str, room_id: str, *, expected_departure_epoch: int) -> None:
        """Remove one fence only when no newer departure superseded the join."""
        key = (principal_id, room_id)
        if self._room_departure_epochs.get(key, 0) == expected_departure_epoch:
            self._departed_rooms.discard(key)

    def room_departure_epoch(self, principal_id: str, room_id: str) -> int:
        """Return the current fence epoch for one principal-room."""
        return self._room_departure_epochs.get((principal_id, room_id), 0)

    def is_room_departed(self, principal_id: str, room_id: str) -> bool:
        """Return whether one principal-room is fenced after leave or ban."""
        return (principal_id, room_id) in self._departed_rooms

    def record_pending_principal_purge(self, principal_id: str) -> None:
        """Remember a principal deletion until a SQLite transaction commits it."""
        self._pending_principal_purges.add(principal_id)

    def has_pending_principal_purge(self, principal_id: str) -> bool:
        """Return whether every row for one principal must be deleted."""
        return principal_id in self._pending_principal_purges

    def forget_pending_principal_purge(self, principal_id: str) -> None:
        """Forget one committed principal deletion and covered room deletions."""
        self._pending_principal_purges.discard(principal_id)
        self._pending_room_purges = {key for key in self._pending_room_purges if key[0] != principal_id}

    def has_pending_room_purge(self, principal_id: str, room_id: str) -> bool:
        """Return whether one principal-room deletion is still pending."""
        return (principal_id, room_id) in self._pending_room_purges

    def forget_pending_room_purge(self, principal_id: str, room_id: str) -> None:
        """Forget one committed principal-room deletion."""
        self._pending_room_purges.discard((principal_id, room_id))

    def pending_room_purge_ids(self, principal_id: str) -> tuple[str, ...]:
        """Return rooms with runtime-only deletions for one principal."""
        return tuple(sorted(room_id for owner, room_id in self._pending_room_purges if owner == principal_id))

    def departed_room_ids(self, principal_id: str) -> tuple[str, ...]:
        """Return runtime-fenced rooms for one principal."""
        return tuple(sorted(room_id for owner, room_id in self._departed_rooms if owner == principal_id))

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        async with self._db_lock:
            if self._disabled_reason is not None or self._db is not None:
                return
            db = await _initialize_event_cache_db(self._db_path)
            cursor = await db.execute(
                "SELECT value FROM event_cache_metadata WHERE key = 'generation'",
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row is None:
                await _close_sqlite_connection_best_effort(db, operation="initialize_generation")
                msg = "SQLite event cache generation metadata is missing"
                raise RuntimeError(msg)
            self._db = db
            self._generation = str(row[0])

    async def close(self) -> None:
        """Close the SQLite connection when the cache is no longer needed."""
        async with self._db_lock:
            if self._db is None:
                return
            await self._db.close()
            self._db = None
            self._generation = None

    @asynccontextmanager
    async def acquire_db_operation(self) -> AsyncIterator[aiosqlite.Connection]:
        """Serialize one operation on the runtime's single SQLite connection."""
        if self._db is None:
            await self.initialize()
        async with self._db_lock:
            yield self.require_db()

    def require_db(self) -> aiosqlite.Connection:
        """Return the active SQLite connection or raise if uninitialized."""
        if self._db is None:
            msg = "SqliteEventCache has not been initialized"
            raise RuntimeError(msg)
        return self._db


class SqliteEventCache:
    """SQLite-backed ConversationEventCache implementation."""

    def __init__(
        self,
        db_path: Path,
        *,
        principal_id: str = _DEFAULT_PRINCIPAL_ID,
        _runtime: _SqliteEventCacheRuntime | None = None,
    ) -> None:
        self._owns_runtime = _runtime is None
        self._runtime = _SqliteEventCacheRuntime(db_path) if _runtime is None else _runtime
        self._principal_id = principal_id

    @property
    def principal_id(self) -> str:
        """Return the Matrix principal bound to this cache view."""
        return self._principal_id

    def for_principal(self, principal_id: str) -> SqliteEventCache:
        """Return a view restricted to one stable Matrix user ID."""
        normalized_principal_id = principal_id.strip()
        if not normalized_principal_id:
            msg = "Matrix event cache principal_id must be non-empty"
            raise ValueError(msg)
        return SqliteEventCache(
            self.db_path,
            principal_id=normalized_principal_id,
            _runtime=self._runtime,
        )

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
        return (
            self._runtime.is_initialized
            and not self._runtime.is_disabled
            and not self._runtime.is_principal_disabled(self.principal_id)
        )

    @property
    def cache_generation(self) -> str | None:
        """Return the generation that certified sync checkpoints must match when available."""
        generation = self._runtime.generation
        if (
            generation is None
            or self._runtime.is_principal_disabled(self.principal_id)
            or self._runtime.has_pending_principal_purge(self.principal_id)
        ):
            return None
        principal_generation = f"{generation}\0{self.principal_id}".encode()
        return hashlib.sha256(principal_generation).hexdigest()

    def runtime_diagnostics(self) -> dict[str, object]:
        """Return log-safe runtime state for sync certification diagnostics."""
        return {
            "cache_backend": "sqlite",
            "cache_sqlite_initialized": self._runtime.is_initialized,
            "cache_sqlite_disabled": self._runtime.is_disabled,
            "cache_sqlite_principal_disabled": self._runtime.is_principal_disabled(self.principal_id),
            "cache_sqlite_pending_room_purges": len(
                self._runtime.pending_room_purge_ids(self.principal_id),
            ),
            "cache_sqlite_pending_principal_purge": self._runtime.has_pending_principal_purge(
                self.principal_id,
            ),
            "cache_sqlite_departed_room_count": len(self._runtime.departed_room_ids(self.principal_id)),
        }

    def pending_durable_write_room_ids(self) -> tuple[str, ...]:
        """Return rooms with runtime-only writes that must persist before certifying a sync token."""
        return self._runtime.pending_room_purge_ids(self.principal_id)

    async def flush_pending_durable_writes(self, room_id: str) -> None:
        """Persist runtime-only writes for one room before certifying a sync token."""
        if self._runtime.has_pending_room_purge(self.principal_id, room_id):
            await self._write_operation(
                room_id,
                operation="flush_pending_durable_writes",
                disabled_result=None,
                writer=_noop_write,
                allow_departed=True,
            )

    async def initialize(self) -> None:
        """Open the SQLite database and create the cache schema."""
        await self._runtime.initialize()

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""
        if self._owns_runtime:
            self._runtime.disable(reason)
        else:
            self._runtime.disable_principal(self.principal_id, reason)

    async def close(self) -> None:
        """Close shared storage only from the root cache owner."""
        if self._owns_runtime:
            await self._runtime.close()

    async def _read_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: _T,
        reader: Callable[[aiosqlite.Connection], Awaitable[_T]],
    ) -> _T:
        if (
            self._runtime.is_disabled
            or self._runtime.is_principal_disabled(self.principal_id)
            or self._runtime.is_room_departed(self.principal_id, room_id)
        ):
            return disabled_result
        async with self._runtime.acquire_db_operation() as db:
            if self._runtime.is_principal_disabled(self.principal_id) or self._runtime.is_room_departed(
                self.principal_id,
                room_id,
            ):
                return disabled_result
            if self._runtime.has_pending_principal_purge(self.principal_id):
                try:
                    await sqlite_event_cache_events.purge_principal_locked(
                        db,
                        principal_id=self.principal_id,
                    )
                    await db.commit()
                except BaseException:
                    await _rollback_sqlite_connection_best_effort(db, operation=operation)
                    raise
                self._runtime.forget_pending_principal_purge(self.principal_id)
            result = await reader(db)
            if (
                self._runtime.is_disabled
                or self._runtime.is_principal_disabled(self.principal_id)
                or self._runtime.is_room_departed(self.principal_id, room_id)
            ):
                return disabled_result
            return result

    async def _write_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: _T,
        writer: Callable[[aiosqlite.Connection], Awaitable[_T]],
        allow_departed: bool = False,
    ) -> _T:
        if not self._can_expose_write_result(room_id, allow_departed=allow_departed):
            return disabled_result
        async with self._runtime.acquire_db_operation() as db:
            if not self._can_expose_write_result(room_id, allow_departed=allow_departed):
                return disabled_result
            pending_principal_purge = self._runtime.has_pending_principal_purge(self.principal_id)
            pending_room_purge = self._runtime.has_pending_room_purge(self.principal_id, room_id)
            try:
                await db.execute("BEGIN IMMEDIATE")
                if pending_principal_purge:
                    await sqlite_event_cache_events.purge_principal_locked(
                        db,
                        principal_id=self.principal_id,
                    )
                elif pending_room_purge:
                    await sqlite_event_cache_events.purge_room_locked(
                        db,
                        principal_id=self.principal_id,
                        room_id=room_id,
                    )
                if pending_principal_purge or pending_room_purge:
                    result = disabled_result
                else:
                    result = await writer(db)
                await db.commit()
            except BaseException:
                await _rollback_sqlite_connection_best_effort(db, operation=operation)
                raise
            if pending_principal_purge:
                self._runtime.forget_pending_principal_purge(self.principal_id)
            elif pending_room_purge:
                self._runtime.forget_pending_room_purge(self.principal_id, room_id)
            if not self._can_expose_write_result(room_id, allow_departed=allow_departed):
                return disabled_result
            return result

    def _can_expose_write_result(self, room_id: str, *, allow_departed: bool) -> bool:
        """Return whether current runtime state still authorizes one write result."""
        return (
            not self._runtime.is_disabled
            and not self._runtime.is_principal_disabled(self.principal_id)
            and (allow_departed or not self._runtime.is_room_departed(self.principal_id, room_id))
        )

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""
        return await self._read_operation(
            room_id,
            operation="get_thread_events",
            disabled_result=None,
            reader=lambda db: sqlite_event_cache_threads.load_thread_events(
                db,
                principal_id=self.principal_id,
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
                principal_id=self.principal_id,
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
                principal_id=self.principal_id,
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
            reader=lambda db: sqlite_event_cache_events.load_event(
                db,
                principal_id=self.principal_id,
                room_id=room_id,
                event_id=event_id,
            ),
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
                principal_id=self.principal_id,
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
                principal_id=self.principal_id,
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
                principal_id=self.principal_id,
                room_id=room_id,
                thread_id=thread_id,
                sender=sender,
                runtime_started_at=runtime_started_at,
            ),
        )

    async def get_mxc_text(self, room_id: str, event_id: str, mxc_url: str) -> str | None:
        """Return MXC plaintext only while a visible owning event references it."""
        return await self._read_operation(
            room_id,
            operation="get_mxc_text",
            disabled_result=None,
            reader=lambda db: sqlite_event_cache_events.load_mxc_text(
                db,
                principal_id=self.principal_id,
                room_id=room_id,
                event_id=event_id,
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
                        principal_id=self.principal_id,
                        room_id=room_id,
                        room_events=room_events,
                        cached_at=cached_at,
                    )
                ),
            )

    async def store_mxc_text(self, room_id: str, event_id: str, mxc_url: str, text: str) -> bool:
        """Cache MXC plaintext only for a visible, non-tombstoned owning event."""
        return bool(
            await self._write_operation(
                room_id,
                operation="store_mxc_text",
                disabled_result=False,
                writer=lambda db: sqlite_event_cache_events.persist_mxc_text(
                    db,
                    principal_id=self.principal_id,
                    room_id=room_id,
                    event_id=event_id,
                    mxc_url=mxc_url,
                    text=text,
                    cached_at=time.time(),
                ),
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
                principal_id=self.principal_id,
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
                principal_id=self.principal_id,
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
                principal_id=self.principal_id,
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
                principal_id=self.principal_id,
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
                principal_id=self.principal_id,
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
                    principal_id=self.principal_id,
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
                    principal_id=self.principal_id,
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
                principal_id=self.principal_id,
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
                    principal_id=self.principal_id,
                    room_id=room_id,
                    event_id=event_id,
                ),
            ),
        )

    async def purge_room(self, room_id: str) -> None:
        """Delete only this principal's cached ownership for one left or banned room."""
        if not self._runtime.is_room_departed(self.principal_id, room_id):
            self.mark_room_departed(room_id)

        await self._write_operation(
            room_id,
            operation="purge_room",
            disabled_result=None,
            writer=_noop_write,
            allow_departed=True,
        )

    def mark_room_departed(self, room_id: str) -> int:
        """Synchronously reject access and return the new room-fence epoch."""
        return self._runtime.mark_room_departed(self.principal_id, room_id)

    def room_departure_epoch(self, room_id: str) -> int:
        """Return the current room-fence epoch."""
        return self._runtime.room_departure_epoch(self.principal_id, room_id)

    async def mark_room_joined(
        self,
        room_id: str,
        *,
        expected_departure_epoch: int | None = None,
    ) -> None:
        """Remove a departure fence only after any pending purge commits."""
        if not self._runtime.is_room_departed(self.principal_id, room_id):
            return
        departure_epoch = (
            self.room_departure_epoch(room_id) if expected_departure_epoch is None else expected_departure_epoch
        )
        if self.room_departure_epoch(room_id) != departure_epoch:
            return
        if not self.durable_writes_available:
            return

        await self._write_operation(
            room_id,
            operation="mark_room_joined",
            disabled_result=None,
            writer=_noop_write,
            allow_departed=True,
        )
        if not self._runtime.has_pending_room_purge(self.principal_id, room_id):
            self._runtime.mark_room_joined(
                self.principal_id,
                room_id,
                expected_departure_epoch=departure_epoch,
            )

    async def purge_principal(self) -> None:
        """Delete every cache row owned by this principal."""
        self._runtime.record_pending_principal_purge(self.principal_id)

        await self._write_operation(
            _PRINCIPAL_PURGE_LOCK_SCOPE,
            operation="purge_principal",
            disabled_result=None,
            writer=_noop_write,
        )
