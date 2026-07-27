"""Fail-closed gap marking for Matrix thread snapshots.

Recording a gap is what stops a stale snapshot being served, so a marker that cannot be written
leaves the cache claiming freshness it does not have. These helpers fall back to purging the rows
outright, which is strictly stronger: a read that finds nothing refetches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .event_cache import ConversationEventCache, EventCacheBackendUnavailableError

if TYPE_CHECKING:
    import structlog


async def mark_thread_gap_fail_closed(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    thread_id: str,
    reason: str,
    logger: structlog.stdlib.BoundLogger,
    raise_on_failure: bool = False,
) -> None:
    """Persist a gap marker, deleting rows or disabling the cache when persistence fails."""
    try:
        await event_cache.mark_thread_gap(room_id, thread_id, reason=reason)
    except Exception as gap_marker_error:
        logger.warning(
            "Failed to mark a gap against a cached thread",
            room_id=room_id,
            thread_id=thread_id,
            reason=reason,
            error=str(gap_marker_error),
        )
        try:
            await event_cache.invalidate_thread(room_id, thread_id)
        except Exception as invalidate_error:
            if isinstance(gap_marker_error, EventCacheBackendUnavailableError):
                logger.warning(
                    "Cached thread gap marker is pending because cache backend is temporarily unavailable",
                    room_id=room_id,
                    thread_id=thread_id,
                    reason=reason,
                    gap_marker_error=str(gap_marker_error),
                    error=str(invalidate_error),
                )
            else:
                logger.warning(
                    "Failed to delete cached thread rows after gap-marker failure; disabling cache",
                    room_id=room_id,
                    thread_id=thread_id,
                    reason=reason,
                    gap_marker_error=str(gap_marker_error),
                    error=str(invalidate_error),
                )
                event_cache.disable(f"gap_marker_failed:thread:{thread_id}:{room_id}:{reason}")
        if raise_on_failure:
            raise


async def mark_room_threads_gap_fail_closed(
    event_cache: ConversationEventCache,
    *,
    room_id: str,
    reason: str,
    logger: structlog.stdlib.BoundLogger,
    raise_on_failure: bool = False,
) -> None:
    """Persist a room gap marker, deleting rows or disabling the cache when persistence fails."""
    try:
        await event_cache.mark_room_threads_gap(room_id, reason=reason)
    except Exception as gap_marker_error:
        logger.warning(
            "Failed to mark a gap against a room's cached threads",
            room_id=room_id,
            reason=reason,
            error=str(gap_marker_error),
        )
        try:
            await event_cache.invalidate_room_threads(room_id)
        except Exception as invalidate_error:
            if isinstance(gap_marker_error, EventCacheBackendUnavailableError):
                logger.warning(
                    "Cached room gap marker is pending because cache backend is temporarily unavailable",
                    room_id=room_id,
                    reason=reason,
                    gap_marker_error=str(gap_marker_error),
                    error=str(invalidate_error),
                )
            else:
                logger.warning(
                    "Failed to delete cached room thread rows after gap-marker failure; disabling cache",
                    room_id=room_id,
                    reason=reason,
                    gap_marker_error=str(gap_marker_error),
                    error=str(invalidate_error),
                )
                event_cache.disable(f"gap_marker_failed:room:{room_id}:{reason}")
        if raise_on_failure:
            raise
