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
T = TypeVar("T")

# PostgreSQL SQLSTATE codes that indicate a transient connection-level failure.
# 08xxx: connection_exception family. 57P01-57P03: server-side admin / shutdown
# events. Operations that fail with one of these states are safe to retry on a
# fresh connection.
_TRANSIENT_SQLSTATES: frozenset[str] = frozenset(
    {
        "08000",
        "08001",
        "08003",
        "08004",
        "08006",
        "08007",
        "57P01",
        "57P02",
        "57P03",
    },
)
_TRANSIENT_MESSAGE_FRAGMENTS: tuple[str, ...] = (
    "the connection is closed",
    "connection already closed",
    "server closed the connection",
    "terminating connection",
)


def _is_transient_db_error(exc: BaseException) -> bool:
    """Return whether *exc* represents a transient PostgreSQL connection failure.

    Transient errors include connection-establishment failures (08xxx), normal
    server-side shutdown signals (57P01-57P03), and the ``InterfaceError`` that
    psycopg raises when the underlying socket has already been closed.
    """
    if isinstance(exc, psycopg.InterfaceError):
        return True
    if isinstance(exc, psycopg.OperationalError):
        diag = getattr(exc, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None) if diag is not None else None
        if sqlstate in _TRANSIENT_SQLSTATES:
            return True
        message = str(exc).lower()
        return any(fragment in message for fragment in _TRANSIENT_MESSAGE_FRAGMENTS)
    return False

logger = get_logger(__name__)


async def initialize_postgres_event_cache_db(database_url: str) -> psycopg.AsyncConnection:
    """Open the PostgreSQL database and ensure the event-cache schema exists."""
    db = await psycopg.AsyncConnection.connect(database_url)
    try:
        await create_postgres_event_cache_schema(db)
        await validate_postgres_event_cache_schema(db)
        await db.commit()
    except Exception:
        await db.rollback()
        await db.close()
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


class _PostgresEventCacheRuntime:
    """Own runtime-only lifecycle, locking, and disable state for one cache instance."""

    def __init__(self, database_url: str, namespace: str) -> None:
        self._database_url = database_url
        self._namespace = namespace
        self._db: psycopg.AsyncConnection | None = None
        self._disabled_reason: str | None = None
        self._db_lock = asyncio.Lock()
        self._room_locks: OrderedDict[str, _RoomLockEntry] = OrderedDict()
        self._reconnect_attempts = 0
        self._unavailable_reason: str | None = None

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
        return self._db is not None

    @property
    def is_disabled(self) -> bool:
        """Return whether the advisory cache is disabled for this runtime."""
        return self._disabled_reason is not None

    @property
    def disabled_reason(self) -> str | None:
        """Return the reason this runtime was permanently disabled, if any."""
        return self._disabled_reason

    @property
    def unavailable_reason(self) -> str | None:
        """Return the most recent transient unavailability cause, if any."""
        return self._unavailable_reason

    @property
    def reconnect_attempts(self) -> int:
        """Return the number of times this runtime has reopened its connection."""
        return self._reconnect_attempts

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

    async def handle_transient_failure(self, exc: BaseException, *, operation: str) -> None:
        """Drop the dead connection so the next acquire opens a fresh one.

        Caller must have already determined that *exc* is transient via
        :func:`_is_transient_db_error`. Cleaning up the connection is best-effort —
        if the close itself raises, we log and proceed with reconnection on the
        next acquire.
        """
        async with self._db_lock:
            if self._db is not None:
                try:
                    await self._db.close()
                except Exception as close_exc:
                    logger.debug(
                        "Ignoring error while closing dead Postgres event cache connection",
                        namespace=self._namespace,
                        operation=operation,
                        error=str(close_exc),
                    )
                self._db = None
            self._reconnect_attempts += 1
            self._unavailable_reason = type(exc).__name__
        logger.info(
            "Reconnecting Postgres event cache after transient failure",
            database_url=self.redacted_database_url,
            namespace=self._namespace,
            operation=operation,
            error=str(exc),
            reconnect_attempts=self._reconnect_attempts,
        )

    async def initialize(self) -> None:
        """Open the PostgreSQL database and create the cache schema."""
        async with self._db_lock:
            if self._disabled_reason is not None or self._db is not None:
                return
            self._db = await initialize_postgres_event_cache_db(self._database_url)

    async def close(self) -> None:
        """Close the PostgreSQL connection when the cache is no longer needed."""
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
        if self._db is None:
            await self.initialize()
        async with self._db_lock, self.acquire_room_lock(room_id, operation=operation):
            db = self.require_db()
            await db.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                (self._namespace, room_id),
            )
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
        return self._runtime.is_initialized and not self._runtime.is_disabled

    @property
    def disabled_reason(self) -> str | None:
        """Return the reason this cache was permanently disabled, if any."""
        return self._runtime.disabled_reason

    @property
    def unavailable_reason(self) -> str | None:
        """Return the most recent transient unavailability cause, if any."""
        return self._runtime.unavailable_reason

    @property
    def reconnect_attempts(self) -> int:
        """Return the total reconnection attempts for this cache instance."""
        return self._runtime.reconnect_attempts

    async def initialize(self) -> None:
        """Open the PostgreSQL database and create the cache schema."""
        await self._runtime.initialize()

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
        if self._runtime.is_disabled:
            return disabled_result
        for attempt in (1, 2):
            try:
                async with self._runtime.acquire_db_operation(room_id, operation=operation) as db:
                    try:
                        result = await reader(db)
                        await db.commit()
                    except Exception:
                        await db.rollback()
                        raise
            except Exception as exc:
                if attempt == 1 and _is_transient_db_error(exc):
                    await self._runtime.handle_transient_failure(exc, operation=operation)
                    continue
                raise
            else:
                return result
        msg = "transient retry loop exited without returning"
        raise RuntimeError(msg)

    async def _write_operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: T,
        writer: Callable[[psycopg.AsyncConnection], Awaitable[T]],
    ) -> T:
        if self._runtime.is_disabled:
            return disabled_result
        for attempt in (1, 2):
            try:
                async with self._runtime.acquire_db_operation(room_id, operation=operation) as db:
                    try:
                        result = await writer(db)
                        await db.commit()
                    except Exception:
                        await db.rollback()
                        raise
            except Exception as exc:
                if attempt == 1 and _is_transient_db_error(exc):
                    await self._runtime.handle_transient_failure(exc, operation=operation)
                    continue
                raise
            else:
                return result
        msg = "transient retry loop exited without returning"
        raise RuntimeError(msg)

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

    async def get_latest_edit(self, room_id: str, original_event_id: str) -> dict[str, Any] | None:
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
        await self._write_operation(
            room_id,
            operation="mark_thread_stale",
            disabled_result=None,
            writer=lambda db: postgres_event_cache_threads.mark_thread_stale_locked(
                db,
                namespace=self._runtime.namespace,
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
            writer=lambda db: postgres_event_cache_threads.mark_room_stale_locked(
                db,
                namespace=self._runtime.namespace,
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
