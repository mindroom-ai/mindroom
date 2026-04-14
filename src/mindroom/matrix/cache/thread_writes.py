"""Thread mutation and write-through policy for Matrix conversation cache."""

from __future__ import annotations

import time
import typing
from typing import TYPE_CHECKING, Any

import nio

from mindroom.matrix.cache.event_cache import normalize_event_source_for_cache
from mindroom.matrix.cache.thread_cache_helpers import event_id_from_event_source, log_resolved_thread_cache
from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Coroutine, Sequence

    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.matrix.cache.event_cache import ConversationEventCache
    from mindroom.matrix.cache.thread_cache import ResolvedThreadCache
    from mindroom.matrix.reply_chain import ReplyChainCaches


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
    if event_info.is_thread or event_info.is_edit:
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
    event_source = event.source if isinstance(event.source, dict) else {}
    event_id = event.event_id
    if not isinstance(event_id, str) or not event_id:
        return None
    sender = event.sender if isinstance(event.sender, str) else None
    server_timestamp = _sync_event_origin_server_ts(event)
    normalized_event_source = normalize_event_source_for_cache(
        event_source,
        event_id=event_id,
        sender=sender,
        origin_server_ts=server_timestamp,
    )
    return room_id, normalized_event_source


def _sync_event_origin_server_ts(event: nio.Event) -> int | None:
    server_timestamp = event.server_timestamp
    if isinstance(server_timestamp, int) and not isinstance(server_timestamp, bool):
        return server_timestamp
    event_source = event.source if isinstance(event.source, dict) else {}
    origin_server_ts = event_source.get("origin_server_ts")
    if isinstance(origin_server_ts, int) and not isinstance(origin_server_ts, bool):
        return origin_server_ts
    return None


def _threaded_sync_event_cache_update(
    room_id: str,
    event: nio.Event,
) -> tuple[str, dict[str, object]] | None:
    event_source = event.source if isinstance(event.source, dict) else {}
    event_info = EventInfo.from_event(event_source)
    if not (event_info.is_thread or event_info.is_edit):
        return None
    event_id = event.event_id
    if not isinstance(event_id, str) or not event_id:
        return None
    sender = event.sender if isinstance(event.sender, str) else None
    server_timestamp = _sync_event_origin_server_ts(event)
    normalized_event_source = normalize_event_source_for_cache(
        event_source,
        event_id=event_id,
        sender=sender,
        origin_server_ts=server_timestamp,
    )
    return room_id, normalized_event_source


def _has_explicit_thread_relation(event_info: EventInfo) -> bool:
    return isinstance(event_info.thread_id, str) or isinstance(event_info.thread_id_from_edit, str)


async def _resolve_thread_id_for_cached_event_append(
    room_id: str,
    *,
    event_info: EventInfo,
    event_cache: ConversationEventCache,
) -> str | None:
    if isinstance(event_info.thread_id, str):
        return event_info.thread_id
    if isinstance(event_info.thread_id_from_edit, str):
        return event_info.thread_id_from_edit
    if not isinstance(event_info.original_event_id, str):
        return None
    return await event_cache.get_thread_id_for_event(room_id, event_info.original_event_id)


