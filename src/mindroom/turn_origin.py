"""Canonical origin classification for inbound Matrix turns."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_source import (
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    is_automation_source_kind,
)


class SenderKind(StrEnum):
    """Transport-level sender category for one inbound turn."""

    USER = "user"
    MANAGED_ENTITY = "managed_entity"


class TurnIntent(StrEnum):
    """Semantic intent of one inbound turn after trusted metadata is normalized."""

    USER_MESSAGE = "user_message"
    MANAGED_MESSAGE = "managed_message"
    ROUTER_HANDOFF = "router_handoff"
    ROUTER_NOTICE = "router_notice"
    SCHEDULED_FIRE = "scheduled_fire"
    HOOK_MESSAGE = "hook_message"
    HOOK_DISPATCH = "hook_dispatch"
    TRUSTED_INTERNAL_RELAY = "trusted_internal_relay"


class TurnTrust(StrEnum):
    """How much dispatch policy may trust one turn's internal metadata."""

    EXTERNAL = "external"
    TRUSTED_INTERNAL = "trusted_internal"
    TRUSTED_USER_RELAY = "trusted_user_relay"


@dataclass(frozen=True, slots=True)
class TurnOrigin:
    """Single source of truth for dispatch policy about where a turn came from."""

    transport_sender_id: str
    requester_id: str
    author_id: str | None
    sender_entity_name: str | None
    requester_entity_name: str | None
    sender_kind: SenderKind
    requester_kind: SenderKind
    intent: TurnIntent
    source_kind: str
    trust: TurnTrust

    @property
    def is_automation(self) -> bool:
        """Return whether the turn is synthetic automation."""
        return is_automation_source_kind(self.source_kind)

    @property
    def may_dispatch_without_mention(self) -> bool:
        """Return whether this synthetic turn may bypass the managed-sender mention gate."""
        return self.intent in {
            TurnIntent.HOOK_DISPATCH,
            TurnIntent.ROUTER_HANDOFF,
            TurnIntent.SCHEDULED_FIRE,
        }

    @property
    def blocks_unmentioned_managed_sender(self) -> bool:
        """Return whether an unmentioned managed sender should be treated as chatter."""
        return self.requester_kind == SenderKind.MANAGED_ENTITY and not self.may_dispatch_without_mention

    @property
    def may_answer_interactive_prompt(self) -> bool:
        """Return whether the turn may be handled as a human interactive response."""
        return not self.is_automation


def classify_turn_origin(
    *,
    transport_sender_id: str,
    requester_id: str,
    sender_entity_name: str | None,
    requester_entity_name: str | None,
    source_kind: str,
    original_sender: str | None,
    trusted_user_relay: bool,
) -> TurnOrigin:
    """Return the canonical origin policy for one inbound turn."""
    sender_kind = SenderKind.MANAGED_ENTITY if sender_entity_name is not None else SenderKind.USER
    requester_kind = SenderKind.MANAGED_ENTITY if requester_entity_name is not None else SenderKind.USER
    trust = _turn_trust(
        sender_kind=sender_kind,
        original_sender=original_sender,
        trusted_user_relay=trusted_user_relay,
    )
    return TurnOrigin(
        transport_sender_id=transport_sender_id,
        requester_id=requester_id,
        author_id=_author_id(
            requester_id=requester_id,
            original_sender=original_sender,
            trust=trust,
        ),
        sender_entity_name=sender_entity_name,
        requester_entity_name=requester_entity_name,
        sender_kind=sender_kind,
        requester_kind=requester_kind,
        intent=_turn_intent(
            sender_entity_name=sender_entity_name,
            sender_kind=sender_kind,
            source_kind=source_kind,
            trust=trust,
        ),
        source_kind=source_kind,
        trust=trust,
    )


def original_sender_for_router_handoff(
    *,
    target_entity_name: str | None,
    requester_id: str,
    requester_entity_name: str | None,
) -> str | None:
    """Return original-sender metadata for a real router handoff."""
    if target_entity_name is None:
        return None
    if requester_entity_name is not None:
        return None
    return requester_id


def _turn_trust(
    *,
    sender_kind: SenderKind,
    original_sender: str | None,
    trusted_user_relay: bool,
) -> TurnTrust:
    if trusted_user_relay and original_sender:
        return TurnTrust.TRUSTED_USER_RELAY
    if sender_kind == SenderKind.MANAGED_ENTITY:
        return TurnTrust.TRUSTED_INTERNAL
    return TurnTrust.EXTERNAL


def _author_id(
    *,
    requester_id: str,
    original_sender: str | None,
    trust: TurnTrust,
) -> str | None:
    if trust == TurnTrust.TRUSTED_USER_RELAY:
        return original_sender or requester_id
    return original_sender


def _turn_intent(
    *,
    sender_entity_name: str | None,
    sender_kind: SenderKind,
    source_kind: str,
    trust: TurnTrust,
) -> TurnIntent:
    intent: TurnIntent
    if trust == TurnTrust.TRUSTED_USER_RELAY:
        if sender_entity_name == ROUTER_AGENT_NAME:
            intent = TurnIntent.ROUTER_HANDOFF
        else:
            intent = TurnIntent.TRUSTED_INTERNAL_RELAY
    elif source_kind == SCHEDULED_SOURCE_KIND:
        intent = TurnIntent.SCHEDULED_FIRE
    elif source_kind == HOOK_DISPATCH_SOURCE_KIND:
        intent = TurnIntent.HOOK_DISPATCH
    elif source_kind == HOOK_SOURCE_KIND:
        intent = TurnIntent.HOOK_MESSAGE
    elif sender_entity_name == ROUTER_AGENT_NAME:
        intent = TurnIntent.ROUTER_NOTICE
    elif source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND:
        intent = TurnIntent.TRUSTED_INTERNAL_RELAY
    elif sender_kind == SenderKind.MANAGED_ENTITY:
        intent = TurnIntent.MANAGED_MESSAGE
    else:
        intent = TurnIntent.USER_MESSAGE
    return intent
