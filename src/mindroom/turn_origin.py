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
    HOOK_DISPATCH = HOOK_DISPATCH_SOURCE_KIND
    TRUSTED_INTERNAL_RELAY = TRUSTED_INTERNAL_RELAY_SOURCE_KIND


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
    sender_entity_name: str | None
    requester_entity_name: str | None
    sender_kind: SenderKind
    requester_kind: SenderKind
    intent: TurnIntent
    source_kind: str
    trust: TurnTrust

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
        """Return whether a human-requested turn may answer an interactive prompt."""
        return self.requester_kind == SenderKind.USER and self.intent in {
            TurnIntent.USER_MESSAGE,
            TurnIntent.ROUTER_HANDOFF,
            TurnIntent.TRUSTED_INTERNAL_RELAY,
        }


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
    inherited_original_sender: str | None = None,
    inherited_original_sender_entity_name: str | None = None,
) -> str | None:
    """Return original-sender metadata for a real router handoff."""
    if target_entity_name is None:
        return None
    return original_sender_for_router_relay(
        requester_id=requester_id,
        requester_entity_name=requester_entity_name,
        inherited_original_sender=inherited_original_sender,
        inherited_original_sender_entity_name=inherited_original_sender_entity_name,
    )


def original_sender_for_router_relay(
    *,
    requester_id: str,
    requester_entity_name: str | None,
    inherited_original_sender: str | None = None,
    inherited_original_sender_entity_name: str | None = None,
) -> str | None:
    """Return original-sender metadata for router-authored user relays."""
    if requester_entity_name is None:
        return requester_id
    if inherited_original_sender is not None and inherited_original_sender_entity_name is None:
        return inherited_original_sender
    return None


def requester_id_from_trusted_original_sender(
    *,
    original_sender: str | None,
    original_sender_entity_name: str | None,
    source_kind: str | None,
    sender_trusts_original_sender: bool,
) -> str | None:
    """Return original-sender metadata that may act as the dispatch requester."""
    if not sender_trusts_original_sender or not original_sender:
        return None
    if original_sender_entity_name is None:
        return original_sender
    if source_kind == SCHEDULED_SOURCE_KIND:
        return original_sender
    return None


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
