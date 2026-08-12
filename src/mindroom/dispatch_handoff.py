"""Typed ingress values and payload metadata used by turn dispatch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeGuard, cast

import nio

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    SKIP_MENTIONS_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
    VOICE_TRANSCRIPT_KEY,
)
from mindroom.matrix.media import (
    MatrixMediaDispatchEvent,
    extract_media_caption,
    is_audio_message_event,
    is_file_message_event,
    is_image_message_event,
    is_matrix_media_dispatch_event,
    is_video_message_event,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.coalescing_batch import CoalescingKey
    from mindroom.message_target import ResponseLifecycleKey


# Voice messages are normalized into PreparedIngress before coalescing, so
# this contract only includes routed image/file/video events.
type MediaDispatchEvent = MatrixMediaDispatchEvent
# Raw formatted messages exist only before normalization. Once ingress reaches
# the coalescing boundary, every text-like value is PreparedIngress.
type _TextDispatchEvent = nio.RoomMessageFormatted | PreparedIngress
type DispatchEvent = _TextDispatchEvent | MediaDispatchEvent


@dataclass(frozen=True)
class PreparedIngress:
    """Canonical prepared ingress value plus its per-source dispatch evidence.

    The core value (sender/event_id/body/source) is the normalized inbound text
    for dispatch; raw Matrix media events are wrapped at enqueue with their
    caption as body and the protocol object retained on ``raw_event`` for
    attachment registration and media planning. The remaining fields carry the
    per-source evidence resolved at enqueue (effective requester, source kinds,
    trust, discovery, and recovery metadata) so queue entries need no parallel
    mutable fields.
    """

    sender: str
    event_id: str
    body: str
    source: dict[str, Any]
    server_timestamp: int | float | None = None
    source_kind_override: str | None = None
    requester_user_id: str | None = None
    source_kind: str | None = None
    dispatch_policy_source_kind: str | None = None
    hook_source: str | None = None
    message_received_depth: int = 0
    trust_internal_payload_metadata: bool = False
    discovery_event_id: str | None = None
    turn_dispatch_recovery: bool = False
    raw_event: MediaDispatchEvent | None = None


@dataclass
class PendingDispatchMetadata:
    """Opaque metadata that must be closed if claimed work cannot dispatch."""

    kind: str
    payload: object
    close: Callable[[], None]
    requires_solo_batch: bool = False
    target_key: ResponseLifecycleKey | None = None
    _closed: bool = field(default=False, init=False, repr=False)

    def close_once(self) -> None:
        """Release the owned resource at most once across converging cleanup paths."""
        if self._closed:
            return
        self._closed = True
        self.close()


@dataclass(frozen=True)
class DispatchIngressMetadata:
    """Trusted ingress source and policy metadata for one dispatch."""

    source_kind: str
    coalescing_key: CoalescingKey | None = None
    dispatch_policy_source_kind: str | None = None
    hook_source: str | None = None
    message_received_depth: int = 0


@dataclass(frozen=True)
class DispatchPayloadMetadata:
    """Payload facts that should not rely on synthetic Matrix event content."""

    attachment_ids: tuple[str, ...] | None = None
    original_sender: str | None = None
    raw_audio_fallback: bool | None = None
    voice_transcript: bool | None = None
    mentioned_user_ids: tuple[str, ...] | None = None
    formatted_bodies: tuple[str, ...] | None = None
    skip_mentions: bool | None = None


def event_content_dict(event: DispatchEvent) -> dict[str, object] | None:
    """Return Matrix content from a dispatch event when it has mapping content."""
    if not isinstance(event.source, dict):
        return None
    content = event.source.get("content")
    if not isinstance(content, dict):
        return None
    return cast("dict[str, object]", content)


def is_media_dispatch_event(event: DispatchEvent) -> bool:
    """Return whether one dispatch event is image, file, or video media, direct or wrapped."""
    if isinstance(event, PreparedIngress):
        return event.raw_event is not None
    return is_matrix_media_dispatch_event(event)


def is_text_dispatch_event(event: DispatchEvent) -> TypeGuard[_TextDispatchEvent]:
    """Return whether one dispatch event is a text-like utterance.

    The runtime companion to ``_TextDispatchEvent``, which cannot be used with
    ``isinstance`` directly. Every caller asking this question used to spell the
    class list out, and each copy had to be widened separately -- which is how
    `m.emote` came to be text in one place and media-ish in another.
    """
    return isinstance(event, nio.RoomMessageFormatted) or (
        isinstance(event, PreparedIngress) and event.raw_event is None
    )


def dispatch_prompt_for_event(event: DispatchEvent) -> str:
    """Return the prompt text contributed by one dispatch event."""
    if is_audio_message_event(event):
        msg = "Raw audio must be normalized into PreparedIngress before coalescing"
        raise TypeError(msg)
    if is_image_message_event(event):
        return extract_media_caption(event, default="[Attached image]")
    if is_video_message_event(event):
        return extract_media_caption(event, default="[Attached video]")
    if is_file_message_event(event):
        return extract_media_caption(event, default="[Attached file]")
    return event.body


def prepare_media_ingress(event: MediaDispatchEvent) -> PreparedIngress:
    """Wrap one raw Matrix media event into its prepared ingress form.

    The body is the caption exactly as ``dispatch_prompt_for_event`` renders it;
    the raw protocol event is retained on ``raw_event`` for attachment
    registration and media planning.
    """
    return PreparedIngress(
        sender=event.sender,
        event_id=event.event_id,
        body=dispatch_prompt_for_event(event),
        source=event.source,
        server_timestamp=event.server_timestamp,
        raw_event=event,
    )


def payload_metadata_from_source(
    source: dict[str, Any],
    *,
    trust_internal_metadata: bool,
) -> DispatchPayloadMetadata:
    """Extract payload metadata from a resolved Matrix event source."""
    content = source.get("content")
    if not isinstance(content, dict):
        return DispatchPayloadMetadata()

    mentioned_user_ids: tuple[str, ...] = ()
    mentions = content.get("m.mentions")
    if isinstance(mentions, dict):
        mentioned_user_ids = tuple(uid for uid in mentions.get("user_ids", ()) if isinstance(uid, str))

    formatted_body = content.get("formatted_body")
    formatted_bodies = (formatted_body,) if isinstance(formatted_body, str) and formatted_body else ()
    if not trust_internal_metadata:
        return DispatchPayloadMetadata(
            attachment_ids=(),
            original_sender=None,
            raw_audio_fallback=False,
            voice_transcript=False,
            mentioned_user_ids=mentioned_user_ids,
            formatted_bodies=formatted_bodies,
            skip_mentions=False,
        )

    original_sender = content.get(ORIGINAL_SENDER_KEY)
    raw_audio_fallback = content.get(VOICE_RAW_AUDIO_FALLBACK_KEY)
    voice_transcript = content.get(VOICE_TRANSCRIPT_KEY)
    return DispatchPayloadMetadata(
        attachment_ids=tuple(parse_attachment_ids_from_event_source(source)),
        original_sender=original_sender if isinstance(original_sender, str) else None,
        raw_audio_fallback=raw_audio_fallback is True,
        voice_transcript=voice_transcript is True,
        mentioned_user_ids=mentioned_user_ids,
        formatted_bodies=formatted_bodies,
        skip_mentions=content.get(SKIP_MENTIONS_KEY) is True,
    )
