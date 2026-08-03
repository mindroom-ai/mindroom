"""Thread mutation grouping and advisory bookkeeping for Matrix conversation cache.

These three policies are the only writers of durable thread-cache state:

1. ``ThreadOutboundWritePolicy`` records locally sent events after Matrix delivery succeeded, so it must
   fail open: every cancellation or exception is swallowed and logged, never re-raised to the sender.

2. ``ThreadLiveWritePolicy`` and ``ThreadSyncWritePolicy`` record homeserver timeline events; the sync
   policy can additionally run in fail-closed mode (``raise_on_cache_write_failure``) so sync-token
   certification only certifies responses whose writes durably landed.

3. Barrier routing: mutations whose thread is known pre-queue run on the per-thread barrier.
   Locally streamed responses reserve their certified thread identity before relation-free edits begin,
   so those edits avoid cache lookups and retain the per-thread barrier.
   Mutations whose identity remains unknown stay on the room barrier because earlier queued writes can
   create the lookup rows they depend on.

4. UNKNOWN-impact mutations gap-mark the whole room's cached threads eagerly, outside the per-thread
   queue: the mutation's thread is unknown, so no per-thread barrier can cover it.

5. Within one sync batch, UNKNOWN impacts gap-mark the room at most once per pass (once across the
   message pass and once across the redaction pass); later UNKNOWN mutations in the same pass reuse
   that marker instead of writing duplicates.

6. A limited sync timeline gap-marks the room before any partial-window event is admitted.

7. A still-opaque ``m.room.encrypted`` mutation with a known thread never appends into the snapshot;
   it only gap-marks that thread, so the snapshot stays rejected until a decryption-capable refresh
   replaces it.

8. Every other threaded mutation appends through one atomic cache operation that also records the gap
   marker when the append cannot land, so a snapshot is never readable while missing the event.
   Point rows and explicit relation indexes are still persisted by the batch store, and unknown-impact
   opaque mutations fail closed through the standard room-scope marker.
"""

from __future__ import annotations

import asyncio
import time
import typing
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import nio

from mindroom.constants import (
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_PENDING,
    STREAM_STATUS_STREAMING,
)
from mindroom.matrix.event_info import EventInfo, is_thread_affecting_relation
from mindroom.matrix.sync_certification import SyncCacheWriteResult
from mindroom.matrix.thread_bookkeeping import (
    MutationResolutionContext,
    MutationThreadImpact,
    MutationThreadImpactState,
    MutationWriteContext,
    ThreadMutationResolver,
)
from mindroom.timing import elapsed_ms_since, emit_timing_event, timing_enabled

from .event_normalization import (
    is_opaque_encrypted_event_source,
    normalize_event_source_for_cache,
    normalize_nio_event_for_cache,
)
from .outbound_thread_reservations import OutboundThreadReservations

if TYPE_CHECKING:
    from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps

__all__ = [
    "ThreadLiveWritePolicy",
    "ThreadOutboundWritePolicy",
    "ThreadSyncWritePolicy",
]


_NONTERMINAL_STREAM_STATUSES = frozenset({STREAM_STATUS_PENDING, STREAM_STATUS_STREAMING})
_TERMINAL_STREAM_STATUSES = frozenset(
    {
        STREAM_STATUS_CANCELLED,
        STREAM_STATUS_COMPLETED,
        STREAM_STATUS_ERROR,
    },
)
_LIMITED_SYNC_TIMELINE_REASON = "limited_sync_timeline"
_SYNC_TIMELINE_WRITE_FAILED_REASON = "sync_timeline_write_failed"


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
    event_type = event_source.get("type")
    if is_thread_affecting_relation(
        event_info,
        event_type=event_type if isinstance(event_type, str) else None,
    ):
        cache_update = _collect_sync_event_cache_update(room_id, event)
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


def _outbound_streaming_edit_coalesce_context(
    *,
    event_info: EventInfo,
    event_source: dict[str, object],
) -> tuple[tuple[str, str], dict[str, object]] | None:
    if not event_info.is_edit or not isinstance(event_info.original_event_id, str):
        return None
    content = event_source.get("content")
    if not isinstance(content, dict):
        return None
    new_content = typing.cast("dict[str, object]", content).get("m.new_content")
    if not isinstance(new_content, dict):
        return None
    if typing.cast("dict[str, object]", new_content).get(STREAM_STATUS_KEY) not in _NONTERMINAL_STREAM_STATUSES:
        return None
    return (
        ("outbound_streaming_edit", event_info.original_event_id),
        {
            "stream_status": typing.cast("dict[str, object]", new_content).get(STREAM_STATUS_KEY),
        },
    )


def _outbound_stream_status(
    event_source: dict[str, object],
    *,
    event_info: EventInfo,
) -> object:
    content = event_source.get("content")
    if not isinstance(content, dict):
        return None
    status_content = typing.cast("dict[str, object]", content)
    if event_info.is_edit:
        new_content = status_content.get("m.new_content")
        if isinstance(new_content, dict):
            status_content = typing.cast("dict[str, object]", new_content)
    return status_content.get(STREAM_STATUS_KEY)


