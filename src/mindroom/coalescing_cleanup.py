"""Cleanup ownership helpers for coalesced dispatch work."""

from __future__ import annotations

from dataclasses import dataclass

from .coalescing_batch import PendingEvent, close_pending_event_metadata

__all__ = [
    "ClaimedSegmentOwner",
    "ReadyPendingEvent",
    "close_pending_event_metadata_once",
    "close_ready_task_result_metadata",
]


@dataclass(frozen=True)
class ReadyPendingEvent:
    """Resolved event returned by async ingress normalization."""

    pending_event: PendingEvent


def close_pending_event_metadata_once(pending_events: list[PendingEvent]) -> None:
    """Close pending-event metadata and clear it so later cleanup is idempotent."""
    close_pending_event_metadata(pending_events)
    for pending_event in pending_events:
        pending_event.dispatch_metadata = ()


def close_ready_task_result_metadata(result: object) -> int:
    """Close dispatch metadata for a ready-task result and report dropped ready work."""
    if isinstance(result, ReadyPendingEvent):
        close_pending_event_metadata_once([result.pending_event])
        return 1
    return 0


@dataclass
class ClaimedSegmentOwner:
    """Own metadata closure for one resolved dispatch segment."""

    pending_events: list[PendingEvent]
    metadata_closed: bool = False

    def event_ids(self) -> set[str]:
        """Return source event IDs owned by this segment."""
        return {pending_event.event.event_id for pending_event in self.pending_events}

    def close_metadata_once(self) -> None:
        """Close metadata for this segment if it has not already been closed."""
        if self.metadata_closed:
            return
        close_pending_event_metadata_once(self.pending_events)
        self.metadata_closed = True
