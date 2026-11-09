"""Read a room's joined members through a client whose member store can refuse a read that a membership change raced."""

from __future__ import annotations

import nio

# The bots' client refuses a member list with this when its sync applied a change of the room's members during the
# read, as the join of someone who calls right after joining does; a read after that change answers.
_MEMBERSHIP_CHANGED = "room membership changed during lookup"
_READS = 3


async def read_joined_members(
    client: nio.AsyncClient,
    room_id: str,
) -> nio.JoinedMembersResponse | nio.JoinedMembersError:
    """Return the room's joined members, read again, 3 reads at most, while the client refuses them for a member change.

    The last refusal, like any other error, raises.
    """
    for _ in range(_READS - 1):
        try:
            return await client.joined_members(room_id)
        except nio.LocalProtocolError as error:
            if str(error) != _MEMBERSHIP_CHANGED:
                raise
    return await client.joined_members(room_id)
