"""A fake Matrix client that answers `room_get_state`, for the Slack watch tests."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import nio

if TYPE_CHECKING:
    from collections.abc import Iterable

ROOM_ID = "!room:example.org"


def _state_event(event_type: str, state_key: str, content: dict[str, str]) -> dict[str, object]:
    return {
        "type": event_type,
        "state_key": state_key,
        "sender": "@creator:example.org",
        "content": content,
        "event_id": f"${event_type}{state_key}",
        "origin_server_ts": 1_800_000_000_000,
    }


def room_state_client(
    joined: Iterable[str] = (),
    *,
    invited: Iterable[str] = (),
    departed: Iterable[str] = (),
    history_visibility: str | None = "invited",
) -> AsyncMock:
    """Return a client whose room has these members, `departed` ones having left, and this history setting.

    `history_visibility=None` leaves the setting out of the room's state, as a room that never set it has.
    """
    memberships = [
        *((user_id, "join") for user_id in joined),
        *((user_id, "invite") for user_id in invited),
        *((user_id, "leave") for user_id in departed),
    ]
    events = [_state_event("m.room.member", user_id, {"membership": membership}) for user_id, membership in memberships]
    if history_visibility is not None:
        events.append(_state_event("m.room.history_visibility", "", {"history_visibility": history_visibility}))
    client = AsyncMock()
    client.room_get_state.return_value = nio.RoomGetStateResponse(events, ROOM_ID)
    return client
