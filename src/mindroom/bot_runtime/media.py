"""Media and attachment dispatch workflows for agent bots."""

from __future__ import annotations

from typing import TYPE_CHECKING

import nio

from mindroom import voice_handler
from mindroom.attachments import (
    append_attachment_ids_prompt,
    merge_attachment_ids,
    parse_attachment_ids_from_thread_history,
)
from mindroom.constants import ORIGINAL_SENDER_KEY, ROUTER_AGENT_NAME
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import is_agent_id
from mindroom.media_inputs import MediaInputs

from .types import _DispatchPayload, _MediaDispatchEvent, _SyntheticTextEvent

if TYPE_CHECKING:
    from agno.media import Image

    from mindroom.bot import AgentBot
    from mindroom.bot_runtime.types import _MessageContext


async def _build_dispatch_payload_with_attachments(
    self: AgentBot,
    *,
    room_id: str,
    context: _MessageContext,
    prompt: str,
    current_attachment_ids: list[str],
    media_thread_id: str | None,
    fallback_images: list[Image] | None = None,
) -> _DispatchPayload:
    """Build dispatch payload by merging thread/history attachment media."""
    assert self.client is not None
    thread_attachment_ids = (
        await self.resolve_thread_attachment_ids(
            self.client,
            self.storage_path,
            room_id=room_id,
            thread_id=context.thread_id,
        )
        if context.thread_id
        else []
    )
    history_attachment_ids = parse_attachment_ids_from_thread_history(context.thread_history)
    attachment_ids = merge_attachment_ids(
        current_attachment_ids,
        thread_attachment_ids,
        history_attachment_ids,
    )
    resolved_attachment_ids, attachment_audio, attachment_images, attachment_files, attachment_videos = (
        self.resolve_attachment_media(
            self.storage_path,
            attachment_ids,
            room_id=room_id,
            thread_id=media_thread_id,
        )
    )
    if fallback_images is not None and not attachment_images:
        attachment_images = fallback_images
    return _DispatchPayload(
        prompt=append_attachment_ids_prompt(prompt, resolved_attachment_ids),
        media=MediaInputs.from_optional(
            audio=attachment_audio,
            images=attachment_images,
            files=attachment_files,
            videos=attachment_videos,
        ),
        attachment_ids=resolved_attachment_ids or None,
    )


async def _on_audio_media_message(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
) -> None:
    """Normalize audio into a synthetic text event and reuse text dispatch."""
    assert self.client is not None

    requester_user_id = self._precheck_event(room, event)
    if requester_user_id is None:
        return

    if is_agent_id(event.sender, self.config):
        self.logger.debug(
            "Ignoring agent audio event for voice transcription",
            event_id=event.event_id,
            sender=event.sender,
        )
        self.response_tracker.mark_responded(event.event_id)
        return

    event_info = EventInfo.from_event(event.source)
    _, thread_id, _ = await self._derive_conversation_context(room.room_id, event_info)
    effective_thread_id = self._resolve_reply_thread_id(
        thread_id,
        event.event_id,
        room_id=room.room_id,
        event_source=event.source,
    )
    prepared_voice = await voice_handler.prepare_voice_message(
        self.client,
        self.storage_path,
        room,
        event,
        self.config,
        sender_domain=self.matrix_id.domain,
        thread_id=effective_thread_id,
    )
    if prepared_voice is None:
        self.response_tracker.mark_responded(event.event_id)
        return

    await self._maybe_send_visible_voice_echo(
        room,
        event,
        text=prepared_voice.text,
        thread_id=effective_thread_id,
    )

    await self._dispatch_text_message(
        room,
        _SyntheticTextEvent(
            sender=event.sender,
            event_id=event.event_id,
            body=prepared_voice.text,
            source=prepared_voice.source,
        ),
        requester_user_id,
    )


async def _maybe_send_visible_voice_echo(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    *,
    text: str,
    thread_id: str | None,
) -> str | None:
    """Optionally post a display-only router echo for normalized audio."""
    if self.agent_name != ROUTER_AGENT_NAME or not self.config.voice.visible_router_echo:
        return None

    existing_visible_echo_event_id = self.response_tracker.get_visible_echo_event_id(event.event_id)
    if existing_visible_echo_event_id is not None:
        return existing_visible_echo_event_id

    visible_echo_event_id = await self._send_response(
        room_id=room.room_id,
        reply_to_event_id=event.event_id,
        response_text=text,
        thread_id=thread_id,
        skip_mentions=True,
    )
    if visible_echo_event_id is not None:
        self.response_tracker.mark_visible_echo_sent(event.event_id, visible_echo_event_id)
    return visible_echo_event_id


