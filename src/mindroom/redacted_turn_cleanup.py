"""Serialize Matrix source redactions with durable conversation replay state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

from mindroom.matrix.event_info import EventInfo

if TYPE_CHECKING:
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.message_target import MessageTarget
    from mindroom.response_runner import ResponseRunner
    from mindroom.turn_store import TurnStore


@dataclass(frozen=True)
class RedactedTurnCleanupDeps:
    """Collaborators needed to resolve and remove one redacted source turn."""

    conversation_cache: MatrixConversationCache
    resolver: ConversationResolver
    ingress: IngressValidator
    response_runner: ResponseRunner
    turn_store: TurnStore


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
        )
        target = turn_record.conversation_target if turn_record is not None else None
        requester_user_id = turn_record.requester_id if turn_record is not None else None
        if target is None or requester_user_id is None:
            target, requester_user_id = await self._resolve_missing_context(
                room_id=room.room_id,
                redacted_event_id=redacted_event_id,
                target=target,
                requester_user_id=requester_user_id,
            )
            turn_record = await asyncio.to_thread(
                self.deps.turn_store.mark_source_redacted,
                redacted_event_id,
                requester_user_id=requester_user_id,
                target_hint=target,
            )
            if turn_record is not None:
                target = turn_record.conversation_target or target
                requester_user_id = turn_record.requester_id or requester_user_id

        await self.deps.conversation_cache.apply_redaction(room.room_id, event)
        if target is None or requester_user_id is None:
            return
        await self.deps.response_runner.run_serialized_state_mutation(
            target=target,
            mutation=lambda: self.deps.turn_store.forget_redacted_turn(
                room=room,
                redacted_event_id=redacted_event_id,
                requester_user_id=requester_user_id,
                target_hint=target,
            ),
        )

    async def _resolve_missing_context(
        self,
        *,
        room_id: str,
        redacted_event_id: str,
        target: MessageTarget | None,
        requester_user_id: str | None,
    ) -> tuple[MessageTarget | None, str | None]:
        """Recover target and original requester before the cache applies redaction."""
        response = await self.deps.conversation_cache.get_event(
            room_id,
            redacted_event_id,
            persist_lookup_fill=False,
        )
        if not isinstance(response, nio.RoomGetEventResponse):
            return target, requester_user_id
        source_event = response.event
        source = source_event.source if isinstance(source_event.source, dict) else None
        if target is None:
            event_info = EventInfo.from_event(source)
            target = self.deps.resolver.build_message_target(
                room_id=room_id,
                thread_id=event_info.thread_id,
                reply_to_event_id=redacted_event_id,
                event_source=source,
            )
        if requester_user_id is None:
            requester_user_id = self.deps.ingress.requester_user_id(
                sender=source_event.sender,
                source=source,
            )
        return target, requester_user_id
