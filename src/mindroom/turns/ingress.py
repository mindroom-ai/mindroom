"""Admit one inbound text or media event: from its Matrix callback to the coalescing gate."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol

from mindroom.authorization import ensure_room_membership_synced
from mindroom.coalescing.batch import (
    CoalescingKey,
    PendingEvent,
    build_prepared_turn,
    requester_coalescing_key,
)
from mindroom.coalescing.ingress_lanes import ReceiptLaneKey
from mindroom.coalescing.ingress_ticket import IngressTicket
from mindroom.constants import (
    ROUTER_AGENT_NAME,
    RuntimePaths,
)
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_handoff import (
    DispatchEvent,
    MediaDispatchEvent,
    PreparedIngress,
    dispatch_prompt_for_event,
    prepare_media_ingress,
)
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active
from mindroom.dispatch_source import (
    EDIT_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.inbound_turn_normalizer import (
    InboundTurnNormalizer,
    TextNormalizationRequest,
)
from mindroom.matrix.client_visible_messages import extract_visible_edit_body
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.media import (
    FileMessageEvent,
    MatrixMediaEvent,
    is_audio_message_event,
    is_file_message_event,
    is_image_message_event,
    is_matrix_media_dispatch_event,
)
from mindroom.matrix.message_content import is_v2_sidecar_text_preview
from mindroom.matrix.thread_membership import RelatedEventUnavailableError, ThreadMembershipLookupError
from mindroom.matrix.transport_progress import is_unfinished_reply
from mindroom.response_admission import admitted_response_decision
from mindroom.thread_utils import (
    check_agent_mentioned,
    is_router_only_agent_mention,
    mentions_another_participant,
    thread_requires_explicit_agent_targeting,
)
from mindroom.timing import (
    DispatchPipelineTiming,
    attach_dispatch_pipeline_timing,
    create_dispatch_pipeline_timing,
    emit_elapsed_timing,
    event_timing_scope,
    get_dispatch_pipeline_timing,
)
from mindroom.turn_origin import TurnIntent
from mindroom.turn_record import TurnRecord, canonicalize_turn_record
from mindroom.turns.dispatch import should_skip_deep_synthetic_full_dispatch
from mindroom.turns.facts import PrecheckedEvent, command_for, is_router_voice_echo
from mindroom.turns.queued_notice import mark_waiting_for_approval, queued_notice_reservation_if_busy

if TYPE_CHECKING:
    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.coalescing.gate import CoalescingGate
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.event_journal import PrincipalStore
    from mindroom.hooks import MessageEnvelope
    from mindroom.ingress_validation import IngressValidator
    from mindroom.matrix.identity import MatrixID
    from mindroom.response_runner import ResponseRunner
    from mindroom.turn_policy import TurnPolicy
    from mindroom.turn_store import TurnStore
    from mindroom.turns.dispatch import TurnDispatcher
    from mindroom.turns.facts import IngressFacts
    from mindroom.turns.interactive_selection import InteractiveSelections
    from mindroom.visible_response_reconciliation import VisibleResponseReconciler
    from mindroom.voice.ingress import VoiceIngress


class _EditRegenerator(Protocol):
    """Minimal edit-regeneration surface needed by turn sequencing."""

    async def handle_message_edit(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        event_info: EventInfo,
        facts: IngressFacts,
    ) -> bool | None:
        """Regenerate an edit and report any durable source handoff."""


type _PrecheckedInboundMediaEvent = PrecheckedEvent[MatrixMediaEvent]


def _media_source_kind(event: MatrixMediaEvent) -> str | None:
    """Return the source kind a media event's transport fixes; a long text sent as a file is text."""
    if is_audio_message_event(event):
        return VOICE_SOURCE_KIND
    if is_v2_sidecar_text_preview(event.source):
        return None
    return IMAGE_SOURCE_KIND if is_image_message_event(event) else MEDIA_SOURCE_KIND


class _IngressAdmissionOutcome(Enum):
    DEFERRED = "deferred"
    CONSUMED = "consumed"
    IGNORED = "ignored"


