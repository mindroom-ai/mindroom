"""Typed boundary between coalescing and turn dispatch preparation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Protocol, TypeGuard, cast

import nio

from mindroom.attachments import parse_attachment_ids_from_event_source
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
    ORIGINAL_SENDER_KEY,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
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
from mindroom.matrix.message_content import is_v2_sidecar_text_preview

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from mindroom.coalescing_batch import CoalescedBatch


class _PendingEventLike(Protocol):
    event: DispatchEvent
    source_kind: str
    trust_internal_payload_metadata: bool


@dataclass(frozen=True)
class PreparedTextEvent:
    """Canonical inbound text event for dispatch."""

    sender: str
    event_id: str
    body: str
    source: dict[str, Any]
    server_timestamp: int | float | None = None
    is_synthetic: bool = False
    source_kind_override: str | None = None


# Voice messages are normalized into PreparedTextEvent before coalescing, so
# this contract only includes routed image/file/video events.
type MediaDispatchEvent = MatrixMediaDispatchEvent
type TextDispatchEvent = nio.RoomMessageText | PreparedTextEvent
type DispatchEvent = TextDispatchEvent | MediaDispatchEvent


@dataclass
class PendingDispatchMetadata:
    """Opaque metadata that must be closed if claimed work cannot dispatch."""

    kind: str
    payload: object
    close: Callable[[], None]
    requires_solo_batch: bool = False


@dataclass(frozen=True)
class DispatchIngressMetadata:
    """Trusted ingress source and policy metadata for one dispatch handoff."""

    source_kind: str
    dispatch_policy_source_kind: str | None = None
    hook_source: str | None = None
    message_received_depth: int = 0


@dataclass(frozen=True)
class DispatchPayloadMetadata:
    """Payload facts that should not rely on synthetic Matrix event content."""

    attachment_ids: tuple[str, ...] | None = None
    original_sender: str | None = None
    raw_audio_fallback: bool | None = None
    mentioned_user_ids: tuple[str, ...] | None = None
    formatted_bodies: tuple[str, ...] | None = None
    skip_mentions: bool | None = None


@dataclass(frozen=True)
class DispatchHandoff:
    """Coalesced dispatch input handed to the turn controller."""

    room: nio.MatrixRoom
    event: TextDispatchEvent
    requester_user_id: str
    ingress: DispatchIngressMetadata
    payload: DispatchPayloadMetadata = field(default_factory=DispatchPayloadMetadata)
    trust_hydrated_internal_metadata: bool = False
    source_event_ids: tuple[str, ...] = ()
    source_event_prompts: Mapping[str, str] = field(default_factory=dict)
    media_events: tuple[MediaDispatchEvent, ...] = ()
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = ()


def _event_content_dict(event: DispatchEvent) -> dict[str, object] | None:
    if not isinstance(event.source, dict):
        return None
    content = event.source.get("content")
    if not isinstance(content, dict):
        return None
    return cast("dict[str, object]", content)


def is_media_dispatch_event(event: DispatchEvent) -> TypeGuard[MediaDispatchEvent]:
    """Return whether one dispatch event is image, file, or video media."""
    return is_matrix_media_dispatch_event(event)


def dispatch_prompt_for_event(event: DispatchEvent) -> str:
    """Return the prompt text contributed by one dispatch event."""
    if is_audio_message_event(event):
        msg = "Raw audio must be normalized into PreparedTextEvent before coalescing"
        raise TypeError(msg)
    if is_image_message_event(event):
        return extract_media_caption(event, default="[Attached image]")
    if is_video_message_event(event):
        return extract_media_caption(event, default="[Attached video]")
    if is_file_message_event(event):
        return extract_media_caption(event, default="[Attached file]")
    return event.body


def _collect_batch_mentions_and_formatted_bodies(
    pending_events: tuple[_PendingEventLike, ...],
) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None, bool | None]:
    all_user_ids: list[str] = []
    seen_user_ids: set[str] = set()
    formatted_parts: list[str] = []
    skip_mentions = False
    inspected_content = False
    for pending_event in pending_events:
        content = _event_content_dict(pending_event.event)
        if content is None:
            continue
        inspected_content = True
        raw_mentions = content.get("m.mentions")
        if isinstance(raw_mentions, dict):
            mentions = cast("dict[str, Any]", raw_mentions)
            for uid in mentions.get("user_ids", []):
                if isinstance(uid, str) and uid not in seen_user_ids:
                    all_user_ids.append(uid)
                    seen_user_ids.add(uid)
        formatted_body = content.get("formatted_body")
        if isinstance(formatted_body, str) and formatted_body:
            formatted_parts.append(formatted_body)
        if pending_event.trust_internal_payload_metadata and content.get("com.mindroom.skip_mentions") is True:
            skip_mentions = True
    if not inspected_content:
        return None, None, None
    return tuple(all_user_ids), tuple(formatted_parts), skip_mentions


def _batch_payload_metadata(batch: CoalescedBatch) -> DispatchPayloadMetadata:
    single_raw_sidecar_preview = (
        len(batch.pending_events) == 1
        and isinstance(batch.primary_event, nio.RoomMessageText)
        and is_v2_sidecar_text_preview(batch.primary_event.source)
    )
    mentioned_user_ids, formatted_bodies, skip_mentions = _collect_batch_mentions_and_formatted_bodies(
        batch.pending_events,
    )
    return DispatchPayloadMetadata(
        attachment_ids=None if single_raw_sidecar_preview else tuple(batch.attachment_ids),
        original_sender=None if single_raw_sidecar_preview else batch.original_sender,
        raw_audio_fallback=None if single_raw_sidecar_preview else batch.raw_audio_fallback,
        mentioned_user_ids=None if single_raw_sidecar_preview else mentioned_user_ids,
        formatted_bodies=None if single_raw_sidecar_preview else formatted_bodies,
        skip_mentions=None if single_raw_sidecar_preview else skip_mentions,
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
            mentioned_user_ids=mentioned_user_ids,
            formatted_bodies=formatted_bodies,
            skip_mentions=False,
        )

    original_sender = content.get(ORIGINAL_SENDER_KEY)
    raw_audio_fallback = content.get(VOICE_RAW_AUDIO_FALLBACK_KEY)
    return DispatchPayloadMetadata(
        attachment_ids=tuple(parse_attachment_ids_from_event_source(source)),
        original_sender=original_sender if isinstance(original_sender, str) else None,
        raw_audio_fallback=raw_audio_fallback is True,
        mentioned_user_ids=mentioned_user_ids,
        formatted_bodies=formatted_bodies,
        skip_mentions=content.get("com.mindroom.skip_mentions") is True,
    )


def merge_payload_metadata(
    base: DispatchPayloadMetadata,
    hydrated: DispatchPayloadMetadata,
    *,
    trust_hydrated_internal_metadata: bool,
) -> DispatchPayloadMetadata:
    """Fill unknown handoff metadata from hydrated text content."""
    attachment_ids = base.attachment_ids
    original_sender = base.original_sender
    raw_audio_fallback = base.raw_audio_fallback
    skip_mentions = base.skip_mentions
    if trust_hydrated_internal_metadata:
        if attachment_ids is None:
            attachment_ids = hydrated.attachment_ids
        if original_sender is None:
            original_sender = hydrated.original_sender
        if raw_audio_fallback is None:
            raw_audio_fallback = hydrated.raw_audio_fallback
        if skip_mentions is None:
            skip_mentions = hydrated.skip_mentions
    else:
        attachment_ids = attachment_ids if attachment_ids is not None else ()
        raw_audio_fallback = raw_audio_fallback if raw_audio_fallback is not None else False
        skip_mentions = skip_mentions if skip_mentions is not None else False

    return DispatchPayloadMetadata(
        attachment_ids=attachment_ids,
        original_sender=original_sender,
        raw_audio_fallback=raw_audio_fallback,
        mentioned_user_ids=base.mentioned_user_ids
        if base.mentioned_user_ids is not None
        else hydrated.mentioned_user_ids,
        formatted_bodies=base.formatted_bodies if base.formatted_bodies is not None else hydrated.formatted_bodies,
        skip_mentions=skip_mentions,
    )


_SYNTHETIC_BATCH_INTERNAL_CONTENT_KEYS: frozenset[str] = frozenset(
    {
        ATTACHMENT_IDS_KEY,
        HOOK_MESSAGE_RECEIVED_DEPTH_KEY,
        ORIGINAL_SENDER_KEY,
        VOICE_RAW_AUDIO_FALLBACK_KEY,
        "com.mindroom.hook_source",
        "com.mindroom.skip_mentions",
        "com.mindroom.source_kind",
    },
)


def _merge_batch_source(batch: CoalescedBatch) -> dict[str, Any]:
    primary_source: dict[str, Any] = batch.primary_event.source if isinstance(batch.primary_event.source, dict) else {}
    merged: dict[str, Any] = dict(primary_source)
    primary_content: dict[str, Any] = dict(merged.get("content", {})) if isinstance(merged.get("content"), dict) else {}
    for key in _SYNTHETIC_BATCH_INTERNAL_CONTENT_KEYS:
        primary_content.pop(key, None)
    payload = _batch_payload_metadata(batch)
    if payload.mentioned_user_ids:
        primary_content["m.mentions"] = {"user_ids": list(payload.mentioned_user_ids)}
    if payload.formatted_bodies:
        primary_content["formatted_body"] = "<br>".join(payload.formatted_bodies)
        primary_content["format"] = "org.matrix.custom.html"
    if payload.original_sender is not None:
        primary_content[ORIGINAL_SENDER_KEY] = payload.original_sender
    if payload.raw_audio_fallback:
        primary_content[VOICE_RAW_AUDIO_FALLBACK_KEY] = True
    if payload.attachment_ids:
        primary_content[ATTACHMENT_IDS_KEY] = list(payload.attachment_ids)
    merged["content"] = primary_content
    return merged


def _single_prepared_dispatch_event(event: PreparedTextEvent, source_kind: str) -> PreparedTextEvent:
    if source_kind in {"message", event.source_kind_override}:
        return event
    return replace(event, source_kind_override=source_kind)


def _build_batch_dispatch_event(batch: CoalescedBatch) -> TextDispatchEvent:
    """Return the text dispatch event for one batch."""
    if len(batch.pending_events) == 1 and isinstance(batch.primary_event, nio.RoomMessageText | PreparedTextEvent):
        if isinstance(batch.primary_event, PreparedTextEvent):
            return _single_prepared_dispatch_event(batch.primary_event, batch.source_kind)
        return batch.primary_event
    return PreparedTextEvent(
        sender=batch.primary_event.sender,
        event_id=batch.primary_event.event_id,
        body=batch.prompt,
        source=_merge_batch_source(batch),
        server_timestamp=batch.primary_event.server_timestamp,
        is_synthetic=True,
        source_kind_override=batch.source_kind,
    )


def build_dispatch_handoff(batch: CoalescedBatch) -> DispatchHandoff:
    """Build the explicit dispatch handoff for one coalesced batch."""
    return DispatchHandoff(
        room=batch.room,
        event=_build_batch_dispatch_event(batch),
        requester_user_id=batch.requester_user_id,
        ingress=DispatchIngressMetadata(
            source_kind=batch.source_kind,
            dispatch_policy_source_kind=batch.dispatch_policy_source_kind,
            hook_source=batch.hook_source,
            message_received_depth=batch.message_received_depth,
        ),
        payload=_batch_payload_metadata(batch),
        trust_hydrated_internal_metadata=any(
            pending_event.trust_internal_payload_metadata for pending_event in batch.pending_events
        ),
        source_event_ids=tuple(batch.source_event_ids),
        source_event_prompts=dict(batch.source_event_prompts),
        media_events=tuple(batch.media_events),
        dispatch_metadata=batch.dispatch_metadata,
    )
