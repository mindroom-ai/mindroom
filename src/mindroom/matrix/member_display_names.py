"""Current member display names from the synced Matrix room cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

from mindroom.matrix.room_membership import room_membership_snapshot

if TYPE_CHECKING:
    import nio


def room_member_display_names(room: nio.MatrixRoom) -> dict[str, str]:
    """Use application lookup names when available without rewriting nio's members."""
    snapshot = room_membership_snapshot(room)
    names = {
        user_id: user.display_name
        for user_id, user in room.users.items()
        if user.display_name and (snapshot is None or user.invited)
    }
    if snapshot is not None:
        for user_id in snapshot.joined_user_ids:
            names.pop(user_id, None)
        names.update(snapshot.display_names)
    return names
