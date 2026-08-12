"""Matrix sync-loop selection and Simplified Sliding Sync helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import nio

if TYPE_CHECKING:
    from collections.abc import Iterable

    from mindroom.config.main import Config

# The memberships that end this account's stay in a room.
_DEPARTED_MEMBERSHIPS = frozenset({"leave", "ban"})

_SLIDING_SYNC_REQUIRED_STATE: tuple[tuple[str, str], ...] = (
    ("m.room.create", ""),
    ("m.room.name", ""),
    ("m.room.topic", ""),
    ("m.room.avatar", ""),
    ("m.room.encryption", ""),
    ("m.room.member", "$LAZY"),
)
_SLIDING_SYNC_LIST_ROOM_COUNT = 100


def _sliding_room_config(timeline_limit: int) -> dict[str, object]:
    """Return the shared room request config for Simplified Sliding Sync."""
    return {
        "timeline_limit": timeline_limit,
        "required_state": [list(entry) for entry in _SLIDING_SYNC_REQUIRED_STATE],
    }


def _sliding_sync_lists(timeline_limit: int) -> dict[str, object]:
    """Return list subscriptions that preserve invite and recently-active-room ingress."""
    return {
        "mindroom": {
            "ranges": [[0, _SLIDING_SYNC_LIST_ROOM_COUNT - 1]],
            **_sliding_room_config(timeline_limit),
        },
    }


def _sliding_sync_room_subscriptions(room_ids: list[str], timeline_limit: int) -> dict[str, object]:
    """Return explicit room subscriptions for resolved Matrix room IDs."""
    return {room_id: _sliding_room_config(timeline_limit) for room_id in room_ids if room_id.startswith("!")}


def _sliding_sync_extensions() -> dict[str, object]:
    """Return extension subscriptions required for a bot account sync loop."""
    return {
        "to_device": {"enabled": True},
        "e2ee": {"enabled": True},
        "account_data": {"enabled": True},
    }


@dataclass(frozen=True, slots=True)
class OwnRoomMembership:
    """What one sync response says about this account's own room memberships.

    ``departures`` is one entry per departure the response shows, not a set of
    rooms that departed. The two differ whenever an account leaves, comes back
    and leaves again inside one sync interval, and the difference matters
    because the fence's bookkeeping is per departure: a local leave records
    that it is owed one sync report, and a room id offered once can only ever
    be read as that report. The second departure is then absorbed rather than
    fenced, and everything the membership between them built survives into a
    membership that has no right to it.
    """

    joined_room_ids: frozenset[str]
    left_room_ids: frozenset[str]
    departures: tuple[str, ...]

    @property
    def departed_room_ids(self) -> frozenset[str]:
        """Return the rooms this response reported at least one departure from."""
        return frozenset(self.departures)


def own_membership_from_sync(response: nio.SyncResponse, *, self_user_id: str) -> OwnRoomMembership:
    """Return this account's own membership transitions from one /sync response.

    nio applies the room sections to client state but never surfaces the
    account's own departures, so they are read here: from the leave section,
    and from the timeline of rooms whose membership at the end of the response
    is join because the account came back before it ended.
    """
    left_room_ids = frozenset(response.rooms.leave)
    departures: list[str] = []
    for room_id, room_info in (*response.rooms.join.items(), *response.rooms.leave.items()):
        observed = _own_departures_in(room_info.timeline.events, self_user_id)
        # A room in the leave section departed whether or not the timeline it
        # arrived with is long enough to show the transition.
        departures.extend([room_id] * max(observed, 1 if room_id in left_room_ids else 0))
    return OwnRoomMembership(
        joined_room_ids=frozenset(response.rooms.join),
        left_room_ids=left_room_ids,
        departures=tuple(departures),
    )


def own_membership_from_sliding_sync(
    response: nio.SlidingSyncResponse,
    *,
    self_user_id: str,
) -> OwnRoomMembership:
    """Return this account's own membership transitions from one sliding sync response.

    Same reading as classic /v3/sync, from the shape sliding sync uses: the
    room's own membership rather than which section it arrived in.
    """
    joined_room_ids: set[str] = set()
    left_room_ids: set[str] = set()
    departures: list[str] = []
    for room_id, room in response.rooms.items():
        observed = _own_departures_in(room.timeline, self_user_id)
        if room.membership in _DEPARTED_MEMBERSHIPS:
            left_room_ids.add(room_id)
            departures.extend([room_id] * max(observed, 1))
            continue
        is_invite = room.membership == "invite" or (room.membership is None and bool(room.stripped_state))
        if not is_invite:
            joined_room_ids.add(room_id)
        departures.extend([room_id] * observed)
    return OwnRoomMembership(
        joined_room_ids=frozenset(joined_room_ids),
        left_room_ids=frozenset(left_room_ids),
        departures=tuple(departures),
    )


def _own_departures_in(events: Iterable[object], self_user_id: str) -> int:
    """Return how many distinct departures of this account one timeline shows.

    Counted by event, because one timeline can carry two of them and a repeat
    delivery can carry the same one twice.
    """
    return len(
        {
            event.event_id
            for event in events
            if isinstance(event, nio.RoomMemberEvent)
            and event.state_key == self_user_id
            and event.membership in _DEPARTED_MEMBERSHIPS
        },
    )


async def run_matrix_sync_forever(
    client: nio.AsyncClient,
    *,
    config: Config,
    agent_name: str,
    room_ids: list[str],
    timeout_ms: int,
    sync_filter: dict[str, object],
    first_sync_done: bool,
) -> None:
    """Run the configured Matrix sync loop for one bot account."""
    if config.matrix_sync.mode == "classic":
        await client.sync_forever(timeout=timeout_ms, sync_filter=sync_filter, full_state=not first_sync_done)
        return

    timeline_limit = config.matrix_sync.sliding_timeline_limit
    await client.sliding_sync_forever(
        timeout=timeout_ms,
        conn_id=f"mindroom-{agent_name}",
        lists=_sliding_sync_lists(timeline_limit),
        room_subscriptions=_sliding_sync_room_subscriptions(room_ids, timeline_limit),
        extensions=_sliding_sync_extensions(),
    )
