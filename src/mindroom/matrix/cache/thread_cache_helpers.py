"""Shared pure helpers for Matrix thread cache policies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.matrix.client_visible_messages import ResolvedVisibleMessage


class _ThreadCacheStateLike(Protocol):
    """Structural contract for durable thread cache trust state."""

    validated_at: float | None
    invalidated_at: float | None
    invalidation_reason: str | None
    room_invalidated_at: float | None
    room_invalidation_reason: str | None


def latest_visible_thread_event_id(history: Sequence[ResolvedVisibleMessage]) -> str | None:
    """Return the latest visible event ID from one resolved thread history."""
    if not history:
        return None
    return history[-1].visible_event_id or history[-1].event_id or None


def thread_cache_rejection_reason(
    cache_state: _ThreadCacheStateLike | None,
) -> str | None:
    """Return why one durable thread snapshot must be rejected, if at all."""
    rejection_reason: str | None = None
    if cache_state is None:
        rejection_reason = "no_cache_state"
    elif cache_state.validated_at is None:
        rejection_reason = "cache_never_validated"
    elif cache_state.invalidated_at is not None and cache_state.invalidated_at >= cache_state.validated_at:
        rejection_reason = "thread_invalidated_after_validation"
    elif cache_state.room_invalidated_at is not None and cache_state.room_invalidated_at >= cache_state.validated_at:
        rejection_reason = "room_invalidated_after_validation"
    return rejection_reason


def _thread_cache_state_is_usable(
    cache_state: _ThreadCacheStateLike | None,
) -> bool:
    """Return whether one durable thread snapshot is safe to reuse."""
    return thread_cache_rejection_reason(cache_state) is None
