"""PostgreSQL runtime and lifecycle ownership for the Matrix event cache."""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

import psycopg

from mindroom.logging_config import get_logger

from . import postgres_event_cache_events, postgres_event_cache_threads
from .event_cache import EventCacheBackendUnavailableError
from .event_normalization import normalize_event_source_for_cache
from .postgres_agent_message_snapshot import load_postgres_agent_message_snapshot
from .postgres_redaction import redact_postgres_connection_info

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from psycopg import AsyncConnection

    from .agent_message_snapshot import AgentMessageSnapshot
    from .event_cache import ThreadCacheState


POSTGRES_EVENT_CACHE_SCHEMA_VERSION = 1
_LOCK_WAIT_LOG_THRESHOLD_SECONDS = 0.1
_MAX_CACHED_ROOM_LOCKS = 256
_MAX_TRANSIENT_OPERATION_ATTEMPTS = 2
T = TypeVar("T")

logger = get_logger(__name__)

_TRANSIENT_SQLSTATE_PREFIXES: tuple[str, ...] = ("08",)  # Safe here because cache writes are idempotent upserts.
_TRANSIENT_SQLSTATES: frozenset[str] = frozenset(
    {
        "57P01",  # admin_shutdown
        "57P02",  # crash_shutdown
        "57P03",  # cannot_connect_now
    },
)
_TRANSIENT_ERROR_TEXT: tuple[str, ...] = (
    "connection is closed",
    "connection already closed",
    "connection refused",
    "connection timeout expired",
    "could not connect",
    "failed to resolve host",
    "network is unreachable",
    "name or service not known",
    "no route to host",
    "server closed the connection",
    "temporary failure in name resolution",
    "terminating connection",
)


def _postgres_error_sqlstate(exc: BaseException) -> str | None:
    """Return the SQLSTATE attached to a psycopg error when available."""
    if not isinstance(exc, psycopg.Error):
        return None
    sqlstate = exc.sqlstate
    if isinstance(sqlstate, str):
        return sqlstate
    diag_sqlstate = exc.diag.sqlstate
    return diag_sqlstate if isinstance(diag_sqlstate, str) else None


def _is_transient_postgres_failure(exc: BaseException) -> bool:
    """Return whether one PostgreSQL failure should be retried on a new connection."""
    if isinstance(exc, psycopg.InterfaceError):
        return True
    if not isinstance(exc, psycopg.OperationalError):
        return False

    sqlstate = _postgres_error_sqlstate(exc)
    if sqlstate in _TRANSIENT_SQLSTATES:
        return True
    if sqlstate is not None and sqlstate.startswith(_TRANSIENT_SQLSTATE_PREFIXES):
        return True

    message = str(exc).lower()
    return any(fragment in message for fragment in _TRANSIENT_ERROR_TEXT)


def _cache_backend_unavailable(operation: str, exc: BaseException) -> EventCacheBackendUnavailableError:
    message = str(exc)
    detail = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
    return EventCacheBackendUnavailableError(f"Postgres event cache unavailable during {operation}: {detail}")


async def _rollback_postgres_connection_best_effort(
    db: psycopg.AsyncConnection,
    *,
    namespace: str,
    operation: str,
) -> None:
    """Roll back one shared connection without masking the original failure."""
    try:
        await db.rollback()
    except Exception as exc:
        logger.debug(
            "Ignoring Postgres event cache rollback failure",
            namespace=namespace,
            operation=operation,
            error_type=type(exc).__name__,
            error=str(exc),
        )


async def _close_postgres_connection_best_effort(
    db: psycopg.AsyncConnection,
    *,
    namespace: str,
    operation: str,
) -> None:
    """Close one shared connection without masking the original failure."""
    try:
        await db.close()
    except Exception as exc:
        logger.debug(
            "Ignoring error while closing Postgres event cache connection",
            namespace=namespace,
            operation=operation,
            error_type=type(exc).__name__,
            error=str(exc),
        )


