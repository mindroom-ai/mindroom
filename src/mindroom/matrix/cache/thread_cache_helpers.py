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


def thread_cache_state_is_usable(
    cache_state: ThreadCacheState | None,
    *,
    runtime_started_at: float | None,
    now: float | None = None,
) -> bool:
    """Return whether one durable thread snapshot is safe to reuse."""
    if cache_state is None or cache_state.validated_at is None:
        return False
    if runtime_started_at is not None and cache_state.validated_at < runtime_started_at:
        return False
    if cache_state.invalidated_at is not None and cache_state.invalidated_at >= cache_state.validated_at:
        return False
    if cache_state.room_invalidated_at is not None and cache_state.room_invalidated_at >= cache_state.validated_at:
        return False
    checked_at = time.time() if now is None else now
    return checked_at - cache_state.validated_at <= THREAD_CACHE_MAX_AGE_SECONDS
