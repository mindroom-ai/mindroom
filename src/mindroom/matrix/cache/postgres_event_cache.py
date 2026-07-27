"""PostgreSQL runtime and lifecycle ownership for the Matrix event cache."""

from __future__ import annotations

import asyncio
import hashlib
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

import psycopg

from mindroom.logging_config import get_logger

from . import postgres_event_cache_events, postgres_event_cache_threads
from .event_batching import group_lookup_events_by_room
from .event_cache import EventCacheBackendUnavailableError
from .event_normalization import normalize_event_source_for_cache
from .postgres_agent_message_snapshot import load_postgres_agent_message_snapshot
from .postgres_cache_maintenance import migrate_postgres_schema, run_startup_maintenance
from .postgres_redaction import redact_postgres_connection_info
from .thread_cache_state import (
    CACHE_GAP_UNAVAILABLE,
    THREAD_HISTORY_TRUST_METADATA_KEY,
    THREAD_HISTORY_TRUST_VERSION,
    ThreadAppendOutcome,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Collection

    from psycopg import AsyncConnection

    from .agent_message_snapshot import AgentMessageSnapshot
    from .cache_maintenance import CacheMaintenanceReport
    from .thread_cache_state import ThreadCacheGap


# Bumped to 5 by the gap-marker rework. The version is what makes a downgrade fail loudly: this
# revision DROPs validated_at / invalidated_at / invalidation_reason, and the prior code only ever
# created those columns inside CREATE TABLE IF NOT EXISTS - so rolling back onto an already-migrated
# database would leave it querying columns that no longer exist. Version 4 refuses version 5.
_POSTGRES_EVENT_CACHE_SCHEMA_VERSION = 5
_DEFAULT_PRINCIPAL_ID = "__mindroom_default_principal__"
_POSTGRES_SCHEMA_LOCK_NAME = "mindroom_event_cache_schema"
_MAX_TRANSIENT_OPERATION_ATTEMPTS = 2
_PRINCIPAL_PURGE_LOCK_SCOPE = "__mindroom_principal_purge__"
_PRINCIPAL_NAMESPACE_LOCK_SCOPE = "__mindroom_principal_namespace__"
_T = TypeVar("_T")

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
    "another command is already in progress",
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


async def _noop_write(_db: psycopg.AsyncConnection) -> None:
    """Complete an operation after its pending runtime writes are flushed."""


class _CertificationGenerationChangedError(RuntimeError):
    """Raised when a reconnect no longer sees the initialized cache generation."""


def _require_runtime_certification_generation(generation: str | None) -> str:
    """Return the initialized generation required for a safe reconnect."""
    if generation is None:
        msg = "PostgreSQL event cache reconnect lacks an initialized certification generation"
        raise RuntimeError(msg)
    return generation


def _require_matching_certification_generation(actual: str | None, expected: str) -> None:
    """Reject reconnecting to missing or replaced namespace state."""
    if actual != expected:
        msg = "PostgreSQL event cache certification generation changed during reconnect"
        raise _CertificationGenerationChangedError(msg)


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


async def _initialize_postgres_event_cache_db(
    database_url: str,
    *,
    namespace: str,
) -> tuple[psycopg.AsyncConnection, CacheMaintenanceReport, str]:
    """Open the PostgreSQL database and ensure the event-cache schema exists."""
    db = await psycopg.AsyncConnection.connect(database_url)
    try:
        await db.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))",
            (_POSTGRES_SCHEMA_LOCK_NAME,),
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS mindroom_event_cache_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """,
        )
        current_schema_version = await _postgres_schema_version(db)
        if current_schema_version not in (None, 1, 2, 3, 4, _POSTGRES_EVENT_CACHE_SCHEMA_VERSION):
            msg = (
                "PostgreSQL Matrix event cache schema version "
                f"{current_schema_version} is not compatible with expected version "
                f"{_POSTGRES_EVENT_CACHE_SCHEMA_VERSION}"
            )
            raise RuntimeError(msg)  # noqa: TRY301
        if current_schema_version in {1, 2}:
            await _migrate_postgres_event_cache_security_schema(db)
        await _create_postgres_event_cache_schema(db)
        if current_schema_version is not None and current_schema_version < _POSTGRES_EVENT_CACHE_SCHEMA_VERSION:
            await _migrate_postgres_event_cache_gap_schema(db)
        await db.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
            (namespace, _PRINCIPAL_NAMESPACE_LOCK_SCOPE),
        )
        migration_result = await migrate_postgres_schema(
            db,
            namespace=namespace,
            current_schema_version=current_schema_version,
            target_schema_version=_POSTGRES_EVENT_CACHE_SCHEMA_VERSION,
        )
        certification_generation = await _initialize_namespace_certification_generation(
            db,
            namespace=namespace,
        )
        report = await run_startup_maintenance(
            db,
            namespace=namespace,
            schema_version=_POSTGRES_EVENT_CACHE_SCHEMA_VERSION,
            migrated_from_schema_version=migration_result.migrated_from_schema_version,
            normalized_legacy_thread_payload_rows=migration_result.normalized_legacy_thread_payload_rows,
        )
        await db.commit()
    except BaseException:
        await _rollback_postgres_connection_best_effort(db, namespace=namespace, operation="initialize")
        await _close_postgres_connection_best_effort(db, namespace=namespace, operation="initialize")
        raise
    logger.info(
        "Matrix event cache startup maintenance complete",
        backend="postgres",
        namespace=namespace,
        **report.as_runtime_diagnostics(),
    )
    return db, report, certification_generation


async def _reconnect_postgres_event_cache_db(
    database_url: str,
    *,
    namespace: str,
    expected_certification_generation: str,
) -> psycopg.AsyncConnection:
    """Open a replacement connection without rerunning startup-wide maintenance."""
    db = await psycopg.AsyncConnection.connect(database_url)
    try:
        certification_generation = await _load_namespace_certification_generation(
            db,
            namespace=namespace,
        )
        _require_matching_certification_generation(
            certification_generation,
            expected_certification_generation,
        )
        await db.commit()
    except BaseException:
        await _rollback_postgres_connection_best_effort(db, namespace=namespace, operation="reconnect")
        await _close_postgres_connection_best_effort(db, namespace=namespace, operation="reconnect")
        raise
    return db


async def _create_postgres_event_cache_schema(db: AsyncConnection) -> None:
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
            event_json TEXT,
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
            sender TEXT NOT NULL DEFAULT '',
            cached_at DOUBLE PRECISION NOT NULL,
            write_seq BIGINT NOT NULL DEFAULT nextval('mindroom_event_cache_write_seq'),
            PRIMARY KEY (namespace, room_id, event_id)
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
            PRIMARY KEY (namespace, room_id, edit_event_id)
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
            room_id TEXT NOT NULL,
            mxc_url TEXT NOT NULL,
            text_content TEXT NOT NULL,
            cached_at DOUBLE PRECISION NOT NULL,
            PRIMARY KEY (namespace, room_id, mxc_url)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_event_mxc_references (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            mxc_url TEXT NOT NULL,
            PRIMARY KEY (namespace, room_id, event_id, mxc_url)
        )
        """,
    )
    await db.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_mindroom_event_cache_mxc_references_plaintext
        ON mindroom_event_cache_event_mxc_references(namespace, room_id, mxc_url, event_id)
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_thread_state (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            gap_marked_at DOUBLE PRECISION,
            gap_reason TEXT,
            -- The ``fetch_started_at`` of the fetch whose snapshot is currently installed. Orders
            -- concurrent replacements so a slow older fetch cannot delete a newer fetch's events.
            snapshot_fetch_started_at DOUBLE PRECISION,
            PRIMARY KEY (namespace, room_id, thread_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_room_state (
            namespace TEXT NOT NULL,
            room_id TEXT NOT NULL,
            membership_state TEXT NOT NULL DEFAULT 'joined',
            membership_epoch BIGINT NOT NULL DEFAULT 0,
            -- The newest room-scoped gap. The fan-out across ``thread_state`` covers every thread
            -- that already had a row; this covers the one that did not yet, because its fetch was
            -- still running. Read only when installing a snapshot, never on a read.
            room_gap_marked_at DOUBLE PRECISION,
            room_gap_reason TEXT,
            PRIMARY KEY (namespace, room_id)
        )
        """,
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS mindroom_event_cache_namespace_metadata (
            namespace TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT NOT NULL,
            PRIMARY KEY (namespace, key)
        )
        """,
    )


async def _migrate_postgres_event_cache_gap_schema(db: AsyncConnection) -> None:
    """Swap the trust columns for gap columns on a database created before schema 5.

    Gated on the schema version, never run unconditionally. ``ALTER TABLE`` takes ACCESS EXCLUSIVE
    on the target even when every clause is a no-op, and these two tables are shared by every
    namespace - so running them on each principal's initialization would let one starting agent
    stall all cache traffic for all of them. That is why the security migration above is gated the
    same way.

    The trust columns are dropped rather than translated: the cache is derived from the homeserver
    and disposable, and the thread-history version reset empties both thread tables anyway.
    """
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_thread_state
        ADD COLUMN IF NOT EXISTS gap_marked_at DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS gap_reason TEXT,
        ADD COLUMN IF NOT EXISTS snapshot_fetch_started_at DOUBLE PRECISION,
        DROP COLUMN IF EXISTS validated_at,
        DROP COLUMN IF EXISTS invalidated_at,
        DROP COLUMN IF EXISTS invalidation_reason
        """,
    )
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_room_state
        ADD COLUMN IF NOT EXISTS room_gap_marked_at DOUBLE PRECISION,
        ADD COLUMN IF NOT EXISTS room_gap_reason TEXT,
        DROP COLUMN IF EXISTS invalidated_at,
        DROP COLUMN IF EXISTS invalidation_reason
        """,
    )


