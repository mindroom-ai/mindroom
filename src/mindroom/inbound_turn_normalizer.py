"""Own raw input shaping for inbound Matrix turns."""

from __future__ import annotations

import time
from collections.abc import Sequence  # noqa: TC003
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.attachment_media import resolve_attachment_media
from mindroom.attachments import (
    merge_attachment_ids,
    parse_attachment_ids_from_thread_history,
    register_matrix_media_attachment,
    register_thread_history_media_attachments,
    resolve_thread_attachment_ids,
)
from mindroom.dispatch_handoff import MediaDispatchEvent, PreparedTextEvent
from mindroom.logging_config import bound_log_context
from mindroom.matrix.client_visible_messages import resolve_visible_event_source
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.image_handler import download_image
from mindroom.matrix.media import (
    AudioMessageEvent,
    FileMessageEvent,
    FileOrVideoMessageEvent,
    is_file_or_video_message_event,
    is_image_message_event,
    is_matrix_media_dispatch_event,
)
from mindroom.matrix.message_content import is_v2_sidecar_text_preview
from mindroom.media_inputs import MediaInputs
from mindroom.runtime_protocols import SupportsClientConfig  # noqa: TC001
from mindroom.timing import emit_elapsed_timing
from mindroom.voice_handler import prepare_voice_message

if TYPE_CHECKING:
    from pathlib import Path

    import nio
    import structlog
    from agno.media import Image

    from mindroom.constants import RuntimePaths
    from mindroom.conversation_resolver import ConversationResolver
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


@dataclass(frozen=True)
class TextNormalizationRequest:
    """One inbound text-like event to normalize."""

    event: nio.RoomMessageText | PreparedTextEvent


@dataclass(frozen=True)
class VoiceNormalizationRequest:
    """One inbound audio event to normalize into a text dispatch event."""

    room: nio.MatrixRoom
    event: AudioMessageEvent


@dataclass(frozen=True)
class _VoiceNormalizationResult:
    """Normalized text event plus resolved delivery thread for one audio turn."""

    event: PreparedTextEvent
    effective_thread_id: str | None


@dataclass(frozen=True)
class BatchMediaAttachmentRequest:
    """One batch of media events to register for downstream dispatch."""

    room_id: str
    thread_id: str | None
    media_events: list[MediaDispatchEvent]


@dataclass(frozen=True)
class _BatchMediaAttachmentResult:
    """Attachment IDs and fallback images resolved from one media batch."""

    attachment_ids: list[str]
    fallback_images: list[Image] | None = None


@dataclass(frozen=True)
class DispatchPayload:
    """Prompt plus multimodal payload assembled for downstream response generation."""

    prompt: str
    model_prompt: str | None = None
    media: MediaInputs = field(default_factory=MediaInputs)
    attachment_ids: list[str] | None = None


@dataclass(frozen=True)
class DispatchPayloadWithAttachmentsRequest:
    """One payload build request that merges current, thread, and history attachments."""

    room_id: str
    prompt: str
    current_attachment_ids: list[str]
    thread_id: str | None
    media_thread_id: str | None
    thread_history: Sequence[ResolvedVisibleMessage]
    fallback_images: list[Image] | None = None


@dataclass(frozen=True)
class InboundTurnNormalizerDeps:
    """Explicit collaborators for inbound normalization."""

    runtime: SupportsClientConfig
    logger: structlog.stdlib.BoundLogger
    storage_path: Path
    runtime_paths: RuntimePaths
    sender_domain: str
    conversation_resolver: ConversationResolver


