"""Accepted-request accounting for public gateway onboarding."""

from __future__ import annotations

from collections import deque
from time import monotonic
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable


class OnboardingRateLimiter:
    """Enforce aggregate and per-source accepted-request limits."""

    def __init__(
        self,
        aggregate_limit: int,
        source_limit: int,
        *,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if aggregate_limit < 1 or source_limit < 1:
            msg = "MCP onboarding rate limits must be positive"
            raise ValueError(msg)
        self._aggregate_limit = aggregate_limit
        self._source_limit = source_limit
        self._clock = monotonic if clock is None else clock
        self._requests: deque[tuple[float, str]] = deque()
        self._source_counts: dict[str, int] = {}

    def allow(self, source: str) -> bool:
        """Admit one source when both accepted-request windows have capacity."""
        now = self._clock()
        while self._requests and self._requests[0][0] <= now - 60:
            _, expired_source = self._requests.popleft()
            remaining = self._source_counts[expired_source] - 1
            if remaining:
                self._source_counts[expired_source] = remaining
            else:
                del self._source_counts[expired_source]
        if len(self._requests) >= self._aggregate_limit or self._source_counts.get(source, 0) >= self._source_limit:
            return False
        self._requests.append((now, source))
        self._source_counts[source] = self._source_counts.get(source, 0) + 1
        return True
