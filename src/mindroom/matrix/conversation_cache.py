"""Facade for Matrix conversation reads and advisory cache notifications."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol

import nio
from nio.responses import RoomGetEventError

from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    ConversationEventCache,
    ThreadHistoryResult,
    normalize_nio_event_for_cache,
    thread_history_result,
)
from mindroom.matrix.cache.thread_reads import ThreadReadPolicy
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_writes import (
    ThreadLiveWritePolicy,
    ThreadOutboundWritePolicy,
    ThreadSyncWritePolicy,
)
from mindroom.matrix.client_thread_history import (
    fetch_dispatch_thread_history,
    fetch_dispatch_thread_snapshot,
    fetch_thread_history,
    fetch_thread_snapshot,
    get_room_threads_page,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import active_internal_sender_ids
from mindroom.matrix.message_content import extract_edit_body
from mindroom.matrix.thread_bookkeeping import ThreadMutationResolver
from mindroom.matrix.thread_membership import (
    fetch_event_info_for_client,
    lookup_thread_id_from_conversation_cache,
    resolve_event_thread_id,
)
from mindroom.matrix.thread_room_scan import _room_scan_membership_access_for_client

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Collection
    from contextlib import AbstractAsyncContextManager

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError
type ThreadReadCacheKey = tuple[str, str, bool, bool]

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
    "resolve_thread_root_event_id_for_client",
]


_STARTUP_PREWARM_THREAD_LIMIT = 32
_STARTUP_PREWARM_THREAD_CONCURRENCY = 32
type StartupThreadPrewarmOutcome = Literal["warmed", "failed", "aborted"]


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

    return await resolve_event_thread_id(
        room_id,
        event_info,
        event_id=normalized_event_id,
        allow_current_root=True,
        access=_room_scan_membership_access_for_client(
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
        caller_label: str = "unknown",
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
        *,
        caller_label: str = "unknown",
    ) -> ThreadReadResult:
        """Resolve advisory full thread history for one conversation root."""

    async def get_dispatch_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve strict dispatch thread context using only fresh cache data or a homeserver refill."""

    async def get_dispatch_thread_history(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str = "unknown",
    ) -> ThreadReadResult:
        """Resolve strict full dispatch thread history using only fresh cache data or a homeserver refill."""

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

    def notify_outbound_event(self, room_id: str, event_source: dict[str, Any]) -> None:
        """Schedule one locally sent outbound event for advisory cache bookkeeping."""

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
    trusted_sender_ids: Collection[str] = (),
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

    edited_body, edited_content = await extract_edit_body(
        latest_edit_source,
        client,
        event_cache=event_cache,
        room_id=room_id,
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
    trusted_sender_ids: Collection[str] = (),
) -> nio.RoomGetEventResponse | None:
    """Reconstruct one cached room-get-event response, applying visible edits when present."""
    visible_event_source = await _apply_cached_latest_edit(
        event_source,
        room_id=room_id,
        client=client,
        event_cache=event_cache,
        trusted_sender_ids=trusted_sender_ids,
    )
    cached_response = nio.RoomGetEventResponse.from_dict(visible_event_source)
    return cached_response if isinstance(cached_response, nio.RoomGetEventResponse) else None


