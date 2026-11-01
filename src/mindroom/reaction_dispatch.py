"""Durable semantic routing for inbound Matrix reactions."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.approval.inbound import handle_tool_approval_action
from mindroom.commands import config_confirmation
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.entity_resolution import entity_identity_registry
from mindroom.event_journal import SemanticConsumer
from mindroom.hooks import EVENT_REACTION_RECEIVED, ReactionReceivedContext, emit

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    import nio
    import structlog

    from mindroom.coalescing.ingress_ticket import IngressTicket
    from mindroom.commands.config_confirmation import ConfigConfirmationContext
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.hooks import HookContextSupport
    from mindroom.ingress_validation import IngressValidator
    from mindroom.journal_dispatch import JournalDispatcher
    from mindroom.runtime_protocols import SupportsClientConfigOrchestrator
    from mindroom.turns.facts import IngressFacts
    from mindroom.user_stop_reconciliation import UserStopReconciler


@dataclass(frozen=True)
class ReactionDispatcherDeps:
    """Explicit runtime boundary for semantic reaction consumers."""

    runtime: SupportsClientConfigOrchestrator
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    journal_dispatcher: JournalDispatcher
    user_stop_reconciler: UserStopReconciler
    ingress: IngressValidator
    reserve_prompt_ingress_order: Callable[..., IngressTicket]
    enqueue_interactive_selection: Callable[..., Awaitable[None]]
    hook_context: HookContextSupport
    resolver: ConversationResolver
    config_confirmation: ConfigConfirmationContext


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

    async def _emit_reaction_hooks(self, room_id: str, event: nio.ReactionEvent) -> None:
        """Emit reaction:received for a reaction no built-in consumer took, with its target's thread when known."""
        if not self.deps.hook_context.registry.has_hooks(EVENT_REACTION_RECEIVED):
            return

        target_event_id = event.reacts_to.strip()
        thread_id: str | None = None
        if target_event_id:
            try:
                thread_id = await self.deps.resolver.resolve_related_event_thread_id_dispatch_snapshot_best_effort(
                    room_id,
                    target_event_id,
                )
            except Exception as exc:
                self.deps.logger.debug(
                    "Failed to resolve reaction target thread for hook context",
                    room_id=room_id,
                    target_event_id=target_event_id,
                    error=str(exc),
                )

        context = ReactionReceivedContext(
            **self.deps.hook_context.base_kwargs(EVENT_REACTION_RECEIVED, event.event_id),
            room_id=room_id,
            event_id=event.event_id,
            sender_id=event.sender,
            reaction_key=event.key,
            target_event_id=event.reacts_to,
            thread_id=thread_id,
        )
        # The registry live after the lookup: a plugin reload during it replaces the hooks that run.
        await emit(self.deps.hook_context.registry, EVENT_REACTION_RECEIVED, context)

    async def _maybe_handle_approval_reaction(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        consumer: SemanticConsumer | None,
    ) -> bool:
        """Route a checkmark only to the approval consumer that claimed it."""
        approval_claimed = consumer is SemanticConsumer.TOOL_APPROVAL_REACTION
        if event.key != "✅" or (consumer is not None and not approval_claimed):
            return False

        async def claim_approval_reaction() -> None:
            nonlocal approval_claimed
            await self.deps.journal_dispatcher.claim_semantic_consumer(
                SemanticConsumer.TOOL_APPROVAL_REACTION,
            )
            approval_claimed = True

        approval_handled = await handle_tool_approval_action(
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
            membership_index=self.deps.runtime.agent_reply_memberships,
        )
        return approval_claimed or approval_handled

    async def _maybe_handle_stop_reaction(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        consumer: SemanticConsumer | None,
        requester_user_id: str,
    ) -> bool:
        """Route a stop reaction only to the live run that claimed it."""
        stop_claimed = consumer is SemanticConsumer.STOP_REACTION
        if event.key != "🛑" or (consumer is not None and not stop_claimed):
            return False
        if not stop_claimed:
            sender_agent_name = entity_identity_registry(
                self.deps.runtime.config,
                self.deps.runtime_paths,
            ).current_entity_name_for_user_id(event.sender)
            # An entity's 🛑, its own Stop button included, stops nothing unless it relays
            # the user it acts for, as stop_agent_reply does; access then applies to that user.
            if (sender_agent_name and requester_user_id == event.sender) or not (
                # A running reply in the reaction's room, including one whose create is still unacknowledged.
                await self.deps.user_stop_reconciler.accepts_reply_stop(event.reacts_to, room.room_id)
            ):
                return False
            await self.deps.journal_dispatcher.claim_semantic_consumer(
                SemanticConsumer.STOP_REACTION,
            )

        stopped = await self.deps.user_stop_reconciler.finalize(
            event.reacts_to,
            await self.deps.journal_dispatcher.receipt_order(),
            room_id=room.room_id,
        )
        if stopped:
            self.deps.logger.info(
                "Stop requested for message",
                message_id=event.reacts_to,
                requested_by=event.sender,
                room_id=room.room_id,
            )
        return True

    async def _maybe_handle_interactive_reaction(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        consumer: SemanticConsumer | None,
        ticket: IngressTicket,
        facts: IngressFacts,
    ) -> TurnDispatchOutcome | None:
        """Route an interactive choice only through its source-owned selection."""
        interactive_claimed = consumer is SemanticConsumer.INTERACTIVE_REACTION
        if consumer is not None and not interactive_claimed:
            return None
        if event.sender == self._client().user_id or entity_identity_registry(
            self.deps.runtime.config,
            self.deps.runtime_paths,
        ).is_managed_user_id(event.sender):
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED if interactive_claimed else None
        selection = await self.deps.journal_dispatcher.claim_interactive_reaction()
        if selection is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED if interactive_claimed else None

        await self.deps.enqueue_interactive_selection(
            ticket,
            room,
            selection=selection,
            facts=facts,
            source_event_id=event.event_id,
        )
        return TurnDispatchOutcome.DEFERRED

    async def _maybe_handle_nonconfig_reaction(  # noqa: PLR0911 - one outcome per durable reaction consumer
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        consumer: SemanticConsumer | None,
        ticket: IngressTicket,
        facts: IngressFacts,
    ) -> TurnDispatchOutcome | None:
        """Route one authorized reaction among the non-config consumers."""
        requester_user_id = facts.lane_requester_id
        if await self._maybe_handle_approval_reaction(room, event, consumer):
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        if consumer is None:
            async with self.deps.ingress.admitted_reply_decision(
                requester_user_id,
                room.room_id,
                unanswered=(event.event_id,),
            ) as allowed:
                if not allowed:
                    self.deps.logger.debug("Ignoring reaction due to reply permissions", sender=event.sender)
                    return TurnDispatchOutcome.INTENTIONALLY_IGNORED
                if await self._maybe_handle_stop_reaction(room, event, consumer, requester_user_id):
                    return TurnDispatchOutcome.INTENTIONALLY_IGNORED
                outcome = await self._maybe_handle_interactive_reaction(
                    room,
                    event,
                    consumer,
                    ticket,
                    facts,
                )
                if outcome is not None:
                    return outcome
                await self.deps.journal_dispatcher.claim_semantic_consumer(
                    SemanticConsumer.REACTION_HOOKS,
                )
                await self._emit_reaction_hooks(room.room_id, event)
                return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        if await self._maybe_handle_stop_reaction(room, event, consumer, requester_user_id):
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        return await self._maybe_handle_interactive_reaction(
            room,
            event,
            consumer,
            ticket,
            facts,
        )

    async def _route_reaction(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        semantic_consumer: SemanticConsumer | None,
    ) -> TurnDispatchOutcome:
        """Classify and execute one reaction that has no completed hook claim."""
        pending_change = (
            await config_confirmation.resolve_reaction_pending_change(
                self._client(),
                room.room_id,
                event,
                enabled=self.deps.agent_name == ROUTER_AGENT_NAME,
            )
            if semantic_consumer in {None, SemanticConsumer.CONFIG_CONFIRMATION}
            else None
        )
        if semantic_consumer is SemanticConsumer.CONFIG_CONFIRMATION and pending_change is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        if pending_change is not None and pending_change.decision_event_id is not None:
            if semantic_consumer is None:
                await self.deps.journal_dispatcher.claim_semantic_consumer(
                    SemanticConsumer.CONFIG_CONFIRMATION,
                )
            await config_confirmation.resume_committed_confirmation(
                self.deps.config_confirmation,
                room,
                event,
                pending_change,
            )
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED

        facts = self.deps.ingress.classify(event, room_id=room.room_id)
        requester_user_id = facts.lane_requester_id
        ticket = self.deps.reserve_prompt_ingress_order(
            room,
            requester_user_id,
            receipt_time=time.monotonic(),
        )
        try:
            if pending_change is not None:
                async with self.deps.ingress.admitted_reply_decision(
                    requester_user_id,
                    room.room_id,
                    unanswered=(event.event_id,),
                ) as allowed:
                    if not allowed:
                        self.deps.logger.debug("Ignoring reaction due to current authorization", sender=event.sender)
                        return TurnDispatchOutcome.INTENTIONALLY_IGNORED
                    if semantic_consumer is None:
                        await self.deps.journal_dispatcher.claim_semantic_consumer(
                            SemanticConsumer.CONFIG_CONFIRMATION,
                        )
                    await config_confirmation.handle_confirmation_reaction(
                        self.deps.config_confirmation,
                        room,
                        event,
                    )
                return TurnDispatchOutcome.INTENTIONALLY_IGNORED

            if (
                outcome := await self._maybe_handle_nonconfig_reaction(
                    room,
                    event,
                    semantic_consumer,
                    ticket,
                    facts,
                )
            ) is not None:
                return outcome
        finally:
            ticket.close()
        self.deps.logger.warning(
            "reaction_consumer_did_not_match_event",
            event_id=event.event_id,
            semantic_consumer=semantic_consumer,
        )
        return TurnDispatchOutcome.INTENTIONALLY_IGNORED

    async def _emit_authorized_reaction_hooks(
        self,
        room: nio.MatrixRoom,
        event: nio.ReactionEvent,
        requester_user_id: str,
    ) -> None:
        """Emit generic hooks only while their current reply authority is admitted."""
        async with self.deps.ingress.admitted_reply_decision(
            requester_user_id,
            room.room_id,
            unanswered=(event.event_id,),
        ) as allowed:
            if not allowed:
                self.deps.logger.debug(
                    "Ignoring reaction hook due to current authorization",
                    sender=event.sender,
                )
                return
            await self._emit_reaction_hooks(room.room_id, event)

    async def dispatch(self, room: nio.MatrixRoom, event: nio.ReactionEvent) -> TurnDispatchOutcome:
        """Route one reaction to its sole durable semantic consumer."""
        semantic_consumer = self.deps.journal_dispatcher.semantic_consumer()
        if semantic_consumer is SemanticConsumer.REACTION_HOOKS:
            requester_user_id = self.deps.ingress.classify(event, room_id=room.room_id).lane_requester_id
            await self._emit_authorized_reaction_hooks(room, event, requester_user_id)
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        return await self._route_reaction(room, event, semantic_consumer)