async def _migrate_postgres_event_cache_security_schema(db: AsyncConnection) -> None:
    """Make legacy global rows room-safe and discard ownerless plaintext."""
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_events
            DROP CONSTRAINT IF EXISTS mindroom_event_cache_events_pkey,
            ADD PRIMARY KEY (namespace, room_id, event_id)
        """,
    )
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_event_edits
            DROP CONSTRAINT IF EXISTS mindroom_event_cache_event_edits_pkey,
            ADD PRIMARY KEY (namespace, room_id, edit_event_id)
        """,
    )
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_mxc_text
            ADD COLUMN IF NOT EXISTS room_id TEXT
        """,
    )
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_room_state
            ADD COLUMN IF NOT EXISTS membership_state TEXT NOT NULL DEFAULT 'joined',
            ADD COLUMN IF NOT EXISTS membership_epoch BIGINT NOT NULL DEFAULT 0
        """,
    )
    await db.execute(
        """
        DELETE FROM mindroom_event_cache_mxc_text
        """,
    )
    await db.execute(
        """
        ALTER TABLE mindroom_event_cache_mxc_text
            ALTER COLUMN room_id SET NOT NULL,
            DROP CONSTRAINT IF EXISTS mindroom_event_cache_mxc_text_pkey,
            ADD PRIMARY KEY (namespace, room_id, mxc_url)
        """,
    )


