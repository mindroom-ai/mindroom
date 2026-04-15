"""Cache boundary and public API for Matrix event cache lookups."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

from . import (
    event_cache_codec,
    event_cache_events,
    event_cache_lifecycle,
    event_cache_runtime,
    event_cache_threads,
)
from .event_cache_codec import normalize_event_source_for_cache

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    import aiosqlite

_EVENT_CACHE_SCHEMA_VERSION = event_cache_lifecycle.EVENT_CACHE_SCHEMA_VERSION
_MAX_CACHED_ROOM_LOCKS = event_cache_runtime._MAX_CACHED_ROOM_LOCKS
T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class ThreadCacheState:
    """Durable freshness and invalidation metadata for one cached thread."""

    validated_at: float | None
    invalidated_at: float | None
    invalidation_reason: str | None
    room_invalidated_at: float | None
    room_invalidation_reason: str | None


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
        self._runtime = event_cache_runtime._EventCacheRuntime(db_path)

    @property
    def db_path(self) -> Path:
        """Return the SQLite database path for this cache instance."""
        return self._runtime.db_path

    @property
    def _db(self) -> aiosqlite.Connection | None:
        return self._runtime.db

    @property
    def is_initialized(self) -> bool:
        """Return whether the SQLite connection is currently open."""
        return self._runtime.is_initialized

    @property
    def _room_locks(self) -> dict[str, event_cache_runtime._RoomLockEntry]:
        return self._runtime._room_locks

    def _room_lock_entry(
        self,
        room_id: str,
        *,
        active_user_increment: int = 0,
    ) -> event_cache_runtime._RoomLockEntry:
        return self._runtime.room_lock_entry(room_id, active_user_increment=active_user_increment)

    def _acquire_room_lock(self, room_id: str, *, operation: str) -> AsyncIterator[None]:
        return self._runtime.acquire_room_lock(room_id, operation=operation)

    def _acquire_db_operation(
        self,
        room_id: str,
        *,
        operation: str,
    ) -> AsyncIterator[aiosqlite.Connection]:
        return self._runtime.acquire_db_operation(room_id, operation=operation)

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
        async with self._acquire_db_operation(room_id, operation=operation) as db:
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
        async with self._acquire_db_operation(room_id, operation=operation) as db:
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

    async def get_thread_cache_state(self, room_id: str, thread_id: str) -> ThreadCacheState | None:
        """Return durable freshness metadata for one cached thread."""
        async def read_cache_state(db: aiosqlite.Connection) -> ThreadCacheState | None:
            row = await event_cache_threads.load_thread_cache_state_row(
                db,
                room_id=room_id,
                thread_id=thread_id,
            )
            if row is None or all(value is None for value in row):
                return None
            return ThreadCacheState(
                validated_at=row[0],
                invalidated_at=row[1],
                invalidation_reason=row[2],
                room_invalidated_at=row[3],
                room_invalidation_reason=row[4],
            )
        return await self._read_operation(
            room_id,
            operation="get_thread_cache_state",
            disabled_result=None,
            reader=read_cache_state,
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

    async def store_event(self, event_id: str, room_id: str, event_data: dict[str, Any]) -> None:
        """Insert or replace one individually cached Matrix event."""
        await self.store_events_batch([(event_id, room_id, event_data)])

    async def store_events_batch(self, events: list[tuple[str, str, dict[str, Any]]]) -> None:
        """Insert or replace one batch of individually cached Matrix events."""
        if self._runtime.is_disabled:
            return
        if not events:
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
                writer=lambda db, room_id=room_id, room_events=room_events, cached_at=cached_at: event_cache_events.persist_lookup_events(
                    db,
                    room_id=room_id,
                    room_events=room_events,
                    cached_at=cached_at,
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
        normalized_event = normalize_event_source_for_cache(event)
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

normalize_nio_event_for_cache = event_cache_codec.normalize_nio_event_for_cache
