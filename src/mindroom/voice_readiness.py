"""Prepare durable voice input and satisfy its visible publication requirement."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    SOURCE_KIND_KEY,
    VOICE_PREFIX,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)
from mindroom.dispatch_handoff import PreparedIngress
from mindroom.dispatch_source import VOICE_SOURCE_KIND
from mindroom.inbound_turn_normalizer import VoiceNormalizationRequest
from mindroom.ingress_lanes import IngressRetryError
from mindroom.matrix.media import extract_media_caption
from mindroom.timing import attach_dispatch_pipeline_timing
from mindroom.turn_record import PreparedVoiceSource
from mindroom.visible_voice_echo import VisibleVoiceEchoRequest

if TYPE_CHECKING:
    import nio
    import structlog

    from mindroom.inbound_turn_normalizer import InboundTurnNormalizer
    from mindroom.matrix.media import AudioMessageEvent
    from mindroom.message_target import MessageTarget
    from mindroom.timing import DispatchPipelineTiming
    from mindroom.turn_store import TurnStore
    from mindroom.visible_voice_echo import VisibleVoiceEchoLifecycle


def _text_only_fallback(event: AudioMessageEvent, *, thread_id: str | None) -> PreparedIngress:
    """Return a dispatchable fallback when voice normalization itself fails."""
    body = f"{VOICE_PREFIX}{extract_media_caption(event, default='[Attached voice message]')}"
    source = dict(event.source) if isinstance(event.source, dict) else {}
    source_content = source.get("content")
    original_content = source_content if isinstance(source_content, dict) else {}
    content: dict[str, Any] = {
        "msgtype": "m.text",
        "body": body,
        ORIGINAL_SENDER_KEY: event.sender,
        SOURCE_KIND_KEY: VOICE_SOURCE_KIND,
        VOICE_RAW_AUDIO_FALLBACK_KEY: True,
    }
    inherited_mentions = original_content.get("m.mentions")
    if isinstance(inherited_mentions, dict):
        content["m.mentions"] = inherited_mentions
    attachment_ids = parse_attachment_ids_from_event_source(source)
    if attachment_ids:
        content[ATTACHMENT_IDS_KEY] = attachment_ids
    inherited_relation = original_content.get("m.relates_to")
    if isinstance(inherited_relation, dict):
        content["m.relates_to"] = inherited_relation
    if thread_id is not None:
        content["m.relates_to"] = {"rel_type": "m.thread", "event_id": thread_id}
    source["content"] = content
    return PreparedIngress(
        sender=event.sender,
        event_id=event.event_id,
        body=body,
        source=source,
        server_timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
        source_kind_override=VOICE_SOURCE_KIND,
    )


@dataclass(frozen=True)
class VoiceReadiness:
    """Own preparation, checkpointing, and publication before voice lane handoff."""

    normalizer: InboundTurnNormalizer
    turn_store: TurnStore
    visible_echo: VisibleVoiceEchoLifecycle
    logger: structlog.stdlib.BoundLogger

    def prepared_source(self, source_event_id: str) -> PreparedVoiceSource | None:
        """Read the checkpoint before the controller resolves the conversation target."""
        try:
            return self.turn_store.prepared_voice_for_source(source_event_id)
        except Exception as exc:
            self.logger.exception("Prepared voice checkpoint read failed", event_id=source_event_id)
            raise IngressRetryError from exc

    async def prepare(
        self,
        *,
        room: nio.MatrixRoom,
        event: AudioMessageEvent,
        target: MessageTarget,
        coalescing_thread_id: str | None,
        requester_user_id: str,
        dispatch_timing: DispatchPipelineTiming | None,
    ) -> PreparedIngress:
        """Return one checkpointed event only after required visible publication succeeds."""
        handle = self.visible_echo.start(
            VisibleVoiceEchoRequest(
                source_event_id=event.event_id,
                target=target,
                requester_user_id=requester_user_id,
                raw_source=event.source,
            ),
        )
        try:
            checkpoint = self.prepared_source(event.event_id)
            if checkpoint is not None:
                prepared_event = PreparedIngress(
                    sender=event.sender,
                    event_id=event.event_id,
                    body=checkpoint.body,
                    source={**event.source, "content": checkpoint.to_content()},
                    server_timestamp=event.server_timestamp,
                    source_kind_override=VOICE_SOURCE_KIND,
                )
            else:
                if dispatch_timing is not None:
                    dispatch_timing.mark("ingress_normalize_start")
                prepared_event = await self._normalize(
                    VoiceNormalizationRequest(room=room, event=event, thread_id=target.resolved_thread_id),
                )
                if dispatch_timing is not None:
                    dispatch_timing.mark("ingress_normalize_ready")
                checkpoint = await self.turn_store.record_prepared_voice(
                    event.event_id,
                    PreparedVoiceSource.from_content(
                        prepared_event.body,
                        prepared_event.source["content"],
                        thread_id=target.resolved_thread_id,
                        coalescing_thread_id=coalescing_thread_id,
                    ),
                )
            if (
                checkpoint is None
                or checkpoint.thread_id != target.resolved_thread_id
                or checkpoint.coalescing_thread_id != coalescing_thread_id
            ):
                raise IngressRetryError  # noqa: TRY301 - preserve expected retry without exception logging
            prepared_event = replace(
                prepared_event,
                body=checkpoint.body,
                source={**prepared_event.source, "content": checkpoint.to_content()},
            )
            attach_dispatch_pipeline_timing(prepared_event.source, dispatch_timing)
            if not await self.visible_echo.finish(handle, prepared_event):
                raise IngressRetryError  # noqa: TRY301 - preserve expected retry without exception logging
            if not await self.visible_echo.await_publication(
                room=room,
                source_event_id=event.event_id,
                requester_user_id=requester_user_id,
            ):
                raise IngressRetryError  # noqa: TRY301 - preserve expected retry without exception logging
        except IngressRetryError:
            raise
        except asyncio.CancelledError:
            self.visible_echo.finish_after_cancellation(
                handle,
                _text_only_fallback(event, thread_id=target.resolved_thread_id),
            )
            raise
        except Exception as exc:
            self.logger.exception(
                "Voice preparation checkpoint or publication failed; deferring this voice turn",
                event_id=event.event_id,
                room_id=room.room_id,
            )
            raise IngressRetryError from exc
        finally:
            self.visible_echo.abandon_unsettled(handle)
        return prepared_event

    async def _normalize(self, request: VoiceNormalizationRequest) -> PreparedIngress:
        """Limit audio fallback to input preparation failures, before checkpointing."""
        try:
            normalized = await self.normalizer.prepare_voice_event(request)
        except Exception:
            self.logger.exception(
                "Voice normalization failed; preparing raw-audio fallback",
                event_id=request.event.event_id,
                room_id=request.room.room_id,
            )
            normalized = None
        if normalized is not None:
            return normalized.event
        try:
            return (await self.normalizer.prepare_raw_voice_fallback_event(request)).event
        except Exception:
            self.logger.exception(
                "Voice raw-audio preparation failed; using text-only fallback",
                event_id=request.event.event_id,
                room_id=request.room.room_id,
            )
            return _text_only_fallback(request.event, thread_id=request.thread_id)