async def _initialize_namespace_certification_generation(
    db: AsyncConnection,
    *,
    namespace: str,
) -> str:
    """Return a durable cache generation for one PostgreSQL namespace."""
    trust_cursor = await db.execute(
        """
        SELECT value
        FROM mindroom_event_cache_namespace_metadata
        WHERE namespace = %s AND key = %s
        """,
        (namespace, THREAD_HISTORY_TRUST_METADATA_KEY),
    )
    try:
        trust_row = await trust_cursor.fetchone()
    finally:
        await trust_cursor.close()
    trust_reset = trust_row != (THREAD_HISTORY_TRUST_VERSION,)
    if trust_reset:
        await db.execute(
            "DELETE FROM mindroom_event_cache_thread_events WHERE namespace = %s",
            (namespace,),
        )
        await db.execute(
            "DELETE FROM mindroom_event_cache_thread_state WHERE namespace = %s",
            (namespace,),
        )
        await db.execute(
            """
            INSERT INTO mindroom_event_cache_namespace_metadata(namespace, key, value)
            VALUES (%s, %s, %s)
            ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value
            """,
            (namespace, THREAD_HISTORY_TRUST_METADATA_KEY, THREAD_HISTORY_TRUST_VERSION),
        )
    generation = uuid.uuid4().hex
    await db.execute(
        """
        INSERT INTO mindroom_event_cache_namespace_metadata(namespace, key, value)
        VALUES (%s, 'certification_generation', %s)
        ON CONFLICT(namespace, key) DO UPDATE SET value = excluded.value
        WHERE %s
        """,
        (namespace, generation, trust_reset),
    )
    certification_generation = await _load_namespace_certification_generation(db, namespace=namespace)
    if certification_generation is None:
        msg = "PostgreSQL event cache certification generation was not initialized"
        raise RuntimeError(msg)
    return certification_generation


async def _load_namespace_certification_generation(
    db: AsyncConnection,
    *,
    namespace: str,
) -> str | None:
    """Load the existing certification generation without creating one."""
    cursor = await db.execute(
        """
        SELECT value
        FROM mindroom_event_cache_namespace_metadata
        WHERE namespace = %s AND key = 'certification_generation'
        """,
        (namespace,),
    )
    try:
        row = await cursor.fetchone()
    finally:
        await cursor.close()
    if row is None:
        return None
    generation = str(row[0])
    return generation or None


async def _postgres_schema_version(db: AsyncConnection) -> int | None:
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


@dataclass(frozen=True)
class _PendingGap:
    """One gap marker held in memory until it can be persisted.

    This is the same marker the durable layer stores, just not written yet, so it carries the
    durable field names rather than a second vocabulary for the same thing.
    """

    gap_marked_at: float
    reason: str


@dataclass(frozen=True)
class _FlushedPendingWrites:
    """Runtime-only writes included in one committed PostgreSQL operation."""

    room_purge: bool = False
    principal_purge: bool = False
    gaps: tuple[tuple[str, str | None, _PendingGap], ...] = ()


