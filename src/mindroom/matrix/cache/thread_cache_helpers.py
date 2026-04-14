"""Shared pure helpers for Matrix thread cache policies."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    import structlog

    from mindroom.matrix.client import ResolvedVisibleMessage


def event_id_from_event_source(event_source: dict[str, object]) -> str | None:
    """Return the event ID when one cached event source contains it."""
    event_id = event_source.get("event_id")
    return event_id if isinstance(event_id, str) else None


def sort_thread_history_root_first(
    history: list[ResolvedVisibleMessage],
    *,
    thread_id: str,
) -> None:
    """Keep thread history ordered by timestamp while pinning the root first."""
    history.sort(key=lambda message: (message.timestamp, message.event_id))
    root_index = next(
        (index for index, message in enumerate(history) if message.event_id == thread_id),
        None,
    )
    if root_index not in (None, 0):
        history.insert(0, history.pop(root_index))


def resolved_cache_diagnostics(
    *,
    cache_read_ms: float,
    incremental_refresh_ms: float = 0.0,
    resolution_ms: float = 0.0,
    sidecar_hydration_ms: float = 0.0,
) -> dict[str, float]:
    """Return diagnostics for one resolved-thread cache read path."""
    return {
        "cache_read_ms": cache_read_ms,
        "incremental_refresh_ms": incremental_refresh_ms,
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
