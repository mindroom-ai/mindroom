"""Named thread-read modes and the one property callers branch on.

What survives here is the caller's *contract*, not a cache policy: whether a
read is on the live dispatch path and so must fail open rather than block. The
coordinator wait, the shared dispatch timeout, and the single-flight refill
this module used to describe all belonged to the cache read path that the
visible-message projection replaced, and they went with it.
"""

from __future__ import annotations

from enum import Enum, auto


class ThreadReadMode(Enum):
    """Named thread-read policies for cache coordination and source freshness."""

    ADVISORY_FULL = auto()
    DISPATCH_SNAPSHOT = auto()
    DISPATCH_FULL = auto()
    STRICT_FULL = auto()

    @property
    def dispatch_safe(self) -> bool:
        """Return whether this mode is on the live dispatch fail-open path."""
        # STRICT_FULL intentionally stays false: it may block for authoritative post-lock model context.
        return self in {
            ThreadReadMode.DISPATCH_SNAPSHOT,
            ThreadReadMode.DISPATCH_FULL,
        }