class _PostgresEventCacheRuntime:
    """Own runtime-only lifecycle, locking, and disable state for one cache instance."""

    def __init__(self, database_url: str, namespace: str) -> None:
        self._database_url = database_url
        self._namespace = namespace
        self._db: psycopg.AsyncConnection | None = None
        self._maintenance_report: CacheMaintenanceReport | None = None
        self._certification_generation: str | None = None
        self._disabled_reason: str | None = None
        self._unavailable_reason: str | None = None
        self._transient_failure_count = 0
        self._reconnect_count = 0
        self._reconnect_after_transient = False
        self._explicitly_closed = False
        self._db_lock = asyncio.Lock()
        self._pending_thread_gaps: dict[tuple[str, str], _PendingGap] = {}
        self._pending_room_gaps: dict[str, _PendingGap] = {}
        self._pending_room_purges: set[str] = set()
        self._pending_principal_purge = False
        self._departed_rooms: set[str] = set()
        self._room_departure_epochs: dict[str, int] = {}

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
            "cache_postgres_pending_thread_gaps": len(self._pending_thread_gaps),
            "cache_postgres_pending_room_gaps": len(self._pending_room_gaps),
            "cache_postgres_pending_room_purges": len(self._pending_room_purges),
            "cache_postgres_pending_principal_purge": self._pending_principal_purge,
            "cache_postgres_departed_room_count": len(self._departed_rooms),
            "cache_postgres_explicitly_closed": self._explicitly_closed,
            "cache_certification_generation_present": self._certification_generation is not None,
        }
        if self._disabled_reason is not None:
            diagnostics["cache_postgres_disabled_reason"] = self._disabled_reason
        if self._unavailable_reason is not None:
            diagnostics["cache_postgres_unavailable_reason"] = self._unavailable_reason
        if self._maintenance_report is not None:
            diagnostics.update(self._maintenance_report.as_runtime_diagnostics())
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
                if self._maintenance_report is None:
                    self._db, report, self._certification_generation = await _initialize_postgres_event_cache_db(
                        self._database_url,
                        namespace=self._namespace,
                    )
                    self._maintenance_report = report
                else:
                    expected_certification_generation = _require_runtime_certification_generation(
                        self._certification_generation,
                    )
                    self._db = await _reconnect_postgres_event_cache_db(
                        self._database_url,
                        namespace=self._namespace,
                        expected_certification_generation=expected_certification_generation,
                    )
            except _CertificationGenerationChangedError:
                self.disable("certification_generation_changed")
                raise
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

    def record_pending_thread_gap(
        self,
        room_id: str,
        thread_id: str,
        *,
        gap_marked_at: float,
        reason: str,
    ) -> None:
        """Remember a best-effort gap marker that must be persisted before serving cache rows."""
        key = (room_id, thread_id)
        existing = self._pending_thread_gaps.get(key)
        if existing is not None and existing.gap_marked_at >= gap_marked_at:
            return
        self._pending_thread_gaps[key] = _PendingGap(
            gap_marked_at=gap_marked_at,
            reason=reason,
        )

    def record_pending_room_gap(
        self,
        room_id: str,
        *,
        gap_marked_at: float,
        reason: str,
    ) -> None:
        """Remember a best-effort room gap marker that must be persisted before any snapshot is served."""
        existing = self._pending_room_gaps.get(room_id)
        if existing is not None and existing.gap_marked_at >= gap_marked_at:
            return
        self._pending_room_gaps[room_id] = _PendingGap(
            gap_marked_at=gap_marked_at,
            reason=reason,
        )

    def mark_room_departed(self, room_id: str) -> int:
        """Fence one departed room, queue its deletion, and return its new epoch."""
        epoch = self._room_departure_epochs.get(room_id, 0) + 1
        self._room_departure_epochs[room_id] = epoch
        self._departed_rooms.add(room_id)
        self._pending_room_purges.add(room_id)
        return epoch

    def mark_room_joined(self, room_id: str, *, expected_departure_epoch: int) -> None:
        """Remove one fence only when no newer departure superseded the join."""
        if self._room_departure_epochs.get(room_id, 0) == expected_departure_epoch:
            self._departed_rooms.discard(room_id)

    def room_departure_epoch(self, room_id: str) -> int:
        """Return the current fence epoch for one room."""
        return self._room_departure_epochs.get(room_id, 0)

    def is_room_departed(self, room_id: str) -> bool:
        """Return whether this principal namespace is fenced from one room."""
        return room_id in self._departed_rooms

    def record_pending_principal_purge(self) -> None:
        """Remember a namespace deletion until PostgreSQL commits it."""
        self._pending_principal_purge = True

    @property
    def has_pending_principal_purge(self) -> bool:
        """Return whether every row in this principal namespace must be deleted."""
        return self._pending_principal_purge

    def forget_pending_principal_purge(self) -> None:
        """Forget one committed namespace deletion and covered pending writes."""
        self._pending_principal_purge = False
        self._pending_room_purges.clear()
        self._pending_room_gaps.clear()
        self._pending_thread_gaps.clear()

    def has_pending_room_purge(self, room_id: str) -> bool:
        """Return whether a principal-room deletion is still pending."""
        return room_id in self._pending_room_purges

    def forget_pending_room_purge(self, room_id: str) -> None:
        """Forget one committed room deletion and the gap markers it obsoletes."""
        self._pending_room_purges.discard(room_id)
        self._pending_room_gaps.pop(room_id, None)
        self._pending_thread_gaps = {
            key: pending for key, pending in self._pending_thread_gaps.items() if key[0] != room_id
        }

    def pending_room_gap(self, room_id: str) -> _PendingGap | None:
        """Return one pending room-scoped gap marker, if any."""
        return self._pending_room_gaps.get(room_id)

    def pending_thread_gaps(self, room_id: str) -> tuple[tuple[str, _PendingGap], ...]:
        """Return pending thread gap markers for one room."""
        return tuple(
            (thread_id, pending)
            for (pending_room_id, thread_id), pending in self._pending_thread_gaps.items()
            if pending_room_id == room_id
        )

    def pending_gap_room_ids(self) -> tuple[str, ...]:
        """Return rooms with runtime-only gap markers pending durable persistence."""
        room_ids = set(self._pending_room_purges)
        room_ids.update(self._pending_room_gaps)
        room_ids.update(room_id for room_id, _thread_id in self._pending_thread_gaps)
        return tuple(sorted(room_ids))

    def forget_pending_room_gap(self, room_id: str, pending: _PendingGap) -> None:
        """Forget one persisted room-scoped gap and the thread markers it covers."""
        if self._pending_room_gaps.get(room_id) == pending:
            self._pending_room_gaps.pop(room_id, None)
        self._pending_thread_gaps = {
            key: thread_pending
            for key, thread_pending in self._pending_thread_gaps.items()
            if key[0] != room_id or thread_pending.gap_marked_at > pending.gap_marked_at
        }

    def forget_pending_thread_gap(
        self,
        room_id: str,
        thread_id: str,
        pending: _PendingGap,
    ) -> None:
        """Forget one persisted thread gap marker."""
        key = (room_id, thread_id)
        if self._pending_thread_gaps.get(key) == pending:
            self._pending_thread_gaps.pop(key, None)

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

    @asynccontextmanager
    async def acquire_db_operation(
        self,
        *,
        operation: str,
    ) -> AsyncIterator[psycopg.AsyncConnection]:
        """Serialize one transaction locally and across this principal namespace."""
        if self._db is None or self.connection_is_closed(self._db):
            await self.initialize()
        async with self._db_lock:
            db = self.require_db()
            try:
                await db.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                    (self._namespace, _PRINCIPAL_NAMESPACE_LOCK_SCOPE),
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


