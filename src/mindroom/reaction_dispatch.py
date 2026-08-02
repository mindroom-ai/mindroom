"""Durable semantic routing for inbound Matrix reactions."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom import interactive
from mindroom.commands import config_confirmation
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_obligations import DispatchSemanticConsumer
from mindroom.entity_resolution import entity_identity_registry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio
    import structlog

    from mindroom.commands.config_confirmation import ConfigConfirmationContext
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.dispatch_obligations import DispatchObligationRunner
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.conversation_cache import MatrixConversationCache
    from mindroom.prompt_ingress_reservation import PromptIngressReservationOwner
    from mindroom.runtime_protocols import SupportsClientConfigOrchestrator
    from mindroom.stop import StopManager
    from mindroom.turn_policy import TurnPolicy
    from mindroom.turn_store import TurnStore
    from mindroom.user_stop_reconciliation import UserStopReconciler


@dataclass(frozen=True)
class ReactionDispatcherDeps:
    """Explicit runtime boundary for semantic reaction consumers."""

    runtime: SupportsClientConfigOrchestrator
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    obligation_runner: DispatchObligationRunner
    turn_policy: TurnPolicy
    turn_store: TurnStore
    stop_manager: StopManager
    conversation_cache: MatrixConversationCache
    user_stop_reconciler: UserStopReconciler
    ingress: IngressValidator
    reserve_prompt_ingress_order: Callable[..., PromptIngressReservationOwner]
    handle_interactive_selection: Callable[..., Awaitable[None]]
    emit_reaction_received_hooks: Callable[..., Awaitable[None]]
    config_confirmation: ConfigConfirmationContext
    handle_approval_action: Callable[..., Awaitable[bool]]
    sender_is_authorized: Callable[[str, Config, str, RuntimePaths], bool]


@dataclass
class ReactionDispatcher:
    """Route one reaction to its sole durable semantic consumer."""

    deps: ReactionDispatcherDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for reaction dispatch"
            raise RuntimeError(msg)
        return client

    async def _maybe_handle_approval_reaction(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        consumer: DispatchSemanticConsumer | None,
    ) -> bool:
        """Route a checkmark only to the approval consumer that claimed it."""
        approval_claimed = consumer is DispatchSemanticConsumer.TOOL_APPROVAL_REACTION
        if event.key != "✅" or (consumer is not None and not approval_claimed):
            return False

        async def claim_approval_reaction() -> None:
            nonlocal approval_claimed
            await self.deps.obligation_runner.claim_semantic_consumer(
                DispatchSemanticConsumer.TOOL_APPROVAL_REACTION,
            )
            approval_claimed = True

        approval_handled = await self.deps.handle_approval_action(
            room=room,
            sender_id=event.sender,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            orchestrator=self.deps.runtime.orchestrator,
            logger=self.deps.logger,
            approval_event_id=event.reacts_to,
            status="approved",
            reason=None,
            before_consume=None if approval_claimed else claim_approval_reaction,
            authorization_prevalidated=approval_claimed,
        )
        return approval_claimed or approval_handled

    async def _maybe_handle_stop_reaction(
        self,
        event: nio.ReactionEvent,
        consumer: DispatchSemanticConsumer | None,
    ) -> bool:
        """Route a stop reaction only to the live run that claimed it."""
        stop_claimed = consumer is DispatchSemanticConsumer.STOP_REACTION
        if event.key != "🛑" or (consumer is not None and not stop_claimed):
            return False
        if not stop_claimed:
            sender_agent_name = entity_identity_registry(
                self.deps.runtime.config,
                self.deps.runtime_paths,
            ).current_entity_name_for_user_id(event.sender)
            turn_record = self.deps.turn_store.turn_record_for_response_event_id(event.reacts_to)
            has_incomplete_turn = turn_record is not None and not turn_record.completed
            if sender_agent_name or not (
                self.deps.stop_manager.can_handle_stop_reaction(event.reacts_to) or has_incomplete_turn
            ):
                return False
            await self.deps.obligation_runner.claim_semantic_consumer(
                DispatchSemanticConsumer.STOP_REACTION,
            )

        async def remove_current_stop_button() -> None:
            await self.deps.stop_manager.remove_stop_button(
                self._client(),
                event.reacts_to,
                notify_outbound_redaction=self.deps.conversation_cache.notify_outbound_redaction,
            )

        stopped = await self.deps.user_stop_reconciler.finalize(
            event.reacts_to,
            await self.deps.obligation_runner.receipt_order(),
            remove_current_stop_button,
        )
        if stopped:
            self.deps.logger.info(
                "Stop requested for message",
                message_id=event.reacts_to,
                requested_by=event.sender,
            )
        return True

    async def _maybe_handle_interactive_reaction(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        consumer: DispatchSemanticConsumer | None,
        reservation_owner: PromptIngressReservationOwner,
    ) -> bool:
        """Route an interactive choice only to its claimed question."""
        interactive_claimed = consumer is DispatchSemanticConsumer.INTERACTIVE_REACTION
        if consumer is not None and not interactive_claimed:
            return False
        selection = await interactive.handle_reaction(
            self._client(),
            event,
            self.deps.agent_name,
            self.deps.runtime.config,
            self.deps.runtime_paths,
        )
        if selection is None:
            return interactive_claimed
        if not interactive_claimed:
            try:
                await self.deps.obligation_runner.claim_semantic_consumer(
                    DispatchSemanticConsumer.INTERACTIVE_REACTION,
                )
            except BaseException:
                interactive.restore_selection(selection)
                raise

        # The selection's response may wait behind this conversation's active
        # turn, so release the sender's lane before response completion.
        await reservation_owner.release()
        await self.deps.handle_interactive_selection(
            room,
            selection=selection,
            user_id=event.sender,
            source_event_id=event.event_id,
        )
        return True

    async def _maybe_handle_nonconfig_reaction(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        consumer: DispatchSemanticConsumer | None,
        reservation_owner: PromptIngressReservationOwner,
    ) -> bool:
        """Route one authorized reaction among the non-config consumers."""
        if await self._maybe_handle_approval_reaction(room, event, consumer):
            return True
        if consumer is None and not self.deps.turn_policy.can_reply_to_sender(event.sender):
            self.deps.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
            return True
        if await self._maybe_handle_stop_reaction(event, consumer):
            return True
        return await self._maybe_handle_interactive_reaction(
            room,
            event,
            consumer,
            reservation_owner,
        )

    async def _route_reaction(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        semantic_consumer: DispatchSemanticConsumer | None,
    ) -> None:
        """Classify and execute one reaction that has no completed hook claim."""
        pending_change = (
            await config_confirmation.resolve_reaction_pending_change(
                self._client(),
                room.room_id,
                event,
                enabled=self.deps.agent_name == ROUTER_AGENT_NAME,
            )
            if semantic_consumer in {None, DispatchSemanticConsumer.CONFIG_CONFIRMATION}
            else None
        )
        if semantic_consumer is DispatchSemanticConsumer.CONFIG_CONFIRMATION and pending_change is None:
            return
        if pending_change is not None and pending_change.decision_event_id is not None:
            if semantic_consumer is None:
                await self.deps.obligation_runner.claim_semantic_consumer(
                    DispatchSemanticConsumer.CONFIG_CONFIRMATION,
                )
            await config_confirmation.resume_committed_confirmation(
                self.deps.config_confirmation,
                room,
                event,
                pending_change,
            )
            return

        if semantic_consumer is None and not self.deps.sender_is_authorized(
            event.sender,
            self.deps.runtime.config,
            room.room_id,
            self.deps.runtime_paths,
        ):
            self.deps.logger.debug("ignoring_reaction_from_unauthorized_sender", user_id=event.sender)
            return

        requester_user_id = self.deps.ingress.requester_user_id(
            sender=event.sender,
            source=event.source,
        )
        reservation_owner = self.deps.reserve_prompt_ingress_order(
            room,
            requester_user_id,
            receipt_time=time.monotonic(),
        )
        try:
            if pending_change is not None:
                if semantic_consumer is None and not self.deps.turn_policy.can_reply_to_sender(event.sender):
                    self.deps.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
                    return
                if semantic_consumer is None:
                    await self.deps.obligation_runner.claim_semantic_consumer(
                        DispatchSemanticConsumer.CONFIG_CONFIRMATION,
                    )
                await config_confirmation.handle_confirmation_reaction(
                    self.deps.config_confirmation,
                    room,
                    event,
                )
                return

            if await self._maybe_handle_nonconfig_reaction(
                room,
                event,
                semantic_consumer,
                reservation_owner,
            ):
                return
        finally:
            await reservation_owner.release()

        await self.deps.obligation_runner.claim_semantic_consumer(
            DispatchSemanticConsumer.REACTION_HOOKS,
        )
        await self.deps.emit_reaction_received_hooks(
            room_id=room.room_id,
            event=event,
            correlation_id=event.event_id,
        )

    async def dispatch(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> None:
        """Route one reaction to its sole durable semantic consumer."""
        semantic_consumer = self.deps.obligation_runner.semantic_consumer()
        if semantic_consumer is DispatchSemanticConsumer.REACTION_HOOKS:
            await self.deps.emit_reaction_received_hooks(
                room_id=room.room_id,
                event=event,
                correlation_id=event.event_id,
            )
            return
        await self._route_reaction(room, event, semantic_consumer)
