"""Coalesced dispatch batch construction."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from math import isfinite
from typing import TYPE_CHECKING, NamedTuple
from xml.sax.saxutils import quoteattr as xml_quoteattr

from .attachments import merge_attachment_ids, parse_attachment_ids_from_event_source
from .constants import ORIGINAL_SENDER_KEY, VOICE_RAW_AUDIO_FALLBACK_KEY, VOICE_TRANSCRIPT_KEY
from .dispatch_handoff import (
    DispatchEvent,
    MediaDispatchEvent,
    PendingDispatchMetadata,
    dispatch_prompt_for_event,
    event_content_dict,
    is_media_dispatch_event,
)
from .dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from .handled_turns import SourceEventMetadata

if TYPE_CHECKING:
    import nio


class CoalescingKey(NamedTuple):
    """Physical coalescing scope for one requester in one room or thread."""

    room_id: str
    thread_id: str | None
    requester_user_id: str


type TimestampFormatter = Callable[[float | None], str | None]


_ACTIVE_FOLLOW_UP_OWNER_PREFIX = "__mindroom_active_follow_up__"


def active_follow_up_coalescing_key(room_id: str, thread_id: str | None) -> CoalescingKey:
    """Return the target-scoped key for follow-ups queued behind an active response."""
    return CoalescingKey(
        room_id,
        thread_id,
        f"{_ACTIVE_FOLLOW_UP_OWNER_PREFIX}:{thread_id or 'room'}",
    )


def is_active_follow_up_coalescing_key(key: CoalescingKey) -> bool:
    """Return whether a coalescing key is target-scoped for an active response."""
    return key.requester_user_id.startswith(f"{_ACTIVE_FOLLOW_UP_OWNER_PREFIX}:")


@dataclass
class PendingEvent:
    """One queued inbound event waiting to be coalesced."""

    event: DispatchEvent
    room: nio.MatrixRoom
    source_kind: str
    dispatch_policy_source_kind: str | None = None
    hook_source: str | None = None
    message_received_depth: int = 0
    trust_internal_payload_metadata: bool = False
    requester_user_id: str | None = None
    enqueue_time: float = field(default_factory=time.time)
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = ()


@dataclass(frozen=True)
class CoalescedBatch:
    """One flushed batch ready to dispatch through the text pipeline."""

    coalescing_key: CoalescingKey
    room: nio.MatrixRoom
    primary_event: DispatchEvent
    requester_user_id: str
    pending_events: tuple[PendingEvent, ...]
    prompt: str
    source_kind: str
    dispatch_policy_source_kind: str | None
    hook_source: str | None
    message_received_depth: int
    attachment_ids: list[str]
    source_event_ids: list[str]
    source_event_prompts: dict[str, str]
    source_event_metadata: dict[str, SourceEventMetadata]
    media_events: list[MediaDispatchEvent]
    original_sender: str | None = None
    raw_audio_fallback: bool = False
    voice_transcript: bool = False
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = ()


def _pending_event_trusts_internal_payload(pending_event: PendingEvent) -> bool:
    return pending_event.trust_internal_payload_metadata


def coalesced_prompt(message_bodies: list[str]) -> str:
    """Return the single prompt text used to dispatch one coalesced turn."""
    if len(message_bodies) == 1:
        return message_bodies[0]
    combined_body = "\n".join(message_bodies)
    return (
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        f"{combined_body}"
    )


def _timestamp_attribute(
    timestamp_ms: float | None,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    if timestamp_formatter is None:
        return ""
    formatted_timestamp = timestamp_formatter(timestamp_ms)
    return f" ts={xml_quoteattr(formatted_timestamp)}" if formatted_timestamp is not None else ""


def _render_tagged_message(
    *,
    event_id: str,
    sender: str,
    body: str,
    timestamp_ms: float | None,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    return (
        f"<msg event_id={xml_quoteattr(event_id)} "
        f"from={xml_quoteattr(sender)}"
        f"{_timestamp_attribute(timestamp_ms, timestamp_formatter)}><![CDATA[{body}]]></msg>"
    )


def _tagged_pending_message(
    pending_event: PendingEvent,
    *,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    sender = pending_event.requester_user_id or pending_event.event.sender
    return _render_tagged_message(
        event_id=pending_event.event.event_id,
        sender=sender,
        body=dispatch_prompt_for_event(pending_event.event),
        timestamp_ms=_normalized_timestamp_ms(pending_event.event.server_timestamp),
        timestamp_formatter=timestamp_formatter,
    )


def _active_follow_up_prompt(
    pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    rendered_messages = "\n".join(
        _tagged_pending_message(pending_event, timestamp_formatter=timestamp_formatter)
        for pending_event in pending_events
    )
    return (
        "Messages arrived while the previous response was still running. "
        "They are in chat timeline order. Respond once to the combined context:\n\n"
        f"<queued_messages>\n{rendered_messages}\n</queued_messages>"
    )


def _tagged_coalesced_prompt(
    ordered_pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter,
) -> str:
    rendered_messages = "\n".join(
        _tagged_pending_message(pending_event, timestamp_formatter=timestamp_formatter)
        for pending_event in ordered_pending_events
    )
    return (
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        f"<messages>\n{rendered_messages}\n</messages>"
    )


def tagged_coalesced_prompt(
    source_event_ids: list[str] | tuple[str, ...],
    source_event_prompts: dict[str, str],
    source_event_metadata: dict[str, SourceEventMetadata],
    *,
    timestamp_formatter: TimestampFormatter,
) -> str | None:
    """Render a persisted coalesced turn with the same model-facing message tags."""
    rendered_messages: list[str] = []
    for source_event_id in source_event_ids:
        prompt = source_event_prompts.get(source_event_id)
        metadata = source_event_metadata.get(source_event_id)
        if prompt is None or metadata is None:
            return None
        rendered_messages.append(
            _render_tagged_message(
                event_id=source_event_id,
                sender=metadata.sender,
                body=prompt,
                timestamp_ms=metadata.timestamp_ms,
                timestamp_formatter=timestamp_formatter,
            ),
        )
    rendered_body = "\n".join(rendered_messages)
    return (
        "The user sent the following messages in quick succession. "
        "Treat them as one turn and respond once:\n\n"
        f"<messages>\n{rendered_body}\n</messages>"
    )


def _coalesced_prompt_for_events(
    ordered_pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    if (
        len(ordered_pending_events) > 1
        and _batch_dispatch_policy_source_kind(ordered_pending_events) == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND
    ):
        return _active_follow_up_prompt(
            ordered_pending_events,
            timestamp_formatter=timestamp_formatter,
        )
    if len(ordered_pending_events) > 1 and timestamp_formatter is not None:
        return _tagged_coalesced_prompt(ordered_pending_events, timestamp_formatter=timestamp_formatter)
    return coalesced_prompt(
        [dispatch_prompt_for_event(pending_event.event) for pending_event in ordered_pending_events],
    )


def _batch_metadata(pending_events: list[PendingEvent]) -> tuple[str | None, bool, bool]:
    original_sender: str | None = None
    raw_audio_fallback = False
    voice_transcript = False
    for pending_event in pending_events:
        if not _pending_event_trusts_internal_payload(pending_event):
            continue
        content = event_content_dict(pending_event.event)
        if content is None:
            continue
        if original_sender is None:
            content_original_sender = content.get(ORIGINAL_SENDER_KEY)
            if isinstance(content_original_sender, str):
                original_sender = content_original_sender
        if content.get(VOICE_RAW_AUDIO_FALLBACK_KEY) is True:
            raw_audio_fallback = True
        if content.get(VOICE_TRANSCRIPT_KEY) is True:
            voice_transcript = True
    return original_sender, raw_audio_fallback, voice_transcript


_SOURCE_KIND_PRIORITY: dict[str, int] = {
    VOICE_SOURCE_KIND: 0,
    IMAGE_SOURCE_KIND: 1,
    MEDIA_SOURCE_KIND: 2,
}


def _batch_source_kind(ordered_pending_events: list[PendingEvent]) -> str:
    resolved_source_kinds = [pending_event.source_kind for pending_event in ordered_pending_events]
    return min(resolved_source_kinds, key=lambda sk: _SOURCE_KIND_PRIORITY.get(sk, 999))


def _batch_dispatch_policy_source_kind(ordered_pending_events: list[PendingEvent]) -> str | None:
    resolved_policy_kinds = {
        pending_event.dispatch_policy_source_kind
        for pending_event in ordered_pending_events
        if pending_event.dispatch_policy_source_kind is not None
    }
    if not resolved_policy_kinds:
        return None
    if len(resolved_policy_kinds) == 1:
        return next(iter(resolved_policy_kinds))
    msg = "Coalesced batch carried multiple dispatch policy source kinds"
    raise ValueError(msg)


def _batch_hook_source(ordered_pending_events: list[PendingEvent]) -> str | None:
    hook_sources = {
        pending_event.hook_source for pending_event in ordered_pending_events if pending_event.hook_source is not None
    }
    if not hook_sources:
        return None
    if len(hook_sources) == 1:
        return next(iter(hook_sources))
    msg = "Coalesced batch carried multiple hook sources"
    raise ValueError(msg)


def _batch_message_received_depth(ordered_pending_events: list[PendingEvent]) -> int:
    return max((pending_event.message_received_depth for pending_event in ordered_pending_events), default=0)


def _batch_dispatch_metadata(
    ordered_pending_events: list[PendingEvent],
) -> tuple[PendingDispatchMetadata, ...]:
    metadata = tuple(item for pending_event in ordered_pending_events for item in pending_event.dispatch_metadata)
    if not metadata:
        return ()
    if len(ordered_pending_events) == 1 or not any(item.requires_solo_batch for item in metadata):
        return metadata
    msg = "Pending dispatch metadata requires solo batches"
    raise ValueError(msg)


def close_pending_event_metadata(pending_events: list[PendingEvent]) -> None:
    """Close opaque metadata owned by pending events that cannot dispatch."""
    for pending_event in pending_events:
        for item in pending_event.dispatch_metadata:
            item.close()


def _batch_source_event_prompts(ordered_pending_events: list[PendingEvent]) -> dict[str, str]:
    return {
        pending_event.event.event_id: dispatch_prompt_for_event(pending_event.event)
        for pending_event in ordered_pending_events
    }


def _normalized_timestamp_ms(timestamp_ms: object) -> float | None:
    if isinstance(timestamp_ms, bool) or not isinstance(timestamp_ms, int | float):
        return None
    timestamp_ms_float = float(timestamp_ms)
    return timestamp_ms_float if isfinite(timestamp_ms_float) and timestamp_ms_float >= 0 else None


def _batch_source_event_metadata(ordered_pending_events: list[PendingEvent]) -> dict[str, SourceEventMetadata]:
    return {
        pending_event.event.event_id: SourceEventMetadata(
            sender=pending_event.requester_user_id or pending_event.event.sender,
            timestamp_ms=_normalized_timestamp_ms(pending_event.event.server_timestamp),
        )
        for pending_event in ordered_pending_events
    }


def build_coalesced_batch(
    key: CoalescingKey,
    pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter | None = None,
) -> CoalescedBatch:
    """Build one normalized dispatch batch from queued pending events."""
    ordered_pending_events = list(pending_events)
    primary_pending_event = ordered_pending_events[-1]
    original_sender, raw_audio_fallback, voice_transcript = _batch_metadata(ordered_pending_events)
    return CoalescedBatch(
        coalescing_key=key,
        room=primary_pending_event.room,
        primary_event=primary_pending_event.event,
        requester_user_id=primary_pending_event.requester_user_id or key.requester_user_id,
        pending_events=tuple(ordered_pending_events),
        prompt=_coalesced_prompt_for_events(
            ordered_pending_events,
            timestamp_formatter=timestamp_formatter,
        ),
        source_kind=_batch_source_kind(ordered_pending_events),
        dispatch_policy_source_kind=_batch_dispatch_policy_source_kind(ordered_pending_events),
        hook_source=_batch_hook_source(ordered_pending_events),
        message_received_depth=_batch_message_received_depth(ordered_pending_events),
        attachment_ids=merge_attachment_ids(
            *(
                parse_attachment_ids_from_event_source(pending_event.event.source)
                for pending_event in ordered_pending_events
                if _pending_event_trusts_internal_payload(pending_event)
            ),
        ),
        source_event_ids=[pending_event.event.event_id for pending_event in ordered_pending_events],
        source_event_prompts=_batch_source_event_prompts(ordered_pending_events),
        source_event_metadata=_batch_source_event_metadata(ordered_pending_events),
        media_events=[
            pending_event.event
            for pending_event in ordered_pending_events
            if is_media_dispatch_event(pending_event.event)
        ],
        original_sender=original_sender,
        raw_audio_fallback=raw_audio_fallback,
        voice_transcript=voice_transcript,
        dispatch_metadata=_batch_dispatch_metadata(ordered_pending_events),
    )
