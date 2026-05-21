"""Tests for canonical inbound turn-origin policy."""

from mindroom.dispatch_source import (
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
)
from mindroom.turn_origin import (
    SenderKind,
    TurnIntent,
    TurnTrust,
    classify_turn_origin,
    original_sender_for_router_handoff,
)


def test_managed_sender_message_is_chatter_that_requires_mention() -> None:
    """Managed requesters are treated as agent chatter unless policy says otherwise."""
    origin = classify_turn_origin(
        transport_sender_id="@mindroom_general:localhost",
        requester_id="@mindroom_general:localhost",
        sender_entity_name="general",
        requester_entity_name="general",
        source_kind="message",
        original_sender=None,
        trusted_user_relay=False,
    )

    assert origin.sender_kind == SenderKind.MANAGED_ENTITY
    assert origin.requester_kind == SenderKind.MANAGED_ENTITY
    assert origin.intent == TurnIntent.MANAGED_MESSAGE
    assert origin.trust == TurnTrust.TRUSTED_INTERNAL
    assert origin.blocks_unmentioned_managed_sender
    assert not origin.may_dispatch_without_mention


def test_scheduled_managed_sender_bypasses_agent_chatter_gate() -> None:
    """Scheduled fires bypass the managed-requester chatter gate."""
    origin = classify_turn_origin(
        transport_sender_id="@mindroom_general:localhost",
        requester_id="@mindroom_router:localhost",
        sender_entity_name="general",
        requester_entity_name="router",
        source_kind=SCHEDULED_SOURCE_KIND,
        original_sender="@mindroom_router:localhost",
        trusted_user_relay=False,
    )

    assert origin.intent == TurnIntent.SCHEDULED_FIRE
    assert origin.trust == TurnTrust.TRUSTED_INTERNAL
    assert origin.author_id == "@mindroom_router:localhost"
    assert not origin.blocks_unmentioned_managed_sender
    assert origin.may_dispatch_without_mention


def test_hook_dispatch_bypasses_agent_chatter_gate_but_plain_hook_does_not() -> None:
    """Only hook dispatch grants an explicit mention-gate bypass."""
    hook_dispatch = classify_turn_origin(
        transport_sender_id="@mindroom_general:localhost",
        requester_id="@human:localhost",
        sender_entity_name="general",
        requester_entity_name=None,
        source_kind=HOOK_DISPATCH_SOURCE_KIND,
        original_sender="@human:localhost",
        trusted_user_relay=False,
    )
    plain_hook = classify_turn_origin(
        transport_sender_id="@mindroom_general:localhost",
        requester_id="@human:localhost",
        sender_entity_name="general",
        requester_entity_name=None,
        source_kind=HOOK_SOURCE_KIND,
        original_sender="@human:localhost",
        trusted_user_relay=False,
    )

    assert hook_dispatch.intent == TurnIntent.HOOK_DISPATCH
    assert hook_dispatch.may_dispatch_without_mention
    assert not hook_dispatch.blocks_unmentioned_managed_sender
    assert plain_hook.intent == TurnIntent.HOOK_MESSAGE
    assert not plain_hook.may_dispatch_without_mention
    assert not plain_hook.blocks_unmentioned_managed_sender


def test_plain_hook_with_managed_requester_still_requires_mention() -> None:
    """Plain hook sends keep normal managed-requester mention routing."""
    origin = classify_turn_origin(
        transport_sender_id="@mindroom_general:localhost",
        requester_id="@mindroom_general:localhost",
        sender_entity_name="general",
        requester_entity_name="general",
        source_kind=HOOK_SOURCE_KIND,
        original_sender=None,
        trusted_user_relay=False,
    )

    assert origin.intent == TurnIntent.HOOK_MESSAGE
    assert not origin.may_dispatch_without_mention
    assert origin.blocks_unmentioned_managed_sender


def test_router_handoff_is_trusted_user_relay() -> None:
    """Router handoffs are trusted relays of the original human author."""
    origin = classify_turn_origin(
        transport_sender_id="@mindroom_router:localhost",
        requester_id="@human:localhost",
        sender_entity_name="router",
        requester_entity_name=None,
        source_kind=TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
        original_sender="@human:localhost",
        trusted_user_relay=True,
    )

    assert origin.intent == TurnIntent.ROUTER_HANDOFF
    assert origin.trust == TurnTrust.TRUSTED_USER_RELAY
    assert origin.author_id == "@human:localhost"
    assert not origin.blocks_unmentioned_managed_sender


def test_router_notice_stays_internal_chatter() -> None:
    """Router notices are internal chatter, not user-origin handoffs."""
    origin = classify_turn_origin(
        transport_sender_id="@mindroom_router:localhost",
        requester_id="@mindroom_router:localhost",
        sender_entity_name="router",
        requester_entity_name="router",
        source_kind="message",
        original_sender=None,
        trusted_user_relay=False,
    )

    assert origin.intent == TurnIntent.ROUTER_NOTICE
    assert origin.trust == TurnTrust.TRUSTED_INTERNAL
    assert origin.author_id is None
    assert origin.blocks_unmentioned_managed_sender


def test_router_handoff_original_sender_only_for_human_targeted_handoff() -> None:
    """Router handoff metadata is stamped only on real targeted human requests."""
    assert (
        original_sender_for_router_handoff(
            target_entity_name="general",
            requester_id="@human:localhost",
            requester_entity_name=None,
        )
        == "@human:localhost"
    )
    assert (
        original_sender_for_router_handoff(
            target_entity_name=None,
            requester_id="@human:localhost",
            requester_entity_name=None,
        )
        is None
    )
    assert (
        original_sender_for_router_handoff(
            target_entity_name="general",
            requester_id="@mindroom_router:localhost",
            requester_entity_name="router",
        )
        is None
    )
    assert (
        original_sender_for_router_handoff(
            target_entity_name="general",
            requester_id="@human:localhost",
            requester_entity_name=None,
            inherited_original_sender="@stale:localhost",
            inherited_original_sender_entity_name=None,
        )
        == "@human:localhost"
    )
    assert (
        original_sender_for_router_handoff(
            target_entity_name="general",
            requester_id="@mindroom_alpha:localhost",
            requester_entity_name="alpha",
            inherited_original_sender="@human:localhost",
            inherited_original_sender_entity_name=None,
        )
        == "@human:localhost"
    )
    assert (
        original_sender_for_router_handoff(
            target_entity_name="general",
            requester_id="@mindroom_alpha:localhost",
            requester_entity_name="alpha",
            inherited_original_sender="@mindroom_alpha:localhost",
            inherited_original_sender_entity_name="alpha",
        )
        is None
    )
