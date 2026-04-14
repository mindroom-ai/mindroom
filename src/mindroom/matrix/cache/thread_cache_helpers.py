"""Shared pure helpers for Matrix thread cache policies."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog

    from mindroom.matrix.cache.event_cache import ThreadCacheState
    from mindroom.matrix.client import ResolvedVisibleMessage


_RAW_THREAD_CACHE_TTL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class ThreadCacheFreshnessContext:
    """Runtime freshness inputs used to decide whether raw SQLite thread rows are reusable."""

    runtime_started_at: float
    last_sync_activity_monotonic: float | None
    current_sync_token: str | None


def event_id_from_event_source(event_source: dict[str, object]) -> str | None:
    """Return the event ID when one cached event source contains it."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def thread_cache_state_is_fresh(
    state: ThreadCacheState | None,
    *,
    context: ThreadCacheFreshnessContext,
    ttl_seconds: float = _RAW_THREAD_CACHE_TTL_SECONDS,
) -> bool:
    """Return whether one raw thread cache entry is still trustworthy for reads."""
    if state is None or state.invalidated_at is not None or state.validated_at is None:
        return False
    if state.room_invalidated_at is not None and state.validated_at <= state.room_invalidated_at:
        return False
    if (time.time() - state.validated_at) >= ttl_seconds:
        return False
    if context.last_sync_activity_monotonic is None or context.current_sync_token is None:
        return state.validated_at >= context.runtime_started_at
    return state.validated_sync_token == context.current_sync_token


def resolved_cache_diagnostics(
    *,
    cache_read_ms: float,
    resolution_ms: float = 0.0,
    sidecar_hydration_ms: float = 0.0,
) -> dict[str, float]:
    """Return diagnostics for one resolved-thread cache read path."""
    return {
        "cache_read_ms": cache_read_ms,
        "resolution_ms": resolution_ms,
        "sidecar_hydration_ms": sidecar_hydration_ms,
    }


def log_resolved_thread_cache(
    logger: structlog.stdlib.BoundLogger,
    event: str,
    *,
    room_id: str,
    thread_id: str,
    reason: str | None = None,
) -> None:
    """Emit one structured resolved-thread cache log entry."""
    event_data: dict[str, str] = {
        "room_id": room_id,
        "thread_id": thread_id,
    }
    if reason is not None:
        event_data["reason"] = reason
    logger.debug(event, **event_data)


def latest_visible_thread_event_id(history: Sequence[ResolvedVisibleMessage]) -> str | None:
    """Return the latest visible event ID from one resolved thread history."""
    if not history:
        return None
    return history[-1].visible_event_id or history[-1].event_id or None
