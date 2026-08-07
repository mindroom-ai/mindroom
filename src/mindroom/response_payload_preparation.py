"""Execution-side, under-lock assembly of one response request's payload.

Ingress (``TurnController`` + ``text_ingress_dispatch``) builds a complete,
immutable :class:`ResponsePayloadPreparation` value and hands it to the
response runner inside the :class:`~mindroom.response_runner.ResponseRequest`.
Once the runner owns the lifecycle lock and has refreshed thread history, it
calls :meth:`ResponsePayloadPreparer.prepare` to finish assembling the request.

Data crosses the seam as values, and the work runs here as a first-class named
step rather than as a closure back into ingress.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.attachment_ids import merge_attachment_ids
from mindroom.inbound_turn_normalizer import (
    BatchMediaAttachmentRequest,
    DispatchPayloadWithAttachmentsRequest,
)
from mindroom.matrix.media import parse_matrix_media_dispatch_event_source
from mindroom.matrix.thread_history_result import ThreadHistoryResult
from mindroom.timing import elapsed_ms_between, emit_elapsed_timing
from mindroom.turn_record import TurnInputSnapshot, TurnMediaSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog

    from mindroom.dispatch_handoff import MediaDispatchEvent
    from mindroom.inbound_turn_normalizer import DispatchPayload, InboundTurnNormalizer
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.response_runner import ResponseRequest
    from mindroom.turn_policy import IngressHookRunner, PreparedDispatch


class _TurnInputSnapshotCorruptionError(RuntimeError):
    """A recorded turn input snapshot cannot replay without inventing input."""


@dataclass(frozen=True)
class DispatchPayloadInputs:
    """Attachment and media inputs produced by ingress for one response payload."""

    message_attachment_ids: tuple[str, ...]
    trusted_attachment_ids: tuple[str, ...]
    media_events: tuple[MediaDispatchEvent, ...]
    raw_audio_fallback: bool = False
    voice_transcript: bool = False


def turn_input_snapshot(payload_inputs: DispatchPayloadInputs) -> TurnInputSnapshot:
    """Return the durable form of one turn's media and attachment inputs.

    Recorded with the turn at durable adoption, because the journal payload is
    otherwise the only copy of these facts and nothing in ``TurnRecord``
    describes them.
    """
    return TurnInputSnapshot(
        media_sources=tuple(
            TurnMediaSource(event_id=media_event.event_id, source=dict(media_event.source))
            for media_event in payload_inputs.media_events
        ),
        message_attachment_ids=payload_inputs.message_attachment_ids,
        trusted_attachment_ids=payload_inputs.trusted_attachment_ids,
        raw_audio_fallback=payload_inputs.raw_audio_fallback,
        voice_transcript=payload_inputs.voice_transcript,
    )


def _dispatch_payload_inputs_from_snapshot(snapshot: TurnInputSnapshot) -> DispatchPayloadInputs:
    """Rebuild one turn's exact media and attachment inputs from durable state.

    A source that no longer parses as the media event it was recorded as is
    refused rather than guessed: running the turn on a different input and
    calling it recovery is the failure this snapshot exists to prevent.

    Private and unwired on purpose. The snapshot's consumer is the contract-2
    handoff, where an adopted turn stops being the journal's work and starts
    being ``TurnStore``'s; recording it has to land first, because that handoff
    cannot be written while the media it needs is only in the journal payload.
    """
    media_events: list[MediaDispatchEvent] = []
    for media_source in snapshot.media_sources:
        media_event = parse_matrix_media_dispatch_event_source(media_source.source)
        if media_event is None or media_event.event_id != media_source.event_id:
            msg = f"Recorded turn input media {media_source.event_id!r} does not replay as itself"
            raise _TurnInputSnapshotCorruptionError(msg)
        media_events.append(media_event)
    return DispatchPayloadInputs(
        message_attachment_ids=snapshot.message_attachment_ids,
        trusted_attachment_ids=snapshot.trusted_attachment_ids,
        media_events=tuple(media_events),
        raw_audio_fallback=snapshot.raw_audio_fallback,
        voice_transcript=snapshot.voice_transcript,
    )


@dataclass(frozen=True)
class ResponsePayloadPreparation:
    """Immutable ingress inputs for the under-lock payload-assembly step.

    Everything here is a value captured at dispatch time. The only input that
    is *not* carried is the model-facing thread history, which the runner
    refreshes after acquiring the lifecycle lock and supplies through
    ``ResponseRequest.thread_history``.
    """

    dispatch: PreparedDispatch
    prompt: str
    action_kind: str
    payload_inputs: DispatchPayloadInputs
    target_member_names: tuple[str, ...] | None
    dispatch_started_at: float
    context_ready_monotonic: float


@dataclass
class ResponsePayloadPreparer:
    """Assemble the final payload for one response request under the lock."""

    normalizer: InboundTurnNormalizer
    ingress_hook_runner: IngressHookRunner
    agent_name: str
    logger: structlog.stdlib.BoundLogger

    async def prepare(self, request: ResponseRequest) -> ResponseRequest:
        """Return ``request`` with its payload built from refreshed history.

        The runner has already refreshed ``request.thread_history`` and verified
        that ``request.payload_preparation`` is set before calling this.
        """
        preparation = request.payload_preparation
        assert preparation is not None
        dispatch = preparation.dispatch
        pipeline_timing = request.pipeline_timing
        if pipeline_timing is not None:
            pipeline_timing.mark("response_payload_start")

        payload = await self._build_payload(preparation, thread_history=request.thread_history)

        prepared_payload = await self.ingress_hook_runner.apply_message_enrichment(
            dispatch,
            payload,
            target_entity_name=self.agent_name,
            target_member_names=preparation.target_member_names,
        )
        system_enrichment_items = await self.ingress_hook_runner.apply_system_enrichment(
            dispatch,
            prepared_payload.envelope,
            target_entity_name=self.agent_name,
            target_member_names=preparation.target_member_names,
        )
        payload_ready_monotonic = time.monotonic()
        if pipeline_timing is not None:
            pipeline_timing.mark("response_payload_ready")
        self._log_dispatch_latency(
            preparation,
            payload_ready_monotonic=payload_ready_monotonic,
            thread_history=request.thread_history,
        )
        return replace(
            request,
            prompt=prepared_payload.payload.prompt,
            model_prompt=prepared_payload.payload.model_prompt,
            media=prepared_payload.payload.media,
            attachment_ids=tuple(prepared_payload.payload.attachment_ids or ()),
            response_envelope=prepared_payload.envelope,
            transient_enrichment_items=prepared_payload.transient_enrichment_items,
            system_enrichment_items=tuple(system_enrichment_items),
            requires_model_history_refresh=False,
            payload_preparation=None,
        )

    async def _build_payload(
        self,
        preparation: ResponsePayloadPreparation,
        *,
        thread_history: Sequence[ResolvedVisibleMessage],
    ) -> DispatchPayload:
        """Register batch media and merge attachment context into one payload."""
        dispatch = preparation.dispatch
        payload_inputs = preparation.payload_inputs
        room_id = dispatch.target.room_id
        media_thread_id = dispatch.target.resolved_thread_id
        payload_builder_started = time.monotonic()
        payload_builder_outcome = "failed"
        try:
            media_attachment_ids: list[str] = []
            fallback_images = None
            if payload_inputs.media_events:
                media_result = await self.normalizer.register_batch_media_attachments(
                    BatchMediaAttachmentRequest(
                        room_id=room_id,
                        thread_id=media_thread_id,
                        media_events=list(payload_inputs.media_events),
                    ),
                )
                media_attachment_ids = media_result.attachment_ids
                fallback_images = media_result.fallback_images
            payload = await self.normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id=room_id,
                    prompt=preparation.prompt,
                    current_attachment_ids=merge_attachment_ids(
                        list(payload_inputs.message_attachment_ids),
                        media_attachment_ids,
                    ),
                    trusted_current_attachment_ids=list(payload_inputs.trusted_attachment_ids),
                    thread_id=dispatch.context.thread_id,
                    media_thread_id=media_thread_id,
                    thread_history=thread_history,
                    fallback_images=fallback_images,
                    raw_audio_fallback=payload_inputs.raw_audio_fallback,
                    voice_transcript=payload_inputs.voice_transcript,
                ),
            )
            payload_builder_outcome = "success"
            return payload
        finally:
            emit_elapsed_timing(
                "response_payload.builder",
                payload_builder_started,
                room_id=room_id,
                thread_id=media_thread_id,
                outcome=payload_builder_outcome,
            )

    def _log_dispatch_latency(
        self,
        preparation: ResponsePayloadPreparation,
        *,
        payload_ready_monotonic: float,
        thread_history: Sequence[ResolvedVisibleMessage],
    ) -> None:
        """Emit startup latency metrics for one dispatch that will respond."""
        latency_event_data: dict[str, str | float | int | bool] = {
            "event_id": preparation.dispatch.envelope.source_event_id,
            "action_kind": preparation.action_kind,
            "context_hydration_ms": elapsed_ms_between(
                preparation.dispatch_started_at,
                preparation.context_ready_monotonic,
            ),
            "payload_hydration_ms": elapsed_ms_between(
                preparation.context_ready_monotonic,
                payload_ready_monotonic,
            ),
            "startup_total_ms": elapsed_ms_between(
                preparation.dispatch_started_at,
                payload_ready_monotonic,
            ),
        }
        if isinstance(thread_history, ThreadHistoryResult):
            latency_event_data.update(thread_history.diagnostics)
        self.logger.info("Response startup latency", **latency_event_data)
