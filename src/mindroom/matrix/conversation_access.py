"""Explicit access layer for Matrix conversation reads and advisory cache writes."""

from __future__ import annotations

import time
import typing
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import nio
from nio.responses import RoomGetEventError

from mindroom.matrix.client import (
    ResolvedVisibleMessage,
    fetch_thread_history,
    fetch_thread_snapshot,
    resolve_thread_history_delta,
)
from mindroom.matrix.event_cache import ConversationEventCache, normalize_event_source_for_cache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.room_cache import cached_room_get_event
from mindroom.matrix.thread_cache import ResolvedThreadCache, ResolvedThreadCacheEntry, resolved_thread_cache_entry
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError

_SYNC_FRESHNESS_WINDOW_SECONDS = 30.0
_INCREMENTAL_RESOLVED_CACHE_EVENT_LIMIT = 1


class ConversationReadAccess(Protocol):
    """Conversation-data reads available to resolver and reply-chain code."""

    async def get_event(self, room_id: str, event_id: str) -> EventLookupResult:
        """Resolve one Matrix event by ID."""

    async def get_thread_snapshot(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve lightweight thread context for dispatch."""

    async def get_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve full thread history for one conversation root."""


def _collect_sync_timeline_cache_updates(
    response: nio.SyncResponse,
) -> tuple[list[tuple[str, str, dict[str, object]]], list[tuple[str, str]], list[tuple[str, dict[str, object]]]]:
    """Extract cacheable timeline events and redactions from one sync response."""
    cached_events: list[tuple[str, str, dict[str, object]]] = []
    redacted_events: list[tuple[str, str]] = []
    threaded_events: list[tuple[str, dict[str, object]]] = []
    redacted_event_ids_by_room: dict[str, set[str]] = {}

    for room_id, room_info in response.rooms.join.items():
        for event in room_info.timeline.events:
            _collect_sync_event_cache_update(
                room_id=room_id,
                event=event,
                cached_events=cached_events,
                redacted_events=redacted_events,
                threaded_events=threaded_events,
                redacted_event_ids_by_room=redacted_event_ids_by_room,
            )

    filtered_cached_events = [
        (event_id, room_id, event_source)
        for event_id, room_id, event_source in cached_events
        if event_id not in redacted_event_ids_by_room.get(room_id, set())
    ]
    filtered_threaded_events = [
        (room_id, event_source)
        for room_id, event_source in threaded_events
        if event_source.get("event_id") not in redacted_event_ids_by_room.get(room_id, set())
    ]
    return filtered_cached_events, redacted_events, filtered_threaded_events


def _collect_sync_event_cache_update(
    *,
    room_id: str,
    event: nio.Event | object,
    cached_events: list[tuple[str, str, dict[str, object]]],
    redacted_events: list[tuple[str, str]],
    threaded_events: list[tuple[str, dict[str, object]]],
    redacted_event_ids_by_room: dict[str, set[str]],
) -> None:
    """Collect cache updates for one sync timeline event."""
    if not isinstance(event, nio.Event):
        return
    if not isinstance(event.source, dict):
        return
    if not isinstance(event.event_id, str):
        return
    if isinstance(event, nio.RedactionEvent):
        if not isinstance(event.redacts, str):
            return
        redacted_events.append((room_id, event.redacts))
        redacted_event_ids_by_room.setdefault(room_id, set()).add(event.redacts)
        return

    event_source = normalize_event_source_for_cache(
        event.source,
        event_id=event.event_id,
        sender=event.sender if isinstance(event.sender, str) else None,
        origin_server_ts=_sync_event_origin_server_ts(event),
    )
    cached_events.append((event.event_id, room_id, event_source))

    threaded_event = _threaded_sync_event_cache_update(room_id, event_source)
    if threaded_event is not None:
        threaded_events.append(threaded_event)


def _sync_event_origin_server_ts(event: nio.Event) -> int | None:
    """Return a cacheable integer origin_server_ts from one Matrix event."""
    server_timestamp = event.server_timestamp
    if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool):
        return server_timestamp
    return None


