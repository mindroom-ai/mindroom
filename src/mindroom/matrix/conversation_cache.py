"""Facade for Matrix conversation reads and advisory cache writes."""

from __future__ import annotations

import time
import typing
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import nio
from nio.responses import RoomGetEventError

from mindroom.logging_config import get_logger
from mindroom.matrix._event_cache import (
    ConversationEventCache,
    normalize_event_source_for_cache,
)
from mindroom.matrix._event_cache import (
    _EventCache as EventCache,
)
from mindroom.matrix._event_cache_write_coordinator import (
    _EventCacheWriteCoordinator as EventCacheWriteCoordinator,
)
from mindroom.matrix.client import (
    ResolvedVisibleMessage,
    fetch_thread_history,
    fetch_thread_snapshot,
    resolve_thread_history_delta,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import extract_edit_body
from mindroom.matrix.thread_cache import ResolvedThreadCache, ResolvedThreadCacheEntry, resolved_thread_cache_entry
from mindroom.matrix.thread_history_result import (
    THREAD_HISTORY_SOURCE_HOMESERVER,
    ThreadHistoryResult,
    thread_history_cache_refilled,
    thread_history_is_authoritative_refill,
    thread_history_read_source,
    thread_history_result,
)

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.reply_chain import ReplyChainCaches


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError

_SYNC_FRESHNESS_WINDOW_SECONDS = 30.0
_INCREMENTAL_RESOLVED_CACHE_EVENT_LIMIT = 1
logger = get_logger(__name__)

__all__ = [
    "ConversationCacheProtocol",
    "ConversationEventCache",
    "EventCache",
    "EventCacheWriteCoordinator",
    "EventLookupResult",
    "MatrixConversationCache",
    "ThreadReadResult",
    "ThreadRepairRequiredError",
]


class ConversationCacheProtocol(Protocol):
    """Conversation-data reads available to resolver and reply-chain code."""

    async def get_event(self, room_id: str, event_id: str) -> EventLookupResult:
        """Resolve one Matrix event by ID."""

    async def get_thread_snapshot(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve lightweight thread context for dispatch."""

    async def get_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve full thread history for one conversation root."""

    async def get_latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
    ) -> str | None:
        """Resolve the latest visible thread event when MSC3440 fallback needs it."""

    async def is_thread_history_current(
        self,
        room_id: str,
        thread_id: str,
        history: ThreadReadResult,
    ) -> bool:
        """Return whether one previously fetched history is still current for this thread."""


class ThreadRepairRequiredError(RuntimeError):
    """Raised when a repair-required thread cannot be authoritatively refilled."""


async def _apply_cached_latest_edit(
    event_source: dict[str, Any],
    *,
    room_id: str,
    client: nio.AsyncClient,
    event_cache: ConversationEventCache,
) -> dict[str, Any]:
    """Project one cached original event into its latest visible edited state."""
    if event_source.get("type") != "m.room.message":
        return event_source

    event_info = EventInfo.from_event(event_source)
    event_id = event_source.get("event_id")
    if event_info.is_edit or not isinstance(event_id, str) or not event_id:
        return event_source

    latest_edit_source = await event_cache.get_latest_edit(room_id, event_id)
    if latest_edit_source is None:
        return event_source

    edited_body, edited_content = await extract_edit_body(latest_edit_source, client)
    if edited_body is None or edited_content is None:
        return event_source

    original_content = event_source.get("content", {})
    merged_content = (
        {key: value for key, value in original_content.items() if isinstance(key, str)}
        if isinstance(original_content, dict)
        else {}
    )
    merged_content.update(edited_content)
    merged_content.setdefault("body", edited_body)

    updated_event_source = {key: value for key, value in event_source.items() if isinstance(key, str)}
    updated_event_source["content"] = merged_content

    latest_edit_timestamp = latest_edit_source.get("origin_server_ts")
    if isinstance(latest_edit_timestamp, int) and not isinstance(latest_edit_timestamp, bool):
        updated_event_source["origin_server_ts"] = latest_edit_timestamp
    return updated_event_source


async def _cached_room_get_event_response(
    client: nio.AsyncClient,
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    event_source: dict[str, Any],
) -> nio.RoomGetEventResponse | None:
    """Reconstruct one cached room-get-event response, applying visible edits when present."""
    visible_event_source = await _apply_cached_latest_edit(
        event_source,
        room_id=room_id,
        client=client,
        event_cache=event_cache,
    )
    cached_response = nio.RoomGetEventResponse.from_dict(visible_event_source)
    return cached_response if isinstance(cached_response, nio.RoomGetEventResponse) else None


def _cached_rooms(client: nio.AsyncClient) -> dict[str, nio.MatrixRoom]:
    """Return the client room cache when nio has initialized it."""
    rooms = client.rooms
    return rooms if isinstance(rooms, dict) else {}


def _cached_room(client: nio.AsyncClient, room_id: str) -> nio.MatrixRoom | None:
    """Return one cached room when it is available."""
    return _cached_rooms(client).get(room_id)


async def _cached_room_get_event(
    client: nio.AsyncClient,
    event_cache: ConversationEventCache,
    room_id: str,
    event_id: str,
) -> nio.RoomGetEventResponse | RoomGetEventError:
    """Return one event through the persistent cache when available."""
    normalized_event_id = event_id.strip()
    if normalized_event_id:
        try:
            cached_event = await event_cache.get_event(room_id, normalized_event_id)
        except Exception as exc:
            logger.warning(
                "Failed to read cached Matrix event",
                room_id=room_id,
                event_id=normalized_event_id,
                error=str(exc),
            )
        else:
            if cached_event is not None:
                cached_response = await _cached_room_get_event_response(
                    client,
                    event_cache,
                    room_id=room_id,
                    event_source=cached_event,
                )
                if cached_response is not None:
                    return cached_response
                logger.warning(
                    "Cached Matrix event could not be reconstructed",
                    room_id=room_id,
                    event_id=normalized_event_id,
                    error=str(cached_response),
                )

    response = await client.room_get_event(room_id, normalized_event_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        return response

    event = response.event
    event_source = event.source if isinstance(event.source, dict) else {}
    server_timestamp = event.server_timestamp
    normalized_event_source = normalize_event_source_for_cache(
        event_source,
        event_id=event.event_id if isinstance(event.event_id, str) else normalized_event_id,
        sender=event.sender if isinstance(event.sender, str) else None,
        origin_server_ts=server_timestamp
        if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool)
        else None,
    )
    try:
        await event_cache.store_event(
            normalized_event_id,
            room_id,
            normalized_event_source,
        )
    except Exception as exc:
        logger.warning(
            "Failed to cache Matrix event lookup",
            room_id=room_id,
            event_id=normalized_event_id,
            error=str(exc),
        )
    reconstructed_response = await _cached_room_get_event_response(
        client,
        event_cache,
        room_id=room_id,
        event_source=normalized_event_source,
    )
    if reconstructed_response is not None:
        return reconstructed_response
    return response


def _collect_sync_timeline_cache_updates(
    response: nio.SyncResponse,
) -> tuple[
    list[tuple[str, str, dict[str, object]]],
    list[tuple[str, str]],
    list[tuple[str, dict[str, object]]],
    dict[tuple[str, str], dict[str, object]],
]:
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
    redacted_event_sources = {
        (room_id, event_id): event_source
        for event_id, room_id, event_source in cached_events
        if event_id in redacted_event_ids_by_room.get(room_id, set())
    }
    return filtered_cached_events, redacted_events, filtered_threaded_events, redacted_event_sources


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
class MatrixConversationCache(ConversationCacheProtocol):
    """Own Matrix conversation reads and advisory cache writes for one bot."""

    logger: structlog.stdlib.BoundLogger
    runtime: BotRuntimeView
    _turn_event_cache: ContextVar[dict[tuple[str, str], EventLookupResult] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_event_lookup_cache", default=None),
    )
    _resolved_thread_cache: ResolvedThreadCache = field(default_factory=ResolvedThreadCache, init=False)
    _reply_chain_caches_getter: typing.Callable[[], ReplyChainCaches | None] | None = field(
        default=None,
        init=False,
        repr=False,
    )

    def _require_client(self) -> nio.AsyncClient:
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation cache"
            raise RuntimeError(msg)
        return client

    def bind_reply_chain_caches(self, getter: typing.Callable[[], ReplyChainCaches | None]) -> None:
        """Provide access to the resolver-owned reply-chain caches."""
        self._reply_chain_caches_getter = getter

    def _reply_chain_caches(self) -> ReplyChainCaches | None:
        if self._reply_chain_caches_getter is None:
            return None
        return self._reply_chain_caches_getter()

    def _invalidate_reply_chain(self, room_id: str, *event_ids: str | None) -> None:
        reply_chain_caches = self._reply_chain_caches()
        if reply_chain_caches is None:
            return
        reply_chain_caches.invalidate(room_id, event_ids)

    async def _reply_chain_invalidation_ids_for_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        event_cache: ConversationEventCache,
    ) -> set[str]:
        invalidation_ids = {redacted_event_id}
        try:
            cached_event = await event_cache.get_event(room_id, redacted_event_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to inspect cached redacted event for reply-chain invalidation",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
            return invalidation_ids
        if not isinstance(cached_event, dict):
            return invalidation_ids
        original_event_id = EventInfo.from_event(cached_event).original_event_id
        if isinstance(original_event_id, str):
            invalidation_ids.add(original_event_id)
        return invalidation_ids

    def _reply_chain_invalidation_ids_for_sync_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        event_source: dict[str, object] | None,
    ) -> set[str]:
        invalidation_ids = {redacted_event_id}
        candidate_event_source = event_source
        if candidate_event_source is None:
            reply_chain_caches = self._reply_chain_caches()
            if reply_chain_caches is not None:
                cached_node = reply_chain_caches.nodes.get(room_id, redacted_event_id)
                if cached_node is not None and isinstance(cached_node.event_source, dict):
                    candidate_event_source = cached_node.event_source
        if not isinstance(candidate_event_source, dict):
            return invalidation_ids
        original_event_id = EventInfo.from_event(candidate_event_source).original_event_id
        if isinstance(original_event_id, str):
            invalidation_ids.add(original_event_id)
        return invalidation_ids

    def _queue_room_cache_update(
        self,
        room_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
    ) -> asyncio.Task[object]:
        coordinator = self.runtime.event_cache_write_coordinator
        return coordinator.queue_room_update(room_id, update_coro_factory, name=name)

    @asynccontextmanager
    async def turn_scope(self) -> AsyncIterator[None]:
        """Memoize event lookups for the lifetime of one inbound turn."""
        turn_lookup_cache = self._turn_event_cache.get()
        if turn_lookup_cache is not None:
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

        response = await _cached_room_get_event(
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

    async def _thread_requires_refresh(self, room_id: str, thread_id: str) -> bool:
        return await self.runtime.event_cache.thread_repair_required(room_id, thread_id)

    async def _clear_thread_refresh_required(self, room_id: str, thread_id: str) -> None:
        await self.runtime.event_cache.clear_thread_repair_required(room_id, thread_id)

    def reset_runtime_state(self) -> None:
        """Drop in-memory conversation state tied to one runtime lifetime."""
        self._resolved_thread_cache.clear()

    async def _mark_lookup_repair_pending(
        self,
        room_id: str,
        event_id: str | None,
        *,
        reason: str,
        queue_write: bool = True,
    ) -> None:
        if not isinstance(event_id, str) or not event_id:
            return

        async def persist_lookup_repair() -> None:
            await self.runtime.event_cache.mark_pending_lookup_repair(room_id, event_id)
            self.logger.debug(
                "Marked Matrix thread lookup repair pending",
                room_id=room_id,
                event_id=event_id,
                reason=reason,
            )

        if queue_write:
            await self._queue_room_cache_update(
                room_id,
                persist_lookup_repair,
                name="matrix_cache_mark_lookup_repair_pending",
            )
            return
        await persist_lookup_repair()

    async def _promote_lookup_repairs_locked(
        self,
        room_id: str,
        thread_id: str,
        *,
        promoted_event_ids: frozenset[str] | None = None,
        reason: str = "lookup_repair_required",
    ) -> frozenset[str]:
        if promoted_event_ids is None:
            promoted_event_ids = await self.runtime.event_cache.matching_pending_lookup_repairs(
                room_id,
                thread_id,
            )
        if not promoted_event_ids:
            return frozenset()
        await self.runtime.event_cache.consume_pending_lookup_repairs(room_id, promoted_event_ids)
        thread_version = self._bump_thread_version(room_id, thread_id)
        await self.runtime.event_cache.mark_thread_repair_required(room_id, thread_id)
        self._resolved_thread_cache.invalidate(room_id, thread_id)
        self._log_resolved_thread_cache(
            "resolved_thread_cache_invalidate",
            room_id=room_id,
            thread_id=thread_id,
            reason=reason,
            thread_version=thread_version,
        )
        return promoted_event_ids

    async def _adopt_room_lookup_repairs_locked(
        self,
        room_id: str,
        thread_id: str,
    ) -> frozenset[str]:
        return await self._promote_lookup_repairs_locked(room_id, thread_id)

    async def _adopt_history_lookup_repairs_locked(
        self,
        room_id: str,
        thread_id: str,
        history: ThreadReadResult,
    ) -> frozenset[str]:
        history_event_ids = frozenset(
            event_id for message in history if isinstance((event_id := message.event_id), str) and event_id
        )
        if not history_event_ids:
            return frozenset()
        promoted_event_ids = await self.runtime.event_cache.pending_lookup_repairs_for_event_ids(
            room_id,
            history_event_ids,
        )
        return await self._promote_lookup_repairs_locked(
            room_id,
            thread_id,
            promoted_event_ids=promoted_event_ids,
            reason="lookup_repair_required_from_request_history",
        )

    async def _record_thread_change(
        self,
        room_id: str,
        thread_id: str,
        *,
        invalidate_resolved: bool,
    ) -> int:
        async with self._resolved_thread_cache.entry_lock(room_id, thread_id):
            thread_version = self._bump_thread_version(room_id, thread_id)
            if invalidate_resolved:
                self._resolved_thread_cache.invalidate(room_id, thread_id)
            return thread_version

    async def _mark_thread_refresh_required(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
        bump_version: bool = True,
    ) -> int:
        async with self._resolved_thread_cache.entry_lock(room_id, thread_id):
            thread_version = self.thread_version(room_id, thread_id)
            if bump_version:
                thread_version = self._bump_thread_version(room_id, thread_id)
            self._resolved_thread_cache.invalidate(room_id, thread_id)
            await self.runtime.event_cache.mark_thread_repair_required(room_id, thread_id)
            self._log_resolved_thread_cache(
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason=reason,
                thread_version=thread_version,
            )
            return thread_version

    async def _finalize_thread_cache_mutation(
        self,
        room_id: str,
        thread_id: str | None,
        *,
        persisted: bool,
        invalidate_resolved: bool,
        failure_reason: str,
    ) -> None:
        """Apply the shared version and repair policy for one thread-affecting cache mutation."""
        if thread_id is None:
            return
        if persisted:
            await self._record_thread_change(
                room_id,
                thread_id,
                invalidate_resolved=invalidate_resolved,
            )
            return
        await self._mark_thread_refresh_required(
            room_id,
            thread_id,
            reason=failure_reason,
        )

    def _seconds_since_last_sync_activity(self) -> float | None:
        last_sync_activity_monotonic = self.runtime.last_sync_activity_monotonic
        if last_sync_activity_monotonic is None:
            return None
        return max(time.monotonic() - last_sync_activity_monotonic, 0.0)

    async def _should_refresh_cached_thread_history(self, room_id: str, thread_id: str) -> bool:
        if await self._thread_requires_refresh(room_id, thread_id):
            self.logger.debug(
                "Forcing Matrix thread refresh because local cache repair is required",
                room_id=room_id,
                thread_id=thread_id,
            )
            return True
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
        await self.runtime.event_cache_write_coordinator.wait_for_room_idle(room_id)

    def _history_event_ids(self, history: Sequence[ResolvedVisibleMessage]) -> frozenset[str]:
        return frozenset(message.event_id for message in history if message.event_id)

    def _latest_visible_thread_event_id(
        self,
        history: Sequence[ResolvedVisibleMessage],
    ) -> str | None:
        if not history:
            return None
        return history[-1].visible_event_id or history[-1].event_id or None

    async def _cached_thread_event_sources(
        self,
        room_id: str,
        thread_id: str,
    ) -> Sequence[dict[str, object]] | None:
        return await self.runtime.event_cache.get_thread_events(room_id, thread_id)

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
    ) -> frozenset[str]:
        source_event_ids = await self._cached_thread_source_event_ids(room_id, thread_id)
        self._resolved_thread_cache.store(
            room_id,
            thread_id,
            resolved_thread_cache_entry(
                history=history,
                source_event_ids=source_event_ids,
                thread_version=thread_version,
            ),
        )
        self._log_resolved_thread_cache(
            "resolved_thread_cache_store",
            room_id=room_id,
            thread_id=thread_id,
            thread_version=thread_version,
        )
        return source_event_ids

    def _repair_history_is_authoritative(self, history: ThreadHistoryResult) -> bool:
        """Return whether one repair read came from an authoritative homeserver refill."""
        return thread_history_read_source(
            history,
        ) == THREAD_HISTORY_SOURCE_HOMESERVER and thread_history_is_authoritative_refill(history)

    def _repair_history_durably_refilled(self, history: ThreadHistoryResult) -> bool:
        """Return whether one repair read also repopulated the raw cache durably."""
        return self._repair_history_is_authoritative(history) and thread_history_cache_refilled(history)

    async def _invalidate_raw_thread_before_repair(self, room_id: str, thread_id: str) -> None:
        """Best-effort raw-cache invalidation before a forced repair read."""
        try:
            await self.runtime.event_cache.invalidate_thread(room_id, thread_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to invalidate stale raw thread cache before repair",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
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
        if await self._thread_requires_refresh(room_id, thread_id):
            self._log_resolved_thread_cache(
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason="repair_required",
                thread_version=current_thread_version,
            )
            return None
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

    async def _maybe_use_resolved_thread_cache(
        self,
        room_id: str,
        thread_id: str,
        *,
        current_thread_version: int,
        repair_required: bool,
    ) -> ThreadReadResult | None:
        """Return a reusable resolved-thread entry when it is still valid."""
        lookup_started = time.perf_counter()
        cache_lookup = self._resolved_thread_cache.lookup(room_id, thread_id)
        cache_read_ms = round((time.perf_counter() - lookup_started) * 1000, 1)
        entry = cache_lookup.entry

        if cache_lookup.expired:
            self._log_resolved_thread_cache(
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason="ttl_expired",
                thread_version=current_thread_version,
            )
            entry = None

        if entry is None:
            self._log_resolved_thread_cache(
                "resolved_thread_cache_miss",
                room_id=room_id,
                thread_id=thread_id,
                thread_version=current_thread_version,
            )
            if repair_required:
                self._log_resolved_thread_cache(
                    "resolved_thread_cache_invalidate",
                    room_id=room_id,
                    thread_id=thread_id,
                    reason="repair_required",
                    thread_version=current_thread_version,
                )
        elif entry.thread_version == current_thread_version and not repair_required:
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
        elif repair_required:
            self._log_resolved_thread_cache(
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason="repair_required",
                thread_version=current_thread_version,
            )
            self._resolved_thread_cache.invalidate(room_id, thread_id)
        return None

    async def _fetch_full_thread_history_from_source(
        self,
        room_id: str,
        thread_id: str,
        *,
        current_thread_version: int,
        repair_required: bool,
    ) -> ThreadReadResult:
        """Fetch, validate, and store one full thread history under the entry lock."""
        if repair_required:
            await self._invalidate_raw_thread_before_repair(room_id, thread_id)
        history = self._full_history_result(
            await fetch_thread_history(
                self._require_client(),
                room_id,
                thread_id,
                event_cache=self.runtime.event_cache,
                refresh_cache=await self._should_refresh_cached_thread_history(room_id, thread_id),
            ),
            thread_version=current_thread_version,
        )
        if repair_required and not self._repair_history_is_authoritative(history):
            msg = "Repair-required Matrix thread history could not be authoritatively refilled"
            raise ThreadRepairRequiredError(msg)
        await self._store_resolved_thread_cache_entry(
            room_id,
            thread_id,
            history=history,
            thread_version=current_thread_version,
        )
        if repair_required and self._repair_history_durably_refilled(history):
            await self._clear_thread_refresh_required(room_id, thread_id)
        return history

    async def _read_full_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        await self._wait_for_pending_room_cache_updates(room_id)
        async with self._resolved_thread_cache.entry_lock(room_id, thread_id):
            await self._adopt_room_lookup_repairs_locked(room_id, thread_id)
            current_thread_version = self.thread_version(room_id, thread_id)
            repair_required = await self._thread_requires_refresh(room_id, thread_id)
            cached_history = await self._maybe_use_resolved_thread_cache(
                room_id,
                thread_id,
                current_thread_version=current_thread_version,
                repair_required=repair_required,
            )
            if cached_history is not None:
                return cached_history
            return await self._fetch_full_thread_history_from_source(
                room_id,
                thread_id,
                current_thread_version=current_thread_version,
                repair_required=repair_required,
            )

    async def _read_snapshot_thread(self, room_id: str, thread_id: str) -> ThreadReadResult:
        await self._wait_for_pending_room_cache_updates(room_id)
        async with self._resolved_thread_cache.entry_lock(room_id, thread_id):
            await self._adopt_room_lookup_repairs_locked(room_id, thread_id)
            current_thread_version = self.thread_version(room_id, thread_id)
            repair_required = await self._thread_requires_refresh(room_id, thread_id)
            if repair_required:
                return await self._fetch_full_thread_history_from_source(
                    room_id,
                    thread_id,
                    current_thread_version=current_thread_version,
                    repair_required=repair_required,
                )
            return self._snapshot_result(
                await fetch_thread_snapshot(
                    self._require_client(),
                    room_id,
                    thread_id,
                    event_cache=self.runtime.event_cache,
                ),
                thread_version=current_thread_version,
            )

    def _snapshot_result(
        self,
        history: Sequence[ResolvedVisibleMessage],
        *,
        thread_version: int,
    ) -> ThreadReadResult:
        """Normalize snapshot reads into the shared thread-history result type."""
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=history.is_full_history,
                thread_version=thread_version,
                diagnostics=history.diagnostics,
            )
        return thread_history_result(
            list(history),
            is_full_history=False,
            thread_version=thread_version,
        )

    def _full_history_result(
        self,
        history: Sequence[ResolvedVisibleMessage],
        *,
        thread_version: int,
    ) -> ThreadReadResult:
        """Normalize full-history reads into the shared thread-history result type."""
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=True,
                thread_version=thread_version,
                diagnostics=history.diagnostics,
            )
        return thread_history_result(
            list(history),
            is_full_history=True,
            thread_version=thread_version,
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
        return await self._read_snapshot_thread(room_id, thread_id)

    async def is_thread_history_current(
        self,
        room_id: str,
        thread_id: str,
        history: ThreadReadResult,
    ) -> bool:
        """Return whether one previously fetched history is still current for this thread."""
        if history.thread_version is None:
            return False
        await self._wait_for_pending_room_cache_updates(room_id)
        async with self._resolved_thread_cache.entry_lock(room_id, thread_id):
            await self._adopt_room_lookup_repairs_locked(room_id, thread_id)
            await self._adopt_history_lookup_repairs_locked(room_id, thread_id, history)
            if await self._thread_requires_refresh(room_id, thread_id):
                return False
            return history.thread_version == self.thread_version(room_id, thread_id)

    async def get_thread_snapshot(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve thread snapshot using one explicit access policy."""
        return await self._read_thread(room_id, thread_id, require_full_history=False)

    async def get_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve full thread history using one explicit access policy."""
        return await self._read_thread(room_id, thread_id, require_full_history=True)

    async def get_latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
    ) -> str | None:
        """Resolve the latest visible thread event when MSC3440 fallback needs it."""
        if thread_id is None or existing_event_id is not None or reply_to_event_id is not None:
            return None
        try:
            thread_snapshot = await self.get_thread_snapshot(room_id, thread_id)
        except Exception:
            return thread_id
        return self._latest_visible_thread_event_id(thread_snapshot) or thread_id

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessageText,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live thread event into the advisory cache when the thread is known."""
        if event_info.is_edit:
            self._invalidate_reply_chain(
                room_id,
                event.event_id,
                event_info.original_event_id,
            )
        event_cache = self.runtime.event_cache

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
            await self._mark_lookup_repair_pending(
                room_id,
                event_info.original_event_id,
                reason="live_append_lookup_failed",
            )
            return
        if thread_id is None:
            await self._mark_lookup_repair_pending(
                room_id,
                event_info.original_event_id,
                reason="live_append_lookup_missing",
            )
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

        async def append_and_finalize() -> bool:
            try:
                appended = await event_cache.append_event(room_id, thread_id, event_source)
            except Exception as exc:
                self.logger.warning(
                    "Failed to append live thread event to cache",
                    room_id=room_id,
                    thread_id=thread_id,
                    event_id=event.event_id,
                    error=str(exc),
                )
                appended = False
            await self._finalize_thread_cache_mutation(
                room_id,
                thread_id,
                persisted=bool(appended),
                invalidate_resolved=event_info.is_edit,
                failure_reason="live_append_failed",
            )
            return bool(appended)

        await self._queue_room_cache_update(
            room_id,
            append_and_finalize,
            name="matrix_cache_append_live_event",
        )

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        event_cache = self.runtime.event_cache
        invalidation_ids = await self._reply_chain_invalidation_ids_for_redaction(
            room_id,
            event.redacts,
            event_cache=event_cache,
        )
        self._invalidate_reply_chain(room_id, *invalidation_ids)

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
            await self._mark_lookup_repair_pending(
                room_id,
                event.redacts,
                reason="live_redaction_lookup_failed",
            )
        else:
            if thread_id is None:
                await self._mark_lookup_repair_pending(
                    room_id,
                    event.redacts,
                    reason="live_redaction_lookup_missing",
                )

        async def redact_and_finalize() -> bool:
            try:
                redacted = await event_cache.redact_event(room_id, event.redacts)
            except Exception as exc:
                self.logger.warning(
                    "Failed to apply live redaction to cache",
                    room_id=room_id,
                    thread_id=thread_id,
                    redacted_event_id=event.redacts,
                    error=str(exc),
                )
                redacted = False
            await self._finalize_thread_cache_mutation(
                room_id,
                thread_id,
                persisted=bool(redacted),
                invalidate_resolved=True,
                failure_reason="live_redaction_failed",
            )
            return bool(redacted)

        await self._queue_room_cache_update(
            room_id,
            redact_and_finalize,
            name="matrix_cache_apply_redaction",
        )

    async def _resolve_sync_thread_id(
        self,
        event_cache: ConversationEventCache,
        *,
        room_id: str,
        event_source: dict[str, object],
    ) -> str | None:
        """Resolve the thread root for one sync event."""
        event_info = EventInfo.from_event(event_source)
        event_id = event_source.get("event_id")
        try:
            return await _resolve_thread_id_for_cached_event_append(
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
            await self._mark_lookup_repair_pending(
                room_id,
                event_info.original_event_id,
                reason="sync_thread_lookup_failed",
                queue_write=False,
            )
            return None

    async def _append_sync_thread_event(
        self,
        event_cache: ConversationEventCache,
        *,
        room_id: str,
        event_source: dict[str, object],
    ) -> tuple[str | None, bool]:
        """Append one sync event to cached thread history when its root can be resolved."""
        thread_id = await self._resolve_sync_thread_id(
            event_cache,
            room_id=room_id,
            event_source=event_source,
        )

        if thread_id is None:
            event_info = EventInfo.from_event(event_source)
            await self._mark_lookup_repair_pending(
                room_id,
                event_info.original_event_id,
                reason="sync_thread_lookup_missing",
                queue_write=False,
            )
            return None, False

        event_id = event_source.get("event_id")
        try:
            appended = await event_cache.append_thread_event(room_id, thread_id, event_source)
        except Exception as exc:
            self.logger.warning(
                "Failed to append sync thread event to cache",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                error=str(exc),
            )
            return thread_id, False
        if not appended:
            self.logger.warning(
                "Failed to append sync thread event because raw thread cache is missing",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
            )
        return thread_id, appended

    async def _persist_threaded_sync_events(
        self,
        event_cache: ConversationEventCache,
        room_id: str,
        threaded_events: Sequence[dict[str, object]],
    ) -> None:
        """Persist sync thread appends and apply the shared mutation contract."""
        for event_source in threaded_events:
            thread_id, appended = await self._append_sync_thread_event(
                event_cache,
                room_id=room_id,
                event_source=event_source,
            )
            if thread_id is None:
                continue
            event_info = EventInfo.from_event(event_source)
            await self._finalize_thread_cache_mutation(
                room_id,
                thread_id,
                persisted=appended,
                invalidate_resolved=event_info.is_edit,
                failure_reason="sync_thread_append_failed",
            )

    async def _mark_failed_sync_thread_store(
        self,
        event_cache: ConversationEventCache,
        room_id: str,
        threaded_events: Sequence[dict[str, object]],
    ) -> None:
        """Mark affected threads repair-required when sync point-lookups could not be stored."""
        for event_source in threaded_events:
            thread_id = await self._resolve_sync_thread_id(
                event_cache,
                room_id=room_id,
                event_source=event_source,
            )
            if thread_id is None:
                continue
            await self._mark_thread_refresh_required(
                room_id,
                thread_id,
                reason="sync_store_failed",
            )

    async def _apply_sync_redactions(
        self,
        event_cache: ConversationEventCache,
        room_id: str,
        redacted_event_ids: Sequence[str],
    ) -> None:
        """Apply sync redactions through the shared mutation contract."""
        for redacted_event_id in redacted_event_ids:
            reply_chain_invalidation_ids = await self._reply_chain_invalidation_ids_for_redaction(
                room_id,
                redacted_event_id,
                event_cache=event_cache,
            )
            self._invalidate_reply_chain(room_id, *reply_chain_invalidation_ids)
            thread_id: str | None = None
            try:
                thread_id = await event_cache.get_thread_id_for_event(
                    room_id,
                    redacted_event_id,
                )
                redacted = await event_cache.redact_event(room_id, redacted_event_id)
            except Exception as exc:
                self.logger.warning(
                    "Failed to apply sync redaction to cache",
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    error=str(exc),
                )
                await self._mark_lookup_repair_pending(
                    room_id,
                    redacted_event_id,
                    reason="sync_redaction_lookup_failed",
                    queue_write=False,
                )
                redacted = False
            else:
                if thread_id is None:
                    await self._mark_lookup_repair_pending(
                        room_id,
                        redacted_event_id,
                        reason="sync_redaction_lookup_missing",
                        queue_write=False,
                    )
            await self._finalize_thread_cache_mutation(
                room_id,
                thread_id,
                persisted=bool(redacted),
                invalidate_resolved=True,
                failure_reason="sync_redaction_failed",
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
            await self._persist_threaded_sync_events(
                event_cache,
                room_id,
                threaded_events,
            )
        else:
            await self._mark_failed_sync_thread_store(
                event_cache,
                room_id,
                threaded_events,
            )
        await self._apply_sync_redactions(
            event_cache,
            room_id,
            redacted_event_ids,
        )

    def _track_sync_cached_event(
        self,
        room_id: str,
        event_source: dict[str, object],
    ) -> None:
        event_info = EventInfo.from_event(event_source)
        if event_info.is_edit:
            self._invalidate_reply_chain(
                room_id,
                self._event_id_from_event_source(event_source),
                event_info.original_event_id,
            )

    def _group_sync_timeline_updates(
        self,
        filtered_cached_events: list[tuple[str, str, dict[str, object]]],
        redacted_events: list[tuple[str, str]],
        threaded_events: list[tuple[str, dict[str, object]]],
        redacted_event_sources: dict[tuple[str, str], dict[str, object]],
    ) -> dict[str, tuple[list[tuple[str, str, dict[str, object]]], list[str], list[dict[str, object]]]]:
        updates_by_room: dict[
            str,
            tuple[list[tuple[str, str, dict[str, object]]], list[str], list[dict[str, object]]],
        ] = {}
        for event_id, room_id, event_source in filtered_cached_events:
            self._track_sync_cached_event(room_id, event_source)
            room_events, _room_redactions, _room_threaded_events = updates_by_room.setdefault(room_id, ([], [], []))
            room_events.append((event_id, room_id, event_source))
        for room_id, redacted_event_id in redacted_events:
            invalidation_ids = self._reply_chain_invalidation_ids_for_sync_redaction(
                room_id,
                redacted_event_id,
                event_source=redacted_event_sources.get((room_id, redacted_event_id)),
            )
            self._invalidate_reply_chain(room_id, *invalidation_ids)
            _room_events, room_redactions, _room_threaded_events = updates_by_room.setdefault(room_id, ([], [], []))
            room_redactions.append(redacted_event_id)
        for room_id, event_source in threaded_events:
            _room_events, _room_redactions, room_threaded_events = updates_by_room.setdefault(room_id, ([], [], []))
            room_threaded_events.append(event_source)
        return updates_by_room

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Schedule sync timeline persistence so sync callbacks do not wait on SQLite."""
        filtered_cached_events, redacted_events, threaded_events, redacted_event_sources = (
            _collect_sync_timeline_cache_updates(response)
        )
        if not filtered_cached_events and not redacted_events and not threaded_events:
            return
        updates_by_room = self._group_sync_timeline_updates(
            filtered_cached_events,
            redacted_events,
            threaded_events,
            redacted_event_sources,
        )
        event_cache = self.runtime.event_cache

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
