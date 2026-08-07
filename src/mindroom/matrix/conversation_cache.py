"""Facade for Matrix conversation reads and advisory cache notifications.

``MatrixConversationCache`` is the facade for conversation reads and advisory thread bookkeeping; it
composes the read policy (``cache.thread_reads``), the three write policies (``cache.thread_writes``),
and the mutation resolver (``thread_bookkeeping``) over one shared write coordinator.
Bots and tools still read the event cache directly for non-thread point lookups such as agent message
snapshots and recent room events, but all thread reads and thread bookkeeping go through this facade.

Per-turn memoization covers event lookups only. Thread reads are not memoized: the saving was one
re-read per turn, and paying for it meant every caller reasoning about whether a degraded or stale
read might be replayed later in the same turn.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

import nio
from nio.responses import RoomGetEventError

from mindroom.entity_resolution import current_internal_sender_ids
from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    ConversationEventCache,
)
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_writes import ThreadLiveWritePolicy, ThreadSyncWritePolicy
from mindroom.matrix.client_thread_history import (
    BulkThreadRefreshStats,
    bulk_refresh_room_thread_histories,
    thread_ids_needing_refill,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.event_normalization import normalize_nio_event_for_cache
from mindroom.matrix.media import (
    is_encrypted_media_event_source,
    parse_matrix_media_event_source,
)
from mindroom.matrix.membership_fence import UNCERTIFIED_MEMBERSHIP_EPOCH
from mindroom.matrix.message_content import extract_edit_body
from mindroom.matrix.room_history_reads import get_room_threads_page
from mindroom.matrix.thread_bookkeeping import ThreadMutationResolver
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.matrix.thread_membership import resolve_event_thread_membership
from mindroom.matrix.thread_room_scan import (
    fetch_event_info_for_client,
    lookup_thread_id_from_conversation_cache,
    room_scan_membership_access_for_client,
)
from mindroom.timing import elapsed_ms_since

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator, Callable, Collection
    from contextlib import AbstractAsyncContextManager

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.sync_certification import SyncCacheWriteResult


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError
type _TurnEventCacheKey = tuple[str, str, int]

logger = get_logger(__name__)


__all__ = [
    "ConversationCacheProtocol",
    "ConversationEventCache",
    "EventLookupResult",
    "MatrixConversationCache",
    "ThreadReadResult",
    "resolve_thread_root_event_id_for_client",
]


_STARTUP_PREWARM_THREAD_LIMIT = 32
_STARTUP_PREWARM_MAX_SCAN_PAGES = 20


async def resolve_thread_root_event_id_for_client(
    client: nio.AsyncClient,
    room_id: str,
    event_id: str,
    *,
    conversation_cache: ConversationCacheProtocol | None = None,
) -> str | None:
    """Resolve one event ID into a canonical thread root when thread membership can prove one."""
    normalized_event_id = event_id.strip() if isinstance(event_id, str) else ""
    if not normalized_event_id:
        return None

    event_info = await fetch_event_info_for_client(
        client,
        room_id,
        normalized_event_id,
        strict=False,
    )
    if event_info is None:
        return await lookup_thread_id_from_conversation_cache(
            conversation_cache,
            room_id,
            normalized_event_id,
        )

    resolution = await resolve_event_thread_membership(
        room_id,
        event_info,
        event_id=normalized_event_id,
        allow_current_root=True,
        access=room_scan_membership_access_for_client(
            client,
            conversation_cache=conversation_cache,
            fetch_event_info=lambda lookup_room_id, lookup_event_id: fetch_event_info_for_client(
                client,
                lookup_room_id,
                lookup_event_id,
                strict=False,
            ),
        ),
    )
    return resolution.thread_id


class ConversationCacheProtocol(Protocol):
    """Conversation-data reads available to resolver and related callers."""

    def turn_scope(self) -> AbstractAsyncContextManager[None]:
        """Provide per-turn memoization for event lookups."""

    async def get_event(self, room_id: str, event_id: str) -> EventLookupResult:
        """Resolve one Matrix event by ID."""

    async def get_thread_id_for_event(self, room_id: str, event_id: str) -> str | None:
        """Resolve the cached thread root for one event when known."""

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
    expected_membership_epoch: int | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> dict[str, Any]:
    """Project one cached original event into its latest visible edited state."""
    if event_source.get("type") != "m.room.message":
        return event_source

    event_info = EventInfo.from_event(event_source)
    event_id = event_source.get("event_id")
    sender = event_source.get("sender")
    if event_info.is_edit or not isinstance(event_id, str) or not event_id or not isinstance(sender, str):
        return event_source

    # Scoped to this event's own sender. A replacement is only legitimate from the sender of the
    # event it replaces, and without this the newest edit from anyone wins - so this path would
    # serve someone else's text under the author's event, while the collapsed thread read of the
    # same cache correctly refuses it.
    latest_edit_source = await event_cache.get_latest_edit(room_id, event_id, sender=sender)
    if latest_edit_source is None:
        return event_source

    edited_body, edited_content = await extract_edit_body(
        latest_edit_source,
        client,
        event_cache=event_cache,
        room_id=room_id,
        expected_membership_epoch=expected_membership_epoch,
        trusted_sender_ids=trusted_sender_ids,
    )
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
    expected_membership_epoch: int | None = None,
    trusted_sender_ids: Collection[str] = (),
) -> nio.RoomGetEventResponse | None:
    """Reconstruct one cached room-get-event response, applying visible edits when present."""
    visible_event_source = await _apply_cached_latest_edit(
        event_source,
        room_id=room_id,
        client=client,
        event_cache=event_cache,
        expected_membership_epoch=expected_membership_epoch,
        trusted_sender_ids=trusted_sender_ids,
    )
    if is_encrypted_media_event_source(visible_event_source):
        parsed_media_event = parse_matrix_media_event_source(visible_event_source)
        if parsed_media_event is None:
            return None
        cached_response = nio.RoomGetEventResponse()
        # nio's response parser also assigns BadEvent to this Event-typed field.
        cached_response.event = cast("nio.Event", parsed_media_event)
        return cached_response
    cached_response = nio.RoomGetEventResponse.from_dict(visible_event_source)
    return cached_response if isinstance(cached_response, nio.RoomGetEventResponse) else None


async def _cached_room_get_event(
    client: nio.AsyncClient,
    event_cache: ConversationEventCache,
    room_id: str,
    event_id: str,
    *,
    expected_membership_epoch: int | None = None,
    trusted_sender_ids: Collection[str] = (),
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
                    expected_membership_epoch=expected_membership_epoch,
                    trusted_sender_ids=trusted_sender_ids,
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
        expected_membership_epoch=expected_membership_epoch,
        trusted_sender_ids=trusted_sender_ids,
    )
    return (visible_response if visible_response is not None else response), normalized_event_source


@dataclass
class MatrixConversationCache(ConversationCacheProtocol):
    """Own Matrix conversation reads and advisory cache writes for one bot."""

    logger: structlog.stdlib.BoundLogger
    runtime: BotRuntimeView
    _turn_event_cache: ContextVar[dict[_TurnEventCacheKey, EventLookupResult] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_event_lookup_cache", default=None),
    )
    _write_cache_ops: ThreadMutationCacheOps = field(init=False, repr=False)
    _live: ThreadLiveWritePolicy = field(init=False, repr=False)
    _sync: ThreadSyncWritePolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind extracted read/write collaborators to this facade."""
        resolver = ThreadMutationResolver(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
            fetch_event_info_for_thread_resolution=self._event_info_for_thread_resolution,
        )
        self._write_cache_ops = ThreadMutationCacheOps(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
        )
        self._live = ThreadLiveWritePolicy(
            resolver=resolver,
            cache_ops=self._write_cache_ops,
        )
        self._sync = ThreadSyncWritePolicy(
            resolver=resolver,
            cache_ops=self._write_cache_ops,
        )

    def _require_client(self) -> nio.AsyncClient:
        client = self.runtime.client
        if client is None:
            msg = "Matrix client is not ready for conversation cache"
            raise RuntimeError(msg)
        return client

    def _trusted_sender_ids(self) -> frozenset[str]:
        """Return the exact internal sender IDs allowed to override canonical visible-body reads."""
        return current_internal_sender_ids(self.runtime.config, self.runtime.runtime_paths)

    @asynccontextmanager
    async def turn_scope(self) -> AsyncIterator[None]:
        """Memoize event lookups for the lifetime of one inbound turn."""
        if self._turn_event_cache.get() is not None:
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
    ) -> EventLookupResult:
        """Resolve one event through per-turn memoization and the advisory cache."""
        normalized_event_id = event_id.strip()
        cache_key: _TurnEventCacheKey = (
            room_id,
            normalized_event_id,
            self._write_cache_ops.room_departure_epoch(room_id),
        )
        turn_cache = self._turn_event_cache.get()
        if turn_cache is not None and cache_key in turn_cache:
            return turn_cache[cache_key]

        coordinator = self.runtime.event_cache_write_coordinator
        if coordinator is not None:
            await coordinator.wait_for_prior_room_updates(
                room_id,
                coordination_scope=self.runtime.event_cache.principal_id,
            )

        membership_epoch = await self._capture_membership_epoch(room_id)
        response, fetched_event_source = await _cached_room_get_event(
            self._require_client(),
            self.runtime.event_cache,
            room_id,
            event_id,
            expected_membership_epoch=membership_epoch,
            trusted_sender_ids=self._trusted_sender_ids(),
        )
        if fetched_event_source is not None:
            await self._persist_lookup_fill(
                room_id=room_id,
                event_id=normalized_event_id,
                fetched_event_source=fetched_event_source,
                expected_membership_epoch=membership_epoch,
                queue_write=coordinator is not None,
            )
        if turn_cache is not None:
            turn_cache[cache_key] = response
        return response

    async def _capture_membership_epoch(self, room_id: str) -> int:
        """Return a durable lookup generation or one that rejects every cache write."""
        try:
            membership_epoch = await self.runtime.event_cache.room_membership_epoch(room_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to certify Matrix lookup cache generation; continuing without cache writes",
                room_id=room_id,
                error=str(exc),
            )
            return UNCERTIFIED_MEMBERSHIP_EPOCH
        return UNCERTIFIED_MEMBERSHIP_EPOCH if membership_epoch is None else membership_epoch

    async def _persist_lookup_fill(
        self,
        *,
        room_id: str,
        event_id: str,
        fetched_event_source: dict[str, Any],
        expected_membership_epoch: int,
        queue_write: bool,
    ) -> None:
        """Persist one point-lookup fill without reintroducing same-room barrier deadlocks."""

        async def persist_lookup_event() -> None:
            await self.runtime.event_cache.store_event(
                event_id,
                room_id,
                fetched_event_source,
                expected_membership_epoch=expected_membership_epoch,
            )

        try:
            if queue_write:
                await self.runtime.event_cache_write_coordinator.queue_room_update(
                    room_id,
                    persist_lookup_event,
                    name="matrix_cache_store_room_get_event",
                    coordination_scope=self.runtime.event_cache.principal_id,
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
        """Resolve one related event without memoizing pre-mutation state in the active turn."""
        membership_epoch = await self._capture_membership_epoch(room_id)
        response, _fetched_event_source = await _cached_room_get_event(
            self._require_client(),
            self.runtime.event_cache,
            room_id,
            event_id,
            expected_membership_epoch=membership_epoch,
            trusted_sender_ids=self._trusted_sender_ids(),
        )
        if not isinstance(response, nio.RoomGetEventResponse):
            return None
        return EventInfo.from_event(response.event.source)

    async def _bulk_refresh_startup_threads(
        self,
        room_id: str,
        thread_ids: Collection[str],
    ) -> BulkThreadRefreshStats:
        """Refresh startup threads without occupying the live write coordinator during the scan."""
        return await bulk_refresh_room_thread_histories(
            self._require_client(),
            room_id,
            self.runtime.event_cache,
            thread_root_ids=thread_ids,
            caller_label="startup_thread_prewarm",
            max_scan_pages=_STARTUP_PREWARM_MAX_SCAN_PAGES,
        )

    def _log_startup_thread_prewarm_complete(
        self,
        room_id: str,
        *,
        started_at: float,
        threads_warmed: int,
        threads_failed: int,
    ) -> None:
        self.logger.info(
            "startup_thread_prewarm_complete",
            room_id=room_id,
            threads_warmed=threads_warmed,
            threads_failed=threads_failed,
            elapsed_ms=elapsed_ms_since(started_at, clock=time.perf_counter),
        )

    async def _startup_thread_prewarm_ids(
        self,
        room_id: str,
    ) -> list[str] | None:
        """Return startup-prewarm thread IDs using local recency first and /threads as a top-up.

        Tuwunel does not currently order /threads by latest thread activity, so the local cache is the
        best available recency signal for startup prewarm. /threads is only used to fill any remaining
        slots when we have fewer than the target number of locally known threads.
        """
        thread_ids = await self.runtime.event_cache.get_recent_room_thread_ids(
            room_id,
            limit=_STARTUP_PREWARM_THREAD_LIMIT,
        )
        if len(thread_ids) >= _STARTUP_PREWARM_THREAD_LIMIT:
            return thread_ids
        try:
            thread_roots, _next_batch = await get_room_threads_page(
                self._require_client(),
                room_id,
                limit=_STARTUP_PREWARM_THREAD_LIMIT,
            )
        except Exception as exc:
            self.logger.warning(
                "startup_thread_prewarm_room_threads_failed",
                room_id=room_id,
                error=str(exc),
                local_thread_count=len(thread_ids),
            )
            # Partial local prewarm is still useful here because /threads is only a best-effort top-up.
            return thread_ids or None

        for thread_root in thread_roots:
            thread_id = thread_root.event_id.strip()
            if thread_id and thread_id not in thread_ids:
                thread_ids.append(thread_id)
            if len(thread_ids) >= _STARTUP_PREWARM_THREAD_LIMIT:
                break
        return thread_ids

    async def prewarm_recent_room_threads(
        self,
        room_id: str,
        *,
        is_shutting_down: Callable[[], bool],
    ) -> bool:
        """Warm one room's recent thread roots and report whether the room-level pass finished."""
        if not self.runtime.event_cache.durable_writes_available:
            self.logger.warning(
                "startup_thread_prewarm_skipped",
                room_id=room_id,
                reason="event_cache_writes_unavailable",
            )
            return False
        started_at = time.perf_counter()
        thread_ids = await self._startup_thread_prewarm_ids(room_id)
        if thread_ids is None or is_shutting_down() or not self.runtime.event_cache.durable_writes_available:
            return False
        try:
            thread_ids_to_refill = await thread_ids_needing_refill(
                self.runtime.event_cache,
                room_id,
                thread_ids,
            )
        except Exception as exc:
            self.logger.warning(
                "startup_thread_prewarm_cache_probe_failed",
                room_id=room_id,
                thread_count=len(thread_ids),
                error=str(exc),
                error_type=type(exc).__name__,
                exc_info=True,
            )
            thread_ids_to_refill = None
        if thread_ids_to_refill is None or is_shutting_down() or not self.runtime.event_cache.durable_writes_available:
            return False
        already_warm = len(thread_ids) - len(thread_ids_to_refill)
        if not thread_ids_to_refill:
            self._log_startup_thread_prewarm_complete(
                room_id,
                started_at=started_at,
                threads_warmed=already_warm,
                threads_failed=0,
            )
            return True

        try:
            stats = await self._bulk_refresh_startup_threads(
                room_id,
                thread_ids_to_refill,
            )
        except Exception as exc:
            self.logger.warning(
                "startup_thread_prewarm_bulk_failed",
                room_id=room_id,
                thread_count=len(thread_ids_to_refill),
                error=str(exc),
            )
            return False

        threads_warmed = already_warm + stats.usable_threads
        threads_failed = len(thread_ids_to_refill) - stats.usable_threads
        self._log_startup_thread_prewarm_complete(
            room_id,
            started_at=started_at,
            threads_warmed=threads_warmed,
            threads_failed=threads_failed,
        )
        return not is_shutting_down() and self.runtime.event_cache.durable_writes_available

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

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""
        await self._live.append_live_event(room_id, event, event_info=event_info)

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        await self._live.apply_redaction(room_id, event)

    async def cache_historical_event(
        self,
        room: nio.MatrixRoom,
        event: nio.Event,
    ) -> None:
        """Durably cache one nio-recovered history event before admission."""
        await self._sync.cache_historical_event(room.room_id, event)

    def limited_sync_timeline_room_ids(
        self,
        response: nio.SyncResponse,
    ) -> tuple[tuple[str, ...], tuple[BaseException, ...]]:
        """Return limited joined-room IDs or validation errors for one sync response."""
        return self._sync.limited_sync_timeline_room_ids(response)

    def cache_sync_timeline(
        self,
        response: nio.SyncResponse,
        *,
        raise_on_cache_write_failure: bool = False,
    ) -> list[asyncio.Task[object]]:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        return self._sync.cache_sync_timeline(
            response,
            raise_on_cache_write_failure=raise_on_cache_write_failure,
        )

    async def cache_sync_timeline_for_certification(
        self,
        response: nio.SyncResponse,
    ) -> SyncCacheWriteResult:
        """Durably persist sync timeline events and report cache-certification status."""
        return await self._sync.cache_sync_timeline_for_certification(response)
