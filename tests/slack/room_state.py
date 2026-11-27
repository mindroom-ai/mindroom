"""Room state snapshots and fake Matrix clients for Slack watch tests."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock

import nio

from mindroom.matrix.room_reconciliation import RoomStateSnapshot

if TYPE_CHECKING:
    from collections.abc import Iterable

ROOM_ID = "!room:example.org"


def _state_event(event_type: str, state_key: str, content: dict[str, Any]) -> dict[str, object]:
    return {
        "type": event_type,
        "state_key": state_key,
        "sender": "@creator:example.org",
        "content": content,
        "event_id": f"${event_type}{state_key}",
        "origin_server_ts": 1_800_000_000_000,
    }


def room_state_snapshot(
    joined: Iterable[str] = (),
    *,
    invited: Iterable[str] = (),
    departed: Iterable[str] = (),
    history_visibility: str | None = "invited",
    room_id: str = ROOM_ID,
) -> RoomStateSnapshot:
    """Return a RoomStateSnapshot with these memberships and history visibility."""
    events: dict[tuple[str, str], dict[str, Any]] = {}
    for user_id, membership in (
        *((uid, "join") for uid in joined),
        *((uid, "invite") for uid in invited),
        *((uid, "leave") for uid in departed),
    ):
        events[("m.room.member", user_id)] = {"membership": membership}
    if history_visibility is not None:
        events[("m.room.history_visibility", "")] = {"history_visibility": history_visibility}
    return RoomStateSnapshot(room_id=room_id, events=events, creators=frozenset())


def room_state_events(snapshot: RoomStateSnapshot) -> list[dict[str, object]]:
    """Return raw Matrix state event dicts matching `snapshot`."""
    return [_state_event(etype, skey, content) for (etype, skey), content in snapshot.events.items()]


def room_state_client(
    joined: Iterable[str] = (),
    *,
    invited: Iterable[str] = (),
    departed: Iterable[str] = (),
    history_visibility: str | None = "invited",
    room_id: str = ROOM_ID,
) -> AsyncMock:
    """Return a client whose room has these members, `departed` ones having left, and this history setting."""
    joined_list = list(joined)
    snapshot = room_state_snapshot(
        joined_list,
        invited=invited,
        departed=departed,
        history_visibility=history_visibility,
        room_id=room_id,
    )
    client = AsyncMock()
    client.rooms = {
        room_id: SimpleNamespace(room_id=room_id, users={uid: SimpleNamespace(user_id=uid) for uid in joined_list}),
    }
    client.room_get_state.return_value = nio.RoomGetStateResponse(room_state_events(snapshot), room_id)
    return client
