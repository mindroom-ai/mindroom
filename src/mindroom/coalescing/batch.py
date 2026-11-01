"""Coalesced dispatch batch construction."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, NamedTuple

from mindroom.attachments.ids import merge_attachment_ids
from mindroom.dispatch_handoff import (
    DispatchPayloadMetadata,
    MediaDispatchEvent,
    PreparedIngress,
    dispatch_prompt_for_event,
    payload_metadata_from_source,
)
from mindroom.dispatch_source import (
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    VOICE_SOURCE_KIND,
)
from mindroom.matrix.member_display_names import room_member_display_names
from mindroom.prompt_message_tags import render_msg_tag
from mindroom.timestamp_formatting import normalize_timestamp_ms
from mindroom.turn_record import SourceEventMetadata, TurnRecord
from mindroom.turns.facts import command_for

if TYPE_CHECKING:
    import nio

    from mindroom.commands.parsing import Command
    from mindroom.response_lifecycle import QueuedHumanNoticeReservation
    from mindroom.turn_store import TurnClaim
    from mindroom.turns.facts import IngressFacts, RunIdentity
    from mindroom.turns.interactive_selection import InteractiveSelectionDispatch


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
    """One queued prepared ingress, its admission-time provenance, and what the queue owns for it until it dispatches.

    Besides the event and its facts, the wrapper carries the queue's own state:
    enqueue timing, the turn claim and queued notice ingress moved into it, and
    an interactive selection's work.
    """

    event: PreparedIngress
    room: nio.MatrixRoom
    facts: IngressFacts
    enqueue_time: float = field(default_factory=time.time)
    text_debounce_seconds: float = 0.0
    claim: TurnClaim | None = None
    notice: QueuedHumanNoticeReservation | None = None
    selection: InteractiveSelectionDispatch | None = None

    def close(self) -> None:
        """Release this event's claim and queued notice when it will not dispatch; idempotent."""
        if self.claim is not None:
            self.claim.release()
        if self.notice is not None:
            self.notice.release()


@dataclass(frozen=True)
class PreparedTurn:
    """One logical turn produced by a coalescing-gate flush."""

    room: nio.MatrixRoom
    event: PreparedIngress
    # The batch's provenance: its latest event's sender and requester evidence with the batch source kind.
    facts: IngressFacts
    # The command the turn's text runs as; a turn of several sources has none.
    command: Command | None
    handled_turn: TurnRecord
    # The gate's batching scope; a key without a thread is a room-level conversation.
    coalescing_key: CoalescingKey
    # Whether any of the batch's events queued behind its conversation's running response.
    active_follow_up: bool
    payload: DispatchPayloadMetadata
    current_prompt_is_structured: bool
    media_events: tuple[MediaDispatchEvent, ...]
    # The batch's per-event claims and queued notices, which dispatch replaces and releases.
    claims: tuple[TurnClaim, ...]
    notices: tuple[QueuedHumanNoticeReservation, ...]
    selection: InteractiveSelectionDispatch | None
    # The queued notices of follow-ups still waiting behind this batch when it flushed, which dispatch
    # releases with its own. Only a batch of an active response's follow-ups has any.
    queued_follow_up_notices: tuple[QueuedHumanNoticeReservation, ...]
    # Whether this turn's queue still holds a run other than the given requester's own messages for this
    # agent. A live read, not a snapshot: dispatch asks after awaiting its context, and a message admitted
    # meanwhile counts.
    queues_other_run: Callable[[str], bool]


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
    member_display_names: Mapping[str, str],
) -> str:
    event = pending_event.event
    facts = pending_event.facts
    # Whom the source speaks as: its author when an entity wrote it for the requester, else the requester.
    sender = facts.physical_sender if facts.acts_for_requester else facts.run_requester_id
    return render_msg_tag(
        sender=sender,
        body=dispatch_prompt_for_event(event),
        event_id=event.event_id,
        ts=_format_event_timestamp(event.server_timestamp, timestamp_formatter),
        display_name=member_display_names.get(sender),
    )


