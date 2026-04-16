"""Facade for Matrix conversation reads and advisory cache notifications."""

from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast

import nio
from nio.responses import RoomGetEventError

from mindroom.logging_config import get_logger
from mindroom.matrix.cache import (
    THREAD_HISTORY_SOURCE_DIAGNOSTIC,
    THREAD_HISTORY_SOURCE_STALE_CACHE,
    ConversationEventCache,
    ThreadHistoryResult,
    normalize_nio_event_for_cache,
    thread_history_result,
)
from mindroom.matrix.client import (
    fetch_dispatch_thread_history,
    fetch_dispatch_thread_snapshot,
    fetch_thread_history,
    fetch_thread_snapshot,
)
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import extract_edit_body
from mindroom.matrix.thread_bookkeeping import (
    MutationResolutionContext,
    MutationThreadImpact,
    MutationThreadImpactState,
    ThreadMutationResolver,
    is_thread_affecting_relation,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Sequence
    from contextlib import AbstractAsyncContextManager

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.client import ResolvedVisibleMessage


type ThreadReadResult = ThreadHistoryResult
type EventLookupResult = nio.RoomGetEventResponse | RoomGetEventError
type ThreadReadCacheKey = tuple[str, str, bool, bool]

logger = get_logger(__name__)
_RUNTIME_ONLY_EVENT_SOURCE_KEYS = frozenset({"com.mindroom.dispatch_pipeline_timing"})


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
        """Resolve strict dispatch thread context using only fresh cache data or a homeserver refill."""

    async def get_dispatch_thread_history(
        self,
        room_id: str,
        thread_id: str,
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


class _ThreadReads:
    """Private thread-read helper owned by MatrixConversationCache."""

    def __init__(
        self,
        *,
        logger_getter: Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        fetch_thread_history_from_client: Callable[[str, str], Awaitable[ThreadHistoryResult]],
        fetch_thread_snapshot_from_client: Callable[[str, str], Awaitable[ThreadHistoryResult]],
        fetch_dispatch_thread_history_from_client: Callable[[str, str], Awaitable[ThreadHistoryResult]],
        fetch_dispatch_thread_snapshot_from_client: Callable[[str, str], Awaitable[ThreadHistoryResult]],
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime
        self.fetch_thread_history_from_client = fetch_thread_history_from_client
        self.fetch_thread_snapshot_from_client = fetch_thread_snapshot_from_client
        self.fetch_dispatch_thread_history_from_client = fetch_dispatch_thread_history_from_client
        self.fetch_dispatch_thread_snapshot_from_client = fetch_dispatch_thread_snapshot_from_client

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    async def _wait_for_pending_room_cache_updates(self, room_id: str) -> None:
        coordinator = self.runtime.event_cache_write_coordinator
        if coordinator is None:
            return
        await coordinator.wait_for_room_idle(room_id)

    @staticmethod
    def _full_history_result(
        history: Sequence[ResolvedVisibleMessage] | ThreadHistoryResult,
    ) -> ThreadHistoryResult:
        if isinstance(history, ThreadHistoryResult):
            return thread_history_result(
                history,
                is_full_history=True,
                diagnostics=history.diagnostics,
            )
        return thread_history_result(list(history), is_full_history=True)

    async def _run_thread_read(
        self,
        room_id: str,
        thread_id: str,
        *,
        fetcher: Callable[[str, str], Awaitable[ThreadHistoryResult]],
        name: str,
        full_history: bool,
    ) -> ThreadHistoryResult:
        async def load() -> ThreadHistoryResult:
            thread_history = await fetcher(room_id, thread_id)
            if full_history:
                return self._full_history_result(thread_history)
            return thread_history

        coordinator = self.runtime.event_cache_write_coordinator
        if coordinator is None:
            return await load()
        return cast(
            "ThreadHistoryResult",
            await coordinator.run_room_update(
                room_id,
                load,
                name=name,
            ),
        )

    async def read_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        full_history: bool,
        dispatch_safe: bool,
    ) -> ThreadHistoryResult:
        """Resolve one thread read through the shared barrier and fetch selection path."""
        await self._wait_for_pending_room_cache_updates(room_id)
        if full_history and dispatch_safe:
            return await self._run_thread_read(
                room_id,
                thread_id,
                fetcher=self.fetch_dispatch_thread_history_from_client,
                name="matrix_cache_refresh_dispatch_thread_history",
                full_history=True,
            )
        if full_history:
            return await self._run_thread_read(
                room_id,
                thread_id,
                fetcher=self.fetch_thread_history_from_client,
                name="matrix_cache_refresh_thread_history",
                full_history=True,
            )
        if dispatch_safe:
            return await self._run_thread_read(
                room_id,
                thread_id,
                fetcher=self.fetch_dispatch_thread_snapshot_from_client,
                name="matrix_cache_refresh_dispatch_thread_snapshot",
                full_history=False,
            )
        return await self._run_thread_read(
            room_id,
            thread_id,
            fetcher=self.fetch_thread_snapshot_from_client,
            name="matrix_cache_refresh_thread_snapshot",
            full_history=False,
        )

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
            thread_history = await self.read_thread(
                room_id,
                thread_id,
                full_history=True,
                dispatch_safe=False,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to refresh latest thread event ID; falling back to thread root",
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return thread_id
        if thread_history.diagnostics.get(THREAD_HISTORY_SOURCE_DIAGNOSTIC) == THREAD_HISTORY_SOURCE_STALE_CACHE:
            self.logger.warning(
                "Ignoring stale cached thread tail for latest-event lookup; falling back to thread root",
                room_id=room_id,
                thread_id=thread_id,
            )
            return thread_id
        if not thread_history:
            return thread_id
        latest_message = thread_history[-1]
        return latest_message.visible_event_id or latest_message.event_id or thread_id


def _normalize_event_source_for_cache(
    event_source: dict[str, Any],
    *,
    event_id: str | None = None,
    sender: str | None = None,
    origin_server_ts: int | None = None,
) -> dict[str, Any]:
    """Normalize one raw Matrix event payload for cache-style storage."""
    source = {key: value for key, value in event_source.items() if key not in _RUNTIME_ONLY_EVENT_SOURCE_KEYS}
    if "event_id" not in source and isinstance(event_id, str):
        source["event_id"] = event_id
    if "sender" not in source and isinstance(sender, str):
        source["sender"] = sender
    if (
        "origin_server_ts" not in source
        and isinstance(origin_server_ts, int)
        and not isinstance(origin_server_ts, bool)
    ):
        source["origin_server_ts"] = origin_server_ts
    return source


def _collect_sync_event_cache_update(
    room_id: str,
    event: nio.Event,
) -> tuple[str, dict[str, object]] | None:
    event_id = event.event_id
    if not isinstance(event_id, str) or not event_id:
        return None
    return room_id, normalize_nio_event_for_cache(event)


def _threaded_sync_event_cache_update(
    room_id: str,
    event: nio.Event,
) -> tuple[str, dict[str, object]] | None:
    event_source = event.source if isinstance(event.source, dict) else {}
    event_info = EventInfo.from_event(event_source)
    if not is_thread_affecting_relation(event_info):
        return None
    event_id = event.event_id
    if not isinstance(event_id, str) or not event_id:
        return None
    return room_id, normalize_nio_event_for_cache(event)


def _collect_sync_timeline_cache_updates(
    room_id: str,
    event: nio.Event,
    *,
    room_threaded_events: dict[str, list[dict[str, object]]],
    room_plain_events: dict[str, list[dict[str, object]]],
    room_redactions: dict[str, list[str]],
) -> None:
    event_source = event.source if isinstance(event.source, dict) else {}
    if isinstance(event, nio.RedactionEvent):
        redacted_event_id = event.redacts
        if isinstance(redacted_event_id, str) and redacted_event_id:
            room_redactions.setdefault(room_id, []).append(redacted_event_id)
        return

    event_info = EventInfo.from_event(event_source)
    if is_thread_affecting_relation(event_info):
        cache_update = _threaded_sync_event_cache_update(room_id, event)
        if cache_update is None:
            return
        update_room_id, normalized_event_source = cache_update
        room_threaded_events.setdefault(update_room_id, []).append(normalized_event_source)
        return

    cache_update = _collect_sync_event_cache_update(room_id, event)
    if cache_update is None:
        return
    update_room_id, normalized_event_source = cache_update
    room_plain_events.setdefault(update_room_id, []).append(normalized_event_source)


class _ThreadMutationCacheOps:
    """Own queueing, invalidation, and cache writes for thread mutations."""

    def __init__(
        self,
        *,
        logger_getter: Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    def cache_runtime_available(self) -> bool:
        """Return whether event-cache writes can safely proceed."""
        return self.runtime.event_cache is not None and self.runtime.event_cache_write_coordinator is not None

    def queue_room_cache_update(
        self,
        room_id: str,
        update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
        *,
        name: str,
    ) -> asyncio.Task[object]:
        """Run one cache mutation under the room-ordered write barrier."""
        coordinator = self.runtime.event_cache_write_coordinator
        return coordinator.queue_room_update(room_id, update_coro_factory, name=name)

    async def store_events_batch(
        self,
        room_id: str,
        batch: Sequence[tuple[str, str, dict[str, object]]],
        *,
        failure_message: str,
    ) -> None:
        """Persist one sync batch fail-open so later mutation handling can continue."""
        if not batch:
            return
        try:
            await self.runtime.event_cache.store_events_batch(list(batch))
        except Exception as exc:
            self.logger.warning(
                failure_message,
                room_id=room_id,
                event_count=len(batch),
                error=str(exc),
            )

    async def redact_cached_event(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        thread_id: str | None,
        failure_message: str,
    ) -> bool:
        """Apply one cached redaction fail-open and report whether a row changed."""
        try:
            return bool(await self.runtime.event_cache.redact_event(room_id, redacted_event_id))
        except Exception as exc:
            self.logger.warning(
                failure_message,
                room_id=room_id,
                thread_id=thread_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
            return False

    async def invalidate_after_redaction(
        self,
        room_id: str,
        *,
        impact: MutationThreadImpact,
        redacted: bool,
        success_reason: str,
        failure_reason: str,
        lookup_unavailable_reason: str,
    ) -> None:
        """Apply the post-redaction invalidation policy for one resolved impact."""
        if impact.state is MutationThreadImpactState.THREADED:
            assert impact.thread_id is not None
            await self.invalidate_known_thread(
                room_id,
                impact.thread_id,
                reason=success_reason if redacted else failure_reason,
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            await self.invalidate_room_threads(room_id, reason=lookup_unavailable_reason)

    async def invalidate_known_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
    ) -> None:
        """Mark one cached thread stale and fail closed if the marker cannot be written."""
        try:
            await self.runtime.event_cache.mark_thread_stale(room_id, thread_id, reason=reason)
        except Exception as exc:
            self.logger.warning(
                "Failed to mark cached thread stale",
                room_id=room_id,
                thread_id=thread_id,
                reason=reason,
                error=str(exc),
            )
            await self._fail_closed_thread_invalidation(
                room_id,
                thread_id,
                reason=reason,
                stale_marker_error=exc,
            )

    async def invalidate_room_threads(
        self,
        room_id: str,
        *,
        reason: str,
    ) -> None:
        """Mark one room's cached threads stale and fail closed if the marker cannot be written."""
        try:
            await self.runtime.event_cache.mark_room_threads_stale(room_id, reason=reason)
        except Exception as exc:
            self.logger.warning(
                "Failed to mark cached room threads stale",
                room_id=room_id,
                reason=reason,
                error=str(exc),
            )
            await self._fail_closed_room_invalidation(
                room_id,
                reason=reason,
                stale_marker_error=exc,
            )

    async def append_event_to_cache(
        self,
        room_id: str,
        thread_id: str,
        event_source: dict[str, Any],
        *,
        context: str,
    ) -> bool:
        """Append one event into a cached thread fail-open and report whether a row changed."""
        event_id = event_source.get("event_id")
        try:
            appended = await self.runtime.event_cache.append_event(room_id, thread_id, event_source)
        except Exception as exc:
            self.logger.warning(
                "Failed to append thread event to cache",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                context=context,
                error=str(exc),
            )
            return False
        if not appended:
            self.logger.debug(
                "Skipping thread event append because raw thread cache is missing",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                context=context,
            )
        return bool(appended)

    def _disable_cache_after_fail_closed_invalidation(
        self,
        *,
        room_id: str,
        reason: str,
        scope: str,
    ) -> None:
        self.runtime.event_cache.disable(f"stale_marker_failed:{scope}:{room_id}:{reason}")

    async def _fail_closed_thread_invalidation(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
        stale_marker_error: Exception,
    ) -> None:
        try:
            await self.runtime.event_cache.invalidate_thread(room_id, thread_id)
        except Exception as invalidate_exc:
            self.logger.warning(
                "Failed to delete cached thread rows after stale-marker failure; disabling cache",
                room_id=room_id,
                thread_id=thread_id,
                reason=reason,
                stale_marker_error=str(stale_marker_error),
                error=str(invalidate_exc),
            )
        else:
            return
        self._disable_cache_after_fail_closed_invalidation(
            room_id=room_id,
            reason=reason,
            scope=f"thread:{thread_id}",
        )

    async def _fail_closed_room_invalidation(
        self,
        room_id: str,
        *,
        reason: str,
        stale_marker_error: Exception,
    ) -> None:
        try:
            await self.runtime.event_cache.invalidate_room_threads(room_id)
        except Exception as invalidate_exc:
            self.logger.warning(
                "Failed to delete cached room thread rows after stale-marker failure; disabling cache",
                room_id=room_id,
                reason=reason,
                stale_marker_error=str(stale_marker_error),
                error=str(invalidate_exc),
            )
        else:
            return
        self._disable_cache_after_fail_closed_invalidation(
            room_id=room_id,
            reason=reason,
            scope="room",
        )


class _ThreadOutboundWrites:
    """Own advisory bookkeeping for locally sent thread mutations."""

    def __init__(
        self,
        *,
        resolver: ThreadMutationResolver,
        cache_ops: _ThreadMutationCacheOps,
        require_client: Callable[[], nio.AsyncClient],
    ) -> None:
        self._resolver = resolver
        self._cache_ops = cache_ops
        self._require_client = require_client

    async def _apply_outbound_message_notification(
        self,
        room_id: str,
        event_id: str,
        event_source: dict[str, Any],
        event_info: EventInfo,
    ) -> None:
        impact = await self._resolver.resolve_thread_impact_for_mutation(
            room_id,
            event_info=event_info,
            event_id=event_id,
            context="outbound",
        )
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            self._cache_ops.logger.debug(
                "Skipping outbound thread cache bookkeeping for non-threaded message mutation",
                room_id=room_id,
                event_id=event_id,
                original_event_id=event_info.original_event_id,
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            await self._cache_ops.invalidate_room_threads(
                room_id,
                reason="outbound_thread_lookup_unavailable",
            )
            return
        assert impact.thread_id is not None
        await self._cache_ops.invalidate_known_thread(
            room_id,
            impact.thread_id,
            reason="outbound_thread_mutation",
        )
        await self._cache_ops.append_event_to_cache(
            room_id,
            impact.thread_id,
            event_source,
            context="outbound",
        )

    def notify_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Schedule advisory bookkeeping for one locally sent threaded message or edit."""
        if not self._cache_ops.cache_runtime_available():
            return
        if not isinstance(event_id, str) or not event_id:
            return

        client = self._require_client()
        sender = client.user_id if isinstance(client.user_id, str) else None
        origin_server_ts = int(time.time() * 1000)
        event_source = _normalize_event_source_for_cache(
            {
                "type": "m.room.message",
                "room_id": room_id,
                "event_id": event_id,
                "sender": sender,
                "origin_server_ts": origin_server_ts,
                "content": dict(content),
            },
            event_id=event_id,
            sender=sender,
            origin_server_ts=origin_server_ts,
        )
        event_info = EventInfo.from_event(event_source)
        if not is_thread_affecting_relation(event_info):
            return

        self._schedule_fail_open_room_update(
            room_id,
            lambda: self._apply_outbound_message_notification(room_id, event_id, event_source, event_info),
            name="matrix_cache_notify_outbound_message",
            cancelled_message="Ignoring cancelled outbound threaded message cache bookkeeping after successful send",
            failure_message="Ignoring outbound threaded message cache bookkeeping failure after successful send",
            log_context={"event_id": event_id},
        )

    async def _apply_outbound_redaction_notification(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        impact = await self._resolver.resolve_redaction_thread_impact(
            room_id,
            redacted_event_id,
            failure_message="Ignoring outbound Matrix redaction cache lookup failure after successful redact",
        )
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            self._cache_ops.logger.debug(
                "Skipping outbound thread cache bookkeeping for non-threaded redaction",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
            )
            return
        thread_id = impact.thread_id
        redacted = await self._cache_ops.redact_cached_event(
            room_id,
            redacted_event_id,
            thread_id=thread_id,
            failure_message="Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
        )
        await self._cache_ops.invalidate_after_redaction(
            room_id,
            impact=impact,
            redacted=redacted,
            success_reason="outbound_redaction",
            failure_reason="outbound_redaction_failed",
            lookup_unavailable_reason="outbound_redaction_lookup_unavailable",
        )

    def notify_outbound_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        """Schedule advisory bookkeeping for one locally redacted threaded message."""
        if not self._cache_ops.cache_runtime_available():
            return
        if not redacted_event_id:
            return

        self._schedule_fail_open_room_update(
            room_id,
            lambda: self._apply_outbound_redaction_notification(room_id, redacted_event_id),
            name="matrix_cache_notify_outbound_redaction",
            cancelled_message="Ignoring cancelled outbound Matrix redaction cache bookkeeping after successful redact",
            failure_message="Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
            log_context={"redacted_event_id": redacted_event_id},
        )

    def _schedule_fail_open_room_update(
        self,
        room_id: str,
        update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
        *,
        name: str,
        cancelled_message: str,
        failure_message: str,
        log_context: dict[str, object],
    ) -> None:
        async def safe_update() -> None:
            try:
                await update_coro_factory()
            except asyncio.CancelledError as exc:
                self._cache_ops.logger.warning(
                    cancelled_message,
                    room_id=room_id,
                    error=str(exc),
                    **log_context,
                )
            except Exception as exc:
                self._cache_ops.logger.warning(
                    failure_message,
                    room_id=room_id,
                    error=str(exc),
                    **log_context,
                )

        try:
            self._cache_ops.queue_room_cache_update(
                room_id,
                safe_update,
                name=name,
            )
        except asyncio.CancelledError as exc:
            self._cache_ops.logger.warning(
                cancelled_message,
                room_id=room_id,
                error=str(exc),
                **log_context,
            )
        except Exception as exc:
            self._cache_ops.logger.warning(
                failure_message,
                room_id=room_id,
                error=str(exc),
                **log_context,
            )


class _ThreadLiveWrites:
    """Own live-event and live-redaction thread cache mutations."""

    def __init__(
        self,
        *,
        resolver: ThreadMutationResolver,
        cache_ops: _ThreadMutationCacheOps,
    ) -> None:
        self._resolver = resolver
        self._cache_ops = cache_ops

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""
        if not self._cache_ops.cache_runtime_available():
            return

        impact = await self._resolver.resolve_thread_impact_for_mutation(
            room_id,
            event_info=event_info,
            event_id=event.event_id,
            context="live",
        )
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            self._cache_ops.logger.debug(
                "Skipping live thread cache bookkeeping for known non-threaded message mutation",
                room_id=room_id,
                event_id=event.event_id,
                original_event_id=event_info.original_event_id,
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            await self._cache_ops.invalidate_room_threads(
                room_id,
                reason="live_thread_lookup_unavailable",
            )
            return
        assert impact.thread_id is not None
        thread_id = impact.thread_id
        event_source = normalize_nio_event_for_cache(event)

        async def append_and_invalidate() -> bool:
            await self._cache_ops.invalidate_known_thread(
                room_id,
                thread_id,
                reason="live_thread_mutation",
            )
            appended = await self._cache_ops.append_event_to_cache(
                room_id,
                thread_id,
                event_source,
                context="live",
            )
            if not appended:
                await self._cache_ops.invalidate_known_thread(
                    room_id,
                    thread_id,
                    reason="live_append_failed",
                )
            return appended

        await self._cache_ops.queue_room_cache_update(
            room_id,
            append_and_invalidate,
            name="matrix_cache_append_live_event",
        )

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        if not self._cache_ops.cache_runtime_available():
            return
        impact = await self._resolver.resolve_redaction_thread_impact(
            room_id,
            event.redacts,
            failure_message="Failed to resolve cached thread for redaction",
            event_id=event.event_id,
        )
        thread_id = impact.thread_id

        async def redact_and_invalidate() -> bool:
            redacted = await self._cache_ops.redact_cached_event(
                room_id,
                event.redacts,
                thread_id=thread_id,
                failure_message="Failed to apply live redaction to cache",
            )
            await self._cache_ops.invalidate_after_redaction(
                room_id,
                impact=impact,
                redacted=redacted,
                success_reason="live_redaction",
                failure_reason="live_redaction_failed",
                lookup_unavailable_reason="live_redaction_lookup_unavailable",
            )
            return redacted

        await self._cache_ops.queue_room_cache_update(
            room_id,
            redact_and_invalidate,
            name="matrix_cache_apply_redaction",
        )


class _ThreadSyncWrites:
    """Own sync timeline grouping, persistence, and mutation handling."""

    def __init__(
        self,
        *,
        resolver: ThreadMutationResolver,
        cache_ops: _ThreadMutationCacheOps,
    ) -> None:
        self._resolver = resolver
        self._cache_ops = cache_ops

    async def _persist_threaded_sync_events(
        self,
        room_id: str,
        threaded_events: Sequence[dict[str, object]],
        *,
        resolution_context: MutationResolutionContext,
    ) -> None:
        room_threads_invalidated = False
        for event_source in threaded_events:
            event_info = EventInfo.from_event(event_source)
            event_id = event_source.get("event_id")
            impact = await self._resolver.resolve_thread_impact_for_mutation(
                room_id,
                event_info=event_info,
                event_id=event_id if isinstance(event_id, str) else None,
                context="sync",
                resolution_context=resolution_context,
            )
            if impact.state is MutationThreadImpactState.ROOM_LEVEL:
                self._cache_ops.logger.debug(
                    "Skipping sync thread cache bookkeeping for known non-threaded message mutation",
                    room_id=room_id,
                    event_id=event_id,
                    original_event_id=event_info.original_event_id,
                )
                continue
            if impact.state is MutationThreadImpactState.UNKNOWN:
                if not room_threads_invalidated:
                    await self._cache_ops.invalidate_room_threads(
                        room_id,
                        reason="sync_thread_lookup_unavailable",
                    )
                    room_threads_invalidated = True
                continue
            assert impact.thread_id is not None
            await self._cache_ops.invalidate_known_thread(
                room_id,
                impact.thread_id,
                reason="sync_thread_mutation",
            )
            appended = await self._cache_ops.append_event_to_cache(
                room_id,
                impact.thread_id,
                event_source,
                context="sync",
            )
            if not appended:
                await self._cache_ops.invalidate_known_thread(
                    room_id,
                    impact.thread_id,
                    reason="sync_append_failed",
                )

    async def _apply_sync_redactions(
        self,
        room_id: str,
        redacted_event_ids: Sequence[str],
        *,
        resolution_context: MutationResolutionContext,
    ) -> None:
        room_threads_invalidated = False
        for redacted_event_id in redacted_event_ids:
            impact = await self._resolver.resolve_redaction_thread_impact(
                room_id,
                redacted_event_id,
                failure_message="Failed to resolve cached thread for sync redaction",
                resolution_context=resolution_context,
            )
            thread_id = impact.thread_id
            redacted = await self._cache_ops.redact_cached_event(
                room_id,
                redacted_event_id,
                thread_id=thread_id,
                failure_message="Failed to apply sync redaction to cache",
            )
            if impact.state is MutationThreadImpactState.UNKNOWN:
                if not room_threads_invalidated:
                    await self._cache_ops.invalidate_room_threads(
                        room_id,
                        reason="sync_redaction_lookup_unavailable",
                    )
                    room_threads_invalidated = True
                continue
            await self._cache_ops.invalidate_after_redaction(
                room_id,
                impact=impact,
                redacted=redacted,
                success_reason="sync_redaction",
                failure_reason="sync_redaction_failed",
                lookup_unavailable_reason="sync_redaction_lookup_unavailable",
            )

    async def _persist_room_sync_timeline_updates(
        self,
        room_id: str,
        plain_events: Sequence[dict[str, object]],
        threaded_events: Sequence[dict[str, object]],
        redacted_event_ids: Sequence[str],
    ) -> None:
        plain_batch = [
            (event_id, room_id, event_source)
            for event_source in plain_events
            if isinstance((event_id := event_source.get("event_id")), str) and event_id
        ]
        threaded_batch = [
            (event_id, room_id, event_source)
            for event_source in threaded_events
            if isinstance((event_id := event_source.get("event_id")), str) and event_id
        ]
        await self._cache_ops.store_events_batch(
            room_id,
            plain_batch,
            failure_message="Failed to persist sync events to cache",
        )
        await self._cache_ops.store_events_batch(
            room_id,
            threaded_batch,
            failure_message="Failed to persist sync threaded events to cache",
        )
        resolution_context = await self._resolver.build_sync_mutation_resolution_context(
            room_id,
            plain_events=plain_events,
            threaded_events=threaded_events,
        )
        await self._persist_threaded_sync_events(
            room_id,
            threaded_events,
            resolution_context=resolution_context,
        )
        await self._apply_sync_redactions(
            room_id,
            redacted_event_ids,
            resolution_context=resolution_context,
        )

    def _group_sync_timeline_updates(
        self,
        response: nio.SyncResponse,
    ) -> tuple[
        dict[str, list[dict[str, object]]],
        dict[str, list[dict[str, object]]],
        dict[str, list[str]],
    ]:
        room_threaded_events: dict[str, list[dict[str, object]]] = {}
        room_plain_events: dict[str, list[dict[str, object]]] = {}
        room_redactions: dict[str, list[str]] = {}

        joined_rooms = response.rooms.join if isinstance(response.rooms.join, dict) else {}
        for room_id, room_info in joined_rooms.items():
            timeline = room_info.timeline if room_info is not None else None
            events = timeline.events if timeline is not None else ()
            if not isinstance(events, list):
                continue
            for event in events:
                _collect_sync_timeline_cache_updates(
                    room_id,
                    event,
                    room_threaded_events=room_threaded_events,
                    room_plain_events=room_plain_events,
                    room_redactions=room_redactions,
                )
        return room_plain_events, room_threaded_events, room_redactions

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        if not self._cache_ops.cache_runtime_available():
            return
        room_plain_events, room_threaded_events, room_redactions = self._group_sync_timeline_updates(response)
        for room_id in set(room_plain_events) | set(room_threaded_events) | set(room_redactions):
            plain_events = room_plain_events.get(room_id, ())
            threaded_events = room_threaded_events.get(room_id, ())
            redacted_event_ids = room_redactions.get(room_id, ())
            self._cache_ops.queue_room_cache_update(
                room_id,
                lambda room_id=room_id,
                plain_events=plain_events,
                threaded_events=threaded_events,
                redacted_event_ids=redacted_event_ids: self._persist_room_sync_timeline_updates(
                    room_id,
                    plain_events,
                    threaded_events,
                    redacted_event_ids,
                ),
                name="matrix_cache_sync_timeline",
            )


class _ThreadWrites:
    """Private thread-write helper owned by MatrixConversationCache."""

    def __init__(
        self,
        *,
        logger_getter: Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        require_client: Callable[[], nio.AsyncClient],
        fetch_event_info_for_thread_resolution: Callable[[str, str], Awaitable[EventInfo | None]],
    ) -> None:
        self._resolver = ThreadMutationResolver(
            logger_getter=logger_getter,
            runtime=runtime,
            fetch_event_info_for_thread_resolution=fetch_event_info_for_thread_resolution,
        )
        self._cache_ops = _ThreadMutationCacheOps(
            logger_getter=logger_getter,
            runtime=runtime,
        )
        self._outbound = _ThreadOutboundWrites(
            resolver=self._resolver,
            cache_ops=self._cache_ops,
            require_client=require_client,
        )
        self._live = _ThreadLiveWrites(
            resolver=self._resolver,
            cache_ops=self._cache_ops,
        )
        self._sync = _ThreadSyncWrites(
            resolver=self._resolver,
            cache_ops=self._cache_ops,
        )

    def notify_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Schedule one locally sent threaded message or edit and fail open on bookkeeping errors."""
        self._run_fail_open_outbound_write(
            lambda: self._outbound.notify_outbound_message(room_id, event_id, content),
            cancelled_message="Ignoring cancelled outbound threaded message cache bookkeeping after successful send",
            failure_message="Ignoring outbound threaded message cache bookkeeping failure after successful send",
            room_id=room_id,
            event_id=event_id,
        )

    def notify_outbound_redaction(self, room_id: str, redacted_event_id: str) -> None:
        """Schedule one locally redacted threaded message and fail open on bookkeeping errors."""
        self._run_fail_open_outbound_write(
            lambda: self._outbound.notify_outbound_redaction(room_id, redacted_event_id),
            cancelled_message="Ignoring cancelled outbound threaded message cache redaction bookkeeping after successful redact",
            failure_message="Ignoring outbound threaded message cache redaction bookkeeping failure after successful redact",
            room_id=room_id,
            redacted_event_id=redacted_event_id,
        )

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
            self._cache_ops.logger.warning(
                cancelled_message,
                room_id=room_id,
                error=str(exc),
                **log_context,
            )
        except Exception as exc:
            self._cache_ops.logger.warning(
                failure_message,
                room_id=room_id,
                error=str(exc),
                **log_context,
            )


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
    _reads: _ThreadReads = field(init=False, repr=False)
    _writes: _ThreadWrites = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind private read and write helpers to this facade."""
        self._writes = _ThreadWrites(
            logger_getter=lambda: self.logger,
            runtime=self.runtime,
            require_client=self._require_client,
            fetch_event_info_for_thread_resolution=self._event_info_for_thread_resolution,
        )
        self._reads = _ThreadReads(
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
            runtime_started_at=self.runtime.runtime_started_at,
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
            runtime_started_at=self.runtime.runtime_started_at,
        )

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
        )

    async def get_thread_history(
        self,
        room_id: str,
        thread_id: str,
    ) -> ThreadReadResult:
        """Resolve advisory full thread history for one conversation root."""
        return await self._read_thread_memoized(
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
        """Resolve strict dispatch thread context using only fresh cache data or a homeserver refill."""
        return await self._read_thread_memoized(
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
        """Resolve strict full dispatch thread history using only fresh cache data or a homeserver refill."""
        return await self._read_thread_memoized(
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
        self._writes.notify_outbound_message(room_id, event_id, content)

    def notify_outbound_redaction(self, room_id: str, redacted_event_id: str) -> None:
        """Schedule one locally redacted threaded message for advisory cache bookkeeping."""
        self._writes.notify_outbound_redaction(room_id, redacted_event_id)

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

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        self._writes.cache_sync_timeline(response)
