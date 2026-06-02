"""Tests for canonical inbound turn-origin policy."""

from mindroom.dispatch_source import (
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
)
from mindroom.turn_origin import (
    TurnIntent,
    classify_turn_origin,
    original_sender_for_router_handoff,
    original_sender_for_router_relay,
    requester_id_from_trusted_original_sender,
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

    assert origin.sender_kind.value == "managed_entity"
    assert origin.requester_kind.value == "managed_entity"
    assert origin.intent == TurnIntent.MANAGED_MESSAGE
    assert origin.trust.value == "trusted_internal"
    assert origin.blocks_unmentioned_managed_sender
    assert not origin.may_dispatch_without_mention
    assert not origin.may_answer_interactive_prompt


def test_managed_message_with_human_requester_cannot_answer_interactive_prompt() -> None:
    """Managed-message intent is never a human prompt answer."""
    origin = classify_turn_origin(
        transport_sender_id="@mindroom_general:localhost",
        requester_id="@human:localhost",
        sender_entity_name="general",
        requester_entity_name=None,
        source_kind="message",
        original_sender=None,
        trusted_user_relay=False,
    )

    assert origin.intent == TurnIntent.MANAGED_MESSAGE
    assert not origin.may_answer_interactive_prompt


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
    assert origin.trust.value == "trusted_internal"
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


def test_requester_id_from_trusted_original_sender_accepts_human_metadata() -> None:
    """Trusted human original-sender metadata may act as the requester."""
    assert (
        requester_id_from_trusted_original_sender(
            original_sender="@human:localhost",
            original_sender_entity_name=None,
            source_kind=HOOK_DISPATCH_SOURCE_KIND,
            sender_trusts_original_sender=True,
        )
        == "@human:localhost"
    )


def test_requester_id_from_trusted_original_sender_accepts_managed_scheduled_fires() -> None:
    """Scheduled fires may preserve a managed requester such as the router."""
    assert (
        requester_id_from_trusted_original_sender(
            original_sender="@mindroom_router:localhost",
            original_sender_entity_name="router",
            source_kind=SCHEDULED_SOURCE_KIND,
            sender_trusts_original_sender=True,
        )
        == "@mindroom_router:localhost"
    )


def test_requester_id_from_trusted_original_sender_rejects_managed_plain_hooks() -> None:
    """Managed original-sender metadata is only a requester for scheduled fires."""
    assert (
        requester_id_from_trusted_original_sender(
            original_sender="@mindroom_router:localhost",
            original_sender_entity_name="router",
            source_kind=HOOK_SOURCE_KIND,
            sender_trusts_original_sender=True,
        )
        is None
    )


def test_requester_id_from_trusted_original_sender_requires_original_sender() -> None:
    """Trusted metadata without an original sender cannot act as a requester."""
    assert (
        requester_id_from_trusted_original_sender(
            original_sender=None,
            original_sender_entity_name=None,
            source_kind=HOOK_DISPATCH_SOURCE_KIND,
            sender_trusts_original_sender=True,
        )
        is None
    )


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
    assert origin.trust.value == "trusted_user_relay"
    assert not origin.blocks_unmentioned_managed_sender
    assert origin.may_answer_interactive_prompt


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
    assert origin.trust.value == "trusted_internal"
    assert origin.blocks_unmentioned_managed_sender
    assert not origin.may_answer_interactive_prompt


def test_human_requested_messages_answer_interactive_prompts() -> None:
    """Human-requested non-automation turns may answer prompts."""
    origin = classify_turn_origin(
        transport_sender_id="@human:localhost",
        requester_id="@human:localhost",
        sender_entity_name=None,
        requester_entity_name=None,
        source_kind="message",
        original_sender=None,
        trusted_user_relay=False,
    )

    assert origin.intent == TurnIntent.USER_MESSAGE
    assert origin.trust.value == "external"
    assert origin.may_answer_interactive_prompt


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


def test_router_relay_original_sender_preserves_human_requester_or_inherited_human() -> None:
    """Router relay metadata uses a human requester or a trusted inherited human author."""
    assert (
        original_sender_for_router_relay(
            requester_id="@human:localhost",
            requester_entity_name=None,
        )
        == "@human:localhost"
    )
    assert (
        original_sender_for_router_relay(
            requester_id="@mindroom_router:localhost",
            requester_entity_name="router",
            inherited_original_sender="@human:localhost",
            inherited_original_sender_entity_name=None,
        )
        == "@human:localhost"
    )
    assert (
        original_sender_for_router_relay(
            requester_id="@mindroom_router:localhost",
            requester_entity_name="router",
            inherited_original_sender="@mindroom_router:localhost",
            inherited_original_sender_entity_name="router",
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
