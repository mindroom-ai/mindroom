"""Current member display names from the synced Matrix room cache."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import nio


def room_member_display_names(room: nio.MatrixRoom) -> dict[str, str]:
    """Map every cached room member that has a display name to that current name."""
    return {user_id: user.display_name for user_id, user in room.users.items() if user.display_name}
