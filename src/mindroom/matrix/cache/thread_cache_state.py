"""Backend-neutral durable thread-cache state values and decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .event_cache import ThreadCacheState

if TYPE_CHECKING:
    from collections.abc import Sequence

_INCREMENTAL_THREAD_REVALIDATION_REASONS = (
    "live_thread_mutation",
    "sync_thread_mutation",
    "outbound_thread_mutation",
)


def incremental_thread_revalidation_reasons() -> tuple[str, ...]:
    """Return the invalidation reasons that one successful incremental append may clear."""
    return _INCREMENTAL_THREAD_REVALIDATION_REASONS


def is_incremental_thread_revalidation_reason(reason: str | None) -> bool:
    """Return whether one invalidation reason may be cleared after an incremental append."""
    return reason in _INCREMENTAL_THREAD_REVALIDATION_REASONS


@dataclass(frozen=True, slots=True)
class ThreadCacheStateRow:
    """Backend-neutral values loaded from thread and room cache-state rows."""

    validated_at: float | None
    invalidated_at: float | None
    invalidation_reason: str | None
    room_invalidated_at: float | None
    room_invalidation_reason: str | None

    def as_public_state(self) -> ThreadCacheState:
        """Return the public cache-state value."""
        return ThreadCacheState(
            validated_at=self.validated_at,
            invalidated_at=self.invalidated_at,
            invalidation_reason=self.invalidation_reason,
            room_invalidated_at=self.room_invalidated_at,
            room_invalidation_reason=self.room_invalidation_reason,
        )


def thread_cache_state_row(values: Sequence[float | str | None] | None) -> ThreadCacheStateRow | None:
    """Normalize one backend storage row into backend-neutral cache-state values."""
    if values is None:
        return None
    if len(values) != 5:
        msg = f"Thread cache-state row must contain exactly 5 values, got {len(values)}"
        raise ValueError(msg)
    if all(value is None for value in values):
        return None
    return ThreadCacheStateRow(
        validated_at=None if values[0] is None else float(values[0]),
        invalidated_at=None if values[1] is None else float(values[1]),
        invalidation_reason=values[2] if isinstance(values[2], str) else None,
        room_invalidated_at=None if values[3] is None else float(values[3]),
        room_invalidation_reason=values[4] if isinstance(values[4], str) else None,
    )


def thread_cache_state_changed_after(
    cache_state: ThreadCacheStateRow | None,
    *,
    fetch_started_at: float,
) -> bool:
    """Return whether thread or room cache state changed after one fetch began."""
    if cache_state is None:
        return False
    return any(
        timestamp is not None and timestamp > fetch_started_at
        for timestamp in (cache_state.validated_at, cache_state.invalidated_at, cache_state.room_invalidated_at)
    )


def can_revalidate_after_incremental_update(cache_state: ThreadCacheStateRow | None) -> bool:
    """Return whether an incremental update may clear one thread invalidation."""
    if cache_state is None:
        return False
    return (
        cache_state.validated_at is not None
        and cache_state.invalidated_at is not None
        and is_incremental_thread_revalidation_reason(cache_state.invalidation_reason)
        and not (
            cache_state.room_invalidated_at is not None and cache_state.room_invalidated_at >= cache_state.validated_at
        )
    )


def replacement_validated_at(*, fetch_started_at: float, validated_at: float | None) -> float:
    """Clamp replacement validation to the instant its fetch began."""
    return fetch_started_at if validated_at is None else min(validated_at, fetch_started_at)
