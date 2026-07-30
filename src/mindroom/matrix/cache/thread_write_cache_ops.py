"""Cache mutation operations for Matrix thread cache writes.

This is the application layer below the write policies in ``thread_writes``; it owns how mutations land:

1. Gap marking is durable-marker-first and fails closed: ``mark_thread_gap`` and
   ``mark_room_threads_gap`` write monotonic gap markers; when a marker cannot be written the rows
   are deleted instead, and when even deletion fails (and the backend is not just temporarily
   unavailable) the cache is disabled for the rest of the runtime.

2. Appends are incremental-only and atomic: ``apply_thread_mutation_append`` appends the event and,
   when the append cannot land, records the gap marker in the same transaction. It refuses when the
   thread has no cached snapshot rows (then only recording lookup-index rows), so a partial snapshot
   is never served and a mutation that succeeds is never observably gapped in between.
   That transaction carries the marker, so when it rolls back there is no marker either; the append
   path then writes one through the same fail-closed ladder as rule 1 rather than leaving a thread
   readable while it is missing the mutation.

3. A successful append clears nothing. An append extends a snapshot; it does not prove the snapshot
   complete, and only a full replacement can clear a gap marker.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import create_background_task
from mindroom.matrix.thread_bookkeeping import MutationThreadImpact, MutationThreadImpactState

from .thread_cache_gap import mark_room_threads_gap_fail_closed, mark_thread_gap_fail_closed
from .thread_cache_state import ThreadAppendOutcome

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


class ThreadMutationCacheOps:
    """Own queueing, gap marking, and cache writes for thread mutations."""

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

    def cache_runtime_diagnostics(self) -> dict[str, object]:
        """Return log-safe event-cache runtime diagnostics for sync certification."""
        if self.runtime.event_cache is None:
            return {"cache_backend": "none"}
        return self.runtime.event_cache.runtime_diagnostics()

    def cache_principal_id(self) -> str:
        """Return principal namespace owning outbound write reservations."""
        assert self.runtime.event_cache is not None
        return self.runtime.event_cache.principal_id

    def pending_durable_write_room_ids(self) -> tuple[str, ...]:
        """Return rooms with runtime-only writes that must persist before sync certification."""
        if self.runtime.event_cache is None:
            return ()
        return self.runtime.event_cache.pending_durable_write_room_ids()

    def queue_pending_durable_write_flushes(self) -> tuple[asyncio.Task[object], ...]:
        """Queue flushes for runtime-only writes that are not tied to the current sync response."""
        event_cache = self.runtime.event_cache
        if event_cache is None or not self.cache_runtime_available():
            return ()
        return tuple(
            (
                self.queue_room_cache_update(
                    room_id,
                    lambda room_id=room_id: event_cache.flush_pending_durable_writes(room_id),
                    name="matrix_cache_flush_pending_writes",
                )
            )
            for room_id in event_cache.pending_durable_write_room_ids()
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
        event_cache = self.runtime.event_cache
        coordinator = self.runtime.event_cache_write_coordinator
        scoped_coalesce_key = (
            None if coalesce_key is None else (f"{event_cache.principal_id}:{coalesce_key[0]}", coalesce_key[1])
        )
        return coordinator.queue_room_update(
            room_id,
            update_coro_factory,
            name=name,
            emit_timing=emit_timing,
            coalesce_key=scoped_coalesce_key,
            coalesce_log_context=coalesce_log_context,
            coordination_scope=event_cache.principal_id,
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
        event_cache = self.runtime.event_cache
        coordinator = self.runtime.event_cache_write_coordinator
        scoped_coalesce_key = (
            None if coalesce_key is None else (f"{event_cache.principal_id}:{coalesce_key[0]}", coalesce_key[1])
        )
        return coordinator.queue_thread_update(
            room_id,
            thread_id,
            update_coro_factory,
            name=name,
            emit_timing=emit_timing,
            coalesce_key=scoped_coalesce_key,
            coalesce_log_context=coalesce_log_context,
            coordination_scope=event_cache.principal_id,
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

    async def purge_room(self, room_id: str) -> None:
        """Delete this bot principal's cache rows after an authoritative departure."""
        try:
            await self.runtime.event_cache.purge_room(room_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to purge principal-owned Matrix event cache room; deletion remains pending",
                room_id=room_id,
                error=str(exc),
            )

    def mark_room_departed(self, room_id: str) -> int:
        """Fence reads, queue durable cleanup, and return the new room epoch."""
        return self.runtime.event_cache.mark_room_departed(room_id)

    def room_departure_epoch(self, room_id: str) -> int:
        """Return the durable cache's current room-fence epoch."""
        return self.runtime.event_cache.room_departure_epoch(room_id)

    async def mark_room_joined(self, room_id: str, *, expected_departure_epoch: int) -> None:
        """Lift one departed-room fence after an authoritative rejoin."""
        await self.runtime.event_cache.mark_room_joined(
            room_id,
            expected_departure_epoch=expected_departure_epoch,
        )

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
        """Mark a gap against one cached thread and fail closed if the marker cannot be written."""
        await mark_thread_gap_fail_closed(
            self.runtime.event_cache,
            room_id=room_id,
            thread_id=thread_id,
            reason=reason,
            logger=self.logger,
            raise_on_failure=raise_on_failure,
        )

    async def invalidate_room_threads(
        self,
        room_id: str,
        *,
        reason: str,
        raise_on_failure: bool = False,
    ) -> None:
        """Mark a gap against one room's cached threads and fail closed if the marker cannot be written."""
        await mark_room_threads_gap_fail_closed(
            self.runtime.event_cache,
            room_id=room_id,
            reason=reason,
            logger=self.logger,
            raise_on_failure=raise_on_failure,
        )

    async def append_event_to_cache(
        self,
        room_id: str,
        thread_id: str,
        event_source: dict[str, Any],
        *,
        context: str,
        append_failed_reason: str,
        raise_on_failure: bool = False,
    ) -> bool:
        """Append one event into a cached thread fail-open and report whether a row changed.

        Appending, and gap-marking when the append cannot land, happen in one durable operation.
        Splitting them used to leave a snapshot readable while it was missing the event.
        """
        event_id = event_source.get("event_id")
        try:
            outcome = await self.runtime.event_cache.apply_thread_mutation_append(
                room_id,
                thread_id,
                event_source,
                append_failed_reason=append_failed_reason,
            )
        except (Exception, asyncio.CancelledError) as exc:
            self.logger.warning(
                "Failed to append thread event to cache",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                context=context,
                error=str(exc),
            )
            # The atomic operation rolled back, so it wrote no marker either. Without one, a thread
            # that was readable before this mutation stays readable while missing the event, so the
            # marker has to be written separately and fail closed exactly as pre-marking did.
            # Cancellation rolls the transaction back the same way and is not an ``Exception``, so it
            # is caught here too.
            await self._write_append_failure_marker(room_id, thread_id, reason=append_failed_reason)
            if raise_on_failure or isinstance(exc, asyncio.CancelledError):
                raise
            return False

        if outcome is ThreadAppendOutcome.SNAPSHOT_MISSING:
            self.logger.debug(
                "Skipping thread event append because raw thread cache is missing",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event_id,
                context=context,
            )
        # An append that did not land leaves the thread gap-marked, and the next read refetches
        # it from the homeserver. Nothing is scheduled here: the marker is the whole recovery.
        return outcome.wrote_event

    async def _write_append_failure_marker(self, room_id: str, thread_id: str, *, reason: str) -> None:
        """Persist the marker a rolled-back append owes, surviving the cancellation that caused it.

        Shielding alone is not enough: it keeps the write running but leaves it untracked, so
        ``close`` returns without it and a marker abandoned mid-shutdown is the fail-open this
        handler exists to prevent. Owning the task puts it in the set ``close`` waits on.

        It is owned separately from ordinary cache writes because the common way to owe a marker is
        for the drain to cancel the append: by then the shared budget is spent, and a marker put in
        the same set would simply be cancelled by the next round. ``close`` drains this owner after
        that one, on its own budget.
        """
        # Every caller reaches an append only through `cache_runtime_available`, and the queue
        # helpers dereference the coordinator unguarded, so a missing one is a bug, not a mode.
        coordinator = self.runtime.event_cache_write_coordinator
        assert coordinator is not None
        await asyncio.shield(
            create_background_task(
                self.invalidate_known_thread(room_id, thread_id, reason=reason),
                name="matrix_cache_append_failure_marker",
                owner=coordinator.failure_marker_task_owner,
            ),
        )
