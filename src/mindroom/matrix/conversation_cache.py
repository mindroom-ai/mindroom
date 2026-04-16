"""Facade for Matrix conversation reads and advisory cache notifications."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import nio
from nio.responses import RoomGetEventError

from mindroom.logging_config import get_logger
from mindroom.matrix.cache.event_cache import (
    ConversationEventCache,
)
from mindroom.matrix.cache.event_cache_events import normalize_nio_event_for_cache
from mindroom.matrix.cache.thread_history_result import ThreadHistoryResult
from mindroom.matrix.cache.thread_reads import ThreadReadPolicy
from mindroom.matrix.cache.thread_writes import ThreadWritePolicy
from mindroom.matrix.client import (
    fetch_dispatch_thread_history,
    fetch_dispatch_thread_snapshot,
    fetch_thread_history,
    fetch_thread_snapshot,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import extract_edit_body

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from contextlib import AbstractAsyncContextManager

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError

logger = get_logger(__name__)


@dataclass
class _TurnEventLookup:
    """One memoized event lookup plus metadata for deferred cache persistence."""

    response: EventLookupResult
    fetched_event_source: dict[str, Any] | None
    lookup_fill_persisted: bool


__all__ = [
    "ConversationCacheProtocol",
    "ConversationEventCache",
    "EventLookupResult",
    "MatrixConversationCache",
    "ThreadReadResult",
]


class ConversationCacheProtocol(Protocol):
    """Conversation-data reads available to resolver and related callers."""

    def turn_scope(self) -> AbstractAsyncContextManager[None]:
        """Provide per-turn memoization for event lookups."""

    async def get_event(self, room_id: str, event_id: str) -> EventLookupResult:
        """Resolve one Matrix event by ID."""

    async def get_thread_messages(
        self,
        room_id: str,
        thread_id: str,
        *,
        full_history: bool,
        dispatch_safe: bool,
    ) -> ThreadReadResult:
        """Resolve thread context using explicit history and dispatch-safety flags."""

    async def get_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve advisory thread context for non-dispatch callers."""

    async def get_thread_history(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve advisory full thread history for one conversation root."""

    async def get_dispatch_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve strict dispatch thread context without durable-cache reuse or stale fallback."""

    async def get_dispatch_thread_history(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve strict full thread history for dispatch without durable-cache reuse or stale fallback."""

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Resolve the cached thread root for one event when known."""

    async def get_latest_thread_event_id_if_needed(
        self,
        room_id: str,
        thread_id: str | None,
        reply_to_event_id: str | None = None,
        existing_event_id: str | None = None,
    ) -> str | None:
        """Resolve the latest visible thread event when MSC3440 fallback needs it."""

    def notify_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Schedule one locally sent threaded message or edit for advisory cache bookkeeping.

        This is advisory post-send bookkeeping and must fail open.
        Callers should treat Matrix delivery as complete before this local cache work runs.
        """

    def notify_outbound_redaction(self, room_id: str, redacted_event_id: str) -> None:
        """Schedule one locally redacted threaded message for advisory cache bookkeeping.

        This is advisory post-redaction bookkeeping and must fail open.
        """

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""


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
    normalized_event_source = normalize_nio_event_for_cache(
        event,
        event_id=normalized_event_id,
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
    _turn_event_cache: ContextVar[dict[tuple[str, str], _TurnEventLookup] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_event_lookup_cache", default=None),
    )
    _reads: ThreadReadPolicy = field(init=False, repr=False)
    _writes: ThreadWritePolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind extracted read/write policy collaborators to this facade."""
        self._writes = ThreadWritePolicy(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
            require_client=self._require_client,
            fetch_event_info_for_thread_resolution=self._event_info_for_thread_resolution,
        )
        self._reads = ThreadReadPolicy(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
            fetch_thread_history_from_client=self._fetch_thread_history_from_client,
            fetch_thread_snapshot_from_client=self._fetch_thread_snapshot_from_client,
            fetch_dispatch_thread_history_from_client=self._fetch_dispatch_thread_history_from_client,
            fetch_dispatch_thread_snapshot_from_client=self._fetch_dispatch_thread_snapshot_from_client,
        )

    def _require_client(self) -> nio.AsyncClient:
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation cache"
            raise RuntimeError(msg)
        return client

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

    async def get_event(
        self,
        room_id: str,
        event_id: str,
        *,
        persist_lookup_fill: bool = True,
    ) -> EventLookupResult:
        """Resolve one event through per-turn memoization and the advisory cache."""
        normalized_event_id = event_id.strip()
        cache_key = (room_id, normalized_event_id)
        turn_cache = self._turn_event_cache.get()
        if turn_cache is not None and cache_key in turn_cache:
            cached_lookup = turn_cache[cache_key]
            if (
                persist_lookup_fill
                and not cached_lookup.lookup_fill_persisted
                and cached_lookup.fetched_event_source is not None
            ):
                await self._persist_lookup_fill(
                    room_id=room_id,
                    event_id=normalized_event_id,
                    fetched_event_source=cached_lookup.fetched_event_source,
                    queue_write=False,
                )
                turn_cache[cache_key] = _TurnEventLookup(
                    response=cached_lookup.response,
                    fetched_event_source=cached_lookup.fetched_event_source,
                    lookup_fill_persisted=True,
                )
            return cached_lookup.response

        response, fetched_event_source = await _cached_room_get_event(
            self._require_client(),
            self.runtime.event_cache,
            room_id,
            event_id,
        )
        if fetched_event_source is not None and persist_lookup_fill:
            await self._persist_lookup_fill(
                room_id=room_id,
                event_id=normalized_event_id,
                fetched_event_source=fetched_event_source,
                queue_write=True,
            )
        if turn_cache is not None:
            turn_cache[cache_key] = _TurnEventLookup(
                response=response,
                fetched_event_source=fetched_event_source,
                lookup_fill_persisted=fetched_event_source is None or persist_lookup_fill,
            )
        return response

    async def _persist_lookup_fill(
        self,
        *,
        room_id: str,
        event_id: str,
        fetched_event_source: dict[str, Any],
        queue_write: bool,
    ) -> None:
        """Persist one point-lookup fill without reintroducing same-room barrier deadlocks."""

        async def persist_lookup_event() -> None:
            await self.runtime.event_cache.store_event(event_id, room_id, fetched_event_source)

        try:
            if queue_write:
                await self.runtime.event_cache_write_coordinator.queue_room_update(
                    room_id,
                    persist_lookup_event,
                    name="matrix_cache_store_room_get_event",
                )
            else:
                await persist_lookup_event()
        except Exception as exc:
            self.logger.warning(
                "Failed to cache Matrix event lookup",
                room_id=room_id,
                event_id=event_id,
                error=str(exc),
            )

    async def _event_info_for_thread_resolution(
        self,
        room_id: str,
        event_id: str,
    ) -> EventInfo | None:
        """Resolve one related event through the shared conversation-cache lookup path."""
        response = await self.get_event(room_id, event_id, persist_lookup_fill=False)
        if not isinstance(response, nio.RoomGetEventResponse):
            return None
        return EventInfo.from_event(response.event.source)

    async def _fetch_thread_history_from_client(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        return await fetch_thread_history(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
            runtime_started_at=self.runtime.runtime_started_at,
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
            runtime_started_at=self.runtime.runtime_started_at,
        )

    async def _fetch_dispatch_thread_history_from_client(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        return await fetch_dispatch_thread_history(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
        )

    async def _fetch_dispatch_thread_snapshot_from_client(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        return await fetch_dispatch_thread_snapshot(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
        )

    async def get_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve advisory thread context for non-dispatch callers."""
        return await self._reads.read_thread(
            room_id,
            thread_id,
            full_history=False,
            dispatch_safe=False,
        )

    async def get_thread_history(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve advisory full thread history for one conversation root."""
        return await self._reads.read_thread(
            room_id,
            thread_id,
            full_history=True,
            dispatch_safe=False,
        )

    async def get_thread_messages(
        self,
        room_id: str,
        thread_id: str,
        *,
        full_history: bool,
        dispatch_safe: bool,
    ) -> ThreadReadResult:
        """Resolve thread context using one explicit read-mode entrypoint."""
        if dispatch_safe:
            if full_history:
                return await self.get_dispatch_thread_history(room_id, thread_id)
            return await self.get_dispatch_thread_snapshot(room_id, thread_id)
        if full_history:
            return await self.get_thread_history(room_id, thread_id)
        return await self.get_thread_snapshot(room_id, thread_id)

    async def get_dispatch_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve strict dispatch thread context without durable-cache reuse or stale fallback."""
        return await self._reads.read_thread(
            room_id,
            thread_id,
            full_history=False,
            dispatch_safe=True,
        )

    async def get_dispatch_thread_history(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve strict full thread history for dispatch without durable-cache reuse or stale fallback."""
        return await self._reads.read_thread(
            room_id,
            thread_id,
            full_history=True,
            dispatch_safe=True,
        )

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Resolve the cached thread root for one event when known."""
        try:
            return await self.runtime.event_cache.get_thread_id_for_event(room_id, event_id)
        except Exception as error:
            logger.warning(
                "Conversation cache thread lookup failed; continuing without cached thread id",
                room_id=room_id,
                event_id=event_id,
                error=str(error),
            )
            return None

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

    def notify_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Schedule one locally sent threaded message or edit for advisory cache bookkeeping."""
        self._run_fail_open_outbound_write(
            lambda: self._writes.outbound.notify_outbound_message(room_id, event_id, content),
            cancelled_message="Ignoring cancelled outbound threaded message cache bookkeeping after successful send",
            failure_message="Ignoring outbound threaded message cache bookkeeping failure after successful send",
            room_id=room_id,
            event_id=event_id,
        )

    def notify_outbound_redaction(self, room_id: str, redacted_event_id: str) -> None:
        """Schedule one locally redacted threaded message for advisory cache bookkeeping."""
        self._run_fail_open_outbound_write(
            lambda: self._writes.outbound.notify_outbound_redaction(room_id, redacted_event_id),
            cancelled_message="Ignoring cancelled outbound threaded message cache redaction bookkeeping after successful redact",
            failure_message="Ignoring outbound threaded message cache redaction bookkeeping failure after successful redact",
            room_id=room_id,
            redacted_event_id=redacted_event_id,
        )

    def _run_fail_open_outbound_write(
        self,
        callback: Callable[[], None],
        *,
        cancelled_message: str,
        failure_message: str,
        room_id: str,
        **log_context: object,
    ) -> None:
        try:
            callback()
        except asyncio.CancelledError as exc:
            self.logger.warning(
                cancelled_message,
                room_id=room_id,
                error=str(exc),
                **log_context,
            )
        except Exception as exc:
            self.logger.warning(
                failure_message,
                room_id=room_id,
                error=str(exc),
                **log_context,
            )

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""
        await self._writes.live.append_live_event(room_id, event, event_info=event_info)

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        await self._writes.live.apply_redaction(room_id, event)

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        self._writes.sync.cache_sync_timeline(response)
