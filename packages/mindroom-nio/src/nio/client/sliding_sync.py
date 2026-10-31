"""Sliding room snapshots and event ordering shared by both sync consumers."""

from __future__ import annotations

from collections.abc import Collection, Iterator, Mapping
from typing import TYPE_CHECKING, cast

from ..event_provenance import TimelineEventProvenance
from ..events import BadEventType, EphemeralEvent, Event, PowerLevels, RoomMemberEvent
from ..responses import (
    RoomInfo,
    RoomSummary,
    SlidingSyncResponse,
    SlidingSyncRoom,
    SlidingSyncStateStub,
    Timeline,
    UnreadNotifications,
)
from ..rooms import MatrixRoom
from .base_client import _SyncItem

if TYPE_CHECKING:
    from .base_client import Client


def live_event_count(room: SlidingSyncRoom) -> int:
    """Return the live suffix length, clamped to the supplied window."""
    if room.num_live is not None:
        return max(0, min(room.num_live, len(room.timeline)))
    return 0 if room.initial or room.expanded_timeline else len(room.timeline)


def timeline_provenance(
    room: SlidingSyncRoom,
    index: int,
    *,
    history: bool = False,
    recovered: bool = False,
    history_prefix: int = 0,
) -> TimelineEventProvenance:
    if history or index < history_prefix:
        return TimelineEventProvenance.HISTORY
    if index >= len(room.timeline) - live_event_count(room):
        return TimelineEventProvenance.LIVE
    return (
        TimelineEventProvenance.RECOVERED
        if recovered
        else TimelineEventProvenance.HISTORY
    )


def _reset_snapshot(client: Client, room: MatrixRoom) -> None:
    fresh = MatrixRoom(room.room_id, room.own_user_id, room.encrypted)
    for name in (
        "tags",
        "fully_read_marker",
        "read_receipts",
        "threaded_read_receipts",
        "typing_users",
        "summary",
        "unread_notifications",
        "unread_highlights",
    ):
        setattr(fresh, name, getattr(room, name))
    if client.olm and room.encrypted:
        client.invalidate_outbound_session(room.room_id)
    room.__dict__.update(fresh.__dict__)


def _state_stub(client: Client, room: MatrixRoom, stub: SlidingSyncStateStub) -> None:
    if stub.type == "m.room.member" and stub.state_key:
        if room.remove_member(stub.state_key):
            client._invalidate_session_for_member_event(room.room_id)
    elif stub.type == "m.space.parent":
        room.parents.discard(stub.state_key)
    elif stub.type == "m.space.child":
        room.children.discard(stub.state_key)
    elif stub.state_key == "":
        attributes = {
            "m.room.name": ("name", None),
            "m.room.canonical_alias": ("canonical_alias", None),
            "m.room.topic": ("topic", None),
            "m.room.avatar": ("room_avatar_url", None),
            "m.room.join_rules": ("join_rule", "invite"),
            "m.room.guest_access": ("guest_access", "forbidden"),
            "m.room.history_visibility": ("history_visibility", "shared"),
            "m.room.tombstone": ("replacement_room", None),
        }
        if stub.type in attributes:
            name, value = attributes[stub.type]
            setattr(room, name, value)
        elif stub.type == "m.room.power_levels":
            room.power_levels = PowerLevels()
            room.power_levels.creators = dict.fromkeys(room.creators, True)
            for user in room.users.values():
                user.power_level = room.power_levels.defaults.users_default


def _summary(room: MatrixRoom, info: SlidingSyncRoom, departed: set[str]) -> None:
    heroes = info.heroes
    if heroes is not None:
        for hero in heroes:
            if hero.user_id in departed:
                continue
            if hero.user_id not in room.users and (
                (info.joined_count or 0) + (info.invited_count or 0) > 1
            ):
                room.add_member(
                    hero.user_id,
                    hero.displayname,
                    hero.avatar_url,
                    invited=(info.joined_count or 0) <= 1,
                )
    if (
        heroes is not None
        or info.joined_count is not None
        or info.invited_count is not None
    ):
        room.update_summary(
            RoomSummary(
                info.invited_count,
                info.joined_count,
                None if heroes is None else [hero.user_id for hero in heroes],
            )
        )
    room.update_unread_notifications(
        UnreadNotifications(info.notification_count, info.highlight_count)
    )


def _room_state(
    client: Client,
    room: MatrixRoom,
    info: SlidingSyncRoom,
    encrypted_rooms: set[str],
    section: str,
    *,
    reset: bool,
    timeline_state: list[Event | BadEventType],
) -> Iterator[_SyncItem]:
    if reset:
        _reset_snapshot(client, room)
    for event in info.required_state:
        if isinstance(event, SlidingSyncStateStub):
            _state_stub(client, room, event)
        else:
            client._handle_joined_state_event(
                room.room_id, room, event, encrypted_rooms
            )
        source = (
            {"type": event.type, "state_key": event.state_key}
            if isinstance(event, SlidingSyncStateStub)
            else event.source
        )
        yield _SyncItem(event=event, room=room, section=section, source=source)
    departed = {
        event.state_key
        for event in (*info.required_state, *timeline_state)
        if (
            isinstance(event, RoomMemberEvent)
            and event.membership not in ("join", "invite")
        )
        or (isinstance(event, SlidingSyncStateStub) and event.type == "m.room.member")
    }
    _summary(room, info, departed)


