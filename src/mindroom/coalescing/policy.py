"""Pure classification rules for live message coalescing."""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import TYPE_CHECKING, NamedTuple

from mindroom.constants import SUBMISSION_CONTENT_KEY
from mindroom.dispatch_handoff import DispatchEvent, is_media_dispatch_event
from mindroom.dispatch_source import (
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    source_kind_bypasses_coalescing,
)
from mindroom.timestamp_formatting import normalize_timestamp_ms

if TYPE_CHECKING:
    from .batch import PendingEvent


class QueueKind(enum.Enum):
    """Dispatch behavior for one queued event."""

    NORMAL = "normal"
    BYPASS = "bypass"


class SubmissionMarker(NamedTuple):
    """One event's place in the composer submission that sent it."""

    submission_id: str
    # Whether the client sends another event of this submission without the person doing anything.
    more: bool


_ROOM_SCOPE_BATCHING_SOURCE_KINDS: frozenset[str] = frozenset(
    {VOICE_SOURCE_KIND, IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND},
)


def pending_event_is_text(pending_event: PendingEvent) -> bool:
    """Return whether one pending event is a complete text-like utterance.

    Text (typed messages, voice transcripts, edits) terminates an utterance burst:
    clients upload attachments first and send the caption text last, so a batch
    ending in text is complete and a batch ending in media may still grow.
    """
    return pending_event.event.raw_event is None


def submission_marker(pending_event: PendingEvent) -> SubmissionMarker | None:
    """Return the composer submission marker one event carries, or None for an unmarked or malformed one.

    A marker is a mapping with a non-empty string ``id`` and an optional bool
    ``more``, read as False when missing; anything else is no marker.
    """
    content = pending_event.event.source.get("content")
    marker = content.get(SUBMISSION_CONTENT_KEY) if isinstance(content, Mapping) else None
    if not isinstance(marker, Mapping):
        return None
    submission_id = marker.get("id")
    more = marker.get("more", False)
    if not isinstance(submission_id, str) or not submission_id or type(more) is not bool:
        return None
    return SubmissionMarker(submission_id, more)


def pending_events_sent_together(earlier: PendingEvent, later: PendingEvent, *, window_seconds: float) -> bool:
    """Return whether two events were sent within one burst window of each other, or by one composer submission.

    Local receipt time collapses when sync catch-up or slow voice readiness
    delivers a backlog at once, so a send burst compares the homeserver's
    origin timestamps. An event without one is never proven to share a burst.
    """
    earlier_submission = submission_marker(earlier)
    later_submission = submission_marker(later)
    if (
        earlier_submission is not None
        and later_submission is not None
        and earlier_submission.submission_id == later_submission.submission_id
    ):
        return True
    earlier_ms = normalize_timestamp_ms(earlier.event.server_timestamp)
    later_ms = normalize_timestamp_ms(later.event.server_timestamp)
    return (
        earlier_ms is not None and later_ms is not None and abs(later_ms - earlier_ms) <= round(window_seconds * 1000)
    )


def source_or_event_allows_room_scope_batching(
    source_kind: str | None,
    event: DispatchEvent | None = None,
) -> bool:
    """Return whether a source kind or resolved event can batch at room scope."""
    return source_kind in _ROOM_SCOPE_BATCHING_SOURCE_KINDS or (event is not None and is_media_dispatch_event(event))


def queue_kind(pending_event: PendingEvent) -> QueueKind:
    """Return the dispatch behavior for one resolved pending event."""
    # A route is a whole turn its steward selected, whatever its kind: it shares a turn with nothing.
    if source_kind_bypasses_coalescing(pending_event.facts.source_kind) or pending_event.facts.routed_event_ids:
        return QueueKind.BYPASS
    return QueueKind.NORMAL
