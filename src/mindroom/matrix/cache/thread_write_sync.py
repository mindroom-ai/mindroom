"""Sync timeline ingestion for Matrix thread cache writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

from mindroom.matrix.cache.event_cache import normalize_nio_event_for_cache
from mindroom.matrix.cache.thread_write_resolution import (
    MutationThreadImpactState,
    is_thread_affecting_relation,
)
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
    from mindroom.matrix.cache.thread_write_resolution import MutationResolutionContext, ThreadMutationResolver


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
