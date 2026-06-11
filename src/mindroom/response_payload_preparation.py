"""Execution-side, under-lock assembly of one response request's payload.

Ingress (``TurnController`` + ``text_ingress_dispatch``) builds a complete,
immutable :class:`ResponsePayloadPreparation` value and hands it to the
response runner inside the :class:`~mindroom.response_runner.ResponseRequest`.
Once the runner owns the lifecycle lock and has refreshed thread history, it
calls :meth:`ResponsePayloadPreparer.prepare` to finish assembling the request.

This is the execution-side counterpart of what used to be a ``prepare_after_lock``
callback into the caller: the data crosses the seam as values, and the work runs
here as a first-class named step rather than as a closure back into ingress.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from mindroom.attachments import merge_attachment_ids
from mindroom.inbound_turn_normalizer import (
    BatchMediaAttachmentRequest,
    DispatchPayloadWithAttachmentsRequest,
)
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.timing import elapsed_ms_between, emit_elapsed_timing

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog

    from mindroom.dispatch_handoff import MediaDispatchEvent
    from mindroom.inbound_turn_normalizer import DispatchPayload, InboundTurnNormalizer
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage
    from mindroom.response_runner import ResponseRequest
    from mindroom.turn_policy import IngressHookRunner, PreparedDispatch


@dataclass(frozen=True)
class DispatchPayloadInputs:
    """Attachment and media inputs produced by ingress for one response payload."""

    message_attachment_ids: tuple[str, ...]
    trusted_attachment_ids: tuple[str, ...]
    media_events: tuple[MediaDispatchEvent, ...]


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
    message_attachment_ids: tuple[str, ...]
    trusted_attachment_ids: tuple[str, ...]
    media_events: tuple[MediaDispatchEvent, ...]
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
        if system_enrichment_items:
            prepared_payload = replace(
                prepared_payload,
                system_enrichment_items=tuple(system_enrichment_items),
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
            thread_history=request.thread_history,
            prompt=prepared_payload.payload.prompt,
            model_prompt=prepared_payload.payload.model_prompt,
            media=prepared_payload.payload.media,
            attachment_ids=tuple(prepared_payload.payload.attachment_ids or ()),
            response_envelope=prepared_payload.envelope,
            system_enrichment_items=prepared_payload.system_enrichment_items,
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
        room_id = dispatch.target.room_id
        media_thread_id = dispatch.target.resolved_thread_id
        payload_builder_started = time.monotonic()
        payload_builder_outcome = "failed"
        try:
            media_attachment_ids: list[str] = []
            fallback_images = None
            if preparation.media_events:
                media_result = await self.normalizer.register_batch_media_attachments(
                    BatchMediaAttachmentRequest(
                        room_id=room_id,
                        thread_id=media_thread_id,
                        media_events=list(preparation.media_events),
                    ),
                )
                media_attachment_ids = media_result.attachment_ids
                fallback_images = media_result.fallback_images
            payload = await self.normalizer.build_dispatch_payload_with_attachments(
                DispatchPayloadWithAttachmentsRequest(
                    room_id=room_id,
                    prompt=preparation.prompt,
                    current_attachment_ids=merge_attachment_ids(
                        list(preparation.message_attachment_ids),
                        media_attachment_ids,
                    ),
                    trusted_current_attachment_ids=list(preparation.trusted_attachment_ids),
                    thread_id=dispatch.context.thread_id,
                    media_thread_id=media_thread_id,
                    thread_history=thread_history,
                    fallback_images=fallback_images,
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
