"""Coalesced dispatch batch construction."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from .attachments import merge_attachment_ids, parse_attachment_ids_from_event_source
from .constants import ORIGINAL_SENDER_KEY, VOICE_RAW_AUDIO_FALLBACK_KEY
from .dispatch_handoff import (
    DispatchEvent,
    MediaDispatchEvent,
    PendingDispatchMetadata,
    dispatch_prompt_for_event,
    is_media_dispatch_event,
)

if TYPE_CHECKING:
    import nio

type CoalescingKey = tuple[str, str | None, str]


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
    enqueue_time: float = field(default_factory=time.time)
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = ()


@dataclass(frozen=True)
class CoalescedBatch:
    """One flushed batch ready to dispatch through the text pipeline."""

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
    media_events: list[MediaDispatchEvent]
    original_sender: str | None = None
    raw_audio_fallback: bool = False
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = ()


def _event_content_dict(event: DispatchEvent) -> dict[str, object] | None:
    if not isinstance(event.source, dict):
        return None
    content = event.source.get("content")
    if not isinstance(content, dict):
        return None
    return cast("dict[str, object]", content)


def _pending_event_trusts_internal_payload(pending_event: PendingEvent) -> bool:
    return pending_event.trust_internal_payload_metadata


def coalesced_prompt(message_bodies: list[str]) -> str:
    """Return the single prompt text used to dispatch one coalesced turn."""
    if len(message_bodies) == 1:
        return message_bodies[0]
    combined_body = "\n".join(message_bodies)
    return f"The user sent the following messages in quick succession. Treat them as one turn and respond once:\n\n{combined_body}"


def _batch_metadata(pending_events: list[PendingEvent]) -> tuple[str | None, bool]:
    original_sender: str | None = None
    raw_audio_fallback = False
    for pending_event in pending_events:
        if not _pending_event_trusts_internal_payload(pending_event):
            continue
        content = _event_content_dict(pending_event.event)
        if content is None:
            continue
        if original_sender is None:
            content_original_sender = content.get(ORIGINAL_SENDER_KEY)
            if isinstance(content_original_sender, str):
                original_sender = content_original_sender
        if content.get(VOICE_RAW_AUDIO_FALLBACK_KEY) is True:
            raw_audio_fallback = True
        if original_sender is not None and raw_audio_fallback:
            break
    return original_sender, raw_audio_fallback


_SOURCE_KIND_PRIORITY: dict[str, int] = {"voice": 0, "image": 1, "media": 2}


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
    for item in metadata:
        item.close()
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


def build_coalesced_batch(key: CoalescingKey, pending_events: list[PendingEvent]) -> CoalescedBatch:
    """Build one normalized dispatch batch from queued pending events."""
    ordered_pending_events = list(pending_events)
    primary_pending_event = ordered_pending_events[-1]
    original_sender, raw_audio_fallback = _batch_metadata(ordered_pending_events)
    return CoalescedBatch(
        room=primary_pending_event.room,
        primary_event=primary_pending_event.event,
        requester_user_id=key[2],
        pending_events=tuple(ordered_pending_events),
        prompt=coalesced_prompt(
            [dispatch_prompt_for_event(pending_event.event) for pending_event in ordered_pending_events],
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
        media_events=[
            pending_event.event
            for pending_event in ordered_pending_events
            if is_media_dispatch_event(pending_event.event)
        ],
        original_sender=original_sender,
        raw_audio_fallback=raw_audio_fallback,
        dispatch_metadata=_batch_dispatch_metadata(ordered_pending_events),
    )
