"""Shared test helpers for event-cache behavior."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import nio

from mindroom.approval_events import valid_approval_replacement
from mindroom.matrix.media import valid_room_message_replacement

if TYPE_CHECKING:
    from collections.abc import Collection

    from mindroom.matrix.cache import ConversationEventCache


async def get_latest_edit(
    cache: ConversationEventCache,
    room_id: str,
    original_event_id: str,
    *,
    sender: str,
    event_type: str,
    excluded_event_ids: Collection[str] = (),
) -> dict[str, Any] | None:
    """Read an edit through the production original-event contract."""
    validator = (
        valid_approval_replacement if event_type == "io.mindroom.tool_approval" else valid_room_message_replacement
    )
    original = {"event_id": original_event_id, "room_id": room_id, "sender": sender, "type": event_type}
    return await cache.get_latest_edit(
        room_id,
        original,
        validator=validator,
        excluded_event_ids=excluded_event_ids,
    )


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
        expected_membership_epoch=await cache.room_membership_epoch(room_id),
        fetch_started_at=float("inf"),
        validated_at=timestamp,
    )
    assert replaced


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
