"""Inbound text, voice, and media normalization for bot dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

import nio

from mindroom.attachments import append_attachment_ids_prompt
from mindroom.coalescing import PreparedTextEvent
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.message_content import (
    is_v2_sidecar_text_preview,
)
from mindroom.media_inputs import MediaInputs

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from pathlib import Path

    import structlog
    from agno.media import Image

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.client import ResolvedVisibleMessage
    from mindroom.matrix.identity import MatrixID
    from mindroom.message_target import MessageTarget

type MediaDispatchEvent = (
    nio.RoomMessageImage
    | nio.RoomEncryptedImage
    | nio.RoomMessageFile
    | nio.RoomEncryptedFile
    | nio.RoomMessageVideo
    | nio.RoomEncryptedVideo
)


class _PreparedVoiceMessage(Protocol):
    """Minimal prepared voice surface needed by the normalizer."""

    text: str
    source: dict[str, Any]


class _AttachmentRecord(Protocol):
    """Minimal attachment record surface needed by the normalizer."""

    attachment_id: str


@dataclass(frozen=True)
class TextNormalizationRequest:
    """One inbound text-like event to normalize."""

    event: nio.RoomMessageText | PreparedTextEvent


@dataclass(frozen=True)
class VoiceNormalizationRequest:
    """One inbound audio event to normalize into a text dispatch event."""

    room: nio.MatrixRoom
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio


@dataclass(frozen=True)
class VoiceNormalizationResult:
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
class BatchMediaAttachmentResult:
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

    client_getter: Callable[[], nio.AsyncClient | None]
    storage_path: Path
    config_getter: Callable[[], Config]
    runtime_paths: RuntimePaths
    matrix_id_getter: Callable[[], MatrixID]
    logger_getter: Callable[[], structlog.stdlib.BoundLogger]
    prepare_voice_message: Callable[..., Awaitable[_PreparedVoiceMessage | None]]
    resolve_event_source_content: Callable[..., Awaitable[dict[str, Any]]]
    visible_body_from_event_source: Callable[[dict[str, Any], str], str]
    download_image: Callable[..., Awaitable[Image | None]]
    register_file_or_video_attachment: Callable[..., Awaitable[_AttachmentRecord | None]]
    register_image_attachment: Callable[..., Awaitable[_AttachmentRecord | None]]
    resolve_attachment_media: Callable[..., tuple[list[str], list[Any], list[Image], list[Any], list[Any]]]
    build_message_target: Callable[..., MessageTarget]
    derive_conversation_context: Callable[
        [str, EventInfo],
        Awaitable[tuple[bool, str | None, Sequence[ResolvedVisibleMessage]]],
    ]
    resolve_thread_attachment_ids: Callable[..., Awaitable[list[str]]]
    parse_attachment_ids_from_thread_history: Callable[[Sequence[ResolvedVisibleMessage]], list[str]]
    merge_attachment_ids: Callable[..., list[str]]


@dataclass(frozen=True)
class InboundTurnNormalizer:
    """Normalize raw inbound events into dispatch-ready forms."""

    deps: InboundTurnNormalizerDeps

    def _client(self) -> nio.AsyncClient:
        client = self.deps.client_getter()
        if client is None:
            msg = "Matrix client is not ready for inbound normalization"
            raise RuntimeError(msg)
        return client

    def _config(self) -> Config:
        """Return the bot's current live config."""
        return self.deps.config_getter()

    def _logger(self) -> structlog.stdlib.BoundLogger:
        """Return the bot's current live logger."""
        return self.deps.logger_getter()

    def _matrix_id(self) -> MatrixID:
        """Return the bot's current live Matrix ID."""
        return self.deps.matrix_id_getter()

    async def resolve_text_event(self, request: TextNormalizationRequest) -> PreparedTextEvent:
        """Return one canonical text event for hooks, routing, and command handling."""
        event = request.event
        if isinstance(event, PreparedTextEvent):
            return event

        resolved_source = await self.deps.resolve_event_source_content(event.source, self._client())
        return PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=self.deps.visible_body_from_event_source(resolved_source, event.body),
            source=resolved_source,
            server_timestamp=event.server_timestamp if isinstance(event.server_timestamp, int) else None,
        )

    async def prepare_voice_event(self, request: VoiceNormalizationRequest) -> VoiceNormalizationResult | None:
        """Normalize one audio message into a prepared text event."""
        client = self._client()
        event_info = EventInfo.from_event(request.event.source)
        _, thread_id, _ = await self.deps.derive_conversation_context(request.room.room_id, event_info)
        effective_thread_id = self.deps.build_message_target(
            room_id=request.room.room_id,
            thread_id=thread_id,
            reply_to_event_id=request.event.event_id,
            event_source=request.event.source,
        ).resolved_thread_id
        prepared_voice = await self.deps.prepare_voice_message(
            client,
            self.deps.storage_path,
            request.room,
            request.event,
            self._config(),
            runtime_paths=self.deps.runtime_paths,
            sender_domain=self._matrix_id().domain,
            thread_id=effective_thread_id,
        )
        if prepared_voice is None:
            return None

        return VoiceNormalizationResult(
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
        event: nio.RoomMessageFile | nio.RoomEncryptedFile,
    ) -> PreparedTextEvent | None:
        """Return a prepared text event when a file event is really a long-text preview."""
        if not is_v2_sidecar_text_preview(event.source):
            return None

        resolved_source = await self.deps.resolve_event_source_content(event.source, self._client())
        return PreparedTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=self.deps.visible_body_from_event_source(resolved_source, event.body),
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
        client = self._client()
        if isinstance(
            event,
            nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
        ):
            attachment_record = await self.deps.register_file_or_video_attachment(
                client,
                self.deps.storage_path,
                room_id=room_id,
                thread_id=thread_id,
                event=event,
            )
            if attachment_record is None:
                self._logger().error("Failed to register routed media attachment", event_id=event.event_id)
                return None
            return attachment_record.attachment_id

        if isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage):
            attachment_record = await self.deps.register_image_attachment(
                client,
                self.deps.storage_path,
                room_id=room_id,
                thread_id=thread_id,
                event=event,
            )
            if attachment_record is None:
                self._logger().error("Failed to register routed image attachment", event_id=event.event_id)
                return None
            return attachment_record.attachment_id

        return None

    async def register_batch_media_attachments(
        self,
        request: BatchMediaAttachmentRequest,
    ) -> BatchMediaAttachmentResult:
        """Register media attachments for one coalesced batch."""
        if not request.media_events:
            return BatchMediaAttachmentResult(attachment_ids=[])

        client = self._client()
        attachment_ids: list[str] = []
        fallback_images: list[Image] = []
        for media_event in request.media_events:
            if isinstance(media_event, nio.RoomMessageImage | nio.RoomEncryptedImage):
                image = await self.deps.download_image(client, media_event)
                if image is None:
                    msg = "Failed to download image"
                    raise RuntimeError(msg)
                attachment_record = await self.deps.register_image_attachment(
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

            attachment_record = await self.deps.register_file_or_video_attachment(
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

        return BatchMediaAttachmentResult(
            attachment_ids=attachment_ids,
            fallback_images=fallback_images or None,
        )

    async def build_dispatch_payload_with_attachments(
        self,
        request: DispatchPayloadWithAttachmentsRequest,
    ) -> DispatchPayload:
        """Build dispatch payload by merging thread/history attachment media."""
        thread_attachment_ids = (
            await self.deps.resolve_thread_attachment_ids(
                self._client(),
                self.deps.storage_path,
                room_id=request.room_id,
                thread_id=request.thread_id,
            )
            if request.thread_id
            else []
        )
        history_attachment_ids = self.deps.parse_attachment_ids_from_thread_history(request.thread_history)
        attachment_ids = self.deps.merge_attachment_ids(
            request.current_attachment_ids,
            thread_attachment_ids,
            history_attachment_ids,
        )
        resolved_attachment_ids, attachment_audio, attachment_images, attachment_files, attachment_videos = (
            self.deps.resolve_attachment_media(
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
        return DispatchPayload(
            prompt=append_attachment_ids_prompt(request.prompt, resolved_attachment_ids),
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
    ) -> nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo:
        """Narrow a media dispatch event to the file/video subset used for attachment registration."""
        if isinstance(
            event,
            nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
        ):
            return event
        msg = f"Expected file or video event, got {type(event).__name__}"
        raise TypeError(msg)