class _PostgresRuntimeRegistry:
    """Own the per-principal PostgreSQL runtimes behind one shared cache service."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._runtimes: dict[str, _PostgresEventCacheRuntime] = {}
        self._disabled_reason: str | None = None

    def runtime(self, namespace: str) -> _PostgresEventCacheRuntime:
        """Return the one connection runtime assigned to a principal namespace."""
        runtime = self._runtimes.get(namespace)
        if runtime is None:
            runtime = _PostgresEventCacheRuntime(self._database_url, namespace)
            if self._disabled_reason is not None:
                runtime.disable(self._disabled_reason)
            self._runtimes[namespace] = runtime
        return runtime

    def disable(self, reason: str) -> None:
        """Disable every current and future principal runtime in this service."""
        if self._disabled_reason is None:
            self._disabled_reason = reason
        for runtime in self._runtimes.values():
            runtime.disable(self._disabled_reason)

    async def close(self) -> None:
        """Close every principal runtime created by this shared service."""
        await asyncio.gather(*(runtime.close() for runtime in self._runtimes.values()))


def _principal_namespace(base_namespace: str, principal_id: str) -> str:
    """Derive an opaque, stable PostgreSQL namespace for one Matrix account."""
    if principal_id == _DEFAULT_PRINCIPAL_ID:
        return base_namespace
    principal_digest = hashlib.sha256(principal_id.encode()).hexdigest()
    return f"{base_namespace}:principal:{principal_digest}"


class PostgresEventCache:
    """PostgreSQL-backed ConversationEventCache implementation."""

    def __init__(
        self,
        *,
        database_url: str,
        namespace: str,
        principal_id: str = _DEFAULT_PRINCIPAL_ID,
        _registry: _PostgresRuntimeRegistry | None = None,
        _base_namespace: str | None = None,
    ) -> None:
        self._principal_id = principal_id
        self._base_namespace = namespace if _base_namespace is None else _base_namespace
        self._owns_registry = _registry is None
        self._registry = _PostgresRuntimeRegistry(database_url) if _registry is None else _registry
        self._runtime = self._registry.runtime(_principal_namespace(self._base_namespace, principal_id))

    @property
    def principal_id(self) -> str:
        """Return the Matrix principal owning this namespace."""
        return self._principal_id

    def for_principal(self, principal_id: str) -> PostgresEventCache:
        """Return a cache view backed by a principal-exclusive namespace."""
        normalized_principal_id = principal_id.strip()
        if not normalized_principal_id:
            msg = "Matrix event cache principal_id must be non-empty"
            raise ValueError(msg)
        return PostgresEventCache(
            database_url=self.database_url,
            namespace=self._base_namespace,
            principal_id=normalized_principal_id,
            _registry=self._registry,
            _base_namespace=self._base_namespace,
        )

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

    @property
    def cache_generation(self) -> str | None:
        """Return the principal namespace's durable cache generation when available."""
        if self._runtime.is_disabled or self._runtime.has_pending_principal_purge:
            return None
        return self._runtime.certification_generation

    async def initialize(self) -> None:
        """Open the PostgreSQL database and create the cache schema."""
        await self._runtime.initialize()

    def runtime_diagnostics(self) -> dict[str, object]:
        """Return log-safe runtime state for sync certification diagnostics."""
        return self._runtime.runtime_diagnostics()

    def pending_durable_write_room_ids(self) -> tuple[str, ...]:
        """Return rooms with runtime-only writes that must persist before certifying a sync token."""
        return self._runtime.pending_gap_room_ids()

    async def flush_pending_durable_writes(self, room_id: str) -> None:
        """Persist runtime-only writes for one room before certifying a sync token."""
        await self._operation(
            room_id,
            operation="flush_pending_durable_writes",
            disabled_result=None,
            callback=_noop_write,
            allow_departed=True,
        )

    def disable(self, reason: str) -> None:
        """Disable the advisory cache for the rest of the runtime."""
        if self._owns_registry:
            self._registry.disable(reason)
        else:
            self._runtime.disable(reason)

    async def close(self) -> None:
        """Close shared storage only from the root cache owner."""
        if self._owns_registry:
            await self._registry.close()

    async def _operation(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: _T,
        callback: Callable[[psycopg.AsyncConnection], Awaitable[_T]],
        allow_departed: bool = False,
        expected_membership_epoch: int | None = None,
        membership_epoch_mismatch_result: _T | None = None,
    ) -> _T:
        """Run one cache operation, flushing pending stale markers first even for reads."""
        if not self._can_expose_operation_result(room_id, allow_departed=allow_departed):
            return disabled_result
        transient_attempt = 0
        while True:
            flushed_pending = _FlushedPendingWrites()
            try:
                result, flushed_pending = await self._run_operation_attempt(
                    room_id,
                    operation=operation,
                    disabled_result=disabled_result,
                    callback=callback,
                    allow_departed=allow_departed,
                    expected_membership_epoch=expected_membership_epoch,
                    membership_epoch_mismatch_result=membership_epoch_mismatch_result,
                )
            except EventCacheBackendUnavailableError:
                transient_attempt += 1
                if transient_attempt < _MAX_TRANSIENT_OPERATION_ATTEMPTS:
                    continue
                raise
            except _CertificationGenerationChangedError:
                return disabled_result
            except Exception as exc:
                if not _is_transient_postgres_failure(exc):
                    raise
                await self._runtime.handle_transient_failure(exc, operation=operation)
                transient_attempt += 1
                if transient_attempt < _MAX_TRANSIENT_OPERATION_ATTEMPTS:
                    continue
                raise _cache_backend_unavailable(operation, exc) from exc
            else:
                self._forget_flushed_pending_writes(room_id, flushed_pending)
                return (
                    result
                    if self._can_expose_operation_result(room_id, allow_departed=allow_departed)
                    else disabled_result
                )

    async def _run_operation_attempt(
        self,
        room_id: str,
        *,
        operation: str,
        disabled_result: _T,
        callback: Callable[[psycopg.AsyncConnection], Awaitable[_T]],
        allow_departed: bool,
        expected_membership_epoch: int | None,
        membership_epoch_mismatch_result: _T | None,
    ) -> tuple[_T, _FlushedPendingWrites]:
        """Run one transaction attempt under the principal namespace lock."""
        async with self._runtime.acquire_db_operation(
            operation=operation,
        ) as db:
            try:
                return await self._run_operation_transaction(
                    db,
                    room_id=room_id,
                    disabled_result=disabled_result,
                    callback=callback,
                    allow_departed=allow_departed,
                    expected_membership_epoch=expected_membership_epoch,
                    membership_epoch_mismatch_result=membership_epoch_mismatch_result,
                )
            except BaseException:
                await _rollback_postgres_connection_best_effort(
                    db,
                    namespace=self._runtime.namespace,
                    operation=operation,
                )
                raise

    def _can_expose_operation_result(self, room_id: str, *, allow_departed: bool) -> bool:
        """Return whether current runtime state still authorizes one operation result."""
        return not self._runtime.is_disabled and (allow_departed or not self._runtime.is_room_departed(room_id))

    async def _run_operation_transaction(
        self,
        db: psycopg.AsyncConnection,
        *,
        room_id: str,
        disabled_result: _T,
        callback: Callable[[psycopg.AsyncConnection], Awaitable[_T]],
        allow_departed: bool,
        expected_membership_epoch: int | None,
        membership_epoch_mismatch_result: _T | None,
    ) -> tuple[_T, _FlushedPendingWrites]:
        """Commit one callback unless the transaction first removed its security scope."""
        if self._runtime.is_disabled or (not allow_departed and self._runtime.is_room_departed(room_id)):
            await db.commit()
            return disabled_result, _FlushedPendingWrites()
        flushed_pending = await self._flush_pending_writes(db, room_id)
        if flushed_pending.room_purge or flushed_pending.principal_purge:
            result = disabled_result
        elif not allow_departed:
            membership_state, membership_epoch = await postgres_event_cache_threads.load_room_membership_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
            )
            if membership_state != "joined":
                result = disabled_result
            elif expected_membership_epoch is not None and membership_epoch != expected_membership_epoch:
                result = (
                    disabled_result if membership_epoch_mismatch_result is None else membership_epoch_mismatch_result
                )
            else:
                result = await callback(db)
        else:
            result = await callback(db)
        await db.commit()
        return result, flushed_pending

    async def _flush_pending_writes(
        self,
        db: psycopg.AsyncConnection,
        room_id: str,
    ) -> _FlushedPendingWrites:
        if self._runtime.has_pending_principal_purge:
            await postgres_event_cache_events.purge_principal_locked(
                db,
                namespace=self._runtime.namespace,
            )
            return _FlushedPendingWrites(principal_purge=True)
        if self._runtime.has_pending_room_purge(room_id):
            await postgres_event_cache_events.purge_room_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
            )
            await postgres_event_cache_threads.set_room_membership_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                membership_state="departed",
                reason="room_departed",
            )
            return _FlushedPendingWrites(room_purge=True)

        flushed: list[tuple[str, str | None, _PendingGap]] = []
        room_pending = self._runtime.pending_room_gap(room_id)
        thread_pending = self._runtime.pending_thread_gaps(room_id)
        if room_pending is not None:
            await postgres_event_cache_threads.mark_room_gap_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                gap_marked_at=room_pending.gap_marked_at,
                reason=room_pending.reason,
            )
            flushed.append(("room", None, room_pending))

        for thread_id, pending_gap in thread_pending:
            if room_pending is not None and pending_gap.gap_marked_at <= room_pending.gap_marked_at:
                continue
            await postgres_event_cache_threads.mark_thread_gap_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
                gap_marked_at=pending_gap.gap_marked_at,
                reason=pending_gap.reason,
            )
            flushed.append(("thread", thread_id, pending_gap))
        return _FlushedPendingWrites(gaps=tuple(flushed))

    def _forget_flushed_pending_writes(
        self,
        room_id: str,
        flushed_pending: _FlushedPendingWrites,
    ) -> None:
        if flushed_pending.principal_purge:
            self._runtime.forget_pending_principal_purge()
            return
        if flushed_pending.room_purge:
            self._runtime.forget_pending_room_purge(room_id)
            return
        for kind, thread_id, pending in flushed_pending.gaps:
            if kind == "room":
                self._runtime.forget_pending_room_gap(room_id, pending)
                continue
            if thread_id is not None:
                self._runtime.forget_pending_thread_gap(room_id, thread_id, pending)

    async def get_thread_events(
        self,
        room_id: str,
        thread_id: str,
    ) -> list[dict[str, Any]] | None:
        """Return one thread's cached events oldest first, collapsed to one edit per message."""
        return await self._operation(
            room_id,
            operation="get_thread_events",
            disabled_result=None,
            callback=lambda db: postgres_event_cache_threads.load_thread_events(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def get_thread_event_ids(self, room_id: str, thread_id: str) -> set[str]:
        """Return every raw event ID this thread holds, superseded edits included."""
        return await self._operation(
            room_id,
            operation="get_thread_event_ids",
            disabled_result=set(),
            callback=lambda db: postgres_event_cache_threads.load_thread_event_ids(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def has_thread_snapshot(self, room_id: str, thread_id: str) -> bool:
        """Return whether any snapshot rows exist for one thread."""
        return await self._operation(
            room_id,
            operation="has_thread_snapshot",
            disabled_result=False,
            callback=lambda db: postgres_event_cache_threads.thread_snapshot_exists(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def get_recent_room_thread_ids(self, room_id: str, *, limit: int) -> list[str]:
        """Return locally known thread IDs for one room ordered by newest cached activity."""
        return await self._operation(
            room_id,
            operation="get_recent_room_thread_ids",
            disabled_result=[],
            callback=lambda db: postgres_event_cache_threads.load_recent_room_thread_ids(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                limit=limit,
            ),
        )

    async def get_thread_cache_gap(self, room_id: str, thread_id: str) -> ThreadCacheGap | None:
        """Return the durable gap marker recorded against one cached thread, if any."""
        return await self._operation(
            room_id,
            operation="get_thread_cache_gap",
            disabled_result=CACHE_GAP_UNAVAILABLE,
            callback=lambda db: postgres_event_cache_threads.load_thread_cache_gap(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def get_event(self, room_id: str, event_id: str) -> dict[str, Any] | None:
        """Return one cached event payload by event ID."""
        return await self._operation(
            room_id,
            operation="get_event",
            disabled_result=None,
            callback=lambda db: postgres_event_cache_events.load_event(
                db,
                namespace=self._runtime.namespace,
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
        return await self._operation(
            room_id,
            operation="get_recent_room_events",
            disabled_result=[],
            callback=lambda db: postgres_event_cache_events.load_recent_room_events(
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
        return await self._operation(
            room_id,
            operation="get_latest_edit",
            disabled_result=None,
            callback=lambda db: postgres_event_cache_events.load_latest_edit(
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
        return await self._operation(
            room_id,
            operation="get_latest_agent_message_snapshot",
            disabled_result=None,
            callback=lambda db: load_postgres_agent_message_snapshot(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
                sender=sender,
                runtime_started_at=runtime_started_at,
            ),
        )

    async def get_mxc_text(self, room_id: str, event_id: str, mxc_url: str) -> str | None:
        """Return plaintext only through a surviving event reference."""
        return await self._operation(
            room_id,
            operation="get_mxc_text",
            disabled_result=None,
            callback=lambda db: postgres_event_cache_events.load_mxc_text(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                event_id=event_id,
                mxc_url=mxc_url,
            ),
        )

    async def get_mxc_texts(
        self,
        room_id: str,
        references: Collection[tuple[str, str]],
        *,
        expected_membership_epoch: int,
    ) -> dict[tuple[str, str], str]:
        """Return scoped MXC plaintext for many surviving event references in one read."""
        if not references:
            return {}
        return await self._operation(
            room_id,
            operation="get_mxc_texts",
            disabled_result={},
            callback=lambda db: postgres_event_cache_events.load_mxc_texts(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                references=references,
            ),
            expected_membership_epoch=expected_membership_epoch,
        )

    async def store_event(
        self,
        event_id: str,
        room_id: str,
        event_data: dict[str, Any],
        *,
        expected_membership_epoch: int | None = None,
    ) -> None:
        """Insert or replace one event without replacing clear payloads with opaque ciphertext."""
        await self.store_events_batch(
            [(event_id, room_id, event_data)],
            expected_membership_epoch=expected_membership_epoch,
        )

    async def store_events_batch(
        self,
        events: list[tuple[str, str, dict[str, Any]]],
        *,
        expected_membership_epoch: int | None = None,
    ) -> None:
        """Insert or replace events without replacing clear payloads with opaque ciphertext."""
        if self._runtime.is_disabled or not events:
            return

        cached_at = time.time()
        for room_id, room_events in group_lookup_events_by_room(events).items():
            await self._operation(
                room_id,
                operation="store_events_batch",
                disabled_result=None,
                callback=lambda db, room_id=room_id, room_events=room_events, cached_at=cached_at: (
                    postgres_event_cache_events.persist_lookup_events(
                        db,
                        namespace=self._runtime.namespace,
                        room_id=room_id,
                        room_events=room_events,
                        cached_at=cached_at,
                    )
                ),
                expected_membership_epoch=expected_membership_epoch,
            )

    async def store_mxc_text(
        self,
        room_id: str,
        event_id: str,
        mxc_url: str,
        text: str,
        *,
        expected_membership_epoch: int | None = None,
    ) -> bool:
        """Persist plaintext only through a surviving event reference."""
        return bool(
            await self._operation(
                room_id,
                operation="store_mxc_text",
                disabled_result=False,
                callback=lambda db: postgres_event_cache_events.persist_mxc_text(
                    db,
                    namespace=self._runtime.namespace,
                    room_id=room_id,
                    event_id=event_id,
                    mxc_url=mxc_url,
                    text=text,
                    cached_at=time.time(),
                ),
                expected_membership_epoch=expected_membership_epoch,
            ),
        )

    async def replace_thread(
        self,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        *,
        expected_membership_epoch: int,
        fetch_started_at: float,
    ) -> bool:
        """Install one fetched snapshot, clearing a gap marker the fetch covers."""
        return await self._operation(
            room_id,
            operation="replace_thread",
            disabled_result=False,
            callback=lambda db: self._replace_thread_locked(
                db,
                room_id=room_id,
                thread_id=thread_id,
                events=events,
                fetch_started_at=fetch_started_at,
            ),
            expected_membership_epoch=expected_membership_epoch,
            membership_epoch_mismatch_result=False,
        )

    async def _replace_thread_locked(
        self,
        db: psycopg.AsyncConnection,
        *,
        room_id: str,
        thread_id: str,
        events: list[dict[str, Any]],
        fetch_started_at: float,
    ) -> bool:
        await postgres_event_cache_threads.replace_thread_locked(
            db,
            namespace=self._runtime.namespace,
            room_id=room_id,
            thread_id=thread_id,
            events=events,
            stored_at=time.time(),
            fetch_started_at=fetch_started_at,
        )
        return True

    async def invalidate_thread(self, room_id: str, thread_id: str) -> None:
        """Delete cached events for one thread."""
        await self._operation(
            room_id,
            operation="invalidate_thread",
            disabled_result=None,
            callback=lambda db: postgres_event_cache_threads.invalidate_thread_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
            ),
        )

    async def invalidate_room_threads(self, room_id: str) -> None:
        """Delete every cached thread snapshot for one room."""
        await self._operation(
            room_id,
            operation="invalidate_room_threads",
            disabled_result=None,
            callback=lambda db: postgres_event_cache_threads.invalidate_room_threads_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
            ),
        )

    async def mark_thread_gap(self, room_id: str, thread_id: str, *, reason: str) -> None:
        """Persist one durable thread gap marker."""
        gap_marked_at = time.time()
        try:
            await self._operation(
                room_id,
                operation="mark_thread_gap",
                disabled_result=None,
                callback=lambda db: postgres_event_cache_threads.mark_thread_gap_locked(
                    db,
                    namespace=self._runtime.namespace,
                    room_id=room_id,
                    thread_id=thread_id,
                    gap_marked_at=gap_marked_at,
                    reason=reason,
                ),
            )
        except EventCacheBackendUnavailableError:
            self._runtime.record_pending_thread_gap(
                room_id,
                thread_id,
                gap_marked_at=gap_marked_at,
                reason=reason,
            )
            raise

    async def mark_room_threads_gap(self, room_id: str, *, reason: str) -> None:
        """Record a durable gap marker against every cached thread in one room."""
        gap_marked_at = time.time()
        try:
            await self._operation(
                room_id,
                operation="mark_room_threads_gap",
                disabled_result=None,
                callback=lambda db: postgres_event_cache_threads.mark_room_gap_locked(
                    db,
                    namespace=self._runtime.namespace,
                    room_id=room_id,
                    gap_marked_at=gap_marked_at,
                    reason=reason,
                ),
            )
        except EventCacheBackendUnavailableError:
            self._runtime.record_pending_room_gap(
                room_id,
                gap_marked_at=gap_marked_at,
                reason=reason,
            )
            raise

    async def apply_thread_mutation_append(
        self,
        room_id: str,
        thread_id: str,
        event: dict[str, Any],
        *,
        append_failed_reason: str,
    ) -> ThreadAppendOutcome:
        """Append one threaded mutation, recording a gap marker when it cannot land."""
        normalized_event = normalize_event_source_for_cache(event)
        return await self._operation(
            room_id,
            operation="apply_thread_mutation_append",
            disabled_result=ThreadAppendOutcome.WRITES_UNAVAILABLE,
            callback=lambda db: postgres_event_cache_threads.apply_thread_mutation_append_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                thread_id=thread_id,
                normalized_event=normalized_event,
                append_failed_reason=append_failed_reason,
            ),
        )

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Return the cached thread ID for one event."""
        return await self._operation(
            room_id,
            operation="get_thread_id_for_event",
            disabled_result=None,
            callback=lambda db: postgres_event_cache_events.load_thread_id_for_event(
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
            await self._operation(
                room_id,
                operation="redact_event",
                disabled_result=False,
                callback=lambda db: postgres_event_cache_events.redact_event_locked(
                    db,
                    namespace=self._runtime.namespace,
                    room_id=room_id,
                    event_id=event_id,
                ),
            ),
        )

    async def purge_room(self, room_id: str) -> None:
        """Delete this principal namespace's rows for one departed room."""
        if not self._runtime.is_room_departed(room_id):
            self.mark_room_departed(room_id)

        await self._operation(
            room_id,
            operation="purge_room",
            disabled_result=None,
            callback=_noop_write,
            allow_departed=True,
        )

    def mark_room_departed(self, room_id: str) -> int:
        """Synchronously reject access and return the new room-fence epoch."""
        return self._runtime.mark_room_departed(room_id)

    def room_departure_epoch(self, room_id: str) -> int:
        """Return the current room-fence epoch."""
        return self._runtime.room_departure_epoch(room_id)

    async def room_membership_epoch(self, room_id: str) -> int | None:
        """Certify and return the durable room-membership transition epoch."""
        return await self._operation(
            room_id,
            operation="room_membership_epoch",
            disabled_result=None,
            callback=lambda db: postgres_event_cache_threads.certify_room_membership_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
            ),
            allow_departed=True,
        )

    async def mark_room_joined(
        self,
        room_id: str,
        *,
        expected_departure_epoch: int,
    ) -> None:
        """Remove a departure fence only after any pending purge commits."""
        if self.room_departure_epoch(room_id) != expected_departure_epoch:
            return
        if not self.durable_writes_available:
            return

        if self._runtime.has_pending_room_purge(room_id):
            await self._operation(
                room_id,
                operation="mark_room_joined_flush",
                disabled_result=None,
                callback=_noop_write,
                allow_departed=True,
            )
        if self._runtime.has_pending_room_purge(room_id):
            return

        async def join_if_current(db: psycopg.AsyncConnection) -> bool:
            if self.room_departure_epoch(room_id) != expected_departure_epoch:
                return False
            membership_state, _membership_epoch = await postgres_event_cache_threads.load_room_membership_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
            )
            if membership_state != "joined":
                await postgres_event_cache_threads.set_room_membership_locked(
                    db,
                    namespace=self._runtime.namespace,
                    room_id=room_id,
                    membership_state="joined",
                    reason="room_rejoined",
                )
            if self.room_departure_epoch(room_id) == expected_departure_epoch:
                return True
            await postgres_event_cache_events.purge_room_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
            )
            await postgres_event_cache_threads.set_room_membership_locked(
                db,
                namespace=self._runtime.namespace,
                room_id=room_id,
                membership_state="departed",
                reason="room_departed",
            )
            return False

        joined = await self._operation(
            room_id,
            operation="mark_room_joined",
            disabled_result=False,
            callback=join_if_current,
            allow_departed=True,
        )
        if joined:
            self._runtime.mark_room_joined(
                room_id,
                expected_departure_epoch=expected_departure_epoch,
            )

    async def purge_principal(self) -> None:
        """Delete principal content while preserving durable refill generations."""
        self._runtime.record_pending_principal_purge()

        await self._operation(
            _PRINCIPAL_PURGE_LOCK_SCOPE,
            operation="purge_principal",
            disabled_result=None,
            callback=_noop_write,
        )