def _mutation_reason(
    context: MutationWriteContext,
    suffix: str,
) -> str:
    return f"{context}_{suffix}"


async def _apply_thread_message_mutation(
    *,
    cache_ops: ThreadMutationCacheOps,
    room_id: str,
    event_info: EventInfo,
    impact: MutationThreadImpact,
    event_source: dict[str, Any] | None,
    event_id: str | None,
    context: MutationWriteContext,
    room_level_skip_message: str,
    allow_room_invalidation: bool = True,
    raise_on_cache_write_failure: bool = False,
) -> bool:
    if impact.state is MutationThreadImpactState.ROOM_LEVEL:
        cache_ops.logger.debug(
            room_level_skip_message,
            room_id=room_id,
            event_id=event_id,
            original_event_id=event_info.original_event_id,
        )
        return False
    if impact.state is MutationThreadImpactState.UNKNOWN:
        if not allow_room_invalidation:
            return False
        await cache_ops.invalidate_room_threads(
            room_id,
            reason=_mutation_reason(context, "thread_lookup_unavailable"),
            raise_on_failure=raise_on_cache_write_failure,
        )
        return True
    assert impact.thread_id is not None
    assert event_source is not None
    if is_opaque_encrypted_event_source(event_source):
        # A still-undecryptable payload cannot make the visible snapshot complete, so the thread
        # stays gap-marked until a decryption-capable refresh replaces it.
        await cache_ops.invalidate_known_thread(
            room_id,
            impact.thread_id,
            reason=_mutation_reason(context, "opaque_encrypted_event"),
            raise_on_failure=raise_on_cache_write_failure,
        )
        return False
    await cache_ops.append_event_to_cache(
        room_id,
        impact.thread_id,
        event_source,
        context=context,
        append_failed_reason=_mutation_reason(context, "append_failed"),
        raise_on_failure=raise_on_cache_write_failure,
    )
    return False


async def _resolve_thread_redaction_mutation_impact(
    *,
    resolver: ThreadMutationResolver,
    room_id: str,
    redacted_event_id: str,
    context: MutationWriteContext,
    event_id: str | None = None,
    resolution_context: MutationResolutionContext | None = None,
) -> MutationThreadImpact:
    lookup_failure_message = {
        "outbound": "Ignoring outbound Matrix redaction cache lookup failure after successful redact",
        "live": "Failed to resolve cached thread for redaction",
        "sync": "Failed to resolve cached thread for sync redaction",
    }[context]
    return await resolver.resolve_redaction_thread_impact(
        room_id,
        redacted_event_id,
        failure_message=lookup_failure_message,
        event_id=event_id,
        resolution_context=resolution_context,
    )


async def _apply_thread_redaction_mutation(
    *,
    cache_ops: ThreadMutationCacheOps,
    room_id: str,
    redacted_event_id: str,
    impact: MutationThreadImpact,
    context: MutationWriteContext,
    allow_room_invalidation: bool = True,
    raise_on_cache_write_failure: bool = False,
) -> bool:
    redact_failure_message = {
        "outbound": "Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
        "live": "Failed to apply live redaction to cache",
        "sync": "Failed to apply sync redaction to cache",
    }[context]
    redacted = await cache_ops.redact_cached_event(
        room_id,
        redacted_event_id,
        thread_id=impact.thread_id,
        failure_message=redact_failure_message,
        raise_on_failure=raise_on_cache_write_failure,
    )
    if impact.state is MutationThreadImpactState.UNKNOWN and redacted and not allow_room_invalidation:
        return False
    await cache_ops.invalidate_after_redaction(
        room_id,
        impact=impact,
        redacted=redacted,
        success_reason=_mutation_reason(context, "redaction"),
        failure_reason=_mutation_reason(context, "redaction_failed"),
        lookup_unavailable_reason=_mutation_reason(context, "redaction_lookup_unavailable"),
        raise_on_failure=raise_on_cache_write_failure,
    )
    return impact.state is MutationThreadImpactState.UNKNOWN and redacted


