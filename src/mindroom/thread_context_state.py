"""Explicit trust states for Matrix thread context carried through dispatch."""

from __future__ import annotations

from enum import Enum, auto


class ThreadReadMode(Enum):
    """Named thread-read policies for cache coordination and source freshness."""

    ADVISORY_SNAPSHOT = auto()
    ADVISORY_FULL = auto()
    DISPATCH_SNAPSHOT = auto()
    DISPATCH_FULL = auto()
    STRICT_FULL = auto()

    @property
    def full_history(self) -> bool:
        """Return whether this mode requires fully hydrated thread history."""
        return self in {
            ThreadReadMode.ADVISORY_FULL,
            ThreadReadMode.DISPATCH_FULL,
            ThreadReadMode.STRICT_FULL,
        }

    @property
    def dispatch_safe(self) -> bool:
        """Return whether this mode is on the live dispatch fail-open path.

        STRICT_FULL intentionally stays outside this set: it is used after response selection
        when full prompt-building context is required.
        """
        return self in {
            ThreadReadMode.DISPATCH_SNAPSHOT,
            ThreadReadMode.DISPATCH_FULL,
        }


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
