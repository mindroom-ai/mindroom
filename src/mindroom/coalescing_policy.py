"""Pure classification rules for live message coalescing."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

import nio

from .dispatch_handoff import DispatchEvent, PreparedTextEvent, is_media_dispatch_event
from .dispatch_source import (
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    source_kind_bypasses_coalescing,
)

if TYPE_CHECKING:
    from .coalescing_batch import PendingEvent


class QueueKind(enum.Enum):
    """Dispatch behavior for one queued event."""

    NORMAL = "normal"
    BYPASS = "bypass"


_ROOM_SCOPE_BATCHING_SOURCE_KINDS: frozenset[str] = frozenset(
    {VOICE_SOURCE_KIND, IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND},
)


def _effective_source_kind(
    event: DispatchEvent,
    fallback_source_kind: str | None = None,
) -> str | None:
    if fallback_source_kind is not None:
        return fallback_source_kind
    if isinstance(event, PreparedTextEvent) and event.source_kind_override is not None:
        return event.source_kind_override
    return None


def is_coalescing_exempt_source_kind(
    event: DispatchEvent,
    fallback_source_kind: str | None = None,
) -> bool:
    """Return True when coalescing should be skipped for this event."""
    return source_kind_bypasses_coalescing(_effective_source_kind(event, fallback_source_kind))


def pending_event_is_text(pending_event: PendingEvent) -> bool:
    """Return whether one pending event is a complete text-like utterance.

    Text (typed messages, voice transcripts, edits) terminates an utterance burst:
    clients upload attachments first and send the caption text last, so a batch
    ending in text is complete and a batch ending in media may still grow.
    """
    return isinstance(pending_event.event, nio.RoomMessageText | PreparedTextEvent)


def _pending_event_requires_solo_batch(pending_event: PendingEvent) -> bool:
    """Return whether a pending event must dispatch without neighbors."""
    return any(item.requires_solo_batch for item in pending_event.dispatch_metadata)


def source_or_event_allows_room_scope_batching(
    source_kind: str,
    event: DispatchEvent | None = None,
) -> bool:
    """Return whether a source kind or resolved event can batch at room scope."""
    return source_kind in _ROOM_SCOPE_BATCHING_SOURCE_KINDS or (event is not None and is_media_dispatch_event(event))


def queue_kind(pending_event: PendingEvent) -> QueueKind:
    """Return the dispatch behavior for one resolved pending event."""
    if _pending_event_requires_solo_batch(pending_event):
        return QueueKind.BYPASS
    if is_coalescing_exempt_source_kind(pending_event.event, pending_event.source_kind):
        return QueueKind.BYPASS
    return QueueKind.NORMAL
