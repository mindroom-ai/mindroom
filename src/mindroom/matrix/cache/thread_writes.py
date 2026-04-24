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
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import (
    MutationResolutionContext,
    MutationThreadImpact,
    MutationThreadImpactState,
    MutationWriteContext,
    ThreadMutationResolver,
    is_thread_affecting_relation,
)
from mindroom.timing import emit_timing_event, timing_enabled

if TYPE_CHECKING:
    from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps

__all__ = ["_collect_sync_timeline_cache_updates"]


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
    invalidate_on_append_failure: bool,
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
    await cache_ops.invalidate_known_thread(
        room_id,
        impact.thread_id,
        reason=_mutation_reason(context, "thread_mutation"),
        raise_on_failure=raise_on_cache_write_failure,
    )
    appended = await cache_ops.append_event_to_cache(
        room_id,
        impact.thread_id,
        event_source,
        context=context,
        raise_on_failure=raise_on_cache_write_failure,
    )
    if invalidate_on_append_failure and not appended:
        await cache_ops.invalidate_known_thread(
            room_id,
            impact.thread_id,
            reason=_mutation_reason(context, "append_failed"),
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
    redact_room_level_event: bool = True,
    raise_on_cache_write_failure: bool = False,
) -> bool:
    redact_failure_message = {
        "outbound": "Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
        "live": "Failed to apply live redaction to cache",
        "sync": "Failed to apply sync redaction to cache",
    }[context]
    if impact.state is MutationThreadImpactState.ROOM_LEVEL and not redact_room_level_event:
        cache_ops.logger.debug(
            "Skipping outbound thread cache bookkeeping for non-threaded redaction",
            room_id=room_id,
            redacted_event_id=redacted_event_id,
        )
        return False
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
    ) -> None:
        self._resolver = resolver
        self._cache_ops = cache_ops
        self._require_client = require_client

    async def _apply_outbound_event_notification(
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
        await _apply_thread_message_mutation(
            cache_ops=self._cache_ops,
            room_id=room_id,
            event_info=event_info,
            impact=impact,
            event_source=event_source,
            event_id=event_id,
            context="outbound",
            room_level_skip_message="Skipping outbound thread cache bookkeeping for non-threaded event mutation",
            invalidate_on_append_failure=False,
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
            if event_info.is_reaction:
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
                )
                return
            if not is_thread_affecting_relation(event_info):
                return
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
        """Schedule advisory bookkeeping for one locally sent threaded message or edit."""
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
            redact_room_level_event=False,
        )

    def notify_outbound_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        """Schedule advisory bookkeeping for one locally redacted threaded message."""
        try:
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
        if impact.state is not MutationThreadImpactState.THREADED:
            await _apply_thread_message_mutation(
                cache_ops=self._cache_ops,
                room_id=room_id,
                event_info=event_info,
                impact=impact,
                event_source=None,
                event_id=event.event_id,
                context="live",
                room_level_skip_message=room_level_skip_message,
                invalidate_on_append_failure=True,
            )
            return

        event_source = normalize_nio_event_for_cache(event)

        async def append_and_invalidate() -> bool:
            return await _apply_thread_message_mutation(
                cache_ops=self._cache_ops,
                room_id=room_id,
                event_info=event_info,
                impact=impact,
                event_source=event_source,
                event_id=event.event_id,
                context="live",
                room_level_skip_message=room_level_skip_message,
                invalidate_on_append_failure=True,
            )

        await self._cache_ops.queue_room_cache_update(
            room_id,
            append_and_invalidate,
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

        async def append_and_invalidate() -> bool:
            invalidate_started = time.perf_counter()
            await self._cache_ops.invalidate_known_thread(
                room_id,
                thread_id,
                reason="live_thread_mutation",
            )
            append_metrics["invalidate_ms"] = round((time.perf_counter() - invalidate_started) * 1000, 1)
            append_started = time.perf_counter()
            appended = await self._cache_ops.append_event_to_cache(
                room_id,
                thread_id,
                event_source,
                context="live",
            )
            append_metrics["append_ms"] = round((time.perf_counter() - append_started) * 1000, 1)
            append_metrics["appended"] = appended
            if not appended:
                fallback_invalidate_started = time.perf_counter()
                await self._cache_ops.invalidate_known_thread(
                    room_id,
                    thread_id,
                    reason="live_append_failed",
                )
                append_metrics["append_failure_invalidate_ms"] = round(
                    (time.perf_counter() - fallback_invalidate_started) * 1000,
                    1,
                )
            return appended

        outcome = "ok"
        try:
            appended = await self._cache_ops.queue_room_cache_update(
                room_id,
                append_and_invalidate,
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
                queue_and_update_ms=round((time.perf_counter() - queue_started) * 1000, 1),
                total_ms=round((time.perf_counter() - started) * 1000, 1),
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
        impact_resolution_ms = round((time.perf_counter() - impact_started) * 1000, 1)
        if impact.state is MutationThreadImpactState.ROOM_LEVEL:
            self._cache_ops.logger.debug(
                "Skipping live thread cache bookkeeping for known non-threaded message mutation",
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
                total_ms=round((time.perf_counter() - started) * 1000, 1),
                outcome="non_threaded_skip",
            )
            return
        if impact.state is MutationThreadImpactState.UNKNOWN:
            invalidate_started = time.perf_counter()
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
                invalidate_ms=round((time.perf_counter() - invalidate_started) * 1000, 1),
                total_ms=round((time.perf_counter() - started) * 1000, 1),
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

        async def redact_and_invalidate() -> bool:
            return await _apply_thread_redaction_mutation(
                cache_ops=self._cache_ops,
                room_id=room_id,
                redacted_event_id=event.redacts,
                impact=impact,
                context="live",
            )

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
                    invalidate_on_append_failure=True,
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
        raise_on_cache_write_failure: bool,
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

    def cache_sync_timeline(
        self,
        response: nio.SyncResponse,
        *,
        raise_on_cache_write_failure: bool = False,
    ) -> list[asyncio.Task[object]]:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
        if not self._cache_ops.cache_runtime_available():
            return []
        room_plain_events, room_threaded_events, room_redactions = self._group_sync_timeline_updates(response)
        tasks: list[asyncio.Task[object]] = []
        for room_id in set(room_plain_events) | set(room_threaded_events) | set(room_redactions):
            plain_events = room_plain_events.get(room_id, ())
            threaded_events = room_threaded_events.get(room_id, ())
            redacted_event_ids = room_redactions.get(room_id, ())
            tasks.append(
                self._cache_ops.queue_room_cache_update(
                    room_id,
                    lambda room_id=room_id, plain_events=plain_events, threaded_events=threaded_events, redacted_event_ids=redacted_event_ids: (
                        self._persist_room_sync_timeline_updates(
                            room_id,
                            plain_events,
                            threaded_events,
                            redacted_event_ids,
                            raise_on_cache_write_failure=raise_on_cache_write_failure,
                        )
                    ),
                    name="matrix_cache_sync_timeline",
                ),
            )
        return tasks
