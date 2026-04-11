"""Explicit access layer for Matrix conversation reads and advisory cache writes."""

from __future__ import annotations

import typing
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import nio
from nio.responses import RoomGetEventError

from mindroom.matrix.client import ResolvedVisibleMessage, fetch_thread_history, fetch_thread_snapshot
from mindroom.matrix.event_cache import ConversationEventCache, normalize_event_source_for_cache
from mindroom.matrix.room_cache import cached_room_get_event
from mindroom.matrix.thread_history_result import ThreadHistoryResult, thread_history_result

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.event_info import EventInfo


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError


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
) -> tuple[list[tuple[str, str, dict[str, object]]], list[tuple[str, str]]]:
    """Extract cacheable timeline events and redactions from one sync response."""
    cached_events: list[tuple[str, str, dict[str, object]]] = []
    redacted_events: list[tuple[str, str]] = []
    redacted_event_ids_by_room: dict[str, set[str]] = {}

    for room_id, room_info in response.rooms.join.items():
        for event in room_info.timeline.events:
            if not isinstance(event, nio.Event):
                continue
            if not isinstance(event.source, dict):
                continue
            if not isinstance(event.event_id, str):
                continue
            if isinstance(event, nio.RedactionEvent):
                if not isinstance(event.redacts, str):
                    continue
                redacted_events.append((room_id, event.redacts))
                redacted_event_ids_by_room.setdefault(room_id, set()).add(event.redacts)
                continue

            server_timestamp = event.server_timestamp
            cached_events.append(
                (
                    event.event_id,
                    room_id,
                    normalize_event_source_for_cache(
                        event.source,
                        event_id=event.event_id,
                        sender=event.sender if isinstance(event.sender, str) else None,
                        origin_server_ts=server_timestamp
                        if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
                        else None,
                    ),
                ),
            )

    filtered_cached_events = [
        (event_id, room_id, event_source)
        for event_id, room_id, event_source in cached_events
        if event_id not in redacted_event_ids_by_room.get(room_id, set())
    ]
    return filtered_cached_events, redacted_events