class ThreadWritePolicy:
    """Own thread-affecting cache mutations and outbound write-through."""

    def __init__(
        self,
        *,
        logger_getter: typing.Callable[[], structlog.stdlib.BoundLogger],
        runtime: BotRuntimeView,
        resolved_thread_cache_getter: typing.Callable[[], ResolvedThreadCache],
        reply_chain_caches_getter: typing.Callable[[], ReplyChainCaches | None],
        require_client: typing.Callable[[], nio.AsyncClient],
    ) -> None:
        self._logger_getter = logger_getter
        self.runtime = runtime
        self._resolved_thread_cache_getter = resolved_thread_cache_getter
        self._reply_chain_caches_getter = reply_chain_caches_getter
        self.require_client = require_client

    @property
    def logger(self) -> structlog.stdlib.BoundLogger:
        """Return the facade-bound logger so collaborator rebinding stays visible."""
        return self._logger_getter()

    def _resolved_thread_cache(self) -> ResolvedThreadCache:
        return self._resolved_thread_cache_getter()

    def _reply_chain_caches(self) -> ReplyChainCaches | None:
        return self._reply_chain_caches_getter()

    def _queue_room_cache_update(
        self,
        room_id: str,
        update_coro_factory: Callable[[], Coroutine[Any, Any, object]],
        *,
        name: str,
    ) -> asyncio.Task[object]:
        coordinator = self.runtime.event_cache_write_coordinator
        return coordinator.queue_room_update(room_id, update_coro_factory, name=name)

    def _invalidate_reply_chain(self, room_id: str, *event_ids: str | None) -> None:
        reply_chain_caches = self._reply_chain_caches()
        if reply_chain_caches is None:
            return
        reply_chain_caches.invalidate(room_id, event_ids)

    async def _reply_chain_invalidation_ids_for_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        event_cache: ConversationEventCache,
    ) -> set[str]:
        invalidation_ids = {redacted_event_id}
        try:
            cached_event = await event_cache.get_event(room_id, redacted_event_id)
        except Exception as exc:
            self.logger.warning(
                "Failed to inspect cached redacted event for reply-chain invalidation",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
            return invalidation_ids
        if not isinstance(cached_event, dict):
            return invalidation_ids
        original_event_id = EventInfo.from_event(cached_event).original_event_id
        if isinstance(original_event_id, str):
            invalidation_ids.add(original_event_id)
        return invalidation_ids

    async def _reply_chain_invalidation_ids_for_sync_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
        *,
        event_cache: ConversationEventCache,
        event_source: dict[str, object] | None,
    ) -> set[str]:
        invalidation_ids = {redacted_event_id}
        candidate_event_source = event_source
        if candidate_event_source is None:
            reply_chain_caches = self._reply_chain_caches()
            if reply_chain_caches is not None:
                candidate_event_source = reply_chain_caches.event_source(room_id, redacted_event_id)
        if candidate_event_source is None:
            return await self._reply_chain_invalidation_ids_for_redaction(
                room_id,
                redacted_event_id,
                event_cache=event_cache,
            )
        if not isinstance(candidate_event_source, dict):
            return invalidation_ids
        original_event_id = EventInfo.from_event(candidate_event_source).original_event_id
        if isinstance(original_event_id, str):
            invalidation_ids.add(original_event_id)
        return invalidation_ids

    async def _invalidate_known_thread(
        self,
        room_id: str,
        thread_id: str,
        *,
        reason: str,
    ) -> None:
        async with self._resolved_thread_cache().entry_lock(room_id, thread_id):
            self._resolved_thread_cache().invalidate(room_id, thread_id)
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
        log_resolved_thread_cache(
            self.logger,
            "resolved_thread_cache_invalidate",
            room_id=room_id,
            thread_id=thread_id,
            reason=reason,
        )

    async def _invalidate_room_threads(
        self,
        room_id: str,
        *,
        reason: str,
    ) -> None:
        thread_ids = self._resolved_thread_cache().invalidate_room(room_id)
        for thread_id in thread_ids:
            log_resolved_thread_cache(
                self.logger,
                "resolved_thread_cache_invalidate",
                room_id=room_id,
                thread_id=thread_id,
                reason=reason,
            )
        try:
            await self.runtime.event_cache.mark_room_threads_stale(room_id, reason=reason)
        except Exception as exc:
            self.logger.warning(
                "Failed to mark cached room threads stale",
                room_id=room_id,
                reason=reason,
                error=str(exc),
            )

    async def _resolve_thread_id_for_mutation(
        self,
        room_id: str,
        *,
        event_info: EventInfo,
        event_id: str | None,
        context: str,
    ) -> str | None:
        try:
            thread_id = await _resolve_thread_id_for_cached_event_append(
                room_id,
                event_info=event_info,
                event_cache=self.runtime.event_cache,
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
        if thread_id is not None:
            return thread_id
        if _has_explicit_thread_relation(event_info):
            return event_info.thread_id or event_info.thread_id_from_edit
        return None

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

    async def _record_outbound_message_update(
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
            await self._invalidate_room_threads(room_id, reason="outbound_lookup_missing")
            return
        await self._append_event_to_cache(
            room_id,
            thread_id,
            event_source,
            context="outbound",
        )
        await self._invalidate_known_thread(
            room_id,
            thread_id,
            reason="outbound_thread_mutation",
        )

    async def record_outbound_message(
        self,
        room_id: str,
        event_id: str | None,
        content: dict[str, Any],
    ) -> None:
        """Write one locally sent threaded message or edit through to the cache."""
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
        is_thread_candidate = isinstance(event_info.thread_id, str) or (
            event_info.is_edit
            and (isinstance(event_info.thread_id_from_edit, str) or isinstance(event_info.original_event_id, str))
        )
        if not is_thread_candidate:
            return
        if event_info.is_edit:
            self._invalidate_reply_chain(room_id, event_id, event_info.original_event_id)

        try:
            await self._queue_room_cache_update(
                room_id,
                lambda: self._record_outbound_message_update(room_id, event_id, event_source, event_info),
                name="matrix_cache_record_outbound_message",
            )
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound threaded message cache write-through failure after successful send",
                room_id=room_id,
                event_id=event_id,
                error=str(exc),
            )

    async def _record_outbound_redaction_update(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        event_cache = self.runtime.event_cache
        reply_chain_invalidation_ids = await self._reply_chain_invalidation_ids_for_redaction(
            room_id,
            redacted_event_id,
            event_cache=event_cache,
        )
        self._invalidate_reply_chain(room_id, *reply_chain_invalidation_ids)

        try:
            thread_id = await event_cache.get_thread_id_for_event(room_id, redacted_event_id)
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound Matrix redaction cache lookup failure after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                error=str(exc),
            )
            thread_id = None

        try:
            await event_cache.redact_event(room_id, redacted_event_id)
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound Matrix redaction cache write-through failure after successful redact",
                room_id=room_id,
                redacted_event_id=redacted_event_id,
                thread_id=thread_id,
                error=str(exc),
            )

        if thread_id is not None:
            await self._invalidate_known_thread(
                room_id,
                thread_id,
                reason="outbound_redaction",
            )
            return
        await self._invalidate_room_threads(room_id, reason="outbound_redaction_lookup_missing")

    async def record_outbound_redaction(
        self,
        room_id: str,
        redacted_event_id: str,
    ) -> None:
        """Write one locally redacted threaded message through to the cache."""
        if not redacted_event_id:
            return
        try:
            await self._queue_room_cache_update(
                room_id,
                lambda: self._record_outbound_redaction_update(room_id, redacted_event_id),
                name="matrix_cache_record_outbound_redaction",
            )
        except Exception as exc:
            self.logger.warning(
                "Ignoring outbound Matrix redaction cache write-through failure after successful redact",
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
        if event_info.is_edit:
            self._invalidate_reply_chain(room_id, event.event_id, event_info.original_event_id)

        thread_id = await self._resolve_thread_id_for_mutation(
            room_id,
            event_info=event_info,
            event_id=event.event_id,
            context="live",
        )
        if thread_id is None:
            await self._invalidate_room_threads(room_id, reason="live_lookup_missing")
            return

        raw_event_source = event.source if isinstance(event.source, dict) else {}
        event_source = normalize_event_source_for_cache(
            raw_event_source,
            event_id=event.event_id,
            sender=event.sender,
            origin_server_ts=event.server_timestamp,
        )

        async def append_and_invalidate() -> bool:
            appended = await self._append_event_to_cache(
                room_id,
                thread_id,
                event_source,
                context="live",
            )
            await self._invalidate_known_thread(
                room_id,
                thread_id,
                reason="live_thread_mutation" if appended else "live_append_failed",
            )
            return appended

        await self._queue_room_cache_update(
            room_id,
            append_and_invalidate,
            name="matrix_cache_append_live_event",
        )

    async def apply_redaction(self, room_id: str, event: nio.RedactionEvent) -> None:
        """Apply one redaction to the advisory cache when the affected thread is known."""
        event_cache = self.runtime.event_cache
        invalidation_ids = await self._reply_chain_invalidation_ids_for_redaction(
            room_id,
            event.redacts,
            event_cache=event_cache,
        )
        self._invalidate_reply_chain(room_id, *invalidation_ids)

        try:
            thread_id = await event_cache.get_thread_id_for_event(room_id, event.redacts)
        except Exception as exc:
            self.logger.warning(
                "Failed to resolve cached thread for redaction",
                room_id=room_id,
                event_id=event.event_id,
                redacted_event_id=event.redacts,
                error=str(exc),
            )
            thread_id = None

        async def redact_and_invalidate() -> bool:
            try:
                redacted = await event_cache.redact_event(room_id, event.redacts)
            except Exception as exc:
                self.logger.warning(
                    "Failed to apply live redaction to cache",
                    room_id=room_id,
                    thread_id=thread_id,
                    redacted_event_id=event.redacts,
                    error=str(exc),
                )
                redacted = False
            if thread_id is not None:
                await self._invalidate_known_thread(
                    room_id,
                    thread_id,
                    reason="live_redaction" if redacted else "live_redaction_failed",
                )
            else:
                await self._invalidate_room_threads(room_id, reason="live_redaction_lookup_missing")
            return bool(redacted)

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
                await self._invalidate_room_threads(room_id, reason="sync_lookup_missing")
                continue
            appended = await self._append_event_to_cache(
                room_id,
                thread_id,
                event_source,
                context="sync",
            )
            await self._invalidate_known_thread(
                room_id,
                thread_id,
                reason="sync_thread_mutation" if appended else "sync_append_failed",
            )

    async def _apply_sync_redactions(
        self,
        event_cache: ConversationEventCache,
        room_id: str,
        redacted_event_ids: Sequence[str],
    ) -> None:
        for redacted_event_id in redacted_event_ids:
            reply_chain_invalidation_ids = await self._reply_chain_invalidation_ids_for_sync_redaction(
                room_id,
                redacted_event_id,
                event_cache=event_cache,
                event_source=None,
            )
            self._invalidate_reply_chain(room_id, *reply_chain_invalidation_ids)
            try:
                thread_id = await event_cache.get_thread_id_for_event(room_id, redacted_event_id)
            except Exception as exc:
                self.logger.warning(
                    "Failed to resolve cached thread for sync redaction",
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    error=str(exc),
                )
                thread_id = None

            try:
                redacted = await event_cache.redact_event(room_id, redacted_event_id)
            except Exception as exc:
                self.logger.warning(
                    "Failed to apply sync redaction to cache",
                    room_id=room_id,
                    redacted_event_id=redacted_event_id,
                    thread_id=thread_id,
                    error=str(exc),
                )
                redacted = False

            if thread_id is not None:
                await self._invalidate_known_thread(
                    room_id,
                    thread_id,
                    reason="sync_redaction" if redacted else "sync_redaction_failed",
                )
            else:
                await self._invalidate_room_threads(room_id, reason="sync_redaction_lookup_missing")

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
        await self._apply_sync_redactions(event_cache, room_id, redacted_event_ids)

    def _track_sync_cached_event(
        self,
        room_id: str,
        event_source: dict[str, object],
    ) -> None:
        event_info = EventInfo.from_event(event_source)
        if event_info.is_edit:
            self._invalidate_reply_chain(
                room_id,
                event_info.original_event_id,
                event_id_from_event_source(event_source),
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
                event_source = event.source if isinstance(event.source, dict) else None
                if isinstance(event_source, dict):
                    self._track_sync_cached_event(room_id, event_source)
        return room_plain_events, room_threaded_events, room_redactions

    def cache_sync_timeline(self, response: nio.SyncResponse) -> None:
        """Queue sync timeline persistence through the room-ordered cache barrier."""
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