def _rendered_pending_messages(
    pending_events: list[PendingEvent],
    *,
    timestamp_formatter: TimestampFormatter | None,
) -> str:
    # Every event in one batch shares the coalescing key's room.
    member_display_names = room_member_display_names(pending_events[-1].room)
    return "\n".join(
        _tagged_pending_message(
            pending_event,
            timestamp_formatter=timestamp_formatter,
            member_display_names=member_display_names,
        )
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
    member_display_names: Mapping[str, str],
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
                display_name=member_display_names.get(metadata.sender),
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
    active_follow_up: bool,
    timestamp_formatter: TimestampFormatter | None,
) -> _CoalescedPromptRendering:
    """Render the coalesced prompt and its structured-ness from one branch decision."""
    if len(ordered_pending_events) > 1:
        if active_follow_up:
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
    """Aggregate canonical per-event payload metadata for one prepared turn.

    Mentions come only from events that do not ask receivers to ignore them, so a
    file sent with that flag cannot erase the mention in text batched with it.
    """
    event_metadata = [
        payload_metadata_from_source(
            pending_event.event.source,
            trust_internal_metadata=pending_event.event.payload_trusted or pending_event.facts.sender_is_managed,
        )
        for pending_event in pending_events
    ]
    mention_metadata = [metadata for metadata in event_metadata if metadata.skip_mentions is not True]
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
                dict.fromkeys(
                    user_id for metadata in mention_metadata for user_id in metadata.mentioned_user_ids or ()
                ),
            )
            if inspected_content
            else None
        ),
        formatted_bodies=(
            tuple(body for metadata in mention_metadata for body in metadata.formatted_bodies or ())
            if inspected_content
            else None
        ),
        skip_mentions=not mention_metadata if inspected_content else None,
    )


_SOURCE_KIND_PRIORITY: dict[str, int] = {
    VOICE_SOURCE_KIND: 0,
    IMAGE_SOURCE_KIND: 1,
    MEDIA_SOURCE_KIND: 2,
}


def pending_event_run_identity(pending_event: PendingEvent) -> RunIdentity:
    """Return who one queued event runs as, plus its author when an entity wrote it for that requester.

    A batch runs as one requester and takes its origin from its latest event, so a
    reply an entity wrote for a human never shares a batch with that human's messages.
    """
    return pending_event.facts.run_identity


def pending_event_addressing(pending_event: PendingEvent) -> bool | None:
    """Return whether one queued event is for another participant; ``None`` for an upload that leaves that to its caption.

    A batch takes its mentions from its latest event, so a message for another
    participant never shares a batch with one for this agent: the agent would skip both.
    """
    event = pending_event.event
    if event.for_another_participant is None:
        return None if event.raw_event is not None else False
    return event.for_another_participant


def _batch_facts(ordered_pending_events: list[PendingEvent]) -> IngressFacts:
    """Reduce the batch's per-event facts to the facts the whole turn dispatches with.

    Sender, requester evidence, and target come from the latest event; the source
    kind is the highest-priority modality (voice, then image, then media); the
    first relayed original sender applies; the deepest hook chain counts. A batch
    runs as one requester and one hook, so a mix of either is a gate bug.
    """
    latest = ordered_pending_events[-1].facts
    if len({pending_event_run_identity(pending_event) for pending_event in ordered_pending_events}) != 1:
        msg = "Coalesced batch carried multiple requesters"
        raise ValueError(msg)
    hook_sources = {event.facts.hook_source for event in ordered_pending_events if event.facts.hook_source is not None}
    if len(hook_sources) > 1:
        msg = "Coalesced batch carried multiple hook sources"
        raise ValueError(msg)
    relayed = next((event.facts for event in ordered_pending_events if event.facts.original_sender is not None), latest)
    return replace(
        latest,
        source_kind=min(
            (event.facts.source_kind for event in ordered_pending_events),
            key=lambda source_kind: _SOURCE_KIND_PRIORITY.get(source_kind, 999),
        ),
        original_sender=relayed.original_sender,
        original_sender_is_human=relayed.original_sender_is_human,
        hook_source=next(iter(hook_sources), None),
        message_received_depth=max(event.facts.message_received_depth for event in ordered_pending_events),
    )


