"""Pure classification rules for live message coalescing."""

from __future__ import annotations

import enum
from typing import TYPE_CHECKING

import nio

from .commands.parsing import command_parser
from .dispatch_handoff import DispatchEvent, PreparedTextEvent, is_media_dispatch_event
from .dispatch_source import (
    HOOK_DISPATCH_SOURCE_KIND,
    HOOK_SOURCE_KIND,
    IMAGE_SOURCE_KIND,
    MEDIA_SOURCE_KIND,
    SCHEDULED_SOURCE_KIND,
    TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    VOICE_SOURCE_KIND,
    is_voice_event,
)

if TYPE_CHECKING:
    from .coalescing_batch import PendingEvent


class QueueKind(enum.Enum):
    """Dispatch behavior for one queued event."""

    NORMAL = "normal"
    COMMAND = "command"
    BYPASS = "bypass"


_COALESCING_EXEMPT_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        HOOK_SOURCE_KIND,
        HOOK_DISPATCH_SOURCE_KIND,
        SCHEDULED_SOURCE_KIND,
        TRUSTED_INTERNAL_RELAY_SOURCE_KIND,
    },
)
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
    return _effective_source_kind(event, fallback_source_kind) in _COALESCING_EXEMPT_SOURCE_KINDS


def _is_command_event(
    event: DispatchEvent,
    *,
    fallback_source_kind: str | None = None,
) -> bool:
    """Return whether a dispatch event should bypass coalescing as a command."""
    if not isinstance(event, nio.RoomMessageText | PreparedTextEvent):
        return False
    if fallback_source_kind == VOICE_SOURCE_KIND or is_voice_event(event):
        return False
    if _effective_source_kind(event, fallback_source_kind) in {IMAGE_SOURCE_KIND, MEDIA_SOURCE_KIND}:
        return False
    return command_parser.parse(event.body) is not None


def pending_has_only_text(pending_events: list[PendingEvent]) -> bool:
    """Return whether every pending event is text-like."""
    return bool(pending_events) and all(
        isinstance(pending_event.event, nio.RoomMessageText | PreparedTextEvent) for pending_event in pending_events
    )


def pending_has_room_scope_source(pending_events: list[PendingEvent]) -> bool:
    """Return whether any pending event enables room-scope batching."""
    return any(_pending_event_allows_room_scope_batching(pending_event) for pending_event in pending_events)


def pending_event_requires_solo_batch(pending_event: PendingEvent) -> bool:
    """Return whether a pending event must dispatch without neighbors."""
    return any(item.requires_solo_batch for item in pending_event.dispatch_metadata)


def pending_events_require_solo_batch(pending_events: list[PendingEvent]) -> bool:
    """Return whether any pending event in a group requires solo dispatch."""
    return any(pending_event_requires_solo_batch(pending_event) for pending_event in pending_events)


def _pending_event_allows_room_scope_batching(pending_event: PendingEvent) -> bool:
    return pending_event.source_kind in _ROOM_SCOPE_BATCHING_SOURCE_KINDS or is_voice_event(pending_event.event)


def source_or_event_allows_room_scope_batching(
    source_kind: str,
    event: DispatchEvent | None = None,
) -> bool:
    """Return whether a source kind or resolved event can batch at room scope."""
    return source_kind in _ROOM_SCOPE_BATCHING_SOURCE_KINDS or (event is not None and is_media_dispatch_event(event))


def queue_kind(pending_event: PendingEvent) -> QueueKind:
    """Return the dispatch behavior for one resolved pending event."""
    if pending_event_requires_solo_batch(pending_event):
        return QueueKind.BYPASS
    if is_coalescing_exempt_source_kind(pending_event.event, pending_event.source_kind):
        return QueueKind.BYPASS
    if _is_command_event(pending_event.event, fallback_source_kind=pending_event.source_kind):
        return QueueKind.COMMAND
    return QueueKind.NORMAL
