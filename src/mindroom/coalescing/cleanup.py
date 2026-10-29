"""Cleanup ownership helpers for coalesced dispatch work."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .batch import PendingEvent

__all__ = [
    "ClaimedSegmentOwner",
    "ReadyPendingEvent",
    "close_pending_events",
    "close_ready_result",
]


@dataclass(frozen=True)
class ReadyPendingEvent:
    """Resolved event returned by async ingress normalization."""

    pending_event: PendingEvent


def close_pending_events(pending_events: list[PendingEvent]) -> None:
    """Close pending events that will not dispatch."""
    for pending_event in pending_events:
        pending_event.close()


def close_ready_result(result: object) -> int:
    """Close a ready-task result that will not dispatch and report dropped ready work."""
    if isinstance(result, ReadyPendingEvent):
        result.pending_event.close()
        return 1
    return 0


@dataclass
class ClaimedSegmentOwner:
    """Own closure for one resolved dispatch segment."""

    pending_events: list[PendingEvent]
    closed: bool = False

    def close_once(self) -> None:
        """Close this segment's events if it has not already been closed."""
        if self.closed:
            return
        close_pending_events(self.pending_events)
        self.closed = True
