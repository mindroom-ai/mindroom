"""Bounded retry for embedding work during knowledge indexing.

One transient embedding failure used to abort an entire refresh, so a corpus
large enough to hit any transient fault could never finish. Retrying here keeps
those faults local to the file or batch that hit them, while permanent failures
(bad credentials, wrong model, dimension mismatch) fail immediately instead of
burning the budget on a request that cannot succeed.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeVar

from mindroom.embedding_errors import (
    describe_embedder_error,
    embedder_failure_is_transient,
    embedder_retry_after_seconds,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class EmbeddingRetryPolicy:
    """Bounded exponential backoff with jitter for transient embedding faults."""

    max_attempts: int = 5
    initial_backoff_seconds: float = 0.5
    max_backoff_seconds: float = 30.0
    jitter_ratio: float = 0.25

    def backoff_seconds(self, attempt: int, *, retry_after_seconds: float | None, jitter_unit: float) -> float:
        """Return how long to wait before ``attempt`` (1-based) is retried.

        A provider ``Retry-After`` hint wins over the computed backoff, but is
        still clamped so a hostile or mistaken header cannot stall a refresh.
        """
        exponential = self.initial_backoff_seconds * (2 ** max(attempt - 1, 0))
        base = retry_after_seconds if retry_after_seconds is not None else exponential
        clamped = min(max(base, 0.0), self.max_backoff_seconds)
        # Full-width jitter around the base delay keeps many workers from
        # retrying against a recovering endpoint in lockstep.
        jitter = clamped * self.jitter_ratio * (2.0 * jitter_unit - 1.0)
        return max(clamped + jitter, 0.0)


@dataclass(frozen=True, slots=True)
class _EmbeddingRetryAttempt:
    """One exhausted attempt, reported to callers for progress accounting."""

    attempt: int
    delay_seconds: float
    detail: str


async def run_with_embedding_retry(
    operation: Callable[[], Awaitable[_T]],
    *,
    policy: EmbeddingRetryPolicy,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    jitter: Callable[[], float] = random.random,
    on_retry: Callable[[_EmbeddingRetryAttempt], None] | None = None,
) -> _T:
    """Run ``operation``, retrying only transient embedding failures."""
    last_error: BaseException | None = None
    for attempt in range(1, max(policy.max_attempts, 1) + 1):
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= policy.max_attempts or not embedder_failure_is_transient(exc):
                raise
            delay_seconds = policy.backoff_seconds(
                attempt,
                retry_after_seconds=embedder_retry_after_seconds(exc),
                jitter_unit=jitter(),
            )
            if on_retry is not None:
                on_retry(
                    _EmbeddingRetryAttempt(
                        attempt=attempt,
                        delay_seconds=delay_seconds,
                        detail=describe_embedder_error(exc),
                    ),
                )
            await sleep(delay_seconds)
    # Unreachable: the loop either returns or raises, but keeps type checkers
    # honest about the non-returning tail.
    raise last_error if last_error is not None else RuntimeError("embedding retry loop exited without a result")