def _batch_source_event_prompts(ordered_pending_events: list[PendingEvent]) -> dict[str, str]:
    return {
        pending_event.event.event_id: dispatch_prompt_for_event(pending_event.event)
        for pending_event in ordered_pending_events
    }


def _batch_source_event_metadata(ordered_pending_events: list[PendingEvent]) -> dict[str, SourceEventMetadata]:
    return {
        pending_event.event.event_id: SourceEventMetadata(
            sender=pending_event.facts.run_requester_id,
            timestamp_ms=normalize_timestamp_ms(pending_event.event.server_timestamp),
            discovery_event_id=pending_event.facts.discovery_event_id,
            speaker=pending_event.facts.physical_sender if pending_event.facts.acts_for_requester else None,
        )
        for pending_event in ordered_pending_events
    }


def build_prepared_turn(
    key: CoalescingKey,
    pending_events: list[PendingEvent],
    *,
    queues_other_run: Callable[[str], bool],
    queued_follow_up_notices: tuple[QueuedHumanNoticeReservation, ...] = (),
    timestamp_formatter: TimestampFormatter | None = None,
) -> PreparedTurn:
    """Build the logical turn represented by one coalescing-gate flush."""
    ordered_pending_events = list(pending_events)
    primary_pending_event = ordered_pending_events[-1]
    active_follow_up = any(pending_event.event.active_follow_up for pending_event in ordered_pending_events)
    prompt_rendering = _render_coalesced_prompt(
        ordered_pending_events,
        active_follow_up=active_follow_up,
        timestamp_formatter=timestamp_formatter,
    )
    source_event_ids = tuple(pending_event.event.event_id for pending_event in ordered_pending_events)
    source_event_prompts = _batch_source_event_prompts(ordered_pending_events)
    source_event_metadata = _batch_source_event_metadata(ordered_pending_events)
    routed_aliases = tuple(filter(None, (item.discovery_event_id for item in source_event_metadata.values())))
    facts = _batch_facts(ordered_pending_events)
    return PreparedTurn(
        room=primary_pending_event.room,
        event=replace(primary_pending_event.event, body=prompt_rendering.prompt),
        facts=facts,
        # One source's prompt is its own ``dispatch_prompt_for_event``, the command's canonical text.
        command=command_for(facts, prompt_rendering.prompt, len(source_event_ids)),
        handled_turn=TurnRecord.create(
            source_event_ids,
            discovery_event_ids=routed_aliases,
            source_event_prompts=source_event_prompts,
            source_event_metadata=(
                source_event_metadata
                if len(source_event_ids) > 1 or routed_aliases or facts.acts_for_requester
                else None
            ),
            requester_id=facts.run_requester_id,
        ),
        coalescing_key=key,
        active_follow_up=active_follow_up,
        payload=_batch_payload_metadata(ordered_pending_events),
        current_prompt_is_structured=prompt_rendering.is_structured,
        media_events=tuple(
            pending_event.event.raw_event
            for pending_event in ordered_pending_events
            if pending_event.event.raw_event is not None
        ),
        claims=tuple(event.claim for event in ordered_pending_events if event.claim is not None),
        notices=tuple(event.notice for event in ordered_pending_events if event.notice is not None),
        selection=next((event.selection for event in ordered_pending_events if event.selection is not None), None),
        queued_follow_up_notices=queued_follow_up_notices,
        queues_other_run=queues_other_run,
    )