async def initialize_postgres_event_cache_db(
    database_url: str,
    *,
    namespace: str = "",
) -> psycopg.AsyncConnection:
    """Open the PostgreSQL database and ensure the event-cache schema exists."""
    db = await psycopg.AsyncConnection.connect(database_url)
    try:
        await create_postgres_event_cache_schema(db)
        await validate_postgres_event_cache_schema(db)
        await db.commit()
    except BaseException:
        await _rollback_postgres_connection_best_effort(db, namespace=namespace, operation="initialize")
        await _close_postgres_connection_best_effort(db, namespace=namespace, operation="initialize")
        raise
    return db


async def create_postgres_event_cache_schema(db: AsyncConnection) -> None:
    """Create the current PostgreSQL cache schema in one connection."""
    await db.execute("CREATE SEQUENCE IF NOT EXISTS mindroom_event_cache_write_seq")
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_thread_events (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            origin_server_ts BIGINT NOT NULL,
            event_json TEXT NOT NULL,
            write_seq BIGINT NOT NULL DEFAULT nextval('mindroom_event_cache_write_seq'),
            PRIMARY KEY (namespace, room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mindroom_event_cache_thread_events_room_thread_ts
        ON mindroom_event_cache_thread_events(namespace, room_id, thread_id, origin_server_ts)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_events (
            namespace TEXT NOT NULL,
            event_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            origin_server_ts BIGINT NOT NULL,
            event_json TEXT NOT NULL,
            cached_at DOUBLE PRECISION NOT NULL,
            write_seq BIGINT NOT NULL DEFAULT nextval('mindroom_event_cache_write_seq'),
            PRIMARY KEY (namespace, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mindroom_event_cache_events_room_origin_ts
        ON mindroom_event_cache_events(namespace, room_id, origin_server_ts DESC)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_event_edits (
            namespace TEXT NOT NULL,
            edit_event_id TEXT NOT NULL,
            room_id TEXT NOT NULL,
            original_event_id TEXT NOT NULL,
            origin_server_ts BIGINT NOT NULL,
            PRIMARY KEY (namespace, edit_event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mindroom_event_cache_event_edits_room_original_ts
        ON mindroom_event_cache_event_edits(
            namespace,
            room_id,
            original_event_id,
            origin_server_ts DESC,
            edit_event_id DESC
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_event_threads (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            PRIMARY KEY (namespace, room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mindroom_event_cache_event_threads_room_thread
        ON mindroom_event_cache_event_threads(namespace, room_id, thread_id, event_id)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_redacted_events (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            PRIMARY KEY (namespace, room_id, event_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_mxc_text (
            namespace TEXT NOT NULL,
            mxc_url TEXT NOT NULL,
            text_content TEXT NOT NULL,
            cached_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (namespace, mxc_url)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_thread_state (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            validated_at DOUBLE PRECISION,
            invalidated_at DOUBLE PRECISION,
            invalidation_reason TEXT,
            PRIMARY KEY (namespace, room_id, thread_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_room_state (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            invalidated_at DOUBLE PRECISION,
            invalidation_reason TEXT,
            PRIMARY KEY (namespace, room_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
    )


async def postgres_schema_version(db: AsyncConnection) -> int | None:
    """Return the current PostgreSQL schema version for this cache."""
    cursor = await db.execute(
        """
        SELECT value
        FROM mindroom_event_cache_metadata
        WHERE key = 'schema_version'
        """,
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    return None if row is None else int(row[0])


async def validate_postgres_event_cache_schema(db: AsyncConnection) -> None:
    """Store or validate the PostgreSQL cache schema version."""
    current_schema_version = await postgres_schema_version(db)
    if current_schema_version is None:
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_metadata(key, value)
            VALUES ('schema_version', %s)
            ON CONFLICT(key) DO NOTHING
            """,
            (str(POSTGRES_EVENT_CACHE_SCHEMA_VERSION),),
        )
        current_schema_version = await postgres_schema_version(db)
        if current_schema_version is None:
            msg = "PostgreSQL Matrix event cache schema version was not initialized"
            raise RuntimeError(msg)
    if current_schema_version == POSTGRES_EVENT_CACHE_SCHEMA_VERSION:
        return
    msg = (
        "PostgreSQL Matrix event cache schema version "
        f"{current_schema_version} is not compatible with expected version "
        f"{POSTGRES_EVENT_CACHE_SCHEMA_VERSION}"
    )
    raise RuntimeError(msg)


@dataclass
class _RoomLockEntry:
    """Track one room lock plus queued users that still rely on it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    active_users: int = 0


@dataclass(frozen=True)
class _PendingInvalidation:
    """Stale marker that must be persisted before cache rows can be trusted again."""

    invalidated_at: float
    reason: str


class _PostgresEventCacheRuntime:
    """Own runtime-only lifecycle, locking, and disable state for one cache instance."""

    def __init__(self, database_url: str, namespace: str) -> None:
        self._database_url = database_url
        self._namespace = namespace
        self._db: psycopg.AsyncConnection | None = None
        self._disabled_reason: str | None = None
        self._unavailable_reason: str | None = None
        self._transient_failure_count = 0
        self._reconnect_count = 0
        self._reconnect_after_transient = False
        self._explicitly_closed = False
        self._db_lock = asyncio.Lock()
        self._room_locks: OrderedDict[str, _RoomLockEntry] = OrderedDict()
        self._pending_thread_invalidations: dict[tuple[str, str], _PendingInvalidation] = {}
        self._pending_room_invalidations: dict[str, _PendingInvalidation] = {}

    @property
    def database_url(self) -> str:
        """Return the PostgreSQL connection URL for this cache instance."""
        return self._database_url

    @property
    def redacted_database_url(self) -> str:
        """Return the log-safe PostgreSQL connection URL for this cache instance."""
        return redact_postgres_connection_info(self._database_url)

    @property
    def namespace(self) -> str:
        """Return the logical cache namespace."""
        return self._namespace

    @property
    def db(self) -> psycopg.AsyncConnection | None:
        """Return the active PostgreSQL connection, if initialized."""
        return self._db

    @property
    def room_locks(self) -> dict[str, _RoomLockEntry]:
        """Return the cached room-lock table for observability and tests."""
        return self._room_locks

    @property
    def is_initialized(self) -> bool:
        """Return whether the PostgreSQL connection is currently open."""
        return self._db is not None and not self.connection_is_closed(self._db)

    @property
    def is_disabled(self) -> bool:
        """Return whether the advisory cache is disabled for this runtime."""
        return self._disabled_reason is not None

    @property
    def durable_writes_available(self) -> bool:
        """Return whether callers should still attempt durable cache writes."""
        return not self.is_disabled and not self._explicitly_closed

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""
        if self._disabled_reason is not None:
            return
        self._disabled_reason = reason
        logger.warning(
            "Disabling advisory Matrix event cache",
            database_url=self.redacted_database_url,
            namespace=self._namespace,
            reason=reason,
        )

    def runtime_diagnostics(self) -> dict[str, object]:
        """Return log-safe runtime state for sync certification diagnostics."""
        diagnostics: dict[str, object] = {
            "cache_backend": "postgres",
            "cache_postgres_initialized": self.is_initialized,
            "cache_postgres_disabled": self.is_disabled,
            "cache_postgres_transient_failure_count": self._transient_failure_count,
            "cache_postgres_reconnect_count": self._reconnect_count,
            "cache_postgres_pending_thread_invalidations": len(self._pending_thread_invalidations),
            "cache_postgres_pending_room_invalidations": len(self._pending_room_invalidations),
            "cache_postgres_explicitly_closed": self._explicitly_closed,
        }
        if self._disabled_reason is not None:
            diagnostics["cache_postgres_disabled_reason"] = self._disabled_reason
        if self._unavailable_reason is not None:
            diagnostics["cache_postgres_unavailable_reason"] = self._unavailable_reason
        return diagnostics

    async def initialize(self) -> None:
        """Open the PostgreSQL database and create the cache schema."""
        async with self._db_lock:
            if self._disabled_reason is not None:
                return
            self._explicitly_closed = False
            if self._db is not None and not self.connection_is_closed(self._db):
                return
            had_previous_connection = self._db is not None
            await self._close_db_locked(operation="initialize")
            try:
                self._db = await initialize_postgres_event_cache_db(self._database_url, namespace=self._namespace)
            except Exception as exc:
                if _is_transient_postgres_failure(exc):
                    self._transient_failure_count += 1
                    self._unavailable_reason = self._unavailable_reason_from_exception(exc)
                    operation = "initialize"
                    raise _cache_backend_unavailable(operation, exc) from exc
                raise
            if had_previous_connection or self._reconnect_after_transient:
                self._reconnect_count += 1
            self._reconnect_after_transient = False
            self._unavailable_reason = None

    async def close(self) -> None:
        """Close the PostgreSQL connection when the cache is no longer needed."""
        async with self._db_lock:
            self._explicitly_closed = True
            self._reconnect_after_transient = False
            await self._close_db_locked(operation="close")
            self._room_locks.clear()

    async def handle_transient_failure(self, exc: BaseException, *, operation: str) -> None:
        """Drop a dead connection and leave the runtime eligible for later reconnect."""
        async with self._db_lock:
            self._transient_failure_count += 1
            self._unavailable_reason = self._unavailable_reason_from_exception(exc)
            self._reconnect_after_transient = True
            await self._close_db_locked(operation=operation)
        logger.info(
            "Postgres event cache temporarily unavailable",
            database_url=self.redacted_database_url,
            namespace=self._namespace,
            operation=operation,
            error_type=type(exc).__name__,
            error=str(exc),
            transient_failure_count=self._transient_failure_count,
        )

    def record_pending_thread_invalidation(
        self,
        room_id: str,
        thread_id: str,
        *,
        invalidated_at: float,
        reason: str,
    ) -> None:
        """Remember a best-effort stale marker that must be persisted before trusting cache rows."""
        key = (room_id, thread_id)
        existing = self._pending_thread_invalidations.get(key)
        if existing is not None and existing.invalidated_at >= invalidated_at:
            return
        self._pending_thread_invalidations[key] = _PendingInvalidation(
            invalidated_at=invalidated_at,
            reason=reason,
        )

    def record_pending_room_invalidation(
        self,
        room_id: str,
        *,
        invalidated_at: float,
        reason: str,
    ) -> None:
        """Remember a best-effort room stale marker that must be persisted before trusting cache rows."""
        existing = self._pending_room_invalidations.get(room_id)
        if existing is not None and existing.invalidated_at >= invalidated_at:
            return
        self._pending_room_invalidations[room_id] = _PendingInvalidation(
            invalidated_at=invalidated_at,
            reason=reason,
        )

    def pending_room_invalidation(self, room_id: str) -> _PendingInvalidation | None:
        """Return one pending room invalidation, if any."""
        return self._pending_room_invalidations.get(room_id)

    def pending_thread_invalidations(self, room_id: str) -> tuple[tuple[str, _PendingInvalidation], ...]:
        """Return pending thread invalidations for one room."""
        return tuple(
            (thread_id, pending)
            for (pending_room_id, thread_id), pending in self._pending_thread_invalidations.items()
            if pending_room_id == room_id
        )

    def pending_invalidation_room_ids(self) -> tuple[str, ...]:
        """Return rooms with runtime-only invalidation markers pending durable persistence."""
        room_ids = set(self._pending_room_invalidations)
        room_ids.update(room_id for room_id, _thread_id in self._pending_thread_invalidations)
        return tuple(sorted(room_ids))

    def forget_pending_room_invalidation(self, room_id: str, pending: _PendingInvalidation) -> None:
        """Forget one persisted room invalidation and thread markers covered by it."""
        if self._pending_room_invalidations.get(room_id) == pending:
            self._pending_room_invalidations.pop(room_id, None)
        self._pending_thread_invalidations = {
            key: thread_pending
            for key, thread_pending in self._pending_thread_invalidations.items()
            if key[0] != room_id or thread_pending.invalidated_at > pending.invalidated_at
        }

    def forget_pending_thread_invalidation(
        self,
        room_id: str,
        thread_id: str,
        pending: _PendingInvalidation,
    ) -> None:
        """Forget one persisted thread invalidation."""
        key = (room_id, thread_id)
        if self._pending_thread_invalidations.get(key) == pending:
            self._pending_thread_invalidations.pop(key, None)

    async def _close_db_locked(self, *, operation: str) -> None:
        """Close the active connection best-effort. Caller must hold ``_db_lock``."""
        db = self._db
        self._db = None
        if db is None:
            return
        await _close_postgres_connection_best_effort(db, namespace=self._namespace, operation=operation)

    def connection_is_closed(self, db: psycopg.AsyncConnection) -> bool:
        """Return whether psycopg considers one connection closed."""
        return bool(db.closed)

    def _unavailable_reason_from_exception(self, exc: BaseException) -> str:
        message = str(exc)
        if not message:
            return type(exc).__name__
        return f"{type(exc).__name__}: {message[:200]}"

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
                    "Waited for PostgresEventCache room lock",
                    room_id=room_id,
                    namespace=self._namespace,
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
    ) -> AsyncIterator[psycopg.AsyncConnection]:
        """Serialize one DB operation with lifecycle changes and room ordering."""
        if self._db is None or self.connection_is_closed(self._db):
            await self.initialize()
        async with self._db_lock, self.acquire_room_lock(room_id, operation=operation):
            db = self.require_db()
            try:
                await db.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (self._namespace, room_id),
                )
            except BaseException:
                await _rollback_postgres_connection_best_effort(db, namespace=self._namespace, operation=operation)
                raise
            yield db

    def require_db(self) -> psycopg.AsyncConnection:
        """Return the active PostgreSQL connection or raise if uninitialized."""
        if self._db is None:
            msg = "PostgresEventCache has not been initialized"
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
PostgresEventCacheRuntime = _PostgresEventCacheRuntime


class PostgresEventCache:
    """PostgreSQL-backed ConversationEventCache implementation."""

    def __init__(self, *, database_url: str, namespace: str) -> None:
        self._runtime = _PostgresEventCacheRuntime(database_url, namespace)

    @property
    def database_url(self) -> str:
        """Return the PostgreSQL connection URL for this cache instance."""
        return self._runtime.database_url

    @property
    def namespace(self) -> str:
        """Return the logical cache namespace."""
        return self._runtime.namespace

    @property
    def is_initialized(self) -> bool:
        """Return whether the PostgreSQL connection is currently open."""
        return self._runtime.is_initialized

    @property
    def durable_writes_available(self) -> bool:
        """Return whether cache writes can durably persist data."""
        return self._runtime.durable_writes_available

    async def initialize(self) -> None:
        """Open the PostgreSQL database and create the cache schema."""
        await self._runtime.initialize()

    def runtime_diagnostics(self) -> dict[str, object]:
        """Return log-safe runtime state for sync certification diagnostics."""
        return self._runtime.runtime_diagnostics()

    def pending_durable_write_room_ids(self) -> tuple[str, ...]:
        """Return rooms with runtime-only writes that must persist before certifying a sync token."""
        return self._runtime.pending_invalidation_room_ids()

    async def flush_pending_durable_writes(self, room_id: str) -> None:
        """Persist runtime-only writes for one room before certifying a sync token."""

        async def flush_only(_db: psycopg.AsyncConnection) -> None:
            return None

        await self._write_operation(
            room_id,
            operation="flush_pending_durable_writes",
            disabled_result=None,
            writer=flush_only,
        )

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""
        self._runtime.disable(reason)

    async def close(self) -> None:
        """Close the PostgreSQL connection when the cache is no longer needed."""
        await self._runtime.close()

    async def _read_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: T,
        reader: Callable[[psycopg.AsyncConnection], Awaitable[T]],
    ) -> T:
        return await self._operation(
            room_id,
            operation=operation,
            disabled_result=disabled_result,
            callback=reader,
        )

    async def _write_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: T,
        writer: Callable[[psycopg.AsyncConnection], Awaitable[T]],
    ) -> T:
        return await self._operation(
            room_id,
            operation=operation,
            disabled_result=disabled_result,
            callback=writer,
        )

    async def _operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: T,
        callback: Callable[[psycopg.AsyncConnection], Awaitable[T]],
    ) -> T:
        """Run one cache operation, flushing pending stale markers first even for reads."""
        if self._runtime.is_disabled:
            return disabled_result
        transient_error: BaseException | None = None
        for attempt in range(_MAX_TRANSIENT_OPERATION_ATTEMPTS):
            flushed_pending: tuple[tuple[str, str | None, _PendingInvalidation], ...] = ()
            try:
                async with self._runtime.acquire_db_operation(room_id, operation=operation) as db:
                    try:
                        flushed_pending = await self._flush_pending_invalidations(db, room_id)
                        result = await callback(db)
                        await db.commit()
                    except BaseException:
                        await _rollback_postgres_connection_best_effort(
                            db,
                            namespace=self._runtime.namespace,
                            operation=operation,
                        )
                        raise
            except EventCacheBackendUnavailableError as exc:
                transient_error = exc
                if attempt + 1 < _MAX_TRANSIENT_OPERATION_ATTEMPTS:
                    continue
                raise
            except Exception as exc:
                if not _is_transient_postgres_failure(exc):
                    raise
                transient_error = exc
                await self._runtime.handle_transient_failure(exc, operation=operation)
                if attempt + 1 < _MAX_TRANSIENT_OPERATION_ATTEMPTS:
                    continue
                raise _cache_backend_unavailable(operation, exc) from exc
            else:
                self._forget_flushed_pending_invalidations(room_id, flushed_pending)
                return result
        raise _cache_backend_unavailable(operation, transient_error or RuntimeError("operation did not run"))

    async def _flush_pending_invalidations(
        self,
        db: psycopg.AsyncConnection,
        room_id: str,
    ) -> tuple[tuple[str, str | None, _PendingInvalidation], ...]:
        flushed: list[tuple[str, str | None, _PendingInvalidation]] = []
        room_pending = self._runtime.pending_room_invalidation(room_id)
        thread_pending = self._runtime.pending_thread_invalidations(room_id)
        if room_pending is not None:
            await postgres_event_cache_threads.mark_room_stale_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                invalidated_at=room_pending.invalidated_at,
                reason=room_pending.reason,
            )
            flushed.append(("room", None, room_pending))

        for thread_id, pending_invalidation in thread_pending:
            if room_pending is not None and pending_invalidation.invalidated_at <= room_pending.invalidated_at:
                continue
            await postgres_event_cache_threads.mark_thread_stale_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
                invalidated_at=pending_invalidation.invalidated_at,
                reason=pending_invalidation.reason,
            )
            flushed.append(("thread", thread_id, pending_invalidation))
        return tuple(flushed)

    def _forget_flushed_pending_invalidations(
        self,
        room_id: str,
        flushed_pending: tuple[tuple[str, str | None, _PendingInvalidation], ...],
    ) -> None:
        for kind, thread_id, pending in flushed_pending:
            if kind == "room":
                self._runtime.forget_pending_room_invalidation(room_id, pending)
                continue
            if thread_id is not None:
                self._runtime.forget_pending_thread_invalidation(room_id, thread_id, pending)

    async def get_thread_events(self, room_id: str, thread_id: str) -> list[dict[str, Any]] | None:
        """Return cached events for one thread sorted by timestamp."""
        return await self._read_operation(
            room_id,
            operation="get_thread_events",
            disabled_result=None,
            reader=lambda db: postgres_event_cache_threads.load_thread_events(
                db,
                namespace=self._runtime.namespace,
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
            reader=lambda db: postgres_event_cache_threads.load_recent_room_thread_ids(
                db,
                namespace=self._runtime.namespace,
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
            reader=lambda db: postgres_event_cache_threads.load_thread_cache_state(
                db,
                namespace=self._runtime.namespace,
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
            reader=lambda db: postgres_event_cache_events.load_event(
                db,
                namespace=self._runtime.namespace,
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
            reader=lambda db: postgres_event_cache_events.load_recent_room_events(
                db,
                namespace=self._runtime.namespace,
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
            reader=lambda db: postgres_event_cache_events.load_latest_edit(
                db,
                namespace=self._runtime.namespace,
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
            reader=lambda db: load_postgres_agent_message_snapshot(
                db,
                namespace=self._runtime.namespace,
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
            reader=lambda db: postgres_event_cache_events.load_mxc_text(
                db,
                namespace=self._runtime.namespace,
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
            normalized_event = normalize_event_source_for_cache(event_data, event_id=event_id)
            events_by_room.setdefault(room_id, []).append((event_id, normalized_event))

        for room_id, room_events in events_by_room.items():
            await self._write_operation(
                room_id,
                operation="store_events_batch",
                disabled_result=None,
                writer=lambda db, room_id=room_id, room_events=room_events, cached_at=cached_at: (
                    postgres_event_cache_events.persist_lookup_events(
                        db,
                        namespace=self._runtime.namespace,
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
            writer=lambda db: postgres_event_cache_events.persist_mxc_text(
                db,
                namespace=self._runtime.namespace,
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
            writer=lambda db: postgres_event_cache_threads.replace_thread_locked(
                db,
                namespace=self._runtime.namespace,
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
        replacement_validated_at = fetch_started_at if validated_at is None else min(validated_at, fetch_started_at)

        async def replace_if_still_safe(db: psycopg.AsyncConnection) -> bool:
            return await postgres_event_cache_threads.replace_thread_locked_if_not_newer(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
                events=events,
                fetch_started_at=fetch_started_at,
                validated_at=replacement_validated_at,
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
            writer=lambda db: postgres_event_cache_threads.invalidate_thread_locked(
                db,
                namespace=self._runtime.namespace,
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
            writer=lambda db: postgres_event_cache_threads.invalidate_room_threads_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
            ),
        )

    async def mark_thread_stale(self, room_id: str, thread_id: str, *, reason: str) -> None:
        """Persist one durable thread invalidation marker."""
        invalidated_at = time.time()
        try:
            await self._write_operation(
                room_id,
                operation="mark_thread_stale",
                disabled_result=None,
                writer=lambda db: postgres_event_cache_threads.mark_thread_stale_locked(
                    db,
                    namespace=self._runtime.namespace,
                    room_id=room_id,
                    thread_id=thread_id,
                    invalidated_at=invalidated_at,
                    reason=reason,
                ),
            )
        except EventCacheBackendUnavailableError:
            self._runtime.record_pending_thread_invalidation(
                room_id,
                thread_id,
                invalidated_at=invalidated_at,
                reason=reason,
            )
            raise

    async def mark_room_threads_stale(self, room_id: str, *, reason: str) -> None:
        """Persist a durable invalidate-and-refetch marker for every cached thread in one room."""
        invalidated_at = time.time()
        try:
            await self._write_operation(
                room_id,
                operation="mark_room_threads_stale",
                disabled_result=None,
                writer=lambda db: postgres_event_cache_threads.mark_room_stale_locked(
                    db,
                    namespace=self._runtime.namespace,
                    room_id=room_id,
                    invalidated_at=invalidated_at,
                    reason=reason,
                ),
            )
        except EventCacheBackendUnavailableError:
            self._runtime.record_pending_room_invalidation(
                room_id,
                invalidated_at=invalidated_at,
                reason=reason,
            )
            raise

    async def append_event(self, room_id: str, thread_id: str, event: dict[str, Any]) -> bool:
        """Append one event when the thread already has cached data."""
        normalized_event = normalize_event_source_for_cache(event)
        return bool(
            await self._write_operation(
                room_id,
                operation="append_event",
                disabled_result=False,
                writer=lambda db: postgres_event_cache_threads.append_existing_thread_event(
                    db,
                    namespace=self._runtime.namespace,
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
                writer=lambda db: postgres_event_cache_threads.revalidate_thread_after_incremental_update_locked(
                    db,
                    namespace=self._runtime.namespace,
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
            reader=lambda db: postgres_event_cache_events.load_thread_id_for_event(
                db,
                namespace=self._runtime.namespace,
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
                writer=lambda db: postgres_event_cache_events.redact_event_locked(
                    db,
                    namespace=self._runtime.namespace,
                    room_id=room_id,
                    event_id=event_id,
                ),
            ),
        )
