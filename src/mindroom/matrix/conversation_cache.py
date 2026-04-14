"""Facade for Matrix conversation reads and advisory cache writes."""

from __future__ import annotations

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
from mindroom.matrix.thread_cache import ResolvedThreadCache, ResolvedThreadCacheEntry
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_reads import ThreadReadPolicy, ThreadRepairRequiredError
from mindroom.matrix.thread_writes import ThreadWritePolicy

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.reply_chain import ReplyChainCaches


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError

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

    async def record_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Write one locally sent threaded message or edit through to the cache.

        This is advisory post-send bookkeeping and must fail open.
        Callers should be able to treat the Matrix delivery as successful even if cache repair state cannot be updated.
        """

    async def record_outbound_redaction(self, room_id: str, redacted_event_id: str) -> None:
        """Write one locally redacted threaded message through to the cache.

        This is advisory post-redaction bookkeeping and must fail open.
        """


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
) -> tuple[nio.RoomGetEventResponse | RoomGetEventError, dict[str, Any] | None]:
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
                    return cached_response, None
                logger.warning(
                    "Cached Matrix event could not be reconstructed",
                    room_id=room_id,
                    event_id=normalized_event_id,
                    error=str(cached_response),
                )

    response = await client.room_get_event(room_id, normalized_event_id)
    if not isinstance(response, nio.RoomGetEventResponse):
        return response, None

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
    visible_response = await _cached_room_get_event_response(
        client,
        event_cache,
        room_id=room_id,
        event_source=normalized_event_source,
    )
    return (visible_response if visible_response is not None else response), normalized_event_source


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
    _reads: ThreadReadPolicy = field(init=False, repr=False)
    _writes: ThreadWritePolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind extracted read/write policy collaborators to this facade."""
        self._reads = ThreadReadPolicy(self)
        self._writes = ThreadWritePolicy(self)

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

    async def _reply_chain_invalidation_ids_for_sync_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        event_cache: ConversationEventCache,
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
        if candidate_event_source is None:
            return await self._reply_chain_invalidation_ids_for_redaction(
                room_id,
                redacted_event_id,
                event_cache=event_cache,
            )
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

        normalized_event_id = event_id.strip()
        response, fetched_event_source = await _cached_room_get_event(
            self._require_client(),
            self.runtime.event_cache,
            room_id,
            event_id,
        )
        if fetched_event_source is not None:

            async def persist_lookup_event() -> None:
                await self.runtime.event_cache.store_event(normalized_event_id, room_id, fetched_event_source)

            try:
                await self._queue_room_cache_update(
                    room_id,
                    persist_lookup_event,
                    name="matrix_cache_store_room_get_event",
                )
            except Exception as exc:
                self.logger.warning(
                    "Failed to cache Matrix event lookup",
                    room_id=room_id,
                    event_id=event_id,
                    error=str(exc),
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
        reply_chain_caches = self._reply_chain_caches()
        if reply_chain_caches is not None:
            reply_chain_caches.clear()

    async def _fetch_thread_history_from_client(
        self,
        room_id: str,
        thread_id: str,
        *,
        refresh_cache: bool,
    ) -> ThreadHistoryResult:
        return await fetch_thread_history(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
            refresh_cache=refresh_cache,
        )

    async def _fetch_thread_snapshot_from_client(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        return await fetch_thread_snapshot(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
        )

    async def _resolve_thread_history_delta_from_client(
        self,
        *,
        thread_id: str,
        event_sources: Sequence[dict[str, Any]],
    ) -> ThreadHistoryResult:
        return await resolve_thread_history_delta(
            self._require_client(),
            thread_id=thread_id,
            event_sources=event_sources,
        )

    async def _mark_lookup_repair_pending(
        self,
        room_id: str,
        event_id: str | None,
        *,
        reason: str,
    ) -> None:
        await self._writes._mark_lookup_repair_pending(
            room_id,
            event_id,
            reason=reason,
        )

    async def _mark_lookup_repair_pending_locked(
        self,
        room_id: str,
        event_id: str | None,
        *,
        reason: str,
    ) -> None:
        await self._writes._mark_lookup_repair_pending_locked(
            room_id,
            event_id,
            reason=reason,
        )

    async def _promote_lookup_repairs_locked(
        self,
        room_id: str,
        thread_id: str,
        *,
        promoted_event_ids: frozenset[str] | None = None,
        reason: str = "lookup_repair_required",
    ) -> frozenset[str]:
        return await self._writes._promote_lookup_repairs_locked(
            room_id,
            thread_id,
            promoted_event_ids=promoted_event_ids,
            reason=reason,
        )

    async def _adopt_room_lookup_repairs_locked(
        self,
        room_id: str,
        thread_id: str,
    ) -> frozenset[str]:
        return await self._writes._adopt_room_lookup_repairs_locked(room_id, thread_id)

    async def _record_thread_change(
        self,
        room_id: str,
        thread_id: str,
        *,
        invalidate_resolved: bool,
    ) -> int:
        return await self._writes._record_thread_change(
            room_id,
            thread_id,
            invalidate_resolved=invalidate_resolved,
        )

    async def _mark_thread_refresh_required(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
        bump_version: bool = True,
    ) -> int:
        return await self._writes._mark_thread_refresh_required(
            room_id,
            thread_id,
            reason=reason,
            bump_version=bump_version,
        )

    async def _finalize_thread_cache_mutation(
        self,
        room_id: str,
        thread_id: str | None,
        *,
        persisted: bool,
        invalidate_resolved: bool,
        failure_reason: str,
    ) -> None:
        await self._writes._finalize_thread_cache_mutation(
            room_id,
            thread_id,
            persisted=persisted,
            invalidate_resolved=invalidate_resolved,
            failure_reason=failure_reason,
        )

    def _seconds_since_last_sync_activity(self) -> float | None:
        return self._reads._seconds_since_last_sync_activity()

    async def _should_refresh_cached_thread_history(self, room_id: str, thread_id: str) -> bool:
        return await self._reads._should_refresh_cached_thread_history(room_id, thread_id)

    def _event_id_from_event_source(self, event_source: dict[str, object]) -> str | None:
        return self._reads._event_id_from_event_source(event_source)

    def _sort_thread_history_root_first(
        self,
        history: list[ResolvedVisibleMessage],
        *,
        thread_id: str,
    ) -> None:
        self._reads._sort_thread_history_root_first(history, thread_id=thread_id)

    def _resolved_cache_diagnostics(
        self,
        *,
        cache_read_ms: float,
        incremental_refresh_ms: float = 0.0,
        resolution_ms: float = 0.0,
        sidecar_hydration_ms: float = 0.0,
    ) -> dict[str, float]:
        return self._reads._resolved_cache_diagnostics(
            cache_read_ms=cache_read_ms,
            incremental_refresh_ms=incremental_refresh_ms,
            resolution_ms=resolution_ms,
            sidecar_hydration_ms=sidecar_hydration_ms,
        )

    def _log_resolved_thread_cache(
        self,
        event: str,
        *,
        room_id: str,
        thread_id: str,
        reason: str | None = None,
        thread_version: int | None = None,
    ) -> None:
        self._reads._log_resolved_thread_cache(
            event,
            room_id=room_id,
            thread_id=thread_id,
            reason=reason,
            thread_version=thread_version,
        )

    async def _wait_for_pending_room_cache_updates(self, room_id: str) -> None:
        await self._reads._wait_for_pending_room_cache_updates(room_id)

    def _history_event_ids(self, history: Sequence[ResolvedVisibleMessage]) -> frozenset[str]:
        return self._reads._history_event_ids(history)

    def _latest_visible_thread_event_id(
        self,
        history: Sequence[ResolvedVisibleMessage],
    ) -> str | None:
        return self._reads._latest_visible_thread_event_id(history)

    async def _invalidate_resolved_threads_for_event_ids(
        self,
        room_id: str,
        *event_ids: str | None,
        reason: str,
    ) -> None:
        await self._writes._invalidate_resolved_threads_for_event_ids(
            room_id,
            *event_ids,
            reason=reason,
        )

    async def _cached_thread_event_sources(
        self,
        room_id: str,
        thread_id: str,
    ) -> Sequence[dict[str, object]] | None:
        return await self._reads._cached_thread_event_sources(room_id, thread_id)

    async def _cached_thread_source_event_ids(
        self,
        room_id: str,
        thread_id: str,
    ) -> frozenset[str]:
        return await self._reads._cached_thread_source_event_ids(room_id, thread_id)

    async def _store_resolved_thread_cache_entry(
        self,
        room_id: str,
        thread_id: str,
        *,
        history: Sequence[ResolvedVisibleMessage],
        thread_version: int,
    ) -> frozenset[str]:
        return await self._reads._store_resolved_thread_cache_entry(
            room_id,
            thread_id,
            history=history,
            thread_version=thread_version,
        )

    def _should_store_resolved_thread_cache_entry(self, history: ThreadHistoryResult) -> bool:
        return self._reads._should_store_resolved_thread_cache_entry(history)

    def _repair_history_is_authoritative(self, history: ThreadHistoryResult) -> bool:
        return self._reads._repair_history_is_authoritative(history)

    def _repair_history_durably_refilled(self, history: ThreadHistoryResult) -> bool:
        return self._reads._repair_history_durably_refilled(history)

    async def _invalidate_raw_thread_before_repair(self, room_id: str, thread_id: str) -> None:
        await self._reads._invalidate_raw_thread_before_repair(room_id, thread_id)

    async def _incrementally_refresh_resolved_thread_cache(
        self,
        room_id: str,
        thread_id: str,
        *,
        entry: ResolvedThreadCacheEntry,
        entry_version: int,
        current_thread_version: int,
    ) -> ThreadReadResult | None:
        return await self._reads._incrementally_refresh_resolved_thread_cache(
            room_id,
            thread_id,
            entry=entry,
            entry_version=entry_version,
            current_thread_version=current_thread_version,
        )

    async def _maybe_use_resolved_thread_cache(
        self,
        room_id: str,
        thread_id: str,
        *,
        current_thread_version: int,
        repair_required: bool,
    ) -> ThreadReadResult | None:
        return await self._reads._maybe_use_resolved_thread_cache(
            room_id,
            thread_id,
            current_thread_version=current_thread_version,
            repair_required=repair_required,
        )

    async def _fetch_full_thread_history_from_source(
        self,
        room_id: str,
        thread_id: str,
        *,
        current_thread_version: int,
        repair_required: bool,
    ) -> ThreadReadResult:
        return await self._reads._fetch_full_thread_history_from_source(
            room_id,
            thread_id,
            current_thread_version=current_thread_version,
            repair_required=repair_required,
        )

    async def _read_full_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        return await self._reads._read_full_thread_history(room_id, thread_id)

    async def _read_snapshot_thread(self, room_id: str, thread_id: str) -> ThreadReadResult:
        return await self._reads._read_snapshot_thread(room_id, thread_id)

    def _snapshot_result(
        self,
        history: Sequence[ResolvedVisibleMessage],
        *,
        thread_version: int,
    ) -> ThreadReadResult:
        return self._reads._snapshot_result(history, thread_version=thread_version)

    def _full_history_result(
        self,
        history: Sequence[ResolvedVisibleMessage],
        *,
        thread_version: int,
    ) -> ThreadReadResult:
        return self._reads._full_history_result(history, thread_version=thread_version)

    async def _read_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        require_full_history: bool,
    ) -> ThreadReadResult:
        return await self._reads._read_thread(
            room_id,
            thread_id,
            require_full_history=require_full_history,
        )

    async def get_thread_snapshot(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve lightweight thread context for dispatch."""
        return await self._reads.get_thread_snapshot(room_id, thread_id)

    async def get_thread_history(self, room_id: str, thread_id: str) -> ThreadReadResult:
        """Resolve full thread history for one conversation root."""
        return await self._reads.get_thread_history(room_id, thread_id)

    async def get_latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
    ) -> str | None:
        """Resolve the latest visible thread event when MSC3440 fallback needs it."""
        return await self._reads.get_latest_thread_event_id_if_needed(
            room_id,
            thread_id,
            reply_to_event_id=reply_to_event_id,
            existing_event_id=existing_event_id,
        )

    async def record_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Write one locally sent threaded message or edit through to the cache."""
        try:
            await self._writes.record_outbound_message(room_id, event_id, content)
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound threaded message cache write-through failure after successful send",
                room_id=room_id,
                event_id=event_id,
                error=str(exc),
            )

    async def record_outbound_redaction(self, room_id: str, redacted_event_id: str) -> None:
        """Write one locally redacted threaded message through to the cache."""
        try:
            await self._writes.record_outbound_redaction(room_id, redacted_event_id)
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound threaded message cache redaction failure after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""
        await self._writes.append_live_event(room_id, event, event_info=event_info)

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        await self._writes.apply_redaction(room_id, event)

    def _track_sync_cached_event(
        self,
        room_id: str,
        event_source: dict[str, object],
    ) -> None:
        self._writes._track_sync_cached_event(room_id, event_source)

    def _group_sync_timeline_updates(
        self,
        response: nio.SyncResponse,
    ) -> tuple[
        dict[str, list[dict[str, object]]],
        dict[str, list[dict[str, object]]],
        dict[str, list[str]],
    ]:
        return self._writes._group_sync_timeline_updates(response)

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        self._writes.cache_sync_timeline(response)
