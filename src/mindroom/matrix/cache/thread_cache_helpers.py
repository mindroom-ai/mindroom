"""Shared pure helpers for Matrix thread cache policies."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.cache.event_cache import ThreadCacheState
    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


THREAD_CACHE_MAX_AGE_SECONDS = 300.0


def latest_visible_thread_event_id(history: Sequence[ResolvedVisibleMessage]) -> str | None:
    """Return the latest visible event ID from one resolved thread history."""
    if not history:
        return None
    return history[-1].visible_event_id or history[-1].event_id or None


def thread_cache_rejection_reason(
    cache_state: ThreadCacheState | None,
    *,
    runtime_started_at: float | None,
    now: float | None = None,
) -> str | None:
    """Return why one durable thread snapshot must be rejected, if at all."""
    rejection_reason: str | None = None
    if cache_state is None:
        rejection_reason = "no_cache_state"
    elif cache_state.validated_at is None:
        rejection_reason = "cache_never_validated"
    elif runtime_started_at is not None and cache_state.validated_at < runtime_started_at:
        rejection_reason = "validated_before_runtime_start"
    elif cache_state.invalidated_at is not None and cache_state.invalidated_at >= cache_state.validated_at:
        rejection_reason = "thread_invalidated_after_validation"
    elif cache_state.room_invalidated_at is not None and cache_state.room_invalidated_at >= cache_state.validated_at:
        rejection_reason = "room_invalidated_after_validation"
    else:
        checked_at = time.time() if now is None else now
        if checked_at - cache_state.validated_at > THREAD_CACHE_MAX_AGE_SECONDS:
            rejection_reason = "cache_too_old"
    return rejection_reason


def thread_cache_state_is_usable(
    cache_state: ThreadCacheState | None,
    *,
    runtime_started_at: float | None,
    now: float | None = None,
) -> bool:
    """Return whether one durable thread snapshot is safe to reuse."""
    return (
        thread_cache_rejection_reason(
            cache_state,
            runtime_started_at=runtime_started_at,
            now=now,
        )
        is None
    )
