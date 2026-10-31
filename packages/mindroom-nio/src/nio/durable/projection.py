"""Compact live room metadata and individually addressable member rows."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from ..events import DefaultLevels, PowerLevels
from ..responses import RoomSummary
from ..rooms import MatrixInvitedRoom, MatrixRoom

_ROOM_FIELDS = (
    "own_user_id",
    "federate",
    "room_version",
    "room_type",
    "guest_access",
    "join_rule",
    "history_visibility",
    "canonical_alias",
    "topic",
    "name",
    "encrypted",
    "room_avatar_url",
    "fully_read_marker",
    "tags",
    "unread_notifications",
    "unread_highlights",
    "replacement_room",
)


def encode_room(
    room: MatrixRoom,
    *,
    membership: str | None = None,
    membership_epoch: int = 0,
    members_complete: bool = False,
) -> dict[str, Any]:
    """Snapshot metadata without walking users; caller persists member deltas.

    Set members_complete only when the caller has persisted the full member
    list. The live room's flag alone does not prove that disk has those rows.
    """
    metadata = {field: deepcopy(getattr(room, field)) for field in _ROOM_FIELDS}
    metadata.update(
        invited=isinstance(room, MatrixInvitedRoom),
        membership=membership,
        membership_epoch=membership_epoch,
        members_complete=members_complete and room.members_synced,
        member_count=len(room.users),
        parents=sorted(room.parents),
        children=sorted(room.children),
        creators=sorted(room.creators),
        power_levels=asdict(room.power_levels),
        summary=asdict(room.summary) if room.summary else None,
    )
    if isinstance(room, MatrixInvitedRoom):
        metadata["inviter"] = room.inviter
    return metadata


def encode_member(room: MatrixRoom, user_id: str) -> dict[str, Any] | None:
    """Snapshot one changed member, or return a deletion after leave/ban."""
    user = room.users.get(user_id)
    if user is None:
        return None
    return {
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "invited": user.invited,
    }


def restore_room(
    room_id: str,
    metadata: Mapping[str, Any],
    members: Mapping[str, Mapping[str, Any]],
) -> MatrixRoom:
    """Restore normal room behavior and derive indexes through add_member."""
    cls = MatrixInvitedRoom if metadata.get("invited") else MatrixRoom
    room = cls(room_id, metadata["own_user_id"])
    for field in _ROOM_FIELDS:
        if field in metadata:
            setattr(room, field, deepcopy(metadata[field]))
    for field in ("parents", "children", "creators"):
        setattr(room, field, set(metadata.get(field, ())))
    levels = metadata.get("power_levels", {})
    room.power_levels = PowerLevels(
        DefaultLevels(**deepcopy(levels.get("defaults", {}))),
        deepcopy(levels.get("users", {})),
        deepcopy(levels.get("events", {})),
        deepcopy(levels.get("creators", dict.fromkeys(room.creators, True))),
    )
    if metadata.get("summary") is not None:
        room.summary = RoomSummary(**deepcopy(metadata["summary"]))
    if isinstance(room, MatrixInvitedRoom):
        room.inviter = metadata.get("inviter")
    for user_id, member in members.items():
        room.add_member(
            user_id,
            member.get("display_name"),
            member.get("avatar_url"),
            member.get("invited", False),
        )
    room.members_synced = metadata.get("members_complete") is True and metadata.get(
        "member_count"
    ) == len(members)
    return room