async def _on_media_message(
    self: AgentBot,
    room: nio.MatrixRoom,
    event: _MediaDispatchEvent,
) -> None:
    """Handle image/file/video/audio events and dispatch media-aware responses."""
    assert self.client is not None

    if isinstance(event, nio.RoomMessageAudio | nio.RoomEncryptedAudio):
        await self._on_audio_media_message(room, event)
        return

    is_image_event = isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage)
    default_caption = (
        "[Attached image]"
        if is_image_event
        else "[Attached video]"
        if isinstance(event, nio.RoomMessageVideo | nio.RoomEncryptedVideo)
        else "[Attached file]"
    )
    caption = self.extract_media_caption(event, default=default_caption)

    dispatch = await self._prepare_dispatch(
        room,
        event,
        event_label="image" if is_image_event else "media",
    )
    if dispatch is None:
        return

    context = dispatch.context
    action = await self._resolve_dispatch_action(
        room,
        event,
        dispatch,
        message_for_decision=event.body,
        router_message=caption,
        extra_content={ORIGINAL_SENDER_KEY: event.sender},
    )
    if action is None:
        return

    effective_thread_id = self._resolve_reply_thread_id(
        context.thread_id,
        event.event_id,
        room_id=room.room_id,
        event_source=event.source,
    )
    current_attachment_ids: list[str]
    fallback_images: list[Image] | None = None
    if is_image_event:
        assert isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage)
        image = await self.image_handler.download_image(self.client, event)
        if image is None:
            self.logger.error("Failed to download image", event_id=event.event_id)
            self.response_tracker.mark_responded(event.event_id)
            return
        attachment_record = await self.register_image_attachment(
            self.client,
            self.storage_path,
            room_id=room.room_id,
            thread_id=effective_thread_id,
            event=event,
            image_bytes=image.content,
        )
        current_attachment_ids = [attachment_record.attachment_id] if attachment_record is not None else []
        fallback_images = [image]
    else:
        assert isinstance(
            event,
            nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
        )
        attachment_record = await self.register_file_or_video_attachment(
            self.client,
            self.storage_path,
            room_id=room.room_id,
            thread_id=effective_thread_id,
            event=event,
        )
        if attachment_record is None:
            self.logger.error("Failed to register media attachment", event_id=event.event_id)
            self.response_tracker.mark_responded(event.event_id)
            return
        current_attachment_ids = [attachment_record.attachment_id]
    payload = await self._build_dispatch_payload_with_attachments(
        room_id=room.room_id,
        context=context,
        prompt=caption,
        current_attachment_ids=current_attachment_ids,
        media_thread_id=effective_thread_id,
        fallback_images=fallback_images,
    )
    await self._execute_dispatch_action(
        room,
        event,
        dispatch,
        action,
        payload,
        processing_log="Processing image" if is_image_event else "Processing media message",
    )


async def _register_routed_attachment(
    self: AgentBot,
    *,
    room_id: str,
    thread_id: str | None,
    event: _MediaDispatchEvent,
) -> str | None:
    """Register a routed media event and return its attachment ID when available."""
    if isinstance(
        event,
        nio.RoomMessageFile | nio.RoomEncryptedFile | nio.RoomMessageVideo | nio.RoomEncryptedVideo,
    ):
        assert self.client is not None
        attachment_record = await self.register_file_or_video_attachment(
            self.client,
            self.storage_path,
            room_id=room_id,
            thread_id=thread_id,
            event=event,
        )
        if attachment_record is None:
            self.logger.error("Failed to register routed media attachment", event_id=event.event_id)
            return None
        return attachment_record.attachment_id

    if isinstance(event, nio.RoomMessageImage | nio.RoomEncryptedImage):
        assert self.client is not None
        attachment_record = await self.register_image_attachment(
            self.client,
            self.storage_path,
            room_id=room_id,
            thread_id=thread_id,
            event=event,
        )
        if attachment_record is None:
            self.logger.error("Failed to register routed image attachment", event_id=event.event_id)
            return None
        return attachment_record.attachment_id

    return None
