"""Serialize Matrix source redactions with durable conversation replay state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.handled_turns import TurnRecord
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRunner
    from mindroom.turn_store import TurnStore


_DEFINITIVE_SOURCE_MISS_ERRCODES = frozenset({"M_NOT_FOUND", "M_FORBIDDEN"})


@dataclass(frozen=True)
class RedactedTurnCleanupDeps:
    """Collaborators needed to resolve and remove one redacted source turn."""

    conversation_cache: MatrixConversationCache
    resolver: ConversationResolver
    ingress: IngressValidator
    response_runner: ResponseRunner
    turn_store: TurnStore


@dataclass(frozen=True)
class _RecoveredSourceContext:
    """Context recovered for one redacted source before cache mutation."""

    target: MessageTarget | None
    requester_user_id: str | None
    source_definitively_gone: bool = False


@dataclass
class RedactedTurnCleanup:
    """Own source-redaction identity, tombstoning, and serialized replay cleanup."""

    deps: RedactedTurnCleanupDeps

    async def handle(self, room: nio.MatrixRoom, event: nio.RedactionEvent) -> None:
        """Tombstone one source before cache mutation, then remove durable replay."""
        redacted_event_id = event.redacts
        turn_record = await asyncio.to_thread(
            self.deps.turn_store.mark_source_redacted,
            redacted_event_id,
            room_id=room.room_id,
        )
        await self._complete_cleanup(room=room, event=event, turn_record=turn_record)

    async def resume_pending(self) -> None:
        """Finish durable redactions interrupted before cache and history cleanup."""
        pending_cleanups = await asyncio.to_thread(self.deps.turn_store.pending_redaction_cleanups)
        for redacted_event_id, room_id in pending_cleanups:
            turn_record = await asyncio.to_thread(self.deps.turn_store.get_turn_record, redacted_event_id)
            if turn_record is None:
                continue
            room = nio.MatrixRoom(room_id=room_id, own_user_id="")
            event = nio.RedactionEvent(
                {
                    "event_id": f"$redaction-recovery:{redacted_event_id.removeprefix('$')}",
                    "sender": "",
                    "origin_server_ts": 0,
                    "type": "m.room.redaction",
                    "content": {},
                },
                redacted_event_id,
            )
            await self._complete_cleanup(room=room, event=event, turn_record=turn_record)

    async def _complete_cleanup(
        self,
        *,
        room: nio.MatrixRoom,
        event: nio.RedactionEvent,
        turn_record: TurnRecord | None,
    ) -> None:
        """Resolve one durable intent, sanitize cache state, and serialize history cleanup."""
        redacted_event_id = event.redacts
        target = turn_record.conversation_target if turn_record is not None else None
        requester_user_id = turn_record.requester_id if turn_record is not None else None
        source_definitively_gone = False
        if target is None or requester_user_id is None:
            recovered = await self._resolve_missing_context(
                room_id=room.room_id,
                redacted_event_id=redacted_event_id,
                target=target,
                requester_user_id=requester_user_id,
            )
            target = recovered.target
            requester_user_id = recovered.requester_user_id
            source_definitively_gone = recovered.source_definitively_gone
            turn_record = await asyncio.to_thread(
                self.deps.turn_store.mark_source_redacted,
                redacted_event_id,
                room_id=room.room_id,
                requester_user_id=requester_user_id,
                target_hint=target,
            )
            if turn_record is not None:
                target = turn_record.conversation_target or target
                requester_user_id = turn_record.requester_id or requester_user_id

        cache_sanitized = await self.deps.conversation_cache.apply_redaction(room.room_id, event)
        if target is None or requester_user_id is None:
            # A definitively gone source can never regain context on retry, so
            # once the cache is sanitized the durable intent has nothing left
            # to clean and must not be retried at every startup forever.
            if cache_sanitized and source_definitively_gone:
                await asyncio.to_thread(
                    self.deps.turn_store.clear_pending_redaction_cleanup,
                    redacted_event_id,
                )
            return
        await self.deps.response_runner.run_serialized_state_mutation(
            target=target,
            mutation=lambda: self.deps.turn_store.forget_redacted_turn(
                room=room,
                redacted_event_id=redacted_event_id,
                requester_user_id=requester_user_id,
                target_hint=target,
                cache_sanitized=cache_sanitized,
            ),
        )

    async def _resolve_missing_context(
        self,
        *,
        room_id: str,
        redacted_event_id: str,
        target: MessageTarget | None,
        requester_user_id: str | None,
    ) -> _RecoveredSourceContext:
        """Recover target and original requester before the cache applies redaction."""
        response = await self.deps.conversation_cache.get_event(
            room_id,
            redacted_event_id,
            persist_lookup_fill=False,
        )
        if not isinstance(response, nio.RoomGetEventResponse):
            return _RecoveredSourceContext(
                target=target,
                requester_user_id=requester_user_id,
                source_definitively_gone=(
                    isinstance(response, nio.RoomGetEventError)
                    and response.status_code in _DEFINITIVE_SOURCE_MISS_ERRCODES
                ),
            )
        source_event = response.event
        source = source_event.source if isinstance(source_event.source, dict) else None
        if target is None:
            event_info = EventInfo.from_event(source)
            thread_id = event_info.thread_id or event_info.thread_id_from_edit
            related_event_id = event_info.next_related_event_id(redacted_event_id)
            if thread_id is None and related_event_id is not None:
                thread_id = await self.deps.resolver.resolve_related_event_thread_id_dispatch_snapshot_best_effort(
                    room_id,
                    related_event_id,
                    caller_label="redacted_turn_cleanup",
                )
            target = self.deps.resolver.build_message_target(
                room_id=room_id,
                thread_id=thread_id,
                reply_to_event_id=redacted_event_id,
                event_source=source,
            )
        if requester_user_id is None:
            requester_user_id = self.deps.ingress.requester_user_id(
                sender=source_event.sender,
                source=source,
            )
        return _RecoveredSourceContext(target=target, requester_user_id=requester_user_id)
