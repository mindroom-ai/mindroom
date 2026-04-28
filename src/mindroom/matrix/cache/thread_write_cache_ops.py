"""Cache mutation operations for Matrix thread cache writes."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mindroom.matrix.thread_bookkeeping import MutationThreadImpact, MutationThreadImpactState

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Coroutine, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


class ThreadMutationCacheOps:
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
        return (
            self.runtime.event_cache is not None
            and self.runtime.event_cache_write_coordinator is not None
            and self.runtime.event_cache.durable_writes_available
        )

    def queue_room_cache_update(
        self,
        room_id: str,
        update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
        *,
        name: str,
        emit_timing: bool = False,
        coalesce_key: tuple[str, str] | None = None,
        coalesce_log_context: dict[str, object] | None = None,
    ) -> asyncio.Task[object]:
        """Run one cache mutation under the room-ordered write barrier."""
        coordinator = self.runtime.event_cache_write_coordinator
        return coordinator.queue_room_update(
            room_id,
            update_coro_factory,
            name=name,
            emit_timing=emit_timing,
            coalesce_key=coalesce_key,
            coalesce_log_context=coalesce_log_context,
        )

    def queue_thread_cache_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
        *,
        name: str,
        emit_timing: bool = False,
        coalesce_key: tuple[str, str] | None = None,
        coalesce_log_context: dict[str, object] | None = None,
    ) -> asyncio.Task[object]:
        """Run one thread-specific cache mutation under the same-thread write barrier."""
        coordinator = self.runtime.event_cache_write_coordinator
        return coordinator.queue_thread_update(
            room_id,
            thread_id,
            update_coro_factory,
            name=name,
            emit_timing=emit_timing,
            coalesce_key=coalesce_key,
            coalesce_log_context=coalesce_log_context,
        )

    async def store_events_batch(
        self,
        room_id: str,
        batch: Sequence[tuple[str, str, dict[str, object]]],
        *,
        failure_message: str,
        raise_on_failure: bool = False,
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
            if raise_on_failure:
                raise

    async def redact_cached_event(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        thread_id: str | None,
        failure_message: str,
        raise_on_failure: bool = False,
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
            if raise_on_failure:
                raise
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
        raise_on_failure: bool = False,
    ) -> None:
        """Apply the post-redaction invalidation policy for one resolved impact."""
        if impact.state is MutationThreadImpactState.THREADED:
            assert impact.thread_id is not None
            await self.invalidate_known_thread(
                room_id,
                impact.thread_id,
                reason=success_reason if redacted else failure_reason,
                raise_on_failure=raise_on_failure,
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN and redacted:
            await self.invalidate_room_threads(
                room_id,
                reason=lookup_unavailable_reason,
                raise_on_failure=raise_on_failure,
            )

    async def invalidate_known_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
        raise_on_failure: bool = False,
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
            if raise_on_failure:
                raise

    async def invalidate_room_threads(
        self,
        room_id: str,
        *,
        reason: str,
        raise_on_failure: bool = False,
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
            if raise_on_failure:
                raise

    async def append_event_to_cache(
        self,
        room_id: str,
        thread_id: str,
        event_source: dict[str, Any],
        *,
        context: str,
        raise_on_failure: bool = False,
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
            if raise_on_failure:
                raise
            return False
        if not appended:
            self.logger.debug(
                "Skipping thread event append because raw thread cache is missing",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                context=context,
            )
            return False
        try:
            await self.runtime.event_cache.revalidate_thread_after_incremental_update(
                room_id,
                thread_id,
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to refresh thread cache validation after incremental update",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                context=context,
                error=str(exc),
            )
            if raise_on_failure:
                raise
        return True

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