@dataclass
class MatrixConversationAccess(ConversationReadAccess):
    """Own Matrix conversation reads and advisory cache writes for one bot."""

    logger: structlog.stdlib.BoundLogger
    runtime: BotRuntimeView
    _turn_event_cache: ContextVar[dict[tuple[str, str], EventLookupResult] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_event_lookup_cache", default=None),
    )
    _turn_snapshot_cache: ContextVar[dict[tuple[str, str], ThreadReadResult] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_thread_snapshot_cache", default=None),
    )
    _turn_history_cache: ContextVar[dict[tuple[str, str], ThreadReadResult] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_thread_history_cache", default=None),
    )

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
        """Memoize conversation reads for the lifetime of one inbound turn."""
        event_cache = self._turn_event_cache.get()
        if event_cache is not None:
            yield
            return

        event_token = self._turn_event_cache.set({})
        snapshot_token = self._turn_snapshot_cache.set({})
        history_token = self._turn_history_cache.set({})
        try:
            yield
        finally:
            self._turn_history_cache.reset(history_token)
            self._turn_snapshot_cache.reset(snapshot_token)
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

    def _snapshot_result(self, history: Sequence[ResolvedVisibleMessage]) -> ThreadReadResult:
        """Normalize snapshot reads into the shared thread-history result type."""
        if isinstance(history, ThreadHistoryResult):
            return history
        return thread_history_result(list(history), is_full_history=False)

    def _full_history_result(self, history: Sequence[ResolvedVisibleMessage]) -> ThreadReadResult:
        """Normalize full-history reads into the shared thread-history result type."""
        if isinstance(history, ThreadHistoryResult) and history.is_full_history:
            return history
        return thread_history_result(list(history), is_full_history=True)

    async def _read_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        require_full_history: bool,
    ) -> ThreadReadResult:
        """Resolve one thread through a single cache-promotion policy."""
        cache_key = (room_id, thread_id)
        history_cache = self._turn_history_cache.get()
        if history_cache is not None and cache_key in history_cache:
            return history_cache[cache_key]

        snapshot_cache = self._turn_snapshot_cache.get()
        cached_snapshot = snapshot_cache.get(cache_key) if snapshot_cache is not None else None
        if cached_snapshot is not None and (not require_full_history or cached_snapshot.is_full_history):
            return cached_snapshot

        if require_full_history:
            history = self._full_history_result(
                await fetch_thread_history(
                    self._require_client(),
                    room_id,
                    thread_id,
                    event_cache=self.runtime.event_cache,
                ),
            )
            if history_cache is not None:
                history_cache[cache_key] = history
            if snapshot_cache is not None:
                snapshot_cache[cache_key] = history
            return history

        snapshot = self._snapshot_result(
            await fetch_thread_snapshot(
                self._require_client(),
                room_id,
                thread_id,
                event_cache=self.runtime.event_cache,
            ),
        )
        if snapshot_cache is not None:
            snapshot_cache[cache_key] = snapshot
        if snapshot.is_full_history and history_cache is not None:
            history_cache[cache_key] = snapshot
        return snapshot

    async def get_thread_snapshot(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve thread snapshot using one explicit access policy."""
        return await self._read_thread(room_id, thread_id, require_full_history=False)

    async def get_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve full thread history using one explicit access policy."""
        return await self._read_thread(room_id, thread_id, require_full_history=True)

    def invalidate_turn_thread_history(self, room_id: str, thread_id: str) -> None:
        """Drop one memoized full-history entry inside the active turn scope."""
        history_cache = self._turn_history_cache.get()
        if history_cache is not None:
            history_cache.pop((room_id, thread_id), None)
        snapshot_cache = self._turn_snapshot_cache.get()
        if snapshot_cache is not None:
            snapshot_cache.pop((room_id, thread_id), None)

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

        thread_id = event_info.thread_id
        if thread_id is None and event_info.is_edit and event_info.original_event_id is not None:
            thread_id = event_info.thread_id_from_edit
            if thread_id is None:
                try:
                    thread_id = await event_cache.get_thread_id_for_event(
                        room_id,
                        event_info.original_event_id,
                    )
                except Exception as exc:
                    self.logger.warning(
                        "Failed to resolve cached thread for live edit event",
                        room_id=room_id,
                        event_id=event.event_id,
                        original_event_id=event_info.original_event_id,
                        error=str(exc),
                    )
                    return
        if thread_id is None:
            return

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

        try:
            await self._queue_room_cache_update(
                room_id,
                lambda: event_cache.redact_event(room_id, event.redacts, thread_id=thread_id),
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

    async def _persist_room_sync_timeline_updates(
        self,
        event_cache: ConversationEventCache,
        room_id: str,
        cached_events: list[tuple[str, str, dict[str, object]]],
        redacted_event_ids: list[str],
    ) -> None:
        """Persist one room's prepared sync timeline updates."""
        try:
            if cached_events:
                await event_cache.store_events_batch(cached_events)
            for redacted_event_id in redacted_event_ids:
                await event_cache.redact_event(room_id, redacted_event_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to cache sync timeline events",
                room_id=room_id,
                error=str(exc),
                events=len(cached_events),
                redactions=len(redacted_event_ids),
            )

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Schedule sync timeline persistence so sync callbacks do not wait on SQLite."""
        event_cache = self.runtime.event_cache
        if event_cache is None:
            return

        filtered_cached_events, redacted_events = _collect_sync_timeline_cache_updates(response)
        if not filtered_cached_events and not redacted_events:
            return

        updates_by_room: dict[str, tuple[list[tuple[str, str, dict[str, object]]], list[str]]] = {}
        for event_id, room_id, event_source in filtered_cached_events:
            room_events, _room_redactions = updates_by_room.setdefault(room_id, ([], []))
            room_events.append((event_id, room_id, event_source))
        for room_id, redacted_event_id in redacted_events:
            _room_events, room_redactions = updates_by_room.setdefault(room_id, ([], []))
            room_redactions.append(redacted_event_id)

        for room_id, (room_events, room_redactions) in updates_by_room.items():
            self._queue_room_cache_update(
                room_id,
                lambda room_events=room_events,
                room_id=room_id,
                room_redactions=room_redactions: self._persist_room_sync_timeline_updates(
                    event_cache,
                    room_id,
                    room_events,
                    room_redactions,
                ),
                name="matrix_cache_sync_timeline",
            )
