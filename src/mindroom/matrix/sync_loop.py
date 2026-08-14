"""Matrix sync-loop selection and Simplified Sliding Sync helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import nio

from mindroom.membership_models import ReportedDeparture

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

    ``departures`` contains one record per distinct departure-state
    observation, not a set of rooms that departed. Consecutive leave/ban
    observations can alias one ended membership, while observations separated
    by a join describe different memberships. An exact Matrix event id
    identifies a visible transition; the response token identifies a leave
    section whose truncated timeline omitted it. ``rejoined_after`` records
    where the same response proves a later membership already began.
    ``invited_room_ids`` also contains other non-joined or unresolved initial
    Sliding Sync states because all of them require the same continuity fence.
    """

    joined_room_ids: frozenset[str]
    left_room_ids: frozenset[str]
    invited_room_ids: frozenset[str]
    departures: tuple[ReportedDeparture, ...]

    @property
    def departed_room_ids(self) -> frozenset[str]:
        """Return the rooms this response reported at least one departure from."""
        return frozenset(departure.room_id for departure in self.departures)

    @property
    def continuity_lost_room_ids(self) -> frozenset[str]:
        """Return rooms whose prior joined-members view is no longer authoritative."""
        return self.departed_room_ids | self.invited_room_ids


def own_membership_from_sync(response: nio.SyncResponse, *, self_user_id: str) -> OwnRoomMembership:
    """Return this account's own membership transitions from one /sync response.

    nio applies the room sections to client state but never surfaces the
    account's own departures, so they are read here: from the leave section,
    and from the timeline of rooms whose membership at the end of the response
    is join because the account came back before it ended.
    """
    left_room_ids = frozenset(response.rooms.leave)
    departures: list[ReportedDeparture] = []
    for room_id, room_info in (*response.rooms.join.items(), *response.rooms.leave.items()):
        observed = _own_departures_in(
            room_id,
            room_info.timeline.events,
            self_user_id,
            final_membership_is_joined=room_id not in left_room_ids,
        )
        # A room in the leave section departed whether or not the timeline it
        # arrived with is long enough to show the transition.
        if not observed and room_id in left_room_ids:
            observed = (
                ReportedDeparture(
                    room_id=room_id,
                    observation_id=_sync_departure_observation_id("classic", response.next_batch, room_id),
                ),
            )
        departures.extend(observed)
    return OwnRoomMembership(
        joined_room_ids=frozenset(response.rooms.join),
        left_room_ids=left_room_ids,
        invited_room_ids=frozenset(response.rooms.invite),
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
    invited_room_ids: set[str] = set()
    departures: list[ReportedDeparture] = []
    for room_id, room in response.rooms.items():
        membership_unchanged = room.membership is None and not room.initial and not room.stripped_state
        final_membership_is_joined = room.membership == "join" or membership_unchanged
        observed = _own_departures_in(
            room_id,
            room.timeline,
            self_user_id,
            final_membership_is_joined=final_membership_is_joined,
        )
        if room.membership in _DEPARTED_MEMBERSHIPS:
            left_room_ids.add(room_id)
            if not observed:
                observed = (
                    ReportedDeparture(
                        room_id=room_id,
                        observation_id=_sync_departure_observation_id("sliding", response.pos, room_id),
                    ),
                )
            departures.extend(observed)
            continue
        if final_membership_is_joined:
            joined_room_ids.add(room_id)
        else:
            invited_room_ids.add(room_id)
        departures.extend(observed)
    return OwnRoomMembership(
        joined_room_ids=frozenset(joined_room_ids),
        left_room_ids=frozenset(left_room_ids),
        invited_room_ids=frozenset(invited_room_ids),
        departures=tuple(departures),
    )


def room_member_events_from_sync(
    response: nio.SyncResponse | nio.SlidingSyncResponse,
) -> tuple[tuple[str, nio.RoomMemberEvent], ...]:
    """Return deduplicated member events in their per-room sync order."""
    room_events: Iterable[tuple[str, Iterable[object]]]
    if isinstance(response, nio.SyncResponse):
        room_events = (
            (room_id, (*room.state, *room.timeline.events))
            for room_id, room in (*response.rooms.join.items(), *response.rooms.leave.items())
        )
    else:
        room_events = ((room_id, (*room.required_state, *room.timeline)) for room_id, room in response.rooms.items())

    seen: set[tuple[str, str]] = set()
    transitions: list[tuple[str, nio.RoomMemberEvent]] = []
    for room_id, events in room_events:
        for event in events:
            if not isinstance(event, nio.RoomMemberEvent):
                continue
            identity = (room_id, event.event_id)
            if identity in seen:
                continue
            seen.add(identity)
            transitions.append((room_id, event))
    return tuple(transitions)


def _sync_departure_observation_id(sync_kind: str, token: str, room_id: str) -> str:
    """Identify one truncated leave observation across response replay."""
    return f"{sync_kind}:{token}:{room_id}"


def _own_departures_in(
    room_id: str,
    events: Iterable[object],
    self_user_id: str,
    *,
    final_membership_is_joined: bool,
) -> tuple[ReportedDeparture, ...]:
    """Return distinct departures and the joins that follow each one.

    Identity matters as well as count: one timeline can carry two departures,
    while a replay can carry the same departure again after a rejoin. Only an
    ordered join event proves a new membership between two departures; the
    room section's final state can prove a rejoin only after the last one.
    """
    departures: list[ReportedDeparture] = []
    seen_departures: set[str] = set()
    for event in events:
        if not isinstance(event, nio.RoomMemberEvent) or event.state_key != self_user_id:
            continue
        if event.membership in _DEPARTED_MEMBERSHIPS:
            if event.event_id in seen_departures:
                continue
            seen_departures.add(event.event_id)
            departures.append(ReportedDeparture(room_id=room_id, observation_id=event.event_id))
        elif event.membership == "join" and departures:
            departures[-1] = replace(departures[-1], rejoined_after=True)
    if departures and final_membership_is_joined:
        departures[-1] = replace(departures[-1], rejoined_after=True)
    return tuple(departures)


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
