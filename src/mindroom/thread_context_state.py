"""Explicit trust states for Matrix thread context carried through dispatch."""

from __future__ import annotations

from enum import Enum, auto


class ThreadMembershipTrust(Enum):
    """Whether a dispatch thread id is proven or only a fail-open candidate."""

    ROOM_LEVEL = auto()
    PROVEN = auto()
    PROVISIONAL = auto()


class ThreadHistoryTrust(Enum):
    """Whether thread history may drive planning and routing policy."""

    NONE = auto()
    PLANNING_USABLE = auto()
    DEGRADED = auto()
