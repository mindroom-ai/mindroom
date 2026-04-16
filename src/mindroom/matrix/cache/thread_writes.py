"""Thread mutation grouping and advisory bookkeeping for Matrix conversation cache."""

from __future__ import annotations

import asyncio
import time
import typing
from typing import TYPE_CHECKING, Any

import nio

from mindroom.matrix.cache.event_cache_events import (
    normalize_event_source_for_cache,
    normalize_nio_event_for_cache,
)
from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
from mindroom.matrix.cache.thread_write_resolution import (
    MutationResolutionContext,
    MutationThreadImpactState,
    ThreadMutationResolver,
    is_thread_affecting_relation,
)
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


__all__ = ["ThreadWritePolicy", "_collect_sync_timeline_cache_updates"]


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


class ThreadOutboundWritePolicy:
    """Own advisory bookkeeping for locally sent thread mutations."""

    def __init__(
        self,
        *,
        resolver: ThreadMutationResolver,
        cache_ops: ThreadMutationCacheOps,
        require_client: typing.Callable[[], nio.AsyncClient],
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
        event_source = normalize_event_source_for_cache(
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
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
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


class ThreadLiveWritePolicy:
    """Own live-event and live-redaction thread cache mutations."""

    def __init__(
        self,
        *,
        resolver: ThreadMutationResolver,
        cache_ops: ThreadMutationCacheOps,
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


class ThreadSyncWritePolicy:
    """Own sync timeline grouping, persistence, and mutation handling."""

    def __init__(
        self,
        *,
        resolver: ThreadMutationResolver,
        cache_ops: ThreadMutationCacheOps,
    ) -> None:
        self._resolver = resolver
        self._cache_ops = cache_ops

    async def _persist_threaded_sync_events(
        self,
        room_id: str,
        threaded_events: typing.Sequence[dict[str, object]],
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
        redacted_event_ids: typing.Sequence[str],
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
        plain_events: typing.Sequence[dict[str, object]],
        threaded_events: typing.Sequence[dict[str, object]],
        redacted_event_ids: typing.Sequence[str],
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


class ThreadWritePolicy:
    """Own the public thread-write boundary for one conversation cache."""

    def __init__(
        self,
        *,
        logger_getter: typing.Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        require_client: typing.Callable[[], nio.AsyncClient],
        fetch_event_info_for_thread_resolution: typing.Callable[[str, str], typing.Awaitable[EventInfo | None]],
    ) -> None:
        self._resolver = ThreadMutationResolver(
            logger_getter=logger_getter,
            runtime=runtime,
            fetch_event_info_for_thread_resolution=fetch_event_info_for_thread_resolution,
        )
        self._cache_ops = ThreadMutationCacheOps(
            logger_getter=logger_getter,
            runtime=runtime,
        )
        self._outbound = ThreadOutboundWritePolicy(
            resolver=self._resolver,
            cache_ops=self._cache_ops,
            require_client=require_client,
        )
        self._live = ThreadLiveWritePolicy(
            resolver=self._resolver,
            cache_ops=self._cache_ops,
        )
        self._sync = ThreadSyncWritePolicy(
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
        callback: typing.Callable[[], None],
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