def iter_sliding_sync(
    client: Client,
    response: SlidingSyncResponse,
    *,
    include_left: bool = False,
    recovered_rooms: Collection[str] = (),
    history_rooms: Collection[str] = (),
    suppress_ids: Mapping[str, Collection[str]] | None = None,
    history_prefixes: Mapping[str, int] | None = None,
) -> Iterator[_SyncItem]:
    """Apply protocol state incrementally, preserving event-time authorization.

    Proven recovery retains the existing projection through the unseen window
    tail. History only decrypts and emits observations; it never mutates state.
    Connection positions and durable acceptance belong to the caller.
    """
    yield from client._iter_to_device(response)
    encrypted_rooms: set[str] = set()
    for room_id, info in response.rooms.items():
        invited = info.membership == "invite" or (
            bool(info.stripped_state)
            and info.membership not in ("join", "leave", "ban")
        )
        if invited:
            invited_room = client._get_invited_room(room_id)
            yield _SyncItem(room=invited_room, section="invite")
            for invite_event in info.stripped_state:
                invited_room.handle_event(cast(Event | BadEventType, invite_event))
                yield _SyncItem("invite", invite_event, invited_room, "invite")
            continue
        leaving = info.membership in ("leave", "ban")
        if leaving and not include_left:
            client.rooms.pop(room_id, None)
            client.invited_rooms.pop(room_id, None)
            continue
        known = room_id in client.rooms
        room = client.rooms.setdefault(
            room_id,
            MatrixRoom(room_id, client.user_id, room_id in client.encrypted_rooms),
        )
        client.invited_rooms.pop(room_id, None)
        section = "leave" if leaving else "join"
        if not leaving:
            yield _SyncItem(room=room, section=section)
        live_start = len(info.timeline) - live_event_count(info)
        recovered = room_id in recovered_rooms
        history = room_id in history_rooms
        history_prefix = (history_prefixes or {}).get(room_id, 0)
        timeline_state = (
            []
            if history
            else (
                info.timeline[history_prefix:]
                if recovered
                else info.timeline[max(live_start, history_prefix) :]
            )
        )
        state_first = not known or (
            info.initial
            and not recovered
            and (history or live_start == len(info.timeline))
        )
        if state_first:
            yield from _room_state(
                client,
                room,
                info,
                encrypted_rooms,
                section,
                reset=known and info.initial,
                timeline_state=timeline_state,
            )
        for index, event in enumerate(info.timeline):
            if getattr(event, "event_id", None) in (suppress_ids or {}).get(
                room_id, ()
            ):
                continue
            provenance = timeline_provenance(
                info,
                index,
                history=history,
                recovered=recovered,
                history_prefix=history_prefix,
            )
            timeline = Timeline([event], info.limited, info.prev_batch)
            yield from client._iter_room_timeline(
                room_id,
                RoomInfo(timeline, [], [], []),
                room,
                encrypted_rooms,
                section,
                apply_state=provenance != TimelineEventProvenance.HISTORY,
                provenance=provenance,
            )
            info.timeline[index] = timeline.events[0]
            room = client.rooms.get(room_id, room)
        if not state_first:
            yield from _room_state(
                client,
                room,
                info,
                encrypted_rooms,
                section,
                reset=info.initial,
                timeline_state=timeline_state,
            )
        if room.encrypted and client.olm:
            client.olm.update_tracked_users(room)
        if leaving:
            yield _SyncItem(room=room, section="leave")
            client.rooms.pop(room_id, None)
            client.invited_rooms.pop(room_id, None)
    client.encrypted_rooms.update(encrypted_rooms)
    if encrypted_rooms and client.store:
        yield _SyncItem("encrypted_rooms", encrypted_rooms)

    for kind in ("receipts", "typing"):
        for room_id, raw in response.extensions.get(kind, {}).get("rooms", {}).items():
            ephemeral_room = client.rooms.get(room_id)
            if ephemeral_room is not None:
                ephemeral_event = EphemeralEvent.parse_event(raw)
                if ephemeral_event:
                    ephemeral_room.handle_ephemeral_event(ephemeral_event)
                    yield _SyncItem(
                        "ephemeral", ephemeral_event, ephemeral_room, "join"
                    )

    # Keep account data by its wire type until discovery supplies the room.
    for room_id, events in response.room_account_data.items():
        pending = client._pending_sliding_room_account_data.setdefault(room_id, {})
        raw = (
            response.extensions
            .get("account_data", {})
            .get("rooms", {})
            .get(room_id, [])
        )
        for index, account_event in enumerate(events):
            wire_type = (
                raw[index].get("type")
                if index < len(raw)
                else account_event.source.get("type")
            )
            if isinstance(wire_type, str):
                pending[wire_type] = account_event
    yield from client._iter_global_account_data_events(response)
    for room_id in list(client._pending_sliding_room_account_data):
        account_room = client.rooms.get(room_id)
        if account_room is None:
            continue
        pending = client._pending_sliding_room_account_data.pop(room_id)
        for pending_event in pending.values():
            account_room.handle_account_data(pending_event)
            yield _SyncItem("room_account_data", pending_event, account_room, "join")

    if client.olm:
        yield from client._iter_expired_verifications()
        client._handle_olm_events(response)
        yield from client._iter_key_requests()
