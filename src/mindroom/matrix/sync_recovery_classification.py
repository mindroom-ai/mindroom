"""Classify Matrix sync windows that need no historical recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nio.client.sync_recovery import is_own_join

if TYPE_CHECKING:
    import nio


def classic_no_recovery_needed_room_ids(
    response: nio.SyncResponse,
    *,
    user_id: str,
) -> frozenset[str]:
    """Return rooms whose authoritative membership boundary clears NIO recovery."""
    own_join_room_ids = frozenset(
        room_id
        for room_id, room_info in response.rooms.join.items()
        if any(is_own_join(event, user_id) for event in room_info.timeline.events)
    )
    return frozenset(response.rooms.leave) | frozenset(response.rooms.invite) | own_join_room_ids
