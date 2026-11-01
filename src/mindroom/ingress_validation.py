"""Ingress boundary validation: provenance classification, trust, dedup, and access."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from copy import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, cast

from mindroom.authorization import ReplyMembershipPendingError
from mindroom.constants import (
    ACTING_REQUESTER_KEY,
    HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
    HOOK_SOURCE_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_KEY,
)
from mindroom.contacts.admission import contact_admission
from mindroom.dispatch_source import (
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    source_kind_allows_self_authored_ingress,
    source_kind_allows_trusted_original_sender,
    source_kind_from_content,
)
from mindroom.entity_resolution import entity_identity_registry
from mindroom.logging_config import get_logger
from mindroom.matrix.event_info import EventInfo, reply_to_event_id_from_content
from mindroom.requester_identity import (
    is_access_checked_requester_id,
    is_human_requester_id,
    resolve_human_requester_alias,
)
from mindroom.turn_origin import requester_id_from_trusted_original_sender
from mindroom.turn_record import TurnRecord
from mindroom.turns.facts import IngressFacts

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Mapping

    import nio

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.constants import RuntimePaths
    from mindroom.dispatch_handoff import DispatchEvent
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.media import MatrixMediaEvent
    from mindroom.turn_store import TurnStore

logger = get_logger(__name__)

# Every bot syncs on its own, so this bot can deliver a newcomer's first message just before the router's
# sync applies their join. The first gate re-decides on each membership publication for this long; a
# decision still unresolved then fails into the journal's retry, which also re-reads this bot's roster.
_ROUTER_CATCH_UP_SECONDS = 1.0


class _SenderReplyPolicy(Protocol):
    """Minimal reply-permission surface needed at the ingress boundary."""

    def can_reply_to_sender_in_room(
        self,
        sender_id: str,
        room_id: str,
        *,
        observed_room: nio.MatrixRoom | None = None,
    ) -> bool:
        """Return whether this agent may reply to one requester in a room."""
        ...


@dataclass(frozen=True)
class IngressValidatorDeps:
    """Explicit collaborators for ingress boundary validation."""

    runtime: BotRuntimeView
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    turn_store: TurnStore
    turn_policy: _SenderReplyPolicy
    # Settles sources nothing will answer, as the room's lane does for an event it ignores.
    settle_ignored_sources: Callable[[tuple[str, ...]], Awaitable[None]]


class _InboundEvent(Protocol):
    """Any Matrix event ingress classifies: a callback event, a prepared value, or a reaction."""

    @property
    def sender(self) -> str: ...

    @property
    def source(self) -> Any: ...  # noqa: ANN401


@dataclass(frozen=True)
class IngressValidator:
    """Validate one inbound event at the ingress boundary before any batching."""

    deps: IngressValidatorDeps

    def classify(
        self,
        event: _InboundEvent,
        *,
        source_kind: str | None = None,
        room_id: str | None = None,
    ) -> IngressFacts:
        """Classify one inbound event's provenance once, before precheck.

        ``source_kind`` is the kind its transport fixes (media, voice, edit); without
        one, a trusted sender's content kind applies, else ``message``. ``room_id``
        is the room the event arrived in, which decides whether it is a contact turn.
        """
        return self.classify_source(event.sender, event.source, source_kind=source_kind, room_id=room_id)

    def classify_source(
        self,
        sender: str,
        source: object,
        *,
        source_kind: str | None = None,
        room_id: str | None = None,
    ) -> IngressFacts:
        """Classify provenance from a sender and a Matrix event source, live or stored in history."""
        source_dict = cast("dict[str, Any]", source) if isinstance(source, dict) else {}
        raw_content = source_dict.get("content")
        content: Mapping[str, Any] = cast("Mapping[str, Any]", raw_content) if isinstance(raw_content, dict) else {}
        sender_entity_name = self.managed_entity_name_for_sender(sender)
        # The one trust rule: only a sender the entity registry manages may claim where its content came from.
        trusted_content: Mapping[str, Any] = content if sender_entity_name is not None else {}
        content_source_kind = source_kind_from_content(trusted_content)
        resolved_source_kind = source_kind or content_source_kind or MESSAGE_SOURCE_KIND
        relayed = trusted_content.get(ORIGINAL_SENDER_KEY)
        relayed_sender = (
            relayed
            if isinstance(relayed, str) and relayed and source_kind_allows_trusted_original_sender(content_source_kind)
            else None
        )
        # A relayed human or configured bot account is the relay's original sender; access applies to it.
        original_sender = (
            relayed_sender if relayed_sender is not None and self._is_access_checked(relayed_sender) else None
        )
        promoted_requester = requester_id_from_trusted_original_sender(
            original_sender=relayed_sender,
            original_sender_entity_name=(
                self.managed_entity_name_for_sender(relayed_sender) if relayed_sender is not None else None
            ),
            original_sender_is_access_checked=original_sender is not None,
            source_kind=content_source_kind,
        )
        lane_requester_id = self._resolve_alias(promoted_requester or sender)
        acting_requester_id = self._acting_requester_id(sender_entity_name, trusted_content, content_source_kind)
        contact = (
            None
            if room_id is None
            else contact_admission(
                self.deps.runtime.config,
                self.deps.runtime_paths,
                room_id=room_id,
                agent_name=self.deps.agent_name,
                sender_ids=filter(None, (sender, lane_requester_id, acting_requester_id)),
            )
        )
        if contact is not None and contact.requester_id is not None:
            # Every turn of an agent party in its contact room runs as its owner; anyone else stays the speaker.
            lane_requester_id = contact.requester_id
            acting_requester_id = None if self._resolve_alias(sender) == contact.requester_id else contact.requester_id
        original_sender_is_human = original_sender is not None and is_human_requester_id(
            original_sender,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        router_relay_original_event_id = (
            reply_to_event_id_from_content(content)
            if sender_entity_name == ROUTER_AGENT_NAME
            and resolved_source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND
            and original_sender_is_human
            and not _is_falling_back_reply(content)
            else None
        )
        return IngressFacts(
            physical_sender=sender,
            source_kind=resolved_source_kind,
            lane_requester_id=lane_requester_id,
            original_sender=original_sender,
            original_sender_is_human=original_sender_is_human,
            acting_requester_id=acting_requester_id,
            hook_source=_trusted_hook_source(trusted_content),
            message_received_depth=_trusted_message_received_depth(trusted_content),
            discovery_event_id=router_relay_original_event_id
            or _final_reply_original_event_id(sender_entity_name, source_dict),
            sender_entity_name=sender_entity_name,
            requester_entity_name=self.managed_entity_name_for_sender(acting_requester_id or lane_requester_id),
            contact_id=None if contact is None else contact.contact_id,
            contact_refused=contact is not None and contact.requester_id is None,
        )

    def _is_access_checked(self, user_id: str) -> bool:
        return is_access_checked_requester_id(user_id, self.deps.runtime.config, self.deps.runtime_paths)

    def _resolve_alias(self, user_id: str) -> str:
        return resolve_human_requester_alias(user_id, self.deps.runtime.config, self.deps.runtime_paths)

    def _acting_requester_id(
        self,
        sender_entity_name: str | None,
        trusted_content: Mapping[str, Any],
        content_source_kind: str | None,
    ) -> str | None:
        """Return the human or bot-account requester an agent's or team's own reply was written for, when trusted.

        Entities that this reply mentions act for that requester: they apply their
        access policy to it and run with it as requester, while the sender stays
        the message's author for every sender-based mechanic.
        """
        acting_requester = trusted_content.get(ACTING_REQUESTER_KEY)
        if (
            not isinstance(acting_requester, str)
            or sender_entity_name in {None, ROUTER_AGENT_NAME}
            or content_source_kind not in {None, MESSAGE_SOURCE_KIND}
            or not self._is_access_checked(acting_requester)
        ):
            return None
        return self._resolve_alias(acting_requester)

    def entity_final_reply_original_event_id(self, event: DispatchEvent | MatrixMediaEvent) -> str | None:
        """Return the reply a managed entity's completed edit finalizes, or None for any other event."""
        source = event.source if isinstance(event.source, dict) else {}
        return _final_reply_original_event_id(self.managed_entity_name_for_sender(event.sender), source)

    def entity_final_reply(self, event: nio.RoomMessageFormatted) -> nio.RoomMessageFormatted | None:
        """Return a managed entity's completed edit as the reply message it finalizes.

        An entity posts a placeholder and delivers its final text as an edit of it, so other
        entities read the replacement content; the edit keeps its own event ID and relation.
        """
        content = event.source.get("content") if isinstance(event.source, dict) else None
        if not isinstance(content, dict) or not isinstance(new_content := content.get("m.new_content"), dict):
            return None
        reply = copy(event)
        reply.source = {**event.source, "content": {**new_content, "m.relates_to": content.get("m.relates_to")}}
        body = new_content.get("body")
        reply.body = body if isinstance(body, str) else event.body
        return reply if self.entity_final_reply_original_event_id(reply) is not None else None

    def sender_is_trusted_for_ingress_metadata(self, sender_id: str) -> bool:
        """Return whether one sender may supply trusted ingress metadata: the entity registry manages it."""
        return self.managed_entity_name_for_sender(sender_id) is not None

    def managed_entity_name_for_sender(self, sender_id: str) -> str | None:
        """Return the configured entity alias for an exact current Matrix user ID."""
        registry = entity_identity_registry(self.deps.runtime.config, self.deps.runtime_paths)
        return registry.current_entity_name_for_user_id(sender_id)

    async def precheck_event(
        self,
        room: nio.MatrixRoom,
        event: DispatchEvent | MatrixMediaEvent,
        facts: IngressFacts,
        *,
        is_edit: bool = False,
    ) -> bool:
        """Run shared early-exit checks for inbound text and media events; return whether the event goes on."""
        if facts.lane_requester_id == self.deps.matrix_id.full_id and not source_kind_allows_self_authored_ingress(
            facts.source_kind,
        ):
            return False

        if not is_edit and await self.deps.turn_store.is_handled(room.room_id, event.event_id):
            return False

        # Access would admit an administrator, so a contact room refuses everyone but its parties itself.
        if facts.contact_refused:
            logger.info(
                "contact_room_sender_refused",
                agent_name=self.deps.agent_name,
                room_id=room.room_id,
                event_id=event.event_id,
                sender=facts.physical_sender,
                contact_id=facts.contact_id,
            )
            await self.deps.turn_store.record_turn(TurnRecord.create([event.event_id]), room_id=room.room_id)
            return False

        # The first gate is where a newcomer's first message meets a router that has not seen the join yet.
        if not await self._can_reply_once_router_catches_up(facts.run_requester_id, room):
            await self.deps.turn_store.record_turn(TurnRecord.create([event.event_id]), room_id=room.room_id)
            return False

        return True

    @asynccontextmanager
    async def admitted_reply_decision(
        self,
        requester_id: str,
        room_id: str,
        *,
        unanswered: tuple[str, ...],
    ) -> AsyncIterator[bool]:
        """Hold the admission fence for one decision, and yield whether the requester may still be answered here.

        Authorization is current, never frozen at precheck, so it is checked again
        under the fence a configuration change waits at. A denied decision settles
        ``unanswered`` before the fence opens, so what was decided under the old
        authorization is durable before the change applies.
        """
        async with self.deps.runtime.admission_fence():
            if self.deps.turn_policy.can_reply_to_sender_in_room(requester_id, room_id):
                yield True
                return
            await self.deps.settle_ignored_sources(unanswered)
            yield False

    async def _can_reply_once_router_catches_up(self, sender_id: str, room: nio.MatrixRoom) -> bool:
        """Decide reply access, deciding again on each router membership publication while it stays unresolved.

        Only the router's published membership grants. The message itself proves only that its sender was joined
        when sending it, not that they still are, so waiting changes when the decision settles, never what it allows.
        """
        memberships = self.deps.runtime.agent_reply_memberships
        try:
            async with asyncio.timeout(_ROUTER_CATCH_UP_SECONDS):
                while True:
                    try:
                        return self.deps.turn_policy.can_reply_to_sender_in_room(
                            sender_id,
                            room.room_id,
                            observed_room=room,
                        )
                    except ReplyMembershipPendingError:
                        await memberships.wait_for_publication()
        except TimeoutError:
            raise ReplyMembershipPendingError from None


def _is_falling_back_reply(content: Mapping[str, Any]) -> bool:
    relates_to = content.get("m.relates_to")
    return isinstance(relates_to, dict) and relates_to.get("is_falling_back") is True


def _final_reply_original_event_id(sender_entity_name: str | None, source: dict[str, Any]) -> str | None:
    """Return the reply a managed non-router entity's completed edit finalizes."""
    content = source.get("content")
    if (
        not isinstance(content, dict)
        or content.get(STREAM_STATUS_KEY) != STREAM_STATUS_COMPLETED
        or sender_entity_name in {None, ROUTER_AGENT_NAME}
    ):
        return None
    return EventInfo.from_event(source).original_event_id


def _trusted_hook_source(trusted_content: Mapping[str, Any]) -> str | None:
    hook_source = trusted_content.get(HOOK_SOURCE_KEY)
    return hook_source if isinstance(hook_source, str) and hook_source else None


def _trusted_message_received_depth(trusted_content: Mapping[str, Any]) -> int:
    depth = trusted_content.get(HOOK_MESSAGE_RECEIVED_DEPTH_KEY)
    return depth if isinstance(depth, int) and not isinstance(depth, bool) and depth > 0 else 0
