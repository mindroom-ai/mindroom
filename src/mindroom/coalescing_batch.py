"""Coalesced dispatch batch construction."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, NamedTuple

from .attachment_ids import merge_attachment_ids
from .dispatch_handoff import (
    DispatchIngressMetadata,
    DispatchPayloadMetadata,
    MediaDispatchEvent,
    PendingDispatchMetadata,
    PreparedIngress,
    dispatch_prompt_for_event,
    event_content_dict,
    payload_metadata_from_source,
)
from .dispatch_source import (
    ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    MESSAGE_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    source_kind_from_content,
)
from .handled_turns import SourceEventMetadata, TurnRecord
from .prompt_message_tags import render_msg_tag
from .timestamp_formatting import normalize_timestamp_ms

if TYPE_CHECKING:
    import nio


@dataclass(frozen=True)
class RequesterCoalescingOwner:
    """Batching owner for one effective requester's own burst."""

    requester_user_id: str


@dataclass(frozen=True)
class ActiveFollowUpCoalescingOwner:
    """Batching owner for follow-ups queued behind an active response."""


type CoalescingOwner = RequesterCoalescingOwner | ActiveFollowUpCoalescingOwner


class CoalescingKey(NamedTuple):
    """Batching scope for one owner in one room or thread."""

    room_id: str
    thread_id: str | None
    owner: CoalescingOwner


type TimestampFormatter = Callable[[float | None], str | None]


def requester_coalescing_key(room_id: str, thread_id: str | None, requester_user_id: str) -> CoalescingKey:
    """Return the canonical coalescing key for one effective requester's own burst."""
    return CoalescingKey(room_id, thread_id, RequesterCoalescingOwner(requester_user_id))


def active_follow_up_coalescing_key(room_id: str, thread_id: str | None) -> CoalescingKey:
    """Return the target-scoped key for follow-ups queued behind an active response."""
    return CoalescingKey(room_id, thread_id, ActiveFollowUpCoalescingOwner())


def is_active_follow_up_coalescing_key(key: CoalescingKey) -> bool:
    """Return whether a coalescing key is target-scoped for an active response."""
    return isinstance(key.owner, ActiveFollowUpCoalescingOwner)


def coalescing_owner_log_label(owner: CoalescingOwner) -> str:
    """Return the stable log and task-name label for one coalescing owner."""
    if isinstance(owner, RequesterCoalescingOwner):
        return owner.requester_user_id
    return "active_follow_up"


@dataclass
class PendingEvent:
    """One queued prepared ingress plus its queue-local lifecycle state.

    All per-source evidence lives on the frozen ``PreparedIngress``; this
    wrapper only carries state owned by the queue itself (enqueue timing and
    opaque dispatch metadata).
    """

    event: PreparedIngress
    room: nio.MatrixRoom
    enqueue_time: float = field(default_factory=time.time)
    dispatch_metadata: tuple[PendingDispatchMetadata, ...] = ()


@dataclass(frozen=True)
class PreparedTurn:
    """One logical turn produced by a coalescing-gate flush."""

    room: nio.MatrixRoom
    event: PreparedIngress
    requester_user_id: str
    handled_turn: TurnRecord
    ingress: DispatchIngressMetadata
    payload: DispatchPayloadMetadata
    current_prompt_is_structured: bool
    media_events: tuple[MediaDispatchEvent, ...]
    dispatch_metadata: tuple[PendingDispatchMetadata, ...]


_COALESCED_MESSAGES_INTRO = (
    "The user sent the following messages in quick succession. Treat them as one turn and respond once:"
)
_QUEUED_MESSAGES_INTRO = (
    "Messages arrived while the previous response was still running. "
    "They are in chat timeline order. Respond once to the combined context:"
)


def _messages_envelope(*, intro: str, tag: str, rendered_messages: str) -> str:
    """Wrap rendered <msg> tags in one labeled container under a shared preamble."""
    return f"{intro}\n\n<{tag}>\n{rendered_messages}\n</{tag}>"


def coalesced_prompt(message_bodies: list[str]) -> str:
    """Return the single prompt text used to dispatch one coalesced turn."""
    if len(message_bodies) == 1:
        return message_bodies[0]
    combined_body = "\n".join(message_bodies)
    return f"{_COALESCED_MESSAGES_INTRO}\n\n{combined_body}"


def _format_event_timestamp(
    raw_timestamp_ms: object,
    timestamp_formatter: TimestampFormatter | None,
) -> str | None:
    """Render one raw event timestamp via the formatter, or None when unavailable."""
    if timestamp_formatter is None:
        return None
    return timestamp_formatter(normalize_timestamp_ms(raw_timestamp_ms))


