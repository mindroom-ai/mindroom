"""Classify Matrix sync windows that need no historical recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING

from nio.client.sync_recovery import would_plan_real_gap

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
        if room_info.timeline.limited
        and not would_plan_real_gap(
            timeline_events=room_info.timeline.events,
            user_id=user_id,
            membership="join",
            cursor_token=response.next_batch,
        )
    )
    return frozenset(response.rooms.leave) | own_join_room_ids
