"""Durable semantic routing for inbound Matrix reactions."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from mindroom import interactive
from mindroom.commands import config_confirmation
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_obligations import DispatchSemanticConsumer
from mindroom.entity_resolution import entity_identity_registry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio

    from mindroom.bot import AgentBot
    from mindroom.prompt_ingress_reservation import PromptIngressReservationOwner

__all__ = ["dispatch_reaction"]


async def _maybe_handle_approval_reaction(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
    consumer: DispatchSemanticConsumer | None,
    handle_approval_action: Callable[..., Awaitable[bool]],
) -> bool:
    """Route a checkmark only to the approval consumer that claimed it."""
    approval_claimed = consumer is DispatchSemanticConsumer.TOOL_APPROVAL_REACTION
    if event.key != "✅" or (consumer is not None and not approval_claimed):
        return False

    async def claim_approval_reaction() -> None:
        nonlocal approval_claimed
        await bot._dispatch_obligation_runner.claim_semantic_consumer(
            DispatchSemanticConsumer.TOOL_APPROVAL_REACTION,
        )
        approval_claimed = True

    approval_handled = await handle_approval_action(
        room=room,
        sender_id=event.sender,
        config=bot.config,
        runtime_paths=bot.runtime_paths,
        orchestrator=bot.orchestrator,
        logger=bot.logger,
        approval_event_id=event.reacts_to,
        status="approved",
        reason=None,
        before_consume=None if approval_claimed else claim_approval_reaction,
        authorization_prevalidated=approval_claimed,
    )
    return approval_claimed or approval_handled


async def _maybe_handle_stop_reaction(
    bot: AgentBot,
    event: nio.ReactionEvent,
    consumer: DispatchSemanticConsumer | None,
) -> bool:
    """Route a stop reaction only to the live run that claimed it."""
    stop_claimed = consumer is DispatchSemanticConsumer.STOP_REACTION
    if event.key != "🛑" or (consumer is not None and not stop_claimed):
        return False
    if not stop_claimed:
        sender_agent_name = entity_identity_registry(
            bot.config,
            bot.runtime_paths,
        ).current_entity_name_for_user_id(event.sender)
        turn_record = bot._turn_store.turn_record_for_response_event_id(event.reacts_to)
        has_incomplete_turn = turn_record is not None and not turn_record.completed
        if sender_agent_name or not (bot.stop_manager.can_handle_stop_reaction(event.reacts_to) or has_incomplete_turn):
            return False
        await bot._dispatch_obligation_runner.claim_semantic_consumer(
            DispatchSemanticConsumer.STOP_REACTION,
        )

    async def remove_current_stop_button() -> None:
        assert bot.client is not None
        await bot.stop_manager.remove_stop_button(
            bot.client,
            event.reacts_to,
            notify_outbound_redaction=bot._conversation_cache.notify_outbound_redaction,
        )

    stopped = await bot._turn_controller.finalize_user_stop(
        event.reacts_to,
        await bot._dispatch_obligation_runner.receipt_order(),
        remove_current_stop_button,
    )
    if stopped:
        bot.logger.info(
            "Stop requested for message",
            message_id=event.reacts_to,
            requested_by=event.sender,
        )
    return True


async def _maybe_handle_interactive_reaction(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
    consumer: DispatchSemanticConsumer | None,
    reservation_owner: PromptIngressReservationOwner,
) -> bool:
    """Route an interactive choice only to its claimed question."""
    interactive_claimed = consumer is DispatchSemanticConsumer.INTERACTIVE_REACTION
    if consumer is not None and not interactive_claimed:
        return False
    assert bot.client is not None
    selection = await interactive.handle_reaction(
        bot.client,
        event,
        bot.agent_name,
        bot.config,
        bot.runtime_paths,
    )
    if selection is None:
        return interactive_claimed
    if not interactive_claimed:
        try:
            await bot._dispatch_obligation_runner.claim_semantic_consumer(
                DispatchSemanticConsumer.INTERACTIVE_REACTION,
            )
        except BaseException:
            interactive.restore_selection(selection)
            raise

    # The selection's response may wait behind this conversation's active
    # turn, so release the sender's lane before response completion.
    await reservation_owner.release()
    await bot._turn_controller.handle_interactive_selection(
        room,
        selection=selection,
        user_id=event.sender,
        source_event_id=event.event_id,
    )
    return True


async def _maybe_handle_nonconfig_reaction(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
    consumer: DispatchSemanticConsumer | None,
    reservation_owner: PromptIngressReservationOwner,
    handle_approval_action: Callable[..., Awaitable[bool]],
) -> bool:
    """Route one authorized reaction among the non-config consumers."""
    if await _maybe_handle_approval_reaction(bot, room, event, consumer, handle_approval_action):
        return True
    if consumer is None and not bot._turn_policy.can_reply_to_sender(event.sender):
        bot.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
        return True
    if await _maybe_handle_stop_reaction(bot, event, consumer):
        return True
    return await _maybe_handle_interactive_reaction(
        bot,
        room,
        event,
        consumer,
        reservation_owner,
    )


async def _route_reaction(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
    semantic_consumer: DispatchSemanticConsumer | None,
    handle_approval_action: Callable[..., Awaitable[bool]],
    sender_is_authorized: Callable[..., bool],
) -> None:
    """Classify and execute one reaction that has no completed hook claim."""
    assert bot.client is not None

    pending_change = (
        await config_confirmation.resolve_reaction_pending_change(
            bot.client,
            room.room_id,
            event,
            enabled=bot.agent_name == ROUTER_AGENT_NAME,
        )
        if semantic_consumer in {None, DispatchSemanticConsumer.CONFIG_CONFIRMATION}
        else None
    )
    if semantic_consumer is DispatchSemanticConsumer.CONFIG_CONFIRMATION and pending_change is None:
        return
    if pending_change is not None and pending_change.decision_event_id is not None:
        if semantic_consumer is None:
            await bot._dispatch_obligation_runner.claim_semantic_consumer(
                DispatchSemanticConsumer.CONFIG_CONFIRMATION,
            )
        await config_confirmation.resume_committed_confirmation(bot, room, event, pending_change)
        return

    if semantic_consumer is None and not sender_is_authorized(
        event.sender,
        bot.config,
        room.room_id,
        bot.runtime_paths,
    ):
        bot.logger.debug("ignoring_reaction_from_unauthorized_sender", user_id=event.sender)
        return

    requester_user_id = bot._ingress_validator.requester_user_id(
        sender=event.sender,
        source=event.source,
    )
    reservation_owner = bot._turn_controller._reserve_prompt_ingress_order(
        room,
        requester_user_id,
        receipt_time=time.monotonic(),
    )
    try:
        if pending_change is not None:
            if semantic_consumer is None and not bot._turn_policy.can_reply_to_sender(event.sender):
                bot.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
                return
            if semantic_consumer is None:
                await bot._dispatch_obligation_runner.claim_semantic_consumer(
                    DispatchSemanticConsumer.CONFIG_CONFIRMATION,
                )
            await config_confirmation.handle_confirmation_reaction(bot, room, event)
            return

        if await _maybe_handle_nonconfig_reaction(
            bot,
            room,
            event,
            semantic_consumer,
            reservation_owner,
            handle_approval_action,
        ):
            return
    finally:
        await reservation_owner.release()

    await bot._dispatch_obligation_runner.claim_semantic_consumer(
        DispatchSemanticConsumer.REACTION_HOOKS,
    )
    await bot._emit_reaction_received_hooks(
        room_id=room.room_id,
        event=event,
        correlation_id=event.event_id,
    )


async def dispatch_reaction(
    bot: AgentBot,
    room: nio.MatrixRoom,
    event: nio.ReactionEvent,
    *,
    handle_approval_action: Callable[..., Awaitable[bool]],
    sender_is_authorized: Callable[..., bool],
) -> None:
    """Route one reaction to its sole durable semantic consumer."""
    assert bot.client is not None
    semantic_consumer = bot._dispatch_obligation_runner.semantic_consumer()
    if semantic_consumer is DispatchSemanticConsumer.REACTION_HOOKS:
        await bot._emit_reaction_received_hooks(
            room_id=room.room_id,
            event=event,
            correlation_id=event.event_id,
        )
        return
    await _route_reaction(
        bot,
        room,
        event,
        semantic_consumer,
        handle_approval_action,
        sender_is_authorized,
    )