def _tagged_pending_message(
    pending_event: PendingEvent,
    *,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    return render_msg_tag(
        sender=pending_event.event.requester_user_id or pending_event.event.sender,
        body=dispatch_prompt_for_event(pending_event.event),
        event_id=pending_event.event.event_id,
        ts=_format_event_timestamp(pending_event.event.server_timestamp, timestamp_formatter),
    )


def _rendered_pending_messages(
    pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    return "\n".join(
        _tagged_pending_message(pending_event, timestamp_formatter=timestamp_formatter)
        for pending_event in pending_events
    )


def _active_follow_up_prompt(
    pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    return _messages_envelope(
        intro=_QUEUED_MESSAGES_INTRO,
        tag="queued_messages",
        rendered_messages=_rendered_pending_messages(pending_events, timestamp_formatter=timestamp_formatter),
    )


def _tagged_coalesced_prompt(
    ordered_pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter,
) -> str:
    return _messages_envelope(
        intro=_COALESCED_MESSAGES_INTRO,
        tag="messages",
        rendered_messages=_rendered_pending_messages(ordered_pending_events, timestamp_formatter=timestamp_formatter),
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
            render_msg_tag(
                sender=metadata.sender,
                body=prompt,
                event_id=source_event_id,
                ts=timestamp_formatter(metadata.timestamp_ms),
            ),
        )
    return _messages_envelope(
        intro=_COALESCED_MESSAGES_INTRO,
        tag="messages",
        rendered_messages="\n".join(rendered_messages),
    )


@dataclass(frozen=True)
class _CoalescedPromptRendering:
    """One coalesced turn's model prompt and whether it carries trusted message tags."""

    prompt: str
    is_structured: bool


def _render_coalesced_prompt(
    ordered_pending_events: list[PendingEvent],
    *,
    dispatch_policy_source_kind: str | None,
    timestamp_formatter: TimestampFormatter | None,
) -> _CoalescedPromptRendering:
    """Render the coalesced prompt and its structured-ness from one branch decision."""
    if len(ordered_pending_events) > 1:
        if dispatch_policy_source_kind == ACTIVE_THREAD_FOLLOW_UP_SOURCE_KIND:
            return _CoalescedPromptRendering(
                _active_follow_up_prompt(ordered_pending_events, timestamp_formatter=timestamp_formatter),
                is_structured=True,
            )
        if timestamp_formatter is not None:
            return _CoalescedPromptRendering(
                _tagged_coalesced_prompt(ordered_pending_events, timestamp_formatter=timestamp_formatter),
                is_structured=True,
            )
    return _CoalescedPromptRendering(
        coalesced_prompt([dispatch_prompt_for_event(pending_event.event) for pending_event in ordered_pending_events]),
        is_structured=False,
    )


def _batch_payload_metadata(pending_events: list[PendingEvent]) -> DispatchPayloadMetadata:
    """Aggregate canonical per-event payload metadata for one prepared turn."""
    event_metadata = [
        payload_metadata_from_source(
            pending_event.event.source,
            trust_internal_metadata=pending_event.event.trust_internal_payload_metadata,
        )
        for pending_event in pending_events
    ]
    inspected_content = any(metadata.mentioned_user_ids is not None for metadata in event_metadata)
    return DispatchPayloadMetadata(
        attachment_ids=tuple(
            merge_attachment_ids(*(list(metadata.attachment_ids or ()) for metadata in event_metadata)),
        ),
        original_sender=next(
            (metadata.original_sender for metadata in event_metadata if metadata.original_sender is not None),
            None,
        ),
        raw_audio_fallback=any(metadata.raw_audio_fallback is True for metadata in event_metadata),
        voice_transcript=any(metadata.voice_transcript is True for metadata in event_metadata),
        mentioned_user_ids=(
            tuple(
                dict.fromkeys(user_id for metadata in event_metadata for user_id in metadata.mentioned_user_ids or ()),
            )
            if inspected_content
            else None
        ),
        formatted_bodies=(
            tuple(body for metadata in event_metadata for body in metadata.formatted_bodies or ())
            if inspected_content
            else None
        ),
        skip_mentions=(
            any(metadata.skip_mentions is True for metadata in event_metadata) if inspected_content else None
        ),
    )


_SOURCE_KIND_PRIORITY: dict[str, int] = {
    VOICE_SOURCE_KIND: 0,
    IMAGE_SOURCE_KIND: 1,
    MEDIA_SOURCE_KIND: 2,
}


def _pending_event_source_kind(pending_event: PendingEvent) -> str:
    """Resolve one pending event's ingress source kind from its stamped evidence.

    Gate-admitted events always carry ``source_kind``; the fallbacks mirror the
    content-based resolution used for unstamped prepared events.
    """
    event = pending_event.event
    if event.source_kind is not None:
        return event.source_kind
    if event.source_kind_override is not None:
        return event.source_kind_override
    content = event_content_dict(event)
    if content is not None and (source_kind := source_kind_from_content(content)) is not None:
        return source_kind
    return MESSAGE_SOURCE_KIND


def _batch_source_kind(ordered_pending_events: list[PendingEvent]) -> str:
    resolved_source_kinds = [_pending_event_source_kind(pending_event) for pending_event in ordered_pending_events]
    return min(resolved_source_kinds, key=lambda sk: _SOURCE_KIND_PRIORITY.get(sk, 999))


def _batch_dispatch_policy_source_kind(ordered_pending_events: list[PendingEvent]) -> str | None:
    resolved_policy_kinds = {
        pending_event.event.dispatch_policy_source_kind
        for pending_event in ordered_pending_events
        if pending_event.event.dispatch_policy_source_kind is not None
    }
    if not resolved_policy_kinds:
        return None
    if len(resolved_policy_kinds) == 1:
        return next(iter(resolved_policy_kinds))
    msg = "Coalesced batch carried multiple dispatch policy source kinds"
    raise ValueError(msg)


def _batch_requester_user_id(key: CoalescingKey, primary_pending_event: PendingEvent) -> str:
    """Resolve the batch requester from the primary event, falling back to a requester owner.

    A follow-up owner carries no requester, so a requester-less primary event
    falls back to the event sender, matching the per-message sender fallback.
    """
    if primary_pending_event.event.requester_user_id:
        return primary_pending_event.event.requester_user_id
    if isinstance(key.owner, RequesterCoalescingOwner):
        return key.owner.requester_user_id
    return primary_pending_event.event.sender


def _batch_hook_source(ordered_pending_events: list[PendingEvent]) -> str | None:
    hook_sources = {
        pending_event.event.hook_source
        for pending_event in ordered_pending_events
        if pending_event.event.hook_source is not None
    }
    if not hook_sources:
        return None
    if len(hook_sources) == 1:
        return next(iter(hook_sources))
    msg = "Coalesced batch carried multiple hook sources"
    raise ValueError(msg)


def _batch_message_received_depth(ordered_pending_events: list[PendingEvent]) -> int:
    return max((pending_event.event.message_received_depth for pending_event in ordered_pending_events), default=0)


def _batch_dispatch_metadata(
    ordered_pending_events: list[PendingEvent],
) -> tuple[PendingDispatchMetadata, ...]:
    return tuple(item for pending_event in ordered_pending_events for item in pending_event.dispatch_metadata)


def close_pending_event_metadata(pending_events: list[PendingEvent]) -> None:
    """Close opaque metadata owned by pending events that cannot dispatch."""
    for pending_event in pending_events:
        for item in pending_event.dispatch_metadata:
            item.close_once()


def _batch_source_event_prompts(ordered_pending_events: list[PendingEvent]) -> dict[str, str]:
    return {
        pending_event.event.event_id: dispatch_prompt_for_event(pending_event.event)
        for pending_event in ordered_pending_events
    }


def _batch_source_event_metadata(ordered_pending_events: list[PendingEvent]) -> dict[str, SourceEventMetadata]:
    return {
        pending_event.event.event_id: SourceEventMetadata(
            sender=pending_event.event.requester_user_id or pending_event.event.sender,
            timestamp_ms=normalize_timestamp_ms(pending_event.event.server_timestamp),
            discovery_event_id=pending_event.event.discovery_event_id,
        )
        for pending_event in ordered_pending_events
    }


def build_prepared_turn(
    key: CoalescingKey,
    pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter | None = None,
) -> PreparedTurn:
    """Build the logical turn represented by one coalescing-gate flush."""
    ordered_pending_events = list(pending_events)
    primary_pending_event = ordered_pending_events[-1]
    dispatch_policy_source_kind = _batch_dispatch_policy_source_kind(ordered_pending_events)
    prompt_rendering = _render_coalesced_prompt(
        ordered_pending_events,
        dispatch_policy_source_kind=dispatch_policy_source_kind,
        timestamp_formatter=timestamp_formatter,
    )
    source_event_ids = tuple(pending_event.event.event_id for pending_event in ordered_pending_events)
    source_event_prompts = _batch_source_event_prompts(ordered_pending_events)
    source_event_metadata = _batch_source_event_metadata(ordered_pending_events)
    routed_aliases = tuple(filter(None, (item.discovery_event_id for item in source_event_metadata.values())))
    requester_user_id = _batch_requester_user_id(key, primary_pending_event)
    return PreparedTurn(
        room=primary_pending_event.room,
        event=replace(primary_pending_event.event, body=prompt_rendering.prompt),
        requester_user_id=requester_user_id,
        handled_turn=TurnRecord.create(
            source_event_ids,
            discovery_event_ids=routed_aliases,
            source_event_prompts=source_event_prompts,
            source_event_metadata=source_event_metadata if len(source_event_ids) > 1 or routed_aliases else None,
            requester_id=requester_user_id,
        ),
        ingress=DispatchIngressMetadata(
            source_kind=_batch_source_kind(ordered_pending_events),
            coalescing_key=key,
            dispatch_policy_source_kind=dispatch_policy_source_kind,
            hook_source=_batch_hook_source(ordered_pending_events),
            message_received_depth=_batch_message_received_depth(ordered_pending_events),
        ),
        payload=_batch_payload_metadata(ordered_pending_events),
        current_prompt_is_structured=prompt_rendering.is_structured,
        media_events=tuple(
            pending_event.event.raw_event
            for pending_event in ordered_pending_events
            if pending_event.event.raw_event is not None
        ),
        dispatch_metadata=_batch_dispatch_metadata(ordered_pending_events),
    )
