"""Shared fail-closed invalidation policy for one Matrix thread snapshot."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .event_cache import ConversationEventCache, EventCacheBackendUnavailableError

if TYPE_CHECKING:
    import structlog


async def mark_thread_stale_fail_closed(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
    reason: str,
    logger: structlog.stdlib.BoundLogger,
    raise_on_failure: bool = False,
) -> None:
    """Persist a stale marker, deleting rows or disabling the cache when persistence fails."""
    try:
        await event_cache.mark_thread_stale(room_id, thread_id, reason=reason)
    except Exception as stale_marker_error:
        logger.warning(
            "Failed to mark cached thread stale",
            room_id=room_id,
            thread_id=thread_id,
            reason=reason,
            error=str(stale_marker_error),
        )
        try:
            await event_cache.invalidate_thread(room_id, thread_id)
        except Exception as invalidate_error:
            if isinstance(stale_marker_error, EventCacheBackendUnavailableError):
                logger.warning(
                    "Cached thread stale marker is pending because cache backend is temporarily unavailable",
                    room_id=room_id,
                    thread_id=thread_id,
                    reason=reason,
                    stale_marker_error=str(stale_marker_error),
                    error=str(invalidate_error),
                )
            else:
                logger.warning(
                    "Failed to delete cached thread rows after stale-marker failure; disabling cache",
                    room_id=room_id,
                    thread_id=thread_id,
                    reason=reason,
                    stale_marker_error=str(stale_marker_error),
                    error=str(invalidate_error),
                )
                event_cache.disable(f"stale_marker_failed:thread:{thread_id}:{room_id}:{reason}")
        if raise_on_failure:
            raise


async def mark_room_threads_stale_fail_closed(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    reason: str,
    logger: structlog.stdlib.BoundLogger,
    raise_on_failure: bool = False,
) -> None:
    """Persist a room stale marker, deleting rows or disabling the cache when persistence fails."""
    try:
        await event_cache.mark_room_threads_stale(room_id, reason=reason)
    except Exception as stale_marker_error:
        logger.warning(
            "Failed to mark cached room threads stale",
            room_id=room_id,
            reason=reason,
            error=str(stale_marker_error),
        )
        try:
            await event_cache.invalidate_room_threads(room_id)
        except Exception as invalidate_error:
            if isinstance(stale_marker_error, EventCacheBackendUnavailableError):
                logger.warning(
                    "Cached room stale marker is pending because cache backend is temporarily unavailable",
                    room_id=room_id,
                    reason=reason,
                    stale_marker_error=str(stale_marker_error),
                    error=str(invalidate_error),
                )
            else:
                logger.warning(
                    "Failed to delete cached room thread rows after stale-marker failure; disabling cache",
                    room_id=room_id,
                    reason=reason,
                    stale_marker_error=str(stale_marker_error),
                    error=str(invalidate_error),
                )
                event_cache.disable(f"stale_marker_failed:room:{room_id}:{reason}")
        if raise_on_failure:
            raise
