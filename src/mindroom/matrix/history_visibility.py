"""Where a joined member's view of a room's history begins, under the room's history visibility."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import nio

if TYPE_CHECKING:
    from collections.abc import Mapping

# Matrix's history visibilities from the least strict; a value Matrix does not define counts as `joined`.
_STRICTNESS = {"world_readable": 0, "shared": 1, "invited": 2, "joined": 3}
# Reads of earlier member events the walk back to a join makes, over profile changes and repeated invites, before the
# earliest event reached counts, which starts the view later, never earlier.
_WALK_STEPS = 20
# What a homeserver answers for an event its user may not see, as one from before the bot joined under `joined`.
_HIDDEN_EVENT_ERRCODES = frozenset({"M_NOT_FOUND", "M_FORBIDDEN"})


@dataclass(frozen=True)
class RoomHistory:
    """A room's history visibility, and where a joined member's view of its history begins under it."""

    visibility: str
    """The room's ``m.room.history_visibility``, ``shared`` when it has none."""
    _joined_at: int
    _invited_at: int

    def since(self, recorded_under: str | None = None) -> int | None:
        """Return where the member's view begins, in milliseconds since the epoch, or None for the whole history.

        It applies the stricter of the visibility now and ``recorded_under``, the one a record was kept under: None
        under ``shared`` or ``world_readable``; under ``invited`` the invite their current membership came from, else
        their join; under ``joined`` or any other value their join.
        """
        strictness = _strictness(self.visibility)
        if recorded_under is not None:
            strictness = max(strictness, _strictness(recorded_under))
        if strictness <= _STRICTNESS["shared"]:
            return None
        return self._invited_at if strictness == _STRICTNESS["invited"] else self._joined_at


def _strictness(visibility: str) -> int:
    return _STRICTNESS.get(visibility, _STRICTNESS["joined"])


async def read_room_history(client: nio.AsyncClient, room_id: str, user_id: str) -> RoomHistory | None:
    """Read the room's history visibility and where ``user_id``'s view of it begins; None unless they are joined.

    One read of the room's state, then back along ``replaces_state`` to the event that began their membership. It
    raises when the homeserver answers either read with an error, except for an earlier event it hides from the bot,
    where the walk stops.
    """
    response = await client.room_get_state(room_id)
    if not isinstance(response, nio.RoomGetStateResponse):
        msg = f"Room state read failed: {response}"
        raise RuntimeError(msg)  # noqa: TRY004
    state = {(event["type"], event["state_key"]): event for event in response.events}
    member = state.get(("m.room.member", user_id))
    if member is None or member["content"].get("membership") != "join":
        return None
    visibility = state.get(("m.room.history_visibility", ""), {}).get("content", {}).get("history_visibility", "shared")
    joined_at, invited_at = await _membership_start(client, room_id, member)
    # A value that is not a string counts as `joined`, as does any value Matrix does not define.
    return RoomHistory(visibility if isinstance(visibility, str) else "joined", joined_at, invited_at)


async def _membership_start(client: nio.AsyncClient, room_id: str, member: Mapping[str, Any]) -> tuple[int, int]:
    """Return when the member's current join began, and the invite it came from, else that join.

    Profile changes are joins that replace a join, so the walk passes over them, and on over the invites before it.
    """
    joined_at = invited_at = member["origin_server_ts"]
    event = member
    invited = False
    for _ in range(_WALK_STEPS):
        replaced = event.get("unsigned", {}).get("replaces_state")
        if not isinstance(replaced, str):
            break
        response = await client.room_get_event(room_id, replaced)
        if isinstance(response, nio.RoomGetEventError) and response.status_code in _HIDDEN_EVENT_ERRCODES:
            break
        if not isinstance(response, nio.RoomGetEventResponse):
            msg = f"Membership event read failed: {response}"
            raise RuntimeError(msg)  # noqa: TRY004
        event = response.event.source
        membership = event["content"].get("membership")
        if membership == "invite":
            invited = True
        elif membership != "join" or invited:
            break
        else:
            joined_at = event["origin_server_ts"]
        invited_at = event["origin_server_ts"]
    return joined_at, invited_at