def _threaded_sync_event_cache_update(
    room_id: str,
    event_source: dict[str, object],
) -> tuple[str, dict[str, object]] | None:
    """Return one candidate thread append entry when the event may belong to a thread."""
    event_info = EventInfo.from_event(event_source)
    if isinstance(event_info.thread_id, str):
        return room_id, event_source
    if not event_info.is_edit:
        return None
    if not isinstance(event_info.thread_id_from_edit, str) and not isinstance(
        event_info.original_event_id,
        str,
    ):
        return None
    return room_id, event_source


async def _resolve_thread_id_for_cached_event_append(
    room_id: str,
    *,
    event_info: EventInfo,
    event_cache: ConversationEventCache,
) -> str | None:
    """Resolve the thread root for one cached event append."""
    if isinstance(event_info.thread_id, str):
        return event_info.thread_id
    if not event_info.is_edit:
        return None
    if isinstance(event_info.thread_id_from_edit, str):
        return event_info.thread_id_from_edit
    if not isinstance(event_info.original_event_id, str):
        return None
    return await event_cache.get_thread_id_for_event(room_id, event_info.original_event_id)


@dataclass
class MatrixConversationAccess(ConversationReadAccess):
    """Own Matrix conversation reads and advisory cache writes for one bot."""

    logger: structlog.stdlib.BoundLogger
    runtime: BotRuntimeView
    _turn_event_cache: ContextVar[dict[tuple[str, str], EventLookupResult] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_event_lookup_cache", default=None),
    )
    _resolved_thread_cache: ResolvedThreadCache = field(default_factory=ResolvedThreadCache, init=False)

    def _require_client(self) -> nio.AsyncClient:
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation access"
            raise RuntimeError(msg)
        return client

    def _queue_room_cache_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
    ) -> asyncio.Task[object]:
        coordinator = self.runtime.event_cache_write_coordinator
        if coordinator is None:
            msg = "Event cache write coordinator is not configured"
            raise RuntimeError(msg)
        return coordinator.queue_room_update(room_id, update_coro_factory, name=name)

    @asynccontextmanager
    async def turn_scope(self) -> AsyncIterator[None]:
        """Memoize event lookups for the lifetime of one inbound turn."""
        event_cache = self._turn_event_cache.get()
        if event_cache is not None:
            yield
            return

        event_token = self._turn_event_cache.set({})
        try:
            yield
        finally:
            self._turn_event_cache.reset(event_token)

    async def get_event(self, room_id: str, event_id: str) -> EventLookupResult:
        """Resolve one event through per-turn memoization and the advisory cache."""
        cache_key = (room_id, event_id)
        turn_cache = self._turn_event_cache.get()
        if turn_cache is not None and cache_key in turn_cache:
            return turn_cache[cache_key]

        response = await cached_room_get_event(
            self._require_client(),
            self.runtime.event_cache,
            room_id,
            event_id,
        )
        if turn_cache is not None:
            turn_cache[cache_key] = response
        return response

    def thread_version(self, room_id: str, thread_id: str) -> int:
        """Return the current in-memory version for one thread."""
        return self._resolved_thread_cache.version(room_id, thread_id)

    def _bump_thread_version(self, room_id: str, thread_id: str) -> int:
        return self._resolved_thread_cache.bump_version(room_id, thread_id)

    def _seconds_since_last_sync_activity(self) -> float | None:
        last_sync_activity_monotonic = self.runtime.last_sync_activity_monotonic
        if last_sync_activity_monotonic is None:
            return None
        return max(time.monotonic() - last_sync_activity_monotonic, 0.0)

    def _should_refresh_cached_thread_history(self, room_id: str, thread_id: str) -> bool:
        sync_age_seconds = self._seconds_since_last_sync_activity()
        if sync_age_seconds is None or sync_age_seconds >= _SYNC_FRESHNESS_WINDOW_SECONDS:
            return True
        self.logger.debug(
            "Skipping incremental Matrix thread refresh because sync is fresh",
            room_id=room_id,
            thread_id=thread_id,
            sync_age_ms=round(sync_age_seconds * 1000, 1),
        )
        return False

    def _thread_id_from_event_source(self, event_source: dict[str, object]) -> str | None:
        event_info = EventInfo.from_event(event_source)
        if event_info.thread_id is not None:
            return event_info.thread_id
        if event_info.is_edit and event_info.thread_id_from_edit is not None:
            return event_info.thread_id_from_edit
        return None

    def _event_id_from_event_source(self, event_source: dict[str, object]) -> str | None:
        event_id = event_source.get("event_id")
        return event_id if isinstance(event_id, str) else None

    def _sort_thread_history_root_first(
        self,
        history: list[ResolvedVisibleMessage],
        *,
        thread_id: str,
    ) -> None:
        history.sort(key=lambda message: (message.timestamp, message.event_id))
        root_index = next((index for index, message in enumerate(history) if message.event_id == thread_id), None)
        if root_index not in (None, 0):
            history.insert(0, history.pop(root_index))

    def _resolved_cache_diagnostics(
        self,
        *,
        cache_read_ms: float,
        incremental_refresh_ms: float = 0.0,
        resolution_ms: float = 0.0,
        sidecar_hydration_ms: float = 0.0,
    ) -> dict[str, float]:
        return {
            "cache_read_ms": cache_read_ms,
            "incremental_refresh_ms": incremental_refresh_ms,
            "resolution_ms": resolution_ms,
            "sidecar_hydration_ms": sidecar_hydration_ms,
        }

    def _log_resolved_thread_cache(
        self,
        event: str,
        *,
        room_id: str,
        thread_id: str,
        reason: str | None = None,
        thread_version: int | None = None,
    ) -> None:
        event_data: dict[str, str | int] = {
            "room_id": room_id,
            "thread_id": thread_id,
        }
        if reason is not None:
            event_data["reason"] = reason
        if thread_version is not None:
            event_data["thread_version"] = thread_version
        self.logger.debug(event, **event_data)

    async def _wait_for_pending_room_cache_updates(self, room_id: str) -> None:
        coordinator = self.runtime.event_cache_write_coordinator
        if coordinator is None:
            return
        await coordinator.wait_for_room_idle(room_id)

    async def _cached_thread_event_sources(
        self,
        room_id: str,
        thread_id: str,
    ) -> Sequence[dict[str, object]] | None:
        event_cache = self.runtime.event_cache
        if event_cache is None:
            return None
        return await event_cache.get_thread_events(room_id, thread_id)

    async def _cached_thread_source_event_ids(
        self,
        room_id: str,
        thread_id: str,
    ) -> frozenset[str]:
        try:
            cached_event_sources = await self._cached_thread_event_sources(room_id, thread_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to read raw thread events for resolved cache",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return frozenset()
        if cached_event_sources is None:
            return frozenset()
        return frozenset(
            event_id
            for event_source in cached_event_sources
            if (event_id := self._event_id_from_event_source(event_source)) is not None
        )

    async def _store_resolved_thread_cache_entry(
        self,
        room_id: str,
        thread_id: str,
        *,
        history: Sequence[ResolvedVisibleMessage],
        thread_version: int,
    ) -> None:
        self._resolved_thread_cache.store(
            room_id,
            thread_id,
            resolved_thread_cache_entry(
                history=history,
                source_event_ids=await self._cached_thread_source_event_ids(room_id, thread_id),
                thread_version=thread_version,
            ),
        )
        self._log_resolved_thread_cache(
            "resolved_thread_cache_store",
            room_id=room_id,
            thread_id=thread_id,
            thread_version=thread_version,
        )

    async def _incrementally_refresh_resolved_thread_cache(
        self,
        room_id: str,
        thread_id: str,
        *,
        entry: ResolvedThreadCacheEntry,
        entry_version: int,
        current_thread_version: int,
    ) -> ThreadReadResult | None:
        cache_read_started = time.perf_counter()
        invalidation_reason: str | None = None
        try:
            cached_event_sources = await self._cached_thread_event_sources(room_id, thread_id)
        except Exception as exc:
            self.logger.warning(
                "Resolved thread cache refresh could not read raw thread events",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return None
        cache_read_ms = round((time.perf_counter() - cache_read_started) * 1000, 1)
        current_event_ids: frozenset[str] = frozenset()
        new_event_sources: list[dict[str, object]] = []
        if cached_event_sources is None:
            invalidation_reason = "missing_raw_cache"
        else:
            current_event_ids = frozenset(
                event_id
                for event_source in cached_event_sources
                if (event_id := self._event_id_from_event_source(event_source)) is not None
            )
            if not entry.source_event_ids.issubset(current_event_ids):
                invalidation_reason = "redaction_or_missing_source"
            else:
                new_event_sources = [
                    event_source
                    for event_source in cached_event_sources
                    if (event_id := self._event_id_from_event_source(event_source)) is not None
                    and event_id not in entry.source_event_ids
                ]
                if not new_event_sources:
                    invalidation_reason = "version_changed_without_raw_delta"
                elif len(new_event_sources) > _INCREMENTAL_RESOLVED_CACHE_EVENT_LIMIT:
                    invalidation_reason = "multi_event_delta"
                elif EventInfo.from_event(new_event_sources[0]).is_edit:
                    invalidation_reason = "edit_delta"

        if invalidation_reason is not None:
            self._log_resolved_thread_cache(
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason=invalidation_reason,
                thread_version=current_thread_version,
            )
            return None

        delta_history = await resolve_thread_history_delta(
            self._require_client(),
            thread_id=thread_id,
            event_sources=new_event_sources,
        )
        merged_history_by_event_id = {message.event_id: message for message in entry.clone_history()}
        for message in delta_history:
            merged_history_by_event_id[message.event_id] = message
        merged_history = list(merged_history_by_event_id.values())
        self._sort_thread_history_root_first(merged_history, thread_id=thread_id)
        self._resolved_thread_cache.store(
            room_id,
            thread_id,
            resolved_thread_cache_entry(
                history=merged_history,
                source_event_ids=current_event_ids,
                thread_version=current_thread_version,
            ),
        )
        self._log_resolved_thread_cache(
            "resolved_thread_cache_incremental_refresh",
            room_id=room_id,
            thread_id=thread_id,
            reason=f"{entry_version}->{current_thread_version}",
            thread_version=current_thread_version,
        )
        return thread_history_result(
            merged_history,
            is_full_history=True,
            thread_version=current_thread_version,
            diagnostics=self._resolved_cache_diagnostics(
                cache_read_ms=cache_read_ms,
                incremental_refresh_ms=delta_history.diagnostics.get("resolution_ms", 0.0),
                resolution_ms=delta_history.diagnostics.get("resolution_ms", 0.0),
                sidecar_hydration_ms=delta_history.diagnostics.get("sidecar_hydration_ms", 0.0),
            ),
        )

    async def _read_full_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        await self._wait_for_pending_room_cache_updates(room_id)
        current_thread_version = self.thread_version(room_id, thread_id)
        async with self._resolved_thread_cache.entry_lock(room_id, thread_id):
            lookup_started = time.perf_counter()
            cache_lookup = self._resolved_thread_cache.lookup(room_id, thread_id)
            cache_read_ms = round((time.perf_counter() - lookup_started) * 1000, 1)
            entry = cache_lookup.entry
            if entry is not None and entry.thread_version == current_thread_version:
                self._log_resolved_thread_cache(
                    "resolved_thread_cache_hit",
                    room_id=room_id,
                    thread_id=thread_id,
                    thread_version=current_thread_version,
                )
                return thread_history_result(
                    entry.clone_history(),
                    is_full_history=True,
                    thread_version=current_thread_version,
                    diagnostics=self._resolved_cache_diagnostics(cache_read_ms=cache_read_ms),
                )
            if cache_lookup.expired:
                self._log_resolved_thread_cache(
                    "resolved_thread_cache_invalidate",
                    room_id=room_id,
                    thread_id=thread_id,
                    reason="ttl_expired",
                    thread_version=current_thread_version,
                )
            elif entry is None:
                self._log_resolved_thread_cache(
                    "resolved_thread_cache_miss",
                    room_id=room_id,
                    thread_id=thread_id,
                    thread_version=current_thread_version,
                )
            elif entry.thread_version != current_thread_version:
                incrementally_refreshed = await self._incrementally_refresh_resolved_thread_cache(
                    room_id,
                    thread_id,
                    entry=entry,
                    entry_version=entry.thread_version,
                    current_thread_version=current_thread_version,
                )
                if incrementally_refreshed is not None:
                    return incrementally_refreshed
                self._resolved_thread_cache.invalidate(room_id, thread_id)

            history = self._full_history_result(
                await fetch_thread_history(
                    self._require_client(),
                    room_id,
                    thread_id,
                    event_cache=self.runtime.event_cache,
                    refresh_cache=self._should_refresh_cached_thread_history(room_id, thread_id),
                ),
                room_id=room_id,
                thread_id=thread_id,
            )
            await self._store_resolved_thread_cache_entry(
                room_id,
                thread_id,
                history=history,
                thread_version=current_thread_version,
            )
            return history

    def _snapshot_result(
        self,
        history: Sequence[ResolvedVisibleMessage],
        *,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Normalize snapshot reads into the shared thread-history result type."""
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=history.is_full_history,
                thread_version=self.thread_version(room_id, thread_id),
                diagnostics=history.diagnostics,
            )
        return thread_history_result(
            list(history),
            is_full_history=False,
            thread_version=self.thread_version(room_id, thread_id),
        )

    def _full_history_result(
        self,
        history: Sequence[ResolvedVisibleMessage],
        *,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Normalize full-history reads into the shared thread-history result type."""
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=True,
                thread_version=self.thread_version(room_id, thread_id),
                diagnostics=history.diagnostics,
            )
        return thread_history_result(
            list(history),
            is_full_history=True,
            thread_version=self.thread_version(room_id, thread_id),
        )

    async def _read_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        require_full_history: bool,
    ) -> ThreadReadResult:
        """Resolve one thread through the snapshot or full-history access policy."""
        if require_full_history:
            return await self._read_full_thread_history(room_id, thread_id)

        return self._snapshot_result(
            await fetch_thread_snapshot(
                self._require_client(),
                room_id,
                thread_id,
                event_cache=self.runtime.event_cache,
            ),
            room_id=room_id,
            thread_id=thread_id,
        )

    async def get_thread_snapshot(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve thread snapshot using one explicit access policy."""
        return await self._read_thread(room_id, thread_id, require_full_history=False)

    async def get_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve full thread history using one explicit access policy."""
        return await self._read_thread(room_id, thread_id, require_full_history=True)

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessageText,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live thread event into the advisory cache when the thread is known."""
        event_cache = self.runtime.event_cache
        if event_cache is None:
            return

        try:
            thread_id = await _resolve_thread_id_for_cached_event_append(
                room_id,
                event_info=event_info,
                event_cache=event_cache,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to resolve cached thread for live event",
                room_id=room_id,
                event_id=event.event_id,
                original_event_id=event_info.original_event_id,
                error=str(exc),
            )
            return
        if thread_id is None:
            return
        self._bump_thread_version(room_id, thread_id)
        if event_info.is_edit:
            self._resolved_thread_cache.invalidate(room_id, thread_id)

        server_timestamp = event.server_timestamp
        event_source = normalize_event_source_for_cache(
            event.source,
            event_id=event.event_id,
            sender=event.sender,
            origin_server_ts=server_timestamp
            if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
            else None,
        )

        try:
            await self._queue_room_cache_update(
                room_id,
                lambda: event_cache.append_event(room_id, thread_id, event_source),
                name="matrix_cache_append_live_event",
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to append live thread event to cache",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event.event_id,
                error=str(exc),
            )

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        event_cache = self.runtime.event_cache
        if event_cache is None:
            return

        try:
            thread_id = await event_cache.get_thread_id_for_event(room_id, event.redacts)
        except Exception as exc:
            self.logger.warning(
                "Failed to resolve cached thread for redaction",
                room_id=room_id,
                event_id=event.event_id,
                redacted_event_id=event.redacts,
                error=str(exc),
            )
            thread_id = None
        if thread_id is not None:
            self._bump_thread_version(room_id, thread_id)
            self._resolved_thread_cache.invalidate(room_id, thread_id)

        try:
            await self._queue_room_cache_update(
                room_id,
                lambda: event_cache.redact_event(room_id, event.redacts),
                name="matrix_cache_apply_redaction",
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to apply live redaction to cache",
                room_id=room_id,
                thread_id=thread_id,
                redacted_event_id=event.redacts,
                error=str(exc),
            )

    async def _append_sync_thread_event(
        self,
        event_cache: ConversationEventCache,
        *,
        room_id: str,
        event_source: dict[str, object],
    ) -> None:
        """Append one sync event to cached thread history when its root can be resolved."""
        event_info = EventInfo.from_event(event_source)
        event_id = event_source.get("event_id")
        try:
            thread_id = await _resolve_thread_id_for_cached_event_append(
                room_id,
                event_info=event_info,
                event_cache=event_cache,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to resolve cached thread for sync event",
                room_id=room_id,
                event_id=event_id,
                original_event_id=event_info.original_event_id,
                error=str(exc),
            )
            return

        if thread_id is None:
            return

        try:
            await event_cache.append_thread_event(room_id, thread_id, event_source)
        except Exception as exc:
            self.logger.warning(
                "Failed to append sync thread event to cache",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                error=str(exc),
            )

    async def _persist_room_sync_timeline_updates(
        self,
        event_cache: ConversationEventCache,
        room_id: str,
        cached_events: list[tuple[str, str, dict[str, object]]],
        redacted_event_ids: list[str],
        threaded_events: list[dict[str, object]],
    ) -> None:
        """Persist one room's prepared sync timeline updates."""
        stored_events = True
        if cached_events:
            try:
                await event_cache.store_events_batch(cached_events)
            except Exception as exc:
                stored_events = False
                self.logger.warning(
                    "Failed to cache sync timeline events",
                    room_id=room_id,
                    error=str(exc),
                    events=len(cached_events),
                    thread_appends=len(threaded_events),
                    redactions=len(redacted_event_ids),
                )
        if stored_events:
            # append_thread_event relies on the point-lookup rows written above.
            for event_source in threaded_events:
                await self._append_sync_thread_event(
                    event_cache,
                    room_id=room_id,
                    event_source=event_source,
                )
        redacted_thread_ids: set[str] = set()
        for redacted_event_id in redacted_event_ids:
            try:
                thread_id = await event_cache.get_thread_id_for_event(
                    room_id, redacted_event_id,
                )
                if thread_id is not None:
                    redacted_thread_ids.add(thread_id)
                await event_cache.redact_event(room_id, redacted_event_id)
            except Exception as exc:
                self.logger.warning(
                    "Failed to apply sync redaction to cache",
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    error=str(exc),
                )
        for thread_id in redacted_thread_ids:
            self._bump_thread_version(room_id, thread_id)
            self._resolved_thread_cache.invalidate(room_id, thread_id)

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Schedule sync timeline persistence so sync callbacks do not wait on SQLite."""
        event_cache = self.runtime.event_cache
        if event_cache is None:
            return

        filtered_cached_events, redacted_events, threaded_events = _collect_sync_timeline_cache_updates(response)
        if not filtered_cached_events and not redacted_events and not threaded_events:
            return

        for _event_id, room_id, event_source in filtered_cached_events:
            thread_id = self._thread_id_from_event_source(event_source)
            if thread_id is not None:
                self._bump_thread_version(room_id, thread_id)
                if EventInfo.from_event(event_source).is_edit:
                    self._resolved_thread_cache.invalidate(room_id, thread_id)

        updates_by_room: dict[
            str,
            tuple[list[tuple[str, str, dict[str, object]]], list[str], list[dict[str, object]]],
        ] = {}
        for event_id, room_id, event_source in filtered_cached_events:
            room_events, _room_redactions, _room_threaded_events = updates_by_room.setdefault(room_id, ([], [], []))
            room_events.append((event_id, room_id, event_source))
        for room_id, redacted_event_id in redacted_events:
            _room_events, room_redactions, _room_threaded_events = updates_by_room.setdefault(room_id, ([], [], []))
            room_redactions.append(redacted_event_id)
        for room_id, event_source in threaded_events:
            _room_events, _room_redactions, room_threaded_events = updates_by_room.setdefault(room_id, ([], [], []))
            room_threaded_events.append(event_source)

        for room_id, (room_events, room_redactions, room_threaded_events) in updates_by_room.items():
            self._queue_room_cache_update(
                room_id,
                lambda room_events=room_events,
                room_id=room_id,
                room_redactions=room_redactions,
                room_threaded_events=room_threaded_events: self._persist_room_sync_timeline_updates(
                    event_cache,
                    room_id,
                    room_events,
                    room_redactions,
                    room_threaded_events,
                ),
                name="matrix_cache_sync_timeline",
            )
