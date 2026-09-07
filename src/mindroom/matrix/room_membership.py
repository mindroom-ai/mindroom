"""Application membership snapshots that never mutate nio's owned projection."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from weakref import WeakKeyDictionary

import nio
from nio.durable import RecordKind

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable

    from nio.durable import SyncRecord

logger = get_logger(__name__)

type _ProjectedMembers = tuple[tuple[str, str | None, str | None, bool], ...]


@dataclass(frozen=True, slots=True)
class _RoomMembershipSnapshot:
    """One authoritative application lookup beside the projection it supplemented."""

    projected_members: _ProjectedMembers
    joined_user_ids: frozenset[str]
    display_names: tuple[tuple[str, str], ...]


@dataclass(slots=True)
class _MembershipLookup:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    revision: int = 0
    snapshot: _RoomMembershipSnapshot | None = None


# Room identity scopes both the account and its current membership lifetime.
# Values must not retain the weak key: nio resets and discarded clients release it.
_lookups: WeakKeyDictionary[nio.MatrixRoom, _MembershipLookup] = WeakKeyDictionary()


def _projected_members(room: nio.MatrixRoom) -> _ProjectedMembers:
    return tuple((user_id, user.display_name, user.avatar_url, user.invited) for user_id, user in room.users.items())


def room_membership_snapshot(room: nio.MatrixRoom) -> _RoomMembershipSnapshot | None:
    """Return an application lookup only while its source projection is unchanged."""
    lookup = _lookups.get(room)
    if room.members_synced or lookup is None or lookup.snapshot is None:
        return None
    snapshot = lookup.snapshot
    return snapshot if snapshot.projected_members == _projected_members(room) else None


def room_membership_is_complete(room: nio.MatrixRoom) -> bool:
    """Return whether application decisions have a complete membership snapshot."""
    return room.members_synced or room_membership_snapshot(room) is not None


def cached_joined_member_ids(room: nio.MatrixRoom) -> frozenset[str]:
    """Return application joined members, falling back to nio's partial projection."""
    snapshot = room_membership_snapshot(room)
    if snapshot is not None:
        return snapshot.joined_user_ids
    return frozenset(room.users).difference(room.invited_users)


def cached_member_ids(room: nio.MatrixRoom) -> frozenset[str]:
    """Include invited members when checking who can participate in a room."""
    return cached_joined_member_ids(room).union(room.invited_users)


def invalidate_membership_lookups(records: Iterable[SyncRecord], *, account_id: str) -> None:
    """Drop lookup results before admitting membership changes or unknown history gaps."""
    room_ids = {
        record.room_id
        for record in records
        if record.membership is not None
        or record.kind is RecordKind.LOSS
        or record.source.get("type") == "m.room.member"
    }
    if not room_ids:
        return
    for room, lookup in tuple(_lookups.items()):
        if room.own_user_id == account_id and room.room_id in room_ids:
            lookup.snapshot = None
            lookup.revision += 1


async def ensure_room_membership_synced(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    *,
    sender_id: str,
) -> bool:
    """Reuse or fetch application membership without certifying nio's crypto projection."""
    if room_membership_is_complete(room):
        return True
    lookup = _lookups.setdefault(room, _MembershipLookup())
    async with lookup.lock:
        if room_membership_is_complete(room):
            return True
        projected_members = _projected_members(room)
        revision = lookup.revision
        try:
            response = await client.joined_members(room.room_id)
        except Exception as exc:
            logger.warning(
                "authoritative_room_membership_fetch_failed",
                room_id=room.room_id,
                sender_id=sender_id,
                error=str(exc),
            )
            return False
        if not isinstance(response, nio.JoinedMembersResponse):
            logger.warning(
                "authoritative_room_membership_fetch_failed",
                room_id=room.room_id,
                sender_id=sender_id,
                error=str(response),
            )
            return False
        # An ordinary nio client can complete its own projection during lookup.
        if not room.members_synced:
            if revision != lookup.revision or projected_members != _projected_members(room):
                return False
            lookup.snapshot = _RoomMembershipSnapshot(
                projected_members=projected_members,
                joined_user_ids=frozenset(member.user_id for member in response.members),
                display_names=tuple(
                    (member.user_id, member.display_name) for member in response.members if member.display_name
                ),
            )
        logger.info(
            "authoritative_room_membership_refreshed",
            room_id=room.room_id,
            sender_id=sender_id,
            cached_member_count=len(projected_members),
            refreshed_member_count=len(response.members),
        )
        return True
