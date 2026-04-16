"""Live mutation handling for Matrix thread cache writes."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.cache.event_cache import normalize_nio_event_for_cache
from mindroom.matrix.thread_bookkeeping import MutationThreadImpactState

if TYPE_CHECKING:
    import nio

    from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
    from mindroom.matrix.event_info import EventInfo
    from mindroom.matrix.thread_bookkeeping import ThreadMutationResolver


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