async def _cached_room_get_event(
    client: nio.AsyncClient,
    event_cache: ConversationEventCache,
    room_id: str,
    event_id: str,
    *,
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
        trusted_sender_ids=trusted_sender_ids,
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
    _turn_thread_read_cache: ContextVar[dict[ThreadReadCacheKey, ThreadReadResult] | None] = field(
        default_factory=lambda: ContextVar("mindroom_turn_thread_read_cache", default=None),
    )
    _reads: ThreadReadPolicy = field(init=False, repr=False)
    _write_cache_ops: ThreadMutationCacheOps = field(init=False, repr=False)
    _outbound: ThreadOutboundWritePolicy = field(init=False, repr=False)
    _live: ThreadLiveWritePolicy = field(init=False, repr=False)
    _sync: ThreadSyncWritePolicy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind extracted read/write collaborators to this facade."""
        self._reads = ThreadReadPolicy(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
            fetch_thread_history_from_client=self._fetch_thread_history_from_client,
            fetch_thread_snapshot_from_client=self._fetch_thread_snapshot_from_client,
            fetch_dispatch_thread_history_from_client=self._fetch_dispatch_thread_history_from_client,
            fetch_dispatch_thread_snapshot_from_client=self._fetch_dispatch_thread_snapshot_from_client,
        )
        resolver = ThreadMutationResolver(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
            fetch_event_info_for_thread_resolution=self._event_info_for_thread_resolution,
        )
        self._write_cache_ops = ThreadMutationCacheOps(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
        )
        self._outbound = ThreadOutboundWritePolicy(
            resolver=resolver,
            cache_ops=self._write_cache_ops,
            require_client=self._require_client,
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
        return active_internal_sender_ids(
            self.runtime.config,
            self.runtime.runtime_paths,
        )

    @asynccontextmanager
    async def turn_scope(self) -> AsyncIterator[None]:
        """Memoize event lookups and thread reads for the lifetime of one inbound turn."""
        turn_lookup_cache = self._turn_event_cache.get()
        turn_thread_cache = self._turn_thread_read_cache.get()
        if turn_lookup_cache is not None and turn_thread_cache is not None:
            yield
            return

        event_token = self._turn_event_cache.set({})
        thread_token = self._turn_thread_read_cache.set({})
        try:
            yield
        finally:
            self._turn_thread_read_cache.reset(thread_token)
            self._turn_event_cache.reset(event_token)

    @staticmethod
    def _copy_thread_read_result(result: ThreadReadResult) -> ThreadReadResult:
        """Return a detached copy suitable for per-turn memoization."""
        return thread_history_result(
            list(result),
            is_full_history=result.is_full_history,
            diagnostics=result.diagnostics,
        )

    async def _read_thread_memoized(
        self,
        room_id: str,
        thread_id: str,
        *,
        full_history: bool,
        dispatch_safe: bool,
        caller_label: str,
    ) -> ThreadReadResult:
        """Resolve one thread read through per-turn memoization."""
        cache_key: ThreadReadCacheKey = (room_id, thread_id, full_history, dispatch_safe)
        turn_cache = self._turn_thread_read_cache.get()
        if turn_cache is not None and cache_key in turn_cache:
            return self._copy_thread_read_result(turn_cache[cache_key])

        result = await self._reads.read_thread(
            room_id,
            thread_id,
            full_history=full_history,
            dispatch_safe=dispatch_safe,
            caller_label=caller_label,
        )
        if turn_cache is not None:
            turn_cache[cache_key] = self._copy_thread_read_result(result)
            return self._copy_thread_read_result(turn_cache[cache_key])
        return result

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
            trusted_sender_ids=self._trusted_sender_ids(),
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
        *,
        caller_label: str,
        coordinator_queue_wait_ms: float,
    ) -> ThreadHistoryResult:
        fetch_started_at = time.time()
        return await fetch_thread_history(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
            runtime_started_at=self.runtime.runtime_started_at,
            cache_write_guard_started_at=fetch_started_at,
            trusted_sender_ids=self._trusted_sender_ids(),
            caller_label=caller_label,
            coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        )

    async def _fetch_thread_snapshot_from_client(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str,
        coordinator_queue_wait_ms: float,
    ) -> ThreadHistoryResult:
        fetch_started_at = time.time()
        return await fetch_thread_snapshot(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
            runtime_started_at=self.runtime.runtime_started_at,
            cache_write_guard_started_at=fetch_started_at,
            trusted_sender_ids=self._trusted_sender_ids(),
            caller_label=caller_label,
            coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        )

    async def _fetch_dispatch_thread_history_from_client(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str,
        coordinator_queue_wait_ms: float,
    ) -> ThreadHistoryResult:
        fetch_started_at = time.time()
        return await fetch_dispatch_thread_history(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
            runtime_started_at=self.runtime.runtime_started_at,
            cache_write_guard_started_at=fetch_started_at,
            trusted_sender_ids=self._trusted_sender_ids(),
            caller_label=caller_label,
            coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        )

    async def _fetch_dispatch_thread_snapshot_from_client(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str,
        coordinator_queue_wait_ms: float,
    ) -> ThreadHistoryResult:
        fetch_started_at = time.time()
        return await fetch_dispatch_thread_snapshot(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
            runtime_started_at=self.runtime.runtime_started_at,
            cache_write_guard_started_at=fetch_started_at,
            trusted_sender_ids=self._trusted_sender_ids(),
            caller_label=caller_label,
            coordinator_queue_wait_ms=coordinator_queue_wait_ms,
        )

    async def _refresh_dispatch_thread_snapshot_for_startup_prewarm(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadHistoryResult:
        """Refresh one strict thread snapshot for advisory startup prewarm without the live read barrier."""
        fetch_started_at = time.time()
        return await fetch_dispatch_thread_snapshot(
            self._require_client(),
            room_id,
            thread_id,
            event_cache=self.runtime.event_cache,
            runtime_started_at=self.runtime.runtime_started_at,
            cache_write_guard_started_at=fetch_started_at,
            trusted_sender_ids=self._trusted_sender_ids(),
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
        except asyncio.CancelledError:
            raise
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
        started_at = time.perf_counter()
        threads_warmed = 0
        threads_failed = 0
        thread_ids = await self._startup_thread_prewarm_ids(room_id)
        if thread_ids is None:
            return False

        completed = True
        pending_thread_ids = list(thread_ids)

        async def worker() -> None:
            nonlocal completed, threads_failed, threads_warmed
            while pending_thread_ids:
                if is_shutting_down():
                    completed = False
                    return
                outcome = await self._prewarm_one_startup_thread(
                    room_id,
                    pending_thread_ids.pop(0),
                    is_shutting_down=is_shutting_down,
                )
                if outcome == "aborted":
                    completed = False
                    return
                if outcome == "failed":
                    threads_failed += 1
                else:
                    threads_warmed += 1

        worker_count = min(_STARTUP_PREWARM_THREAD_CONCURRENCY, len(pending_thread_ids))
        await asyncio.gather(*(worker() for _ in range(worker_count)))

        self.logger.info(
            "startup_thread_prewarm_complete",
            room_id=room_id,
            threads_warmed=threads_warmed,
            threads_failed=threads_failed,
            elapsed_ms=round((time.perf_counter() - started_at) * 1000, 1),
        )
        return completed

    async def _prewarm_one_startup_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        is_shutting_down: Callable[[], bool],
    ) -> StartupThreadPrewarmOutcome:
        """Refresh one startup thread snapshot and return its room-level prewarm outcome."""
        if is_shutting_down():
            return "aborted"
        if not thread_id:
            self.logger.warning(
                "startup_thread_prewarm_thread_failed",
                room_id=room_id,
                thread_id=thread_id,
                error="missing_thread_root_event_id",
            )
            await asyncio.sleep(0)
            return "failed"

        try:
            await self._refresh_dispatch_thread_snapshot_for_startup_prewarm(
                room_id,
                thread_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.warning(
                "startup_thread_prewarm_thread_failed",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return "failed"

        await asyncio.sleep(0)
        return "warmed"

    async def get_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve advisory thread context for non-dispatch callers."""
        return await self._read_thread_memoized(
            room_id,
            thread_id,
            full_history=False,
            dispatch_safe=False,
            caller_label="unknown",
        )

    async def get_thread_history(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str = "unknown",
    ) -> ThreadReadResult:
        """Resolve advisory full thread history for one conversation root."""
        return await self._read_thread_memoized(
            room_id,
            thread_id,
            full_history=True,
            dispatch_safe=False,
            caller_label=caller_label,
        )

    async def get_thread_messages(
        self,
        room_id: str,
        thread_id: str,
        *,
        full_history: bool,
        dispatch_safe: bool,
        caller_label: str = "unknown",
    ) -> ThreadReadResult:
        """Resolve thread context using one explicit read-mode entrypoint."""
        if dispatch_safe:
            if full_history:
                return await self.get_dispatch_thread_history(
                    room_id,
                    thread_id,
                    caller_label=caller_label,
                )
            return await self.get_dispatch_thread_snapshot(room_id, thread_id)
        if full_history:
            return await self.get_thread_history(
                room_id,
                thread_id,
                caller_label=caller_label,
            )
        return await self.get_thread_snapshot(room_id, thread_id)

    async def get_dispatch_thread_snapshot(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve strict dispatch thread context using only fresh cache data or a homeserver refill."""
        return await self._read_thread_memoized(
            room_id,
            thread_id,
            full_history=False,
            dispatch_safe=True,
            caller_label="unknown",
        )

    async def get_dispatch_thread_history(
        self,
        room_id: str,
        thread_id: str,
        *,
        caller_label: str = "unknown",
    ) -> ThreadReadResult:
        """Resolve strict full dispatch thread history using only fresh cache data or a homeserver refill."""
        return await self._read_thread_memoized(
            room_id,
            thread_id,
            full_history=True,
            dispatch_safe=True,
            caller_label=caller_label,
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
        self._outbound.notify_outbound_message(room_id, event_id, content)

    def notify_outbound_event(
        self,
        room_id: str,
        event_source: dict[str, Any],
    ) -> None:
        """Schedule one locally sent outbound event for advisory cache bookkeeping."""
        self._outbound.notify_outbound_event(room_id, event_source)

    def notify_outbound_redaction(self, room_id: str, redacted_event_id: str) -> None:
        """Schedule one locally redacted threaded message for advisory cache bookkeeping."""
        self._outbound.notify_outbound_redaction(room_id, redacted_event_id)

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

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        self._sync.cache_sync_timeline(response)