@dataclass(frozen=True)
class TurnIngressDeps:
    """Collaborators needed to admit inbound text and media events."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    agent_name: str
    matrix_id: MatrixID
    interactive_questions: PrincipalStore
    resolver: ConversationResolver
    normalizer: InboundTurnNormalizer
    turn_policy: TurnPolicy
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway
    turn_store: TurnStore
    coalescing_gate: CoalescingGate
    edit_regenerator: _EditRegenerator
    ingress: IngressValidator
    visible_responses: VisibleResponseReconciler
    dispatcher: TurnDispatcher
    selections: InteractiveSelections
    voice: VoiceIngress


@dataclass
class TurnIngress:
    """Own one inbound text or media event from its Matrix callback to gate admission, or to a bypass dispatch."""

    deps: TurnIngressDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for turn execution"
            raise RuntimeError(msg)
        return client

    def reserve_prompt_ingress_order(
        self,
        room: nio.MatrixRoom,
        requester_user_id: str,
        *,
        receipt_time: float | None = None,
    ) -> IngressTicket:
        """Reserve receipt order for one prompt-like Matrix ingress item."""
        return IngressTicket(
            gate=self.deps.coalescing_gate,
            slot=self.deps.coalescing_gate.enter_lane(
                ReceiptLaneKey(room_id=room.room_id, sender_id=requester_user_id),
                receipt_time=receipt_time,
            ),
            turn_store=self.deps.turn_store,
        )

    async def _precheck_dispatch_event[T: DispatchEvent | MatrixMediaEvent](
        self,
        room: nio.MatrixRoom,
        event: T,
        *,
        source_kind: str | None = None,
        is_edit: bool = False,
    ) -> PrecheckedEvent[T] | None:
        """Classify one event once, then run the shared prechecks on that classification."""
        facts = self.deps.ingress.classify(event, source_kind=source_kind)
        async with admitted_response_decision(
            self.deps.runtime.response_admission_gate,
            self.deps.response_runner.wait_for_admission_or_shutdown,
        ):
            if not await self.deps.ingress.precheck_event(room, event, facts, is_edit=is_edit):
                return None
        return PrecheckedEvent(event=event, facts=facts)

    def _event_allows_queued_notice(
        self,
        room: nio.MatrixRoom,
        event: PreparedIngress,
        envelope: MessageEnvelope,
    ) -> bool:
        """Return whether this event may notify the conversation's active response.

        Router handoffs and agent relays (for example a `matrix_message` handoff) that
        are addressed to another participant must not pause this agent's running turn.
        """
        if envelope.origin.intent not in {TurnIntent.ROUTER_HANDOFF, TurnIntent.TRUSTED_INTERNAL_RELAY}:
            return True
        return self._mentions_another_participant(room, event.source) is not True

    def _mentions_another_participant(self, room: nio.MatrixRoom, source: dict[str, Any]) -> bool | None:
        """Return whom a message mentions: ``None`` nobody, ``False`` this agent, ``True`` only other agents or people."""
        return mentions_another_participant(
            source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )

    async def _enqueue_prepared_text_for_dispatch(
        self,
        *,
        room: nio.MatrixRoom,
        prepared_event: PreparedIngress,
        facts: IngressFacts,
        envelope: MessageEnvelope,
        coalescing_thread_id: str | None,
        ticket: IngressTicket,
    ) -> _IngressAdmissionOutcome:
        """Queue one normalized text event; the gate decides busy-conversation routing."""
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=prepared_event.event_id,
            event_source=prepared_event.source,
        )
        if self._event_allows_queued_notice(room, prepared_event, envelope):
            ticket.notice = queued_notice_reservation_if_busy(
                self.deps.response_runner,
                target=target,
                envelope=envelope,
            )
        await self._enqueue_for_dispatch(
            prepared_event,
            room,
            facts=facts,
            ticket=ticket,
            coalescing_key=requester_coalescing_key(room.room_id, coalescing_thread_id, facts.lane_requester_id),
        )
        mark_waiting_for_approval(self.deps, room, prepared_event.source, target, envelope)
        return _IngressAdmissionOutcome.DEFERRED

    async def _enqueue_media_for_dispatch(
        self,
        *,
        room: nio.MatrixRoom,
        event: MediaDispatchEvent,
        facts: IngressFacts,
        coalescing_thread_id: str | None,
        ticket: IngressTicket,
    ) -> _IngressAdmissionOutcome:
        """Queue one media event; the gate decides busy-conversation routing."""
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        envelope = self.deps.resolver.build_envelope(event=event, facts=facts, target=target)
        ticket.notice = queued_notice_reservation_if_busy(self.deps.response_runner, target=target, envelope=envelope)
        await self._enqueue_for_dispatch(
            event,
            room,
            facts=facts,
            ticket=ticket,
            coalescing_key=requester_coalescing_key(room.room_id, coalescing_thread_id, facts.lane_requester_id),
        )
        mark_waiting_for_approval(self.deps, room, event.source, target, envelope)
        return _IngressAdmissionOutcome.DEFERRED

    async def _should_skip_router_before_shared_ingress_work(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        facts: IngressFacts,
        thread_id: str | None,
    ) -> bool:
        """Return whether the router can safely skip shared ingress work for one text event."""
        requester_user_id = facts.lane_requester_id
        if (
            self.deps.agent_name != ROUTER_AGENT_NAME
            or command_for(facts, dispatch_prompt_for_event(event)) is not None
            # A long text's preview is not its text yet, so it may hide a command.
            or is_v2_sidecar_text_preview(event.source)
            # A caption for an upload still waiting in this sender's burst must join that upload's
            # turn, or the router routes the upload alone while the named agent answers the caption.
            or any(
                self.deps.coalescing_gate.has_pending_work(
                    requester_coalescing_key(room.room_id, scope_thread_id, requester_user_id),
                )
                for scope_thread_id in {None, thread_id}
            )
        ):
            return False

        mentioned_agents, _am_i_mentioned, has_non_agent_mentions = check_agent_mentioned(
            event.source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )
        if mentioned_agents or has_non_agent_mentions:
            return not is_router_only_agent_mention(
                mentioned_agents,
                has_non_agent_mentions=has_non_agent_mentions,
                config=self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
            )
        if thread_id is None:
            return False

        try:
            thread_history = await self.deps.resolver.dispatch_thread_snapshot(
                room.room_id,
                thread_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.deps.logger.warning(
                "Router pre-ingress skip ignored thread snapshot failure",
                room_id=room.room_id,
                thread_id=thread_id,
                error=str(exc),
            )
            return False
        if thread_history is None:
            return False
        available_responders = await self.deps.turn_policy.responder_candidates_for_room(
            room,
            requester_user_id,
        )
        return thread_requires_explicit_agent_targeting(
            thread_history,
            sender_id=requester_user_id,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
            membership_index=self.deps.turn_policy.deps.agent_reply_memberships,
            available_responders_in_room=available_responders,
        )

    async def _resolve_text_event_with_ingress_timing(
        self,
        event: nio.RoomMessageFormatted,
        *,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> PreparedIngress:
        """Normalize one inbound text event while recording ingress timing boundaries."""
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_start")
        prepared_event = await self.deps.normalizer.resolve_text_event(
            TextNormalizationRequest(event=event),
        )
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_ready")
        attach_dispatch_pipeline_timing(prepared_event.source, dispatch_timing)
        return prepared_event

    async def _dispatch_prepared_text_like_ingress(
        self,
        *,
        room: nio.MatrixRoom,
        prepared_event: PreparedIngress,
        facts: IngressFacts,
        ticket: IngressTicket,
        coalescing_thread_id: str | None,
    ) -> _IngressAdmissionOutcome:
        """Run shared ingress dispatch for text events and sidecar text previews."""
        target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=prepared_event.event_id,
            event_source=prepared_event.source,
        )
        content = prepared_event.source.get("content") if isinstance(prepared_event.source, dict) else None
        if facts.source_kind == TRUSTED_INTERNAL_RELAY_SOURCE_KIND and is_router_voice_echo(facts, content):
            return _IngressAdmissionOutcome.CONSUMED
        envelope = self.deps.resolver.build_envelope(event=prepared_event, facts=facts, target=target)
        if should_skip_deep_synthetic_full_dispatch(
            self.deps.logger,
            event_id=prepared_event.event_id,
            envelope=envelope,
        ):
            return _IngressAdmissionOutcome.CONSUMED
        message_text = prepared_event.body.strip()
        if envelope.origin.may_answer_interactive_prompt and message_text.isdigit() and len(message_text) == 1:
            selection = await self.deps.interactive_questions.claim_interactive_text(
                source_event_id=prepared_event.event_id,
            )
            if selection is not None:
                # As a reaction's answer does, the response runs on a runner-owned task behind this conversation's
                # earlier messages, so neither its run nor an approval it waits for holds the room's event lane. The
                # queue and that task own the source from here, so a retry of it must not meet this turn's claim.
                ticket.take_claim().release()
                await self.deps.selections.enqueue_interactive_selection(
                    ticket,
                    room,
                    selection=selection,
                    facts=facts,
                    source_event_id=prepared_event.event_id,
                )
                return _IngressAdmissionOutcome.DEFERRED
        if command_for(facts, dispatch_prompt_for_event(prepared_event)) is not None:
            await self._dispatch_command_control_input(
                room=room,
                dispatch_event=prepared_event,
                facts=facts,
                coalescing_thread_id=coalescing_thread_id,
                ticket=ticket,
            )
            return _IngressAdmissionOutcome.CONSUMED
        return await self._enqueue_prepared_text_for_dispatch(
            room=room,
            prepared_event=prepared_event,
            facts=facts,
            envelope=envelope,
            coalescing_thread_id=coalescing_thread_id,
            ticket=ticket,
        )

    def _mentioned_entity_final_reply(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
    ) -> nio.RoomMessageFormatted | None:
        """Return another entity's completed edit as its reply when that reply mentions this entity."""
        final_reply = self.deps.ingress.entity_final_reply(event)
        if final_reply is None:
            return None
        _mentioned_agents, am_i_mentioned, _has_non_agent_mentions = check_agent_mentioned(
            final_reply.source,
            self.deps.matrix_id,
            self.deps.runtime.config,
            self.deps.runtime_paths,
            room=room,
        )
        return final_reply if am_i_mentioned else None

    async def _handle_edit_event(
        self,
        room: nio.MatrixRoom,
        prechecked_event: PrecheckedEvent[nio.RoomMessageFormatted],
        event_info: EventInfo,
    ) -> bool | None:
        """Apply a person's edit to their message still waiting in a queue, or hand it to the edit regenerator.

        An entity's edit changes neither, so a queued message from an entity answers the text it was queued with.
        """
        if prechecked_event.facts.sender_is_managed:
            return None
        async with admitted_response_decision(
            self.deps.runtime.response_admission_gate,
            self.deps.response_runner.wait_for_admission_or_shutdown,
        ):
            if not self.deps.turn_policy.can_reply_to_sender_in_room(
                prechecked_event.facts.lane_requester_id,
                room.room_id,
            ):
                return None
            if await self._edit_queued_message(room, prechecked_event.event, event_info):
                return None
            return await self.deps.edit_regenerator.handle_message_edit(
                room,
                prechecked_event.event,
                event_info,
                prechecked_event.facts,
            )

    async def _edit_queued_message(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        event_info: EventInfo,
    ) -> bool:
        """Give a message still waiting for its turn the text its sender edited it to; return whether one did."""
        original_event_id = event_info.original_event_id
        if original_event_id is None or not self.deps.coalescing_gate.has_pending_source_event(original_event_id):
            return False
        body, content = await extract_visible_edit_body(
            event.source,
            self._client(),
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        if body is None or content is None:
            return False
        return self.deps.coalescing_gate.apply_pending_edit(
            room_id=room.room_id,
            source_event_id=original_event_id,
            sender=event.sender,
            body=body,
            new_content=content,
            for_another_participant=self._mentions_another_participant(room, {"content": content}),
        )

    def _log_unplaceable_event(self, room: nio.MatrixRoom, event: DispatchEvent | MatrixMediaEvent) -> None:
        """Record an event left unanswered because its relation target cannot be read.

        Without that target there is no conversation to place the event in,
        and asking the homeserver again gets the same refusal, so retrying the
        turn would only hold up every later event in the room.
        """
        self.deps.logger.warning(
            "relation_target_unavailable",
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
        )

    async def _notify_command_target_not_ready(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        facts: IngressFacts,
    ) -> bool:
        """Fail one command visibly when its conversation cannot be resolved yet."""
        if command_for(facts, dispatch_prompt_for_event(event)) is None:
            return False
        requester_user_id = facts.lane_requester_id
        self.deps.logger.warning(
            "command_target_not_ready",
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
        )
        if self.deps.agent_name == ROUTER_AGENT_NAME:
            async with admitted_response_decision(
                self.deps.runtime.response_admission_gate,
                self.deps.response_runner.wait_for_admission_or_shutdown,
            ):
                if not self.deps.turn_policy.can_reply_to_sender_in_room(requester_user_id, room.room_id):
                    return True
                target = self.deps.resolver.build_message_target(
                    room_id=room.room_id,
                    thread_id=None,
                    reply_to_event_id=event.event_id,
                    event_source=event.source,
                )
                pending_turn, response_event_id = await self.deps.visible_responses.prepare_visible_delivery_turn(
                    TurnRecord.create([event.event_id]),
                    requester_id=requester_user_id,
                    target=target,
                )
                if pending_turn is None:
                    return True
                response_event_id = await self.deps.visible_responses.deliver_recoverable_text(
                    pending_turn,
                    target=target,
                    response_text=(
                        "I could not run that command yet: the conversation it targets "
                        "is still being resolved. Please resend it in a moment."
                    ),
                    # One notice, then the turn is terminal, so this send satisfies
                    # the outbox's once-per-(turn, stage) rule.
                    recovered_response_event_id=response_event_id,
                )
                await self.deps.turn_store.record_responded_turn(
                    canonicalize_turn_record(pending_turn, response_event_id=response_event_id),
                )
                return True
        await self.deps.visible_responses.settle_source_events_ignored(TurnRecord.create([event.event_id]))
        return True

    async def _dispatch_command_control_input(
        self,
        *,
        room: nio.MatrixRoom,
        dispatch_event: PreparedIngress,
        facts: IngressFacts,
        coalescing_thread_id: str | None,
        ticket: IngressTicket,
    ) -> None:
        """Dispatch one command as a control input without entering the coalescing gate, under its ingress claim."""
        # A command an entity wrote for a human runs with that human's authority, as its conversation would;
        # handle_command refuses the commands only the human may send.
        pending_event = PendingEvent(
            event=replace(dispatch_event, payload_trusted=dispatch_event.payload_trusted or facts.sender_is_managed),
            room=room,
            facts=facts,
        )
        turn = build_prepared_turn(
            requester_coalescing_key(room.room_id, coalescing_thread_id, facts.lane_requester_id),
            [pending_event],
        )
        await self.deps.dispatcher.dispatch_control_turn(replace(turn, claims=(ticket.take_claim(),)))

    async def _adaptive_text_debounce_seconds(
        self,
        event: PreparedIngress,
        room: nio.MatrixRoom,
        *,
        facts: IngressFacts,
        key: CoalescingKey,
    ) -> float:
        """Resolve adaptive eligibility before queue admission without delaying ordinary text."""
        agent = self.deps.runtime.config.agents.get(self.deps.agent_name)
        participation = agent.participation if agent is not None else None
        if (
            participation is None
            or participation.debounce_seconds <= 0
            or key.thread_id is None
            or facts.source_kind != MESSAGE_SOURCE_KIND
            or not facts.origin.may_answer_interactive_prompt
        ):
            return 0.0
        result = await self.deps.resolver.extract_dispatch_context(room, event)
        selected = self.deps.turn_policy.adaptive_participation(
            context=result.context,
            room=room,
            requester_user_id=facts.lane_requester_id,
        )
        return selected.debounce_seconds if selected is not None else 0.0

    async def _enqueue_for_dispatch(
        self,
        event: DispatchEvent,
        room: nio.MatrixRoom,
        *,
        facts: IngressFacts,
        ticket: IngressTicket,
        coalescing_key: CoalescingKey,
    ) -> _IngressAdmissionOutcome:
        """Route one inbound event through the live coalescing gate."""
        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("gate_enter")
        enqueue_start = time.monotonic()
        timing_scope = event_timing_scope(event.event_id)
        source_kind = facts.source_kind
        gate_enqueue_start = time.monotonic()
        if isinstance(event, PreparedIngress):
            prepared_event = event
        elif is_matrix_media_dispatch_event(event):
            prepared_event = prepare_media_ingress(event)
        else:
            msg = f"Unsupported dispatch event: {type(event).__name__}"
            raise TypeError(msg)
        text_debounce_seconds = await self._adaptive_text_debounce_seconds(
            prepared_event,
            room,
            facts=facts,
            key=coalescing_key,
        )
        # Coalescing keys and lanes stay on the sender; the batch runs, and splits, by the effective requester.
        pending_event = PendingEvent(
            text_debounce_seconds=text_debounce_seconds,
            event=replace(
                prepared_event,
                payload_trusted=prepared_event.payload_trusted or facts.sender_is_managed,
                turn_dispatch_recovery=turn_dispatch_recovery_active(),
                for_another_participant=self._mentions_another_participant(room, prepared_event.source),
            ),
            room=room,
            facts=facts,
        )
        ticket.admit(coalescing_key, pending_event)
        emit_elapsed_timing(
            "ingress_handoff.enqueue_for_dispatch.coalescing_gate",
            gate_enqueue_start,
            source_kind=source_kind,
            timing_scope=timing_scope,
        )
        emit_elapsed_timing(
            "ingress_handoff.enqueue_for_dispatch",
            enqueue_start,
            source_kind=source_kind,
            timing_scope=timing_scope,
        )
        return _IngressAdmissionOutcome.DEFERRED

    async def handle_text_event(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        receipt_time: float | None = None,
        ticket: IngressTicket | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one inbound text event, through a ticket the caller reserved early or one of its own."""
        async with self.deps.resolver.turn_lookup_scope():
            return await self._handle_message_inner(
                room,
                event,
                receipt_time=receipt_time,
                ticket=ticket,
            )

    async def _handle_message_inner(
        self,
        room: nio.MatrixRoom,
        event: nio.RoomMessageFormatted,
        *,
        receipt_time: float | None = None,
        ticket: IngressTicket | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one text message inside the per-turn conversation lookup scope."""
        event_info = EventInfo.from_event(event.source)
        event_content = event.source.get("content") if isinstance(event.source, dict) else None
        is_nonterminal_stream = isinstance(event_content, dict) and is_unfinished_reply(event_content)
        # A placeholder still streaming names no requester, so refusing it here would mark the turn its finished
        # reply needs as handled.
        if not isinstance(event.body, str) or is_nonterminal_stream:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        # Another entity's finished reply arrives as an edit of its placeholder; its mentions dispatch like a message.
        final_reply = self._mentioned_entity_final_reply(room, event) if event_info.is_edit else None
        if final_reply is not None:
            event = final_reply
        is_edit = event_info.is_edit and final_reply is None
        prechecked_event = await self._precheck_dispatch_event(
            room,
            event,
            source_kind=EDIT_SOURCE_KIND if is_edit else None,
            is_edit=is_edit,
        )
        if prechecked_event is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        await ensure_room_membership_synced(
            self._client(),
            room,
            sender_id=prechecked_event.facts.lane_requester_id,
        )

        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(event.source, dispatch_timing)
        if ticket is None:
            ticket = self.reserve_prompt_ingress_order(
                room,
                prechecked_event.facts.lane_requester_id,
                receipt_time=receipt_time,
            )
        try:
            if is_edit:
                ticket.close()
                handed_off = await self._handle_edit_event(room, prechecked_event, event_info)
                return TurnDispatchOutcome.DEFERRED if handed_off is True else TurnDispatchOutcome.INTENTIONALLY_IGNORED
            discovery_alias = prechecked_event.facts.discovery_event_id
            claimed = await ticket.claim_turn(
                TurnRecord.create(
                    [event.event_id],
                    discovery_event_ids=(discovery_alias,) if discovery_alias else (),
                    completed=False,
                ),
            )
            if claimed is not None:
                return claimed
            outcome = await self._ingest_live_text_event(
                room,
                prechecked_event,
                dispatch_timing=dispatch_timing,
                ticket=ticket,
            )
            return (
                TurnDispatchOutcome.DEFERRED
                if outcome is _IngressAdmissionOutcome.DEFERRED
                else TurnDispatchOutcome.INTENTIONALLY_IGNORED
            )
        finally:
            ticket.close()

    async def _ingest_live_text_event(
        self,
        room: nio.MatrixRoom,
        prechecked_event: PrecheckedEvent[nio.RoomMessageFormatted],
        *,
        dispatch_timing: DispatchPipelineTiming | None,
        ticket: IngressTicket,
    ) -> _IngressAdmissionOutcome:
        """Resolve, normalize, and admit one live (non-edit) text event."""
        event = prechecked_event.event
        facts = prechecked_event.facts
        try:
            ingress_thread_id = await self.deps.resolver.coalescing_thread_id(room, event)
        except ThreadMembershipLookupError as exc:
            if await self._notify_command_target_not_ready(room, event, facts=facts):
                return _IngressAdmissionOutcome.CONSUMED
            if isinstance(exc, RelatedEventUnavailableError):
                self._log_unplaceable_event(room, event)
                return _IngressAdmissionOutcome.IGNORED
            raise
        if await self._should_skip_router_before_shared_ingress_work(
            room,
            event,
            facts=facts,
            thread_id=ingress_thread_id,
        ):
            self.deps.logger.debug(
                "skip_router_shared_ingress_work",
                event_id=event.event_id,
                room_id=room.room_id,
                thread_id=ingress_thread_id,
            )
            return _IngressAdmissionOutcome.CONSUMED

        self.deps.logger.info(
            "Received message",
            event_id=event.event_id,
            room_id=room.room_id,
            sender=event.sender,
            thread_id=ingress_thread_id,
        )
        prepared_event = await self._resolve_text_event_with_ingress_timing(
            event,
            dispatch_timing=dispatch_timing,
        )
        return await self._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=prepared_event,
            facts=facts,
            ticket=ticket,
            coalescing_thread_id=ingress_thread_id,
        )

    async def handle_media_event(
        self,
        room: nio.MatrixRoom,
        event: MatrixMediaEvent,
        *,
        receipt_time: float | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one inbound media event."""
        async with self.deps.resolver.turn_lookup_scope():
            return await self._handle_media_message_inner(room, event, receipt_time=receipt_time)

    async def _handle_media_message_inner(
        self,
        room: nio.MatrixRoom,
        event: MatrixMediaEvent,
        *,
        receipt_time: float | None = None,
    ) -> TurnDispatchOutcome:
        """Handle one media event inside the per-turn conversation lookup scope."""
        prechecked_event = await self._precheck_dispatch_event(room, event, source_kind=_media_source_kind(event))
        if prechecked_event is None:
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        dispatch_timing = create_dispatch_pipeline_timing(
            event_id=prechecked_event.event.event_id,
            room_id=room.room_id,
        )
        attach_dispatch_pipeline_timing(prechecked_event.event.source, dispatch_timing)
        if is_audio_message_event(prechecked_event.event) and prechecked_event.facts.sender_is_managed:
            self.deps.logger.debug(
                "Ignoring agent audio event for voice transcription",
                event_id=prechecked_event.event.event_id,
                sender=prechecked_event.event.sender,
            )
            return TurnDispatchOutcome.INTENTIONALLY_IGNORED
        await ensure_room_membership_synced(
            self._client(),
            room,
            sender_id=prechecked_event.facts.lane_requester_id,
        )
        ticket = self.reserve_prompt_ingress_order(
            room,
            prechecked_event.facts.lane_requester_id,
            receipt_time=receipt_time,
        )
        try:
            claimed = await ticket.claim_turn(TurnRecord.create([prechecked_event.event.event_id], completed=False))
            if claimed is not None:
                return claimed
            if is_audio_message_event(prechecked_event.event):
                try:
                    dispatch_outcome = await self.deps.voice.admit(
                        room,
                        PrecheckedEvent(event=prechecked_event.event, facts=prechecked_event.facts),
                        dispatch_timing=dispatch_timing,
                        ticket=ticket,
                    )
                except RelatedEventUnavailableError:
                    self._log_unplaceable_event(room, prechecked_event.event)
                    return TurnDispatchOutcome.INTENTIONALLY_IGNORED
            else:
                try:
                    coalescing_thread_id = await self.deps.resolver.coalescing_thread_id(room, prechecked_event.event)
                except RelatedEventUnavailableError:
                    self._log_unplaceable_event(room, prechecked_event.event)
                    return TurnDispatchOutcome.INTENTIONALLY_IGNORED
                admission_outcome = await self._dispatch_special_media_as_text(
                    room,
                    prechecked_event,
                    ticket=ticket,
                    coalescing_thread_id=coalescing_thread_id,
                )
                if admission_outcome is _IngressAdmissionOutcome.DEFERRED:
                    dispatch_outcome = TurnDispatchOutcome.DEFERRED
                elif admission_outcome is _IngressAdmissionOutcome.CONSUMED or not is_matrix_media_dispatch_event(
                    prechecked_event.event,
                ):
                    dispatch_outcome = TurnDispatchOutcome.INTENTIONALLY_IGNORED
                else:
                    await self._enqueue_media_for_dispatch(
                        room=room,
                        event=prechecked_event.event,
                        facts=prechecked_event.facts,
                        coalescing_thread_id=coalescing_thread_id,
                        ticket=ticket,
                    )
                    dispatch_outcome = TurnDispatchOutcome.DEFERRED
        finally:
            ticket.close()
        return dispatch_outcome

    async def _dispatch_special_media_as_text(
        self,
        room: nio.MatrixRoom,
        prechecked_event: _PrecheckedInboundMediaEvent,
        *,
        ticket: IngressTicket,
        coalescing_thread_id: str | None,
    ) -> _IngressAdmissionOutcome:
        """Handle media events that normalize into the text dispatch pipeline."""
        event = prechecked_event.event
        if is_file_message_event(event):
            return await self._dispatch_file_sidecar_text_preview(
                room,
                PrecheckedEvent(event=event, facts=prechecked_event.facts),
                ticket=ticket,
                coalescing_thread_id=coalescing_thread_id,
            )
        return _IngressAdmissionOutcome.IGNORED

    async def _dispatch_file_sidecar_text_preview(
        self,
        room: nio.MatrixRoom,
        prechecked_event: PrecheckedEvent[FileMessageEvent],
        *,
        ticket: IngressTicket,
        coalescing_thread_id: str | None,
    ) -> _IngressAdmissionOutcome:
        """Dispatch one sidecar-backed file preview through the normal text pipeline."""
        event = prechecked_event.event
        if not is_v2_sidecar_text_preview(event.source):
            return _IngressAdmissionOutcome.IGNORED

        dispatch_timing = get_dispatch_pipeline_timing(event.source)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_start")
        prepared_text_event = await self.deps.normalizer.prepare_file_sidecar_text_event(event)
        if dispatch_timing is not None:
            dispatch_timing.mark("ingress_normalize_ready")
        assert prepared_text_event is not None
        attach_dispatch_pipeline_timing(prepared_text_event.source, dispatch_timing)
        return await self._dispatch_prepared_text_like_ingress(
            room=room,
            prepared_event=prepared_text_event,
            facts=prechecked_event.facts,
            ticket=ticket,
            coalescing_thread_id=coalescing_thread_id,
        )
