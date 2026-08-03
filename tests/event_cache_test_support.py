"""Shared test helpers for event-cache behavior."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import nio

if TYPE_CHECKING:
    from mindroom.matrix.cache import ConversationEventCache


async def replace_thread_unconditionally(
    cache: ConversationEventCache,
    room_id: str,
    thread_id: str,
    events: list[dict[str, Any]],
    *,
    fetch_started_at: float | None = None,
) -> None:
    """Replace a cached thread snapshot, clearing any gap already recorded against it.

    The default stands in for "a fetch that started just now": it covers every marker laid down
    before this call, and -- unlike an infinite sentinel -- still lets a later fetch replace what
    it installs, since replacement is ordered by ``fetch_started_at``.
    """
    stored = await cache.replace_thread(
        room_id,
        thread_id,
        events,
        expected_membership_epoch=await cache.room_membership_epoch(room_id),
        fetch_started_at=time.time() if fetch_started_at is None else fetch_started_at,
    )
    assert stored


def raw_nio_event(event_source: dict[str, Any]) -> nio.Event:
    """Return a typed nio event that preserves one exact raw source payload."""
    event_type = event_source.get("type")
    if not isinstance(event_type, str):
        msg = "Test Matrix event is missing type"
        raise TypeError(msg)
    return nio.UnknownEvent(event_source, event_type)


def raw_nio_redaction(
    event_source: dict[str, Any],
    *,
    redacts: str,
) -> nio.RedactionEvent:
    """Return a typed nio redaction with one exact raw source payload."""
    return nio.RedactionEvent(event_source, redacts)