@dataclass(frozen=True)
class InboundTurnNormalizer:
    """Turn raw text, voice, sidecar, and media events into canonical turn inputs."""

    deps: InboundTurnNormalizerDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.runtime.client
        if client is None:
            msg = "Matrix client is not ready for inbound normalization"
            raise RuntimeError(msg)
        return client

    async def resolve_text_event(self, request: TextNormalizationRequest) -> PreparedTextEvent:
        """Return one canonical text event for hooks, routing, and command handling."""
        event = request.event
        if isinstance(event, PreparedTextEvent):
            return event

        resolved_source, body = await resolve_visible_event_source(
            event.source,
            self._client(),
            fallback_body=event.body,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        return PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=body,
            source=resolved_source,
            server_timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
        )

    async def prepare_voice_event(self, request: VoiceNormalizationRequest) -> _VoiceNormalizationResult | None:
        """Normalize one audio message into a prepared text event."""
        client = self._client()
        event_info = EventInfo.from_event(request.event.source)
        _, thread_id, _ = await self.deps.conversation_resolver.derive_conversation_context(
            request.room.room_id,
            event_info,
            event_id=request.event.event_id,
            caller_label="voice_normalization",
        )
        effective_thread_id = self.deps.conversation_resolver.build_message_target(
            room_id=request.room.room_id,
            thread_id=thread_id,
            reply_to_event_id=request.event.event_id,
            event_source=request.event.source,
        ).resolved_thread_id
        with bound_log_context(room_id=request.room.room_id, thread_id=effective_thread_id):
            prepared_voice = await prepare_voice_message(
                client,
                self.deps.storage_path,
                request.room,
                request.event,
                self.deps.runtime.config,
                runtime_paths=self.deps.runtime_paths,
                sender_domain=self.deps.sender_domain,
                thread_id=effective_thread_id,
            )
            if prepared_voice is None:
                return None

            return _VoiceNormalizationResult(
                event=PreparedTextEvent(
                    sender=request.event.sender,
                    event_id=request.event.event_id,
                    body=prepared_voice.text,
                    source={
                        **prepared_voice.source,
                        "content": {
                            **prepared_voice.source.get("content", {}),
                            "com.mindroom.source_kind": "voice",
                        },
                    },
                    server_timestamp=request.event.server_timestamp,
                    is_synthetic=True,
                    source_kind_override="voice",
                ),
                effective_thread_id=effective_thread_id,
            )

    async def prepare_file_sidecar_text_event(
        self,
        event: FileMessageEvent,
    ) -> PreparedTextEvent | None:
        """Return a prepared text event when a file event is really a long-text preview."""
        if not is_v2_sidecar_text_preview(event.source):
            return None

        resolved_source, body = await resolve_visible_event_source(
            event.source,
            self._client(),
            fallback_body=event.body,
            config=self.deps.runtime.config,
            runtime_paths=self.deps.runtime_paths,
        )
        return PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=body,
            source=resolved_source,
            server_timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
        )

    async def register_routed_attachment(
        self,
        *,
        room_id: str,
        thread_id: str | None,
        event: nio.RoomMessageText | PreparedTextEvent | MediaDispatchEvent,
    ) -> str | None:
        """Register a routed media event and return its attachment ID when available."""
        if not is_matrix_media_dispatch_event(event):
            return None
        attachment_record = await register_matrix_media_attachment(
            self._client(),
            self.deps.storage_path,
            room_id=room_id,
            thread_id=thread_id,
            event=event,
        )
        if attachment_record is None:
            self.deps.logger.error(
                "Failed to register routed media attachment",
                event_id=event.event_id,
            )
            return None
        return attachment_record.attachment_id

    async def register_batch_media_attachments(
        self,
        request: BatchMediaAttachmentRequest,
    ) -> _BatchMediaAttachmentResult:
        """Register media attachments for one coalesced batch."""
        started = time.monotonic()
        media_event_count = len(request.media_events)
        image_event_count = 0
        file_or_video_event_count = 0
        attachment_ids: list[str] = []
        fallback_images: list[Image] = []

        def emit_registration_timing(*, outcome: str) -> None:
            emit_elapsed_timing(
                "response_payload.register_batch_media_attachments",
                started,
                room_id=request.room_id,
                thread_id=request.thread_id,
                outcome=outcome,
                media_event_count=media_event_count,
                image_event_count=image_event_count,
                file_or_video_event_count=file_or_video_event_count,
                attachment_count=len(attachment_ids),
                fallback_image_count=len(fallback_images),
            )

        registration_succeeded = False
        try:
            if not request.media_events:
                registration_succeeded = True
                return _BatchMediaAttachmentResult(attachment_ids=[])

            client = self._client()
            for media_event in request.media_events:
                if is_image_message_event(media_event):
                    image_event_count += 1
                    image = await download_image(client, media_event)
                    if image is None:
                        msg = "Failed to download image"
                        raise RuntimeError(msg)
                    attachment_record = await register_matrix_media_attachment(
                        client,
                        self.deps.storage_path,
                        room_id=request.room_id,
                        thread_id=request.thread_id,
                        event=media_event,
                        image_bytes=image.content,
                    )
                    if attachment_record is not None:
                        attachment_ids.append(attachment_record.attachment_id)
                    else:
                        fallback_images.append(image)
                    continue

                file_or_video_event_count += 1
                attachment_record = await register_matrix_media_attachment(
                    client,
                    self.deps.storage_path,
                    room_id=request.room_id,
                    thread_id=request.thread_id,
                    event=self._as_file_or_video_dispatch_event(media_event),
                )
                if attachment_record is None:
                    msg = "Failed to register media attachment"
                    raise RuntimeError(msg)
                attachment_ids.append(attachment_record.attachment_id)

            registration_succeeded = True
            return _BatchMediaAttachmentResult(
                attachment_ids=attachment_ids,
                fallback_images=fallback_images or None,
            )
        finally:
            emit_registration_timing(outcome="success" if registration_succeeded else "failed")

    async def build_dispatch_payload_with_attachments(
        self,
        request: DispatchPayloadWithAttachmentsRequest,
    ) -> DispatchPayload:
        """Build dispatch payload by merging thread/history attachment media."""
        thread_attachment_ids = (
            await resolve_thread_attachment_ids(
                self._client(),
                self.deps.storage_path,
                room_id=request.room_id,
                thread_id=request.thread_id,
            )
            if request.thread_id
            else []
        )
        history_attachment_ids = parse_attachment_ids_from_thread_history(request.thread_history)
        history_media_attachment_ids = await register_thread_history_media_attachments(
            self._client(),
            self.deps.storage_path,
            room_id=request.room_id,
            thread_id=request.media_thread_id,
            thread_history=request.thread_history,
        )
        attachment_ids = merge_attachment_ids(
            request.current_attachment_ids,
            thread_attachment_ids,
            history_attachment_ids,
            history_media_attachment_ids,
        )
        resolved_attachment_ids, attachment_audio, attachment_images, attachment_files, attachment_videos = (
            resolve_attachment_media(
                self.deps.storage_path,
                attachment_ids,
                room_id=request.room_id,
                thread_id=request.media_thread_id,
            )
        )
        if request.fallback_images:
            attachment_images = (
                [*attachment_images, *request.fallback_images] if attachment_images else list(request.fallback_images)
            )
        attachment_prompt = (
            (
                "Available attachment IDs: "
                f"{', '.join(resolved_attachment_ids)}. Use tool calls to inspect or process them."
            )
            if resolved_attachment_ids
            else None
        )
        return DispatchPayload(
            prompt=request.prompt,
            model_prompt=attachment_prompt,
            media=MediaInputs.from_optional(
                audio=attachment_audio,
                images=attachment_images,
                files=attachment_files,
                videos=attachment_videos,
            ),
            attachment_ids=resolved_attachment_ids or None,
        )

    @staticmethod
    def _as_file_or_video_dispatch_event(
        event: MediaDispatchEvent,
    ) -> FileOrVideoMessageEvent:
        """Narrow a media dispatch event to the file/video subset used for attachment registration."""
        if is_file_or_video_message_event(event):
            return event
        msg = f"Expected file or video event, got {type(event).__name__}"
        raise TypeError(msg)
