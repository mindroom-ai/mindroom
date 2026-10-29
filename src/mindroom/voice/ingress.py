"""Admit one voice message: fix its conversation, then make it ready behind its sender's lane."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, cast

from mindroom.coalescing.batch import PendingEvent, requester_coalescing_key
from mindroom.coalescing.gate import ReadyPendingEvent
from mindroom.coalescing.ingress_lanes import IngressRetryError
from mindroom.dispatch_callback_outcome import TurnDispatchOutcome
from mindroom.dispatch_recovery_context import turn_dispatch_recovery_active
from mindroom.thread_utils import mentions_another_participant
from mindroom.turns.queued_notice import mark_waiting_for_approval, queued_notice_reservation_if_busy

if TYPE_CHECKING:
    import nio
    import structlog

    from mindroom.bot_runtime_view import BotRuntimeView
    from mindroom.coalescing.ingress_ticket import IngressTicket
    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.delivery_gateway import DeliveryGateway
    from mindroom.dispatch_handoff import DispatchEvent
    from mindroom.hooks import MessageEnvelope
    from mindroom.matrix.identity import MatrixID
    from mindroom.matrix.media import AudioMessageEvent
    from mindroom.message_target import MessageTarget
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.response_runner import ResponseRunner
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.turns.facts import IngressFacts
    from mindroom.voice.readiness import VoiceReadiness


@dataclass(frozen=True)
class VoiceIngressDeps:
    """Collaborators needed to admit voice messages."""

    runtime: BotRuntimeView
    logger: structlog.stdlib.BoundLogger
    runtime_paths: RuntimePaths
    matrix_id: MatrixID
    resolver: ConversationResolver
    response_runner: ResponseRunner
    delivery_gateway: DeliveryGateway
    readiness: VoiceReadiness


@dataclass
class VoiceIngress:
    """Own one voice message from its prechecked callback to a ready event its lane hands the gate."""

    deps: VoiceIngressDeps

    async def admit(
        self,
        room: nio.MatrixRoom,
        event: AudioMessageEvent,
        facts: IngressFacts,
        *,
        dispatch_timing: DispatchPipelineTiming | None,
        ticket: IngressTicket,
    ) -> TurnDispatchOutcome:
        """Resolve the audio conversation key once, then defer voice normalization.

        Raises ``RelatedEventUnavailableError`` when the message's relation target cannot be read.
        """
        prepared = await self.deps.readiness.prepared_source(event.event_id)
        coalescing_thread_id = (
            prepared.coalescing_thread_id
            if prepared is not None
            else await self.deps.resolver.coalescing_thread_id(room, event)
        )
        voice_target = self.deps.resolver.build_message_target(
            room_id=room.room_id,
            thread_id=coalescing_thread_id,
            reply_to_event_id=event.event_id,
            event_source=event.source,
        )
        if prepared is not None:
            voice_target = voice_target.with_thread_root(prepared.thread_id)
        admission_key = requester_coalescing_key(room.room_id, coalescing_thread_id, facts.lane_requester_id)

        ready_task = asyncio.create_task(
            self._ready_voice_event(
                room=room,
                event=event,
                facts=facts,
                voice_target=voice_target,
                coalescing_thread_id=coalescing_thread_id,
                dispatch_timing=dispatch_timing,
            ),
            name=f"voice_ready:{room.room_id}:{event.event_id}",
        )
        ticket.admit_ready(admission_key, ready_task, source_event_id=event.event_id)
        return TurnDispatchOutcome.DEFERRED

    def _final_queued_notice(
        self,
        *,
        target: MessageTarget,
        envelope: MessageEnvelope,
        reservation: QueuedHumanNoticeReservation | None,
    ) -> QueuedHumanNoticeReservation | None:
        """Keep the queued notice while a response still holds the voice's conversation once readiness ends.

        Release it if that conversation went idle while the voice was transcribed, or reserve one if it became busy.
        """
        if not self.deps.response_runner.has_active_response_for_target(target):
            if reservation is not None:
                reservation.release()
            return None
        return reservation or queued_notice_reservation_if_busy(
            self.deps.response_runner,
            target=target,
            envelope=envelope,
        )

    async def _ready_voice_event(
        self,
        *,
        room: nio.MatrixRoom,
        event: AudioMessageEvent,
        facts: IngressFacts,
        voice_target: MessageTarget,
        coalescing_thread_id: str | None,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> ReadyPendingEvent:
        """Normalize a raw voice event after its conversation key is fixed; its lane holds the turn claim."""
        queued_notice_reservation = None
        try:
            envelope = self.deps.resolver.build_envelope(
                event=cast("DispatchEvent", event),
                facts=facts,
                target=voice_target,
            )
            queued_notice_reservation = queued_notice_reservation_if_busy(
                self.deps.response_runner,
                target=voice_target,
                envelope=envelope,
            )
            normalized_event = await self.deps.readiness.prepare(
                room=room,
                event=event,
                target=voice_target,
                coalescing_thread_id=coalescing_thread_id,
                requester_user_id=facts.lane_requester_id,
                dispatch_timing=dispatch_timing,
            )
            envelope = self.deps.resolver.build_envelope(event=normalized_event, facts=facts, target=voice_target)
            queued_notice_reservation = self._final_queued_notice(
                target=voice_target,
                envelope=envelope,
                reservation=queued_notice_reservation,
            )
            ready = ReadyPendingEvent(
                pending_event=PendingEvent(
                    event=replace(
                        normalized_event,
                        turn_dispatch_recovery=turn_dispatch_recovery_active(),
                        for_another_participant=mentions_another_participant(
                            normalized_event.source,
                            self.deps.matrix_id,
                            self.deps.runtime.config,
                            self.deps.runtime_paths,
                            room=room,
                        ),
                    ),
                    room=room,
                    facts=facts,
                    notice=queued_notice_reservation,
                ),
            )
        except IngressRetryError:
            raise
        except Exception as exc:
            self.deps.logger.exception(
                "Voice dispatch metadata failed; deferring this voice turn",
                event_id=event.event_id,
                room_id=room.room_id,
            )
            raise IngressRetryError from exc
        else:
            # The ready event owns its queued notice from here.
            queued_notice_reservation = None
            mark_waiting_for_approval(self.deps, room, event.source, voice_target, envelope)
            return ready
        finally:
            if queued_notice_reservation is not None:
                queued_notice_reservation.release()
