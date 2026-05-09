"""Shared test helpers for event-cache behavior."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from mindroom.matrix.cache import ConversationEventCache


async def replace_thread_unconditionally(
    cache: ConversationEventCache,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    *,
    validated_at: float | None = None,
) -> None:
    """Replace a cached thread snapshot without timestamp race rejection."""
    timestamp = time.time() if validated_at is None else validated_at
    replaced = await cache.replace_thread_if_not_newer(
        room_id,
        thread_id,
        events,
        fetch_started_at=float("inf"),
        validated_at=timestamp,
    )
    assert replaced
