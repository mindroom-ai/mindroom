"""Thread mutation and advisory bookkeeping policy for Matrix conversation cache."""

from __future__ import annotations

import asyncio
import time
import typing
from typing import TYPE_CHECKING, Any

import nio

from mindroom.matrix.cache.event_cache import normalize_event_source_for_cache, normalize_nio_event_for_cache
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.thread_membership import (
    ThreadMembershipAccess,
    resolve_event_thread_id_best_effort,
    resolve_related_event_thread_id_best_effort,
    room_scan_thread_membership_access_for_client,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView


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
    if _is_thread_affecting_relation(event_info):
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
    if not _is_thread_affecting_relation(event_info):
        return None
    event_id = event.event_id
    if not isinstance(event_id, str) or not event_id:
        return None
    return room_id, normalize_nio_event_for_cache(event)


def _has_explicit_thread_relation(event_info: EventInfo) -> bool:
    return isinstance(event_info.thread_id, str) or isinstance(event_info.thread_id_from_edit, str)


def _is_thread_affecting_relation(event_info: EventInfo) -> bool:
    """Return whether one room message relation can affect thread-scoped cache state."""
    return (
        event_info.is_thread or event_info.is_edit or event_info.is_reply or event_info.relation_type == "m.reference"
    )


def _redaction_can_affect_thread_cache(event_info: EventInfo) -> bool:
    """Return whether redacting one related event can invalidate cached thread messages."""
    return not event_info.is_reaction


class ThreadWritePolicy:
    """Own thread-affecting cache mutations and outbound advisory bookkeeping."""

    def __init__(
        self,
        *,
        logger_getter: typing.Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        require_client: typing.Callable[[], nio.AsyncClient],
        fetch_event_info_for_thread_resolution: typing.Callable[[str, str], typing.Awaitable[EventInfo | None]],
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime
        self.require_client = require_client
        self._fetch_event_info_for_thread_resolution = fetch_event_info_for_thread_resolution

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    def _cache_runtime_available(self) -> bool:
        return self.runtime.event_cache is not None and self.runtime.event_cache_write_coordinator is not None

    def _queue_room_cache_update(
        self,
        room_id: str,
        update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
        *,
        name: str,
    ) -> asyncio.Task[object]:
        coordinator = self.runtime.event_cache_write_coordinator
        return coordinator.queue_room_update(room_id, update_coro_factory, name=name)

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

    async def _lookup_redaction_thread_id(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        failure_message: str,
        event_id: str | None = None,
    ) -> str | None:
        try:
            target_event_info = await self._event_info_for_thread_resolution(room_id, redacted_event_id)
            if target_event_info is not None and not _redaction_can_affect_thread_cache(target_event_info):
                return None
            return await resolve_related_event_thread_id_best_effort(
                room_id,
                redacted_event_id,
                access=self._thread_membership_access(),
            )
        except Exception as exc:
            self.logger.warning(
                failure_message,
                room_id=room_id,
                event_id=event_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
            return None

    async def _redact_cached_event(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        thread_id: str | None,
        failure_message: str,
    ) -> bool:
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

    async def _invalidate_after_redaction(
        self,
        room_id: str,
        *,
        thread_id: str | None,
        redacted: bool,
        success_reason: str,
        failure_reason: str,
        lookup_missing_reason: str,
    ) -> None:
        if thread_id is not None:
            await self._invalidate_known_thread(
                room_id,
                thread_id,
                reason=success_reason if redacted else failure_reason,
            )
            return
        await self._invalidate_room_threads(room_id, reason=lookup_missing_reason)

    async def _invalidate_known_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
    ) -> None:
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

    async def _invalidate_room_threads(
        self,
        room_id: str,
        *,
        reason: str,
    ) -> None:
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

    async def _resolve_thread_id_for_mutation(
        self,
        room_id: str,
        *,
        event_info: EventInfo,
        event_id: str | None,
        context: str,
    ) -> str | None:
        explicit_thread_id = event_info.thread_id or event_info.thread_id_from_edit
        if explicit_thread_id is not None:
            return explicit_thread_id
        try:
            thread_id = await resolve_event_thread_id_best_effort(
                room_id,
                event_info,
                event_id=event_id,
                access=self._thread_membership_access(),
            )
        except Exception as exc:
            self.logger.warning(
                "Failed to resolve cached thread for mutation",
                room_id=room_id,
                event_id=event_id,
                original_event_id=event_info.original_event_id,
                context=context,
                error=str(exc),
            )
            return None
        return thread_id

    async def _event_info_for_thread_resolution(
        self,
        room_id: str,
        event_id: str,
    ) -> EventInfo | None:
        return await self._fetch_event_info_for_thread_resolution(room_id, event_id)

    def _thread_membership_access(self) -> ThreadMembershipAccess:
        """Return the shared thread-membership accessors for cache mutations."""
        return room_scan_thread_membership_access_for_client(
            self.require_client(),
            lookup_thread_id=self.runtime.event_cache.get_thread_id_for_event,
            fetch_event_info=self._event_info_for_thread_resolution,
        )

    async def _append_event_to_cache(
        self,
        room_id: str,
        thread_id: str,
        event_source: dict[str, Any],
        *,
        context: str,
    ) -> bool:
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

    async def _apply_outbound_message_notification(
        self,
        room_id: str,
        event_id: str,
        event_source: dict[str, Any],
        event_info: EventInfo,
    ) -> None:
        thread_id = await self._resolve_thread_id_for_mutation(
            room_id,
            event_info=event_info,
            event_id=event_id,
            context="outbound",
        )
        if thread_id is None:
            self.logger.debug(
                "Skipping outbound thread cache bookkeeping for non-threaded message mutation",
                room_id=room_id,
                event_id=event_id,
                original_event_id=event_info.original_event_id,
            )
            return
        await self._invalidate_known_thread(
            room_id,
            thread_id,
            reason="outbound_thread_mutation",
        )
        await self._append_event_to_cache(
            room_id,
            thread_id,
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
        if not self._cache_runtime_available():
            return
        if not isinstance(event_id, str) or not event_id:
            return

        client = self.require_client()
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
        is_thread_candidate = _is_thread_affecting_relation(event_info)
        if not is_thread_candidate:
            return

        async def safe_update() -> None:
            try:
                await self._apply_outbound_message_notification(room_id, event_id, event_source, event_info)
            except asyncio.CancelledError as exc:
                self.logger.warning(
                    "Ignoring cancelled outbound threaded message cache bookkeeping after successful send",
                    room_id=room_id,
                    event_id=event_id,
                    error=str(exc),
                )
            except Exception as exc:
                self.logger.warning(
                    "Ignoring outbound threaded message cache bookkeeping failure after successful send",
                    room_id=room_id,
                    event_id=event_id,
                    error=str(exc),
                )

        try:
            self._queue_room_cache_update(
                room_id,
                safe_update,
                name="matrix_cache_notify_outbound_message",
            )
        except asyncio.CancelledError as exc:
            self.logger.warning(
                "Ignoring cancelled outbound threaded message cache bookkeeping after successful send",
                room_id=room_id,
                event_id=event_id,
                error=str(exc),
            )
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound threaded message cache bookkeeping failure after successful send",
                room_id=room_id,
                event_id=event_id,
                error=str(exc),
            )

    async def _apply_outbound_redaction_notification(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        thread_id = await self._lookup_redaction_thread_id(
            room_id,
            redacted_event_id,
            failure_message="Ignoring outbound Matrix redaction cache lookup failure after successful redact",
        )
        if thread_id is None:
            self.logger.debug(
                "Skipping outbound thread cache bookkeeping for non-threaded redaction",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
            )
            return
        redacted = await self._redact_cached_event(
            room_id,
            redacted_event_id,
            thread_id=thread_id,
            failure_message="Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
        )
        await self._invalidate_after_redaction(
            room_id,
            thread_id=thread_id,
            redacted=redacted,
            success_reason="outbound_redaction",
            failure_reason="outbound_redaction_failed",
            lookup_missing_reason="outbound_redaction_lookup_missing",
        )

    def notify_outbound_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        """Schedule advisory bookkeeping for one locally redacted threaded message."""
        if not self._cache_runtime_available():
            return
        if not redacted_event_id:
            return

        async def safe_update() -> None:
            try:
                await self._apply_outbound_redaction_notification(room_id, redacted_event_id)
            except asyncio.CancelledError as exc:
                self.logger.warning(
                    "Ignoring cancelled outbound Matrix redaction cache bookkeeping after successful redact",
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    error=str(exc),
                )
            except Exception as exc:
                self.logger.warning(
                    "Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    error=str(exc),
                )

        try:
            self._queue_room_cache_update(
                room_id,
                safe_update,
                name="matrix_cache_notify_outbound_redaction",
            )
        except asyncio.CancelledError as exc:
            self.logger.warning(
                "Ignoring cancelled outbound Matrix redaction cache bookkeeping after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound Matrix redaction cache bookkeeping failure after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )

    async def append_live_event(
        self,
        room_id: str,
        event: nio.RoomMessage,
        *,
        event_info: EventInfo,
    ) -> None:
        """Append one live threaded event into the advisory cache when the thread is known."""
        if not self._cache_runtime_available():
            return

        thread_id = await self._resolve_thread_id_for_mutation(
            room_id,
            event_info=event_info,
            event_id=event.event_id,
            context="live",
        )
        if thread_id is None:
            self.logger.debug(
                "Skipping live thread cache bookkeeping for known non-threaded message mutation",
                room_id=room_id,
                event_id=event.event_id,
                original_event_id=event_info.original_event_id,
            )
            return

        event_source = normalize_nio_event_for_cache(event)

        async def append_and_invalidate() -> bool:
            await self._invalidate_known_thread(
                room_id,
                thread_id,
                reason="live_thread_mutation",
            )
            appended = await self._append_event_to_cache(
                room_id,
                thread_id,
                event_source,
                context="live",
            )
            if not appended:
                await self._invalidate_known_thread(
                    room_id,
                    thread_id,
                    reason="live_append_failed",
                )
            return appended

        await self._queue_room_cache_update(
            room_id,
            append_and_invalidate,
            name="matrix_cache_append_live_event",
        )

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        if not self._cache_runtime_available():
            return
        thread_id = await self._lookup_redaction_thread_id(
            room_id,
            event.redacts,
            failure_message="Failed to resolve cached thread for redaction",
            event_id=event.event_id,
        )

        async def redact_and_invalidate() -> bool:
            redacted = await self._redact_cached_event(
                room_id,
                event.redacts,
                thread_id=thread_id,
                failure_message="Failed to apply live redaction to cache",
            )
            await self._invalidate_after_redaction(
                room_id,
                thread_id=thread_id,
                redacted=redacted,
                success_reason="live_redaction",
                failure_reason="live_redaction_failed",
                lookup_missing_reason="live_redaction_lookup_missing",
            )
            return redacted

        await self._queue_room_cache_update(
            room_id,
            redact_and_invalidate,
            name="matrix_cache_apply_redaction",
        )

    async def _persist_threaded_sync_events(
        self,
        room_id: str,
        threaded_events: Sequence[dict[str, object]],
    ) -> None:
        for event_source in threaded_events:
            event_info = EventInfo.from_event(event_source)
            event_id = event_source.get("event_id")
            thread_id = await self._resolve_thread_id_for_mutation(
                room_id,
                event_info=event_info,
                event_id=event_id if isinstance(event_id, str) else None,
                context="sync",
            )
            if thread_id is None:
                self.logger.debug(
                    "Skipping sync thread cache bookkeeping for known non-threaded message mutation",
                    room_id=room_id,
                    event_id=event_id,
                    original_event_id=event_info.original_event_id,
                )
                continue
            await self._invalidate_known_thread(
                room_id,
                thread_id,
                reason="sync_thread_mutation",
            )
            appended = await self._append_event_to_cache(
                room_id,
                thread_id,
                event_source,
                context="sync",
            )
            if not appended:
                await self._invalidate_known_thread(
                    room_id,
                    thread_id,
                    reason="sync_append_failed",
                )

    async def _apply_sync_redactions(
        self,
        room_id: str,
        redacted_event_ids: Sequence[str],
    ) -> None:
        for redacted_event_id in redacted_event_ids:
            thread_id = await self._lookup_redaction_thread_id(
                room_id,
                redacted_event_id,
                failure_message="Failed to resolve cached thread for sync redaction",
            )
            redacted = await self._redact_cached_event(
                room_id,
                redacted_event_id,
                thread_id=thread_id,
                failure_message="Failed to apply sync redaction to cache",
            )
            await self._invalidate_after_redaction(
                room_id,
                thread_id=thread_id,
                redacted=redacted,
                success_reason="sync_redaction",
                failure_reason="sync_redaction_failed",
                lookup_missing_reason="sync_redaction_lookup_missing",
            )

    async def _persist_room_sync_timeline_updates(
        self,
        room_id: str,
        plain_events: Sequence[dict[str, object]],
        threaded_events: Sequence[dict[str, object]],
        redacted_event_ids: Sequence[str],
    ) -> None:
        event_cache = self.runtime.event_cache
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
        try:
            if plain_batch:
                await event_cache.store_events_batch(plain_batch)
        except Exception as exc:
            self.logger.warning(
                "Failed to persist sync events to cache",
                room_id=room_id,
                event_count=len(plain_batch),
                error=str(exc),
            )
        try:
            if threaded_batch:
                await event_cache.store_events_batch(threaded_batch)
        except Exception as exc:
            self.logger.warning(
                "Failed to persist sync threaded events to cache",
                room_id=room_id,
                event_count=len(threaded_batch),
                error=str(exc),
            )
        await self._persist_threaded_sync_events(room_id, threaded_events)
        await self._apply_sync_redactions(room_id, redacted_event_ids)

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
        if not self._cache_runtime_available():
            return
        room_plain_events, room_threaded_events, room_redactions = self._group_sync_timeline_updates(response)
        for room_id in set(room_plain_events) | set(room_threaded_events) | set(room_redactions):
            plain_events = room_plain_events.get(room_id, ())
            threaded_events = room_threaded_events.get(room_id, ())
            redacted_event_ids = room_redactions.get(room_id, ())
            self._queue_room_cache_update(
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
