"""Outbound bookkeeping for Matrix thread cache writes."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from mindroom.matrix.cache.event_cache import normalize_event_source_for_cache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_bookkeeping import MutationThreadImpactState, is_thread_affecting_relation

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    import nio

    from mindroom.matrix.cache.thread_write_cache_ops import ThreadMutationCacheOps
    from mindroom.matrix.thread_bookkeeping import ThreadMutationResolver


class ThreadOutboundWritePolicy:
    """Own advisory bookkeeping for locally sent thread mutations."""

    def __init__(
        self,
        *,
        resolver: ThreadMutationResolver,
        cache_ops: ThreadMutationCacheOps,
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