class ThreadOutboundWritePolicy:
    """Own advisory bookkeeping for locally sent thread mutations."""

    def __init__(
        self,
        *,
        resolver: ThreadMutationResolver,
        cache_ops: ThreadMutationCacheOps,
        require_client: typing.Callable[[], nio.AsyncClient],
        reservations: OutboundThreadReservations | None = None,
    ) -> None:
        self._resolver = resolver
        self._cache_ops = cache_ops
        self._require_client = require_client
        self._reservations = reservations or OutboundThreadReservations()

    def _emit_outbound_schedule_timing(
        self,
        *,
        barrier_kind: str,
        event_type: str | None,
        event_info: EventInfo,
        has_coalesce_key: bool,
    ) -> None:
        emit_timing_event(
            "Event cache outbound schedule timing",
            operation="matrix_cache_notify_outbound_event",
            barrier_kind=barrier_kind,
            event_type=event_type,
            is_edit=event_info.is_edit,
            is_reaction=event_info.is_reaction,
            has_coalesce_key=has_coalesce_key,
        )

    async def _apply_outbound_event_notification(
        self,
        room_id: str,
        event_id: str,
        event_source: dict[str, Any],
        event_info: EventInfo,
        *,
        known_thread_id: str | None = None,
    ) -> None:
        impact = (
            MutationThreadImpact.threaded(known_thread_id)
            if known_thread_id is not None
            else await self._resolver.resolve_thread_impact_for_mutation(
                room_id,
                event_info=event_info,
                event_id=event_id,
                context="outbound",
            )
        )
        if impact.state is not MutationThreadImpactState.THREADED:
            await self._cache_ops.store_events_batch(
                room_id,
                [(event_id, room_id, event_source)],
                failure_message="Failed to persist outbound event lookup to cache",
            )
        await _apply_thread_message_mutation(
            cache_ops=self._cache_ops,
            room_id=room_id,
            event_info=event_info,
            impact=impact,
            event_source=event_source,
            event_id=event_id,
            context="outbound",
            room_level_skip_message="Skipping outbound thread cache bookkeeping for non-threaded event mutation",
        )

    def _resolve_prequeue_thread_route(
        self,
        *,
        room_id: str,
        event_id: str,
        event_info: EventInfo,
        stream_status: object,
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve explicit or reserved thread scope before entering a write queue."""
        explicit_thread_id = event_info.thread_id or event_info.thread_id_from_edit
        raw_reservation_event_id = event_info.original_event_id if event_info.is_edit else event_id
        reservation_event_id = raw_reservation_event_id if isinstance(raw_reservation_event_id, str) else None
        needs_reservation_scope = reservation_event_id is not None and (
            stream_status in _NONTERMINAL_STREAM_STATUSES
            or stream_status in _TERMINAL_STREAM_STATUSES
            or (explicit_thread_id is None and event_info.is_edit)
        )
        principal_id = self._cache_ops.cache_principal_id() if needs_reservation_scope else None
        if (
            explicit_thread_id is not None
            and reservation_event_id is not None
            and stream_status in _NONTERMINAL_STREAM_STATUSES
        ):
            assert principal_id is not None
            self._reservations.reserve(
                principal_id,
                room_id,
                reservation_event_id,
                explicit_thread_id,
            )
        reserved_thread_id = None
        if (
            explicit_thread_id is None
            and event_info.is_edit
            and isinstance(event_info.original_event_id, str)
            and principal_id is not None
        ):
            reserved_thread_id = self._reservations.resolve(
                principal_id,
                room_id,
                event_info.original_event_id,
                transitioned_event_id=event_id,
            )
        return explicit_thread_id or reserved_thread_id, principal_id, reservation_event_id

    def _release_terminal_thread_reservation(
        self,
        *,
        principal_id: str | None,
        room_id: str,
        reservation_event_id: str | None,
        stream_status: object,
    ) -> None:
        """Release a terminal claim after its queued update captured thread scope."""
        if principal_id is None or reservation_event_id is None or stream_status not in _TERMINAL_STREAM_STATUSES:
            return
        self._reservations.release(
            principal_id,
            room_id,
            reservation_event_id,
        )

    def notify_outbound_event(
        self,
        room_id: str,
        event_source: dict[str, Any],
    ) -> None:
        """Schedule advisory bookkeeping for one locally sent outbound event."""
        try:
            if not self._cache_ops.cache_runtime_available():
                return
            normalized_event_source = self._normalize_outbound_event_source(room_id, event_source)
            if normalized_event_source is None:
                return
            event_id_value = normalized_event_source.get("event_id")
            if not isinstance(event_id_value, str) or not event_id_value:
                return
            event_id = typing.cast("str", event_id_value)

            event_info = EventInfo.from_event(normalized_event_source)
            stream_status = _outbound_stream_status(
                normalized_event_source,
                event_info=event_info,
            )
            coalesce_context = _outbound_streaming_edit_coalesce_context(
                event_info=event_info,
                event_source=normalized_event_source,
            )
            coalesce_key, coalesce_log_context = coalesce_context if coalesce_context is not None else (None, None)
            event_type_value = normalized_event_source.get("type")
            event_type = event_type_value if isinstance(event_type_value, str) else None
            emit_timing = event_info.is_edit
            if event_info.is_reaction:
                self._emit_outbound_schedule_timing(
                    barrier_kind="room",
                    event_type=event_type,
                    event_info=event_info,
                    has_coalesce_key=coalesce_key is not None,
                )
                persisted_batch: list[tuple[str, str, dict[str, object]]] = [
                    (event_id, room_id, normalized_event_source),
                ]
                self._schedule_fail_open_room_update(
                    room_id,
                    lambda: self._cache_ops.store_events_batch(
                        room_id,
                        persisted_batch,
                        failure_message="Failed to persist outbound reaction lookup to cache",
                    ),
                    name="matrix_cache_notify_outbound_event",
                    cancelled_message="Ignoring cancelled outbound cache bookkeeping after successful send",
                    failure_message="Ignoring outbound cache bookkeeping failure after successful send",
                    log_context={"event_id": event_id},
                    emit_timing=emit_timing,
                )
                return
            if not is_thread_affecting_relation(
                event_info,
                event_type=event_type,
            ):
                persisted_batch = [(event_id, room_id, normalized_event_source)]
                self._schedule_fail_open_room_update(
                    room_id,
                    lambda: self._cache_ops.store_events_batch(
                        room_id,
                        persisted_batch,
                        failure_message="Failed to persist outbound event lookup to cache",
                    ),
                    name="matrix_cache_notify_outbound_event",
                    cancelled_message="Ignoring cancelled outbound cache bookkeeping after successful send",
                    failure_message="Ignoring outbound cache bookkeeping failure after successful send",
                    log_context={"event_id": event_id},
                    emit_timing=emit_timing,
                )
                return
            thread_id, principal_id, reservation_event_id = self._resolve_prequeue_thread_route(
                room_id=room_id,
                event_id=event_id,
                event_info=event_info,
                stream_status=stream_status,
            )
            if thread_id is not None:
                self._emit_outbound_schedule_timing(
                    barrier_kind="thread",
                    event_type=event_type,
                    event_info=event_info,
                    has_coalesce_key=coalesce_key is not None,
                )
                try:
                    self._schedule_fail_open_thread_update(
                        room_id,
                        thread_id,
                        lambda: self._apply_outbound_event_notification(
                            room_id,
                            event_id,
                            normalized_event_source,
                            event_info,
                            known_thread_id=thread_id,
                        ),
                        name="matrix_cache_notify_outbound_event",
                        cancelled_message="Ignoring cancelled outbound cache bookkeeping after successful send",
                        failure_message="Ignoring outbound cache bookkeeping failure after successful send",
                        log_context={"event_id": event_id},
                        emit_timing=emit_timing,
                        coalesce_key=coalesce_key,
                        coalesce_log_context=coalesce_log_context,
                    )
                finally:
                    self._release_terminal_thread_reservation(
                        principal_id=principal_id,
                        room_id=room_id,
                        reservation_event_id=reservation_event_id,
                        stream_status=stream_status,
                    )
                return
            # Truly lookup-dependent outbound mutations stay on the room barrier because earlier
            # outbound writes can create the rows needed to resolve thread impact.
            self._emit_outbound_schedule_timing(
                barrier_kind="room",
                event_type=event_type,
                event_info=event_info,
                has_coalesce_key=coalesce_key is not None,
            )
            self._schedule_fail_open_room_update(
                room_id,
                lambda: self._apply_outbound_event_notification(
                    room_id,
                    event_id,
                    normalized_event_source,
                    event_info,
                ),
                name="matrix_cache_notify_outbound_event",
                cancelled_message="Ignoring cancelled outbound cache bookkeeping after successful send",
                failure_message="Ignoring outbound cache bookkeeping failure after successful send",
                log_context={"event_id": event_id},
                emit_timing=emit_timing,
                coalesce_key=coalesce_key,
                coalesce_log_context=coalesce_log_context,
            )
        except asyncio.CancelledError as exc:
            raw_event_id = event_source.get("event_id")
            self._cache_ops.logger.warning(
                "Ignoring cancelled outbound cache bookkeeping after successful send",
                room_id=room_id,
                event_id=raw_event_id if isinstance(raw_event_id, str) else None,
                error=str(exc),
            )
        except Exception as exc:
            raw_event_id = event_source.get("event_id")
            self._cache_ops.logger.warning(
                "Ignoring outbound cache bookkeeping failure after successful send",
                room_id=room_id,
                event_id=raw_event_id if isinstance(raw_event_id, str) else None,
                error=str(exc),
            )

    def notify_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Schedule advisory bookkeeping for one locally sent message or edit."""
        if not self._cache_ops.cache_runtime_available():
            return
        if not isinstance(event_id, str) or not event_id:
            return

        self.notify_outbound_event(
            room_id,
            {
                "type": "m.room.message",
                "room_id": room_id,
                "event_id": event_id,
                "content": dict(content),
            },
        )

    def reserve_thread_response(self, room_id: str, event_id: str, thread_id: str) -> None:
        """Retain one known response thread before later edits drop relation metadata."""
        if not self._cache_ops.cache_runtime_available():
            return
        self._reservations.reserve(
            self._cache_ops.cache_principal_id(),
            room_id,
            event_id,
            thread_id,
        )

    def release_thread_response(self, room_id: str, event_id: str) -> None:
        """Release one terminal response reservation and all retained event aliases."""
        if self._cache_ops.runtime.event_cache is None:
            return
        self._reservations.release(
            self._cache_ops.cache_principal_id(),
            room_id,
            event_id,
        )

    def _normalize_outbound_event_source(
        self,
        room_id: str,
        event_source: dict[str, Any],
    ) -> dict[str, object] | None:
        """Return one outbound event payload normalized for durable cache storage."""
        event_id = event_source.get("event_id")
        if not isinstance(event_id, str) or not event_id:
            return None
        client = self._require_client()
        sender = client.user_id if isinstance(client.user_id, str) else None
        return typing.cast(
            "dict[str, object]",
            normalize_event_source_for_cache(
                {
                    **event_source,
                    "room_id": room_id,
                },
                event_id=event_id,
                sender=sender,
                origin_server_ts=int(time.time() * 1000),
            ),
        )

    async def _apply_outbound_redaction_notification(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        impact = await _resolve_thread_redaction_mutation_impact(
            resolver=self._resolver,
            room_id=room_id,
            redacted_event_id=redacted_event_id,
            context="outbound",
        )
        await _apply_thread_redaction_mutation(
            cache_ops=self._cache_ops,
            room_id=room_id,
            redacted_event_id=redacted_event_id,
            impact=impact,
            context="outbound",
        )

    def notify_outbound_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        """Schedule advisory bookkeeping for one locally redacted message."""
        try:
            if not redacted_event_id or not self._cache_ops.cache_runtime_available():
                return

            # Lookup-dependent outbound mutations stay on the room barrier because earlier outbound writes can create the lookup rows needed to resolve thread impact. Safe parallelization would require reservation-based routing (see ISSUE-189).
            self._schedule_fail_open_room_update(
                room_id,
                lambda: self._apply_outbound_redaction_notification(room_id, redacted_event_id),
                name="matrix_cache_notify_outbound_redaction",
                cancelled_message="Ignoring cancelled outbound Matrix redaction cache bookkeeping after successful redact",
                failure_message="Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
                log_context={"redacted_event_id": redacted_event_id},
            )
        except asyncio.CancelledError as exc:
            self._cache_ops.logger.warning(
                "Ignoring cancelled outbound Matrix redaction cache bookkeeping after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
        except Exception as exc:
            self._cache_ops.logger.warning(
                "Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
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
        emit_timing: bool = False,
        coalesce_key: tuple[str, str] | None = None,
        coalesce_log_context: dict[str, object] | None = None,
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
                emit_timing=emit_timing,
                coalesce_key=coalesce_key,
                coalesce_log_context=coalesce_log_context,
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

    def _schedule_fail_open_thread_update(
        self,
        room_id: str,
        thread_id: str,
        update_coro_factory: typing.Callable[[], typing.Coroutine[Any, Any, object]],
        *,
        name: str,
        cancelled_message: str,
        failure_message: str,
        log_context: dict[str, object],
        emit_timing: bool = False,
        coalesce_key: tuple[str, str] | None = None,
        coalesce_log_context: dict[str, object] | None = None,
    ) -> None:
        async def safe_update() -> None:
            try:
                await update_coro_factory()
            except asyncio.CancelledError as exc:
                self._cache_ops.logger.warning(
                    cancelled_message,
                    room_id=room_id,
                    thread_id=thread_id,
                    error=str(exc),
                    **log_context,
                )
            except Exception as exc:
                self._cache_ops.logger.warning(
                    failure_message,
                    room_id=room_id,
                    thread_id=thread_id,
                    error=str(exc),
                    **log_context,
                )

        try:
            self._cache_ops.queue_thread_cache_update(
                room_id,
                thread_id,
                safe_update,
                name=name,
                emit_timing=emit_timing,
                coalesce_key=coalesce_key,
                coalesce_log_context=coalesce_log_context,
            )
        except asyncio.CancelledError as exc:
            self._cache_ops.logger.warning(
                cancelled_message,
                room_id=room_id,
                thread_id=thread_id,
                error=str(exc),
                **log_context,
            )
        except Exception as exc:
            self._cache_ops.logger.warning(
                failure_message,
                room_id=room_id,
                thread_id=thread_id,
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

    async def _resolve_live_event_impact(
        self,
        room_id: str,
        *,
        event_id: str,
        event_info: EventInfo,
    ) -> MutationThreadImpact:
        return await self._resolver.resolve_thread_impact_for_mutation(
            room_id,
            event_info=event_info,
            event_id=event_id,
            context="live",
        )

    async def _append_live_event_without_timing(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        impact = await self._resolve_live_event_impact(
            room_id,
            event_id=event.event_id,
            event_info=event_info,
        )
        room_level_skip_message = "Skipping live thread cache bookkeeping for known non-threaded message mutation"
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            await _apply_thread_message_mutation(
                cache_ops=self._cache_ops,
                room_id=room_id,
                event_info=event_info,
                impact=impact,
                event_source=None,
                event_id=event.event_id,
                context="live",
                room_level_skip_message=room_level_skip_message,
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            # UNKNOWN-impact mutations gap-mark the whole room eagerly, outside the per-thread
            # queue: the mutation's thread is unknown, so no per-thread barrier can cover it.
            # See ISSUE-189 for the architectural follow-up.
            await self._cache_ops.invalidate_room_threads(
                room_id,
                reason="live_thread_lookup_unavailable",
            )
            return

        thread_id = impact.thread_id
        assert thread_id is not None
        event_source = normalize_nio_event_for_cache(event)

        async def append_live_mutation() -> bool:
            return await _apply_thread_message_mutation(
                cache_ops=self._cache_ops,
                room_id=room_id,
                event_info=event_info,
                impact=impact,
                event_source=event_source,
                event_id=event.event_id,
                context="live",
                room_level_skip_message=room_level_skip_message,
            )

        await self._cache_ops.queue_thread_cache_update(
            room_id,
            thread_id,
            append_live_mutation,
            name="matrix_cache_append_live_event",
        )

    async def _append_live_threaded_event_with_timing(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        impact: MutationThreadImpact,
        impact_resolution_ms: float,
        started: float,
    ) -> None:
        assert impact.thread_id is not None
        thread_id = impact.thread_id
        event_source = normalize_nio_event_for_cache(event)
        queue_started = time.perf_counter()
        append_metrics: dict[str, str | int | float | bool] = {}

        async def append_live_mutation() -> bool:
            append_started = time.perf_counter()
            appended = await self._cache_ops.append_event_to_cache(
                room_id,
                thread_id,
                event_source,
                context="live",
                append_failed_reason="live_append_failed",
            )
            append_metrics["append_ms"] = elapsed_ms_since(append_started, clock=time.perf_counter)
            append_metrics["appended"] = appended
            return appended

        outcome = "ok"
        try:
            appended = await self._cache_ops.queue_thread_cache_update(
                room_id,
                thread_id,
                append_live_mutation,
                name="matrix_cache_append_live_event",
            )
            if appended is False:
                outcome = "append_failed"
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception:
            outcome = "error"
            raise
        finally:
            emit_timing_event(
                "Live event cache append timing",
                room_id=room_id,
                thread_id=thread_id,
                event_id=event.event_id,
                impact_state="threaded",
                impact_resolution_ms=impact_resolution_ms,
                queue_and_update_ms=elapsed_ms_since(queue_started, clock=time.perf_counter),
                total_ms=elapsed_ms_since(started, clock=time.perf_counter),
                outcome=outcome,
                **append_metrics,
            )

    async def _append_live_event_with_timing(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        started = time.perf_counter()
        impact_started = time.perf_counter()
        impact = await self._resolve_live_event_impact(
            room_id,
            event_id=event.event_id,
            event_info=event_info,
        )
        impact_resolution_ms = elapsed_ms_since(impact_started, clock=time.perf_counter)
        room_level_skip_message = "Skipping live thread cache bookkeeping for known non-threaded message mutation"
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            self._cache_ops.logger.debug(
                room_level_skip_message,
                room_id=room_id,
                event_id=event.event_id,
                original_event_id=event_info.original_event_id,
            )
            emit_timing_event(
                "Live event cache append timing",
                room_id=room_id,
                event_id=event.event_id,
                impact_state="room_level",
                impact_resolution_ms=impact_resolution_ms,
                total_ms=elapsed_ms_since(started, clock=time.perf_counter),
                outcome="non_threaded_skip",
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            invalidate_started = time.perf_counter()
            # UNKNOWN-impact mutations gap-mark the whole room eagerly, outside the per-thread
            # queue: the mutation's thread is unknown, so no per-thread barrier can cover it.
            # See ISSUE-189 for the architectural follow-up.
            await self._cache_ops.invalidate_room_threads(
                room_id,
                reason="live_thread_lookup_unavailable",
            )
            emit_timing_event(
                "Live event cache append timing",
                room_id=room_id,
                event_id=event.event_id,
                impact_state="unknown",
                impact_resolution_ms=impact_resolution_ms,
                invalidate_ms=elapsed_ms_since(invalidate_started, clock=time.perf_counter),
                total_ms=elapsed_ms_since(started, clock=time.perf_counter),
                outcome="room_invalidated",
            )
            return
        await self._append_live_threaded_event_with_timing(
            room_id,
            event,
            impact=impact,
            impact_resolution_ms=impact_resolution_ms,
            started=started,
        )

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

        if not timing_enabled():
            await self._append_live_event_without_timing(
                room_id,
                event,
                event_info=event_info,
            )
            return

        await self._append_live_event_with_timing(
            room_id,
            event,
            event_info=event_info,
        )

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        if not self._cache_ops.cache_runtime_available():
            return

        impact = await _resolve_thread_redaction_mutation_impact(
            resolver=self._resolver,
            room_id=room_id,
            redacted_event_id=event.redacts,
            event_id=event.event_id,
            context="live",
        )
        thread_id = impact.thread_id

        async def redact_and_invalidate() -> bool:
            return await _apply_thread_redaction_mutation(
                cache_ops=self._cache_ops,
                room_id=room_id,
                redacted_event_id=event.redacts,
                impact=impact,
                context="live",
            )

        if thread_id is not None:
            await self._cache_ops.queue_thread_cache_update(
                room_id,
                thread_id,
                redact_and_invalidate,
                name="matrix_cache_apply_redaction",
            )
            return
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
        raise_on_cache_write_failure: bool,
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
            room_threads_invalidated = (
                await _apply_thread_message_mutation(
                    cache_ops=self._cache_ops,
                    room_id=room_id,
                    event_info=event_info,
                    impact=impact,
                    event_source=event_source,
                    event_id=event_id if isinstance(event_id, str) else None,
                    context="sync",
                    room_level_skip_message="Skipping sync thread cache bookkeeping for known non-threaded message mutation",
                    allow_room_invalidation=not room_threads_invalidated,
                    raise_on_cache_write_failure=raise_on_cache_write_failure,
                )
                or room_threads_invalidated
            )

    async def _apply_sync_redactions(
        self,
        room_id: str,
        redacted_event_ids: typing.Sequence[str],
        *,
        resolution_context: MutationResolutionContext,
        raise_on_cache_write_failure: bool,
    ) -> None:
        room_threads_invalidated = False
        for redacted_event_id in redacted_event_ids:
            impact = await _resolve_thread_redaction_mutation_impact(
                resolver=self._resolver,
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                context="sync",
                resolution_context=resolution_context,
            )
            room_threads_invalidated = (
                await _apply_thread_redaction_mutation(
                    cache_ops=self._cache_ops,
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    impact=impact,
                    context="sync",
                    allow_room_invalidation=not room_threads_invalidated,
                    raise_on_cache_write_failure=raise_on_cache_write_failure,
                )
                or room_threads_invalidated
            )

    async def _persist_room_sync_timeline_updates(
        self,
        room_id: str,
        plain_events: typing.Sequence[dict[str, object]],
        threaded_events: typing.Sequence[dict[str, object]],
        redacted_event_ids: typing.Sequence[str],
        *,
        limited_timeline: bool,
        raise_on_cache_write_failure: bool,
    ) -> None:
        if limited_timeline:
            # A limited timeline skipped events, so this room's cached thread
            # snapshots must durably stop being trusted before the partial
            # window is admitted.
            await self._cache_ops.invalidate_room_threads(
                room_id,
                reason=_LIMITED_SYNC_TIMELINE_REASON,
                raise_on_failure=raise_on_cache_write_failure,
            )
        if not plain_events and not threaded_events and not redacted_event_ids:
            return
        try:
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
                raise_on_failure=raise_on_cache_write_failure,
            )
            await self._cache_ops.store_events_batch(
                room_id,
                threaded_batch,
                failure_message="Failed to persist sync threaded events to cache",
                raise_on_failure=raise_on_cache_write_failure,
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
                raise_on_cache_write_failure=raise_on_cache_write_failure,
            )
            await self._apply_sync_redactions(
                room_id,
                redacted_event_ids,
                resolution_context=resolution_context,
                raise_on_cache_write_failure=raise_on_cache_write_failure,
            )
        except Exception:
            if raise_on_cache_write_failure and not limited_timeline:
                await self._cache_ops.invalidate_room_threads(
                    room_id,
                    reason=_SYNC_TIMELINE_WRITE_FAILED_REASON,
                )
            raise

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

    async def cache_historical_event(
        self,
        room_id: str,
        event: nio.Event,
    ) -> None:
        """Durably cache one recovered history event before nio accepts it."""
        room_plain_events: dict[str, list[dict[str, object]]] = {}
        room_threaded_events: dict[str, list[dict[str, object]]] = {}
        room_redactions: dict[str, list[str]] = {}
        _collect_sync_timeline_cache_updates(
            room_id,
            event,
            room_threaded_events=room_threaded_events,
            room_plain_events=room_plain_events,
            room_redactions=room_redactions,
        )
        plain_events = room_plain_events.get(room_id, ())
        threaded_events = room_threaded_events.get(room_id, ())
        redacted_event_ids = room_redactions.get(room_id, ())
        if not plain_events and not threaded_events and not redacted_event_ids:
            return
        if not self._cache_ops.cache_runtime_available():
            msg = "Matrix event cache is unavailable for historical recovery"
            raise RuntimeError(msg)
        task = self._cache_ops.queue_room_cache_update(
            room_id,
            lambda: self._persist_room_sync_timeline_updates(
                room_id,
                plain_events,
                threaded_events,
                redacted_event_ids,
                limited_timeline=False,
                raise_on_cache_write_failure=True,
            ),
            name="matrix_cache_historical_event",
        )
        await task

    def cache_sync_timeline(
        self,
        response: nio.SyncResponse,
        *,
        raise_on_cache_write_failure: bool = False,
    ) -> list[asyncio.Task[object]]:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        if not self._cache_ops.cache_runtime_available():
            return []
        limited_room_ids, validation_errors = self.limited_sync_timeline_room_ids(response)
        if validation_errors:
            raise validation_errors[0]
        room_plain_events, room_threaded_events, room_redactions = self._group_sync_timeline_updates(response)
        limited_room_id_set = set(limited_room_ids)
        tasks: list[asyncio.Task[object]] = []
        for room_id in set(room_plain_events) | set(room_threaded_events) | set(room_redactions) | limited_room_id_set:
            plain_events = room_plain_events.get(room_id, ())
            threaded_events = room_threaded_events.get(room_id, ())
            redacted_event_ids = room_redactions.get(room_id, ())
            limited_timeline = room_id in limited_room_id_set
            tasks.append(
                self._cache_ops.queue_room_cache_update(
                    room_id,
                    lambda room_id=room_id, plain_events=plain_events, threaded_events=threaded_events, redacted_event_ids=redacted_event_ids, limited_timeline=limited_timeline: (
                        self._persist_room_sync_timeline_updates(
                            room_id,
                            plain_events,
                            threaded_events,
                            redacted_event_ids,
                            limited_timeline=limited_timeline,
                            raise_on_cache_write_failure=raise_on_cache_write_failure,
                        )
                    ),
                    name="matrix_cache_sync_timeline",
                ),
            )
        return tasks

    @staticmethod
    def limited_sync_timeline_room_ids(
        response: nio.SyncResponse,
    ) -> tuple[tuple[str, ...], tuple[BaseException, ...]]:
        """Return limited joined-room IDs or validation errors for one sync response."""
        try:
            joined_rooms = response.rooms.join
        except AttributeError as exc:
            return (), (exc,)
        if not isinstance(joined_rooms, dict):
            return (), (TypeError("sync response joined rooms must be a dict"),)

        limited_room_ids: list[str] = []
        for room_id, room_info in joined_rooms.items():
            if not isinstance(room_id, str) or room_info is None:
                return (), (TypeError("sync response contains an invalid joined room"),)
            try:
                timeline = room_info.timeline
                limited = False if timeline is None else timeline.limited
                events = [] if timeline is None else timeline.events
            except AttributeError as exc:
                return (), (exc,)
            if not isinstance(limited, bool) or not isinstance(events, list):
                return (), (TypeError("sync response contains an invalid joined-room timeline"),)
            if limited:
                limited_room_ids.append(room_id)
        return tuple(limited_room_ids), ()

    @staticmethod
    def _cache_task_errors(results: list[object | BaseException]) -> tuple[BaseException, ...]:
        """Return task outcomes that prevent cache certification."""
        errors: list[BaseException] = []
        current_task = asyncio.current_task()
        for result in results:
            if isinstance(result, (KeyboardInterrupt, SystemExit)):
                raise result
            if isinstance(result, asyncio.CancelledError):
                if current_task is not None and current_task.cancelling():
                    raise result
                errors.append(result)
                continue
            if isinstance(result, BaseException):
                errors.append(result)
        return tuple(errors)

    async def cache_sync_timeline_for_certification(
        self,
        response: nio.SyncResponse,
    ) -> SyncCacheWriteResult:
        """Persist sync timeline data and report whether it certifies the sync token."""
        limited_room_ids, validation_errors = self.limited_sync_timeline_room_ids(response)
        if validation_errors:
            return SyncCacheWriteResult.from_sync_response(
                response,
                complete=False,
                errors=validation_errors,
                runtime_available=self._cache_ops.cache_runtime_available(),
                runtime_diagnostics=self._cache_ops.cache_runtime_diagnostics(),
            )
        if not self._cache_ops.cache_runtime_available():
            return SyncCacheWriteResult.from_sync_response(
                response,
                complete=False,
                limited_room_ids=limited_room_ids,
                runtime_available=False,
                task_count=0,
                runtime_diagnostics=self._cache_ops.cache_runtime_diagnostics(),
            )

        try:
            tasks = self.cache_sync_timeline(response, raise_on_cache_write_failure=True)
            tasks.extend(self._cache_ops.queue_pending_durable_write_flushes())
        except (KeyboardInterrupt, SystemExit):
            raise
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return SyncCacheWriteResult.from_sync_response(
                response,
                complete=False,
                limited_room_ids=limited_room_ids,
                errors=(exc,),
                runtime_available=self._cache_ops.cache_runtime_available(),
                runtime_diagnostics=self._cache_ops.cache_runtime_diagnostics(),
            )

        results: list[object | BaseException] = []
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
        errors = self._cache_task_errors(results)
        runtime_available = self._cache_ops.cache_runtime_available()
        pending_durable_write_room_ids = self._cache_ops.pending_durable_write_room_ids()
        complete = runtime_available and not errors and not pending_durable_write_room_ids
        cache_result = SyncCacheWriteResult.from_sync_response(
            response,
            complete=complete,
            limited_room_ids=limited_room_ids,
            errors=errors,
            runtime_available=runtime_available,
            task_count=len(tasks),
        )
        if cache_result.certified:
            return cache_result
        return replace(
            cache_result,
            runtime_diagnostics=self._cache_ops.cache_runtime_diagnostics(),
        )
