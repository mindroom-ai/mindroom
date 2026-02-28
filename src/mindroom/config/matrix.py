"""Matrix-specific configuration models."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from mindroom.matrix.identity import managed_room_key_from_alias_localpart, room_alias_localpart

RoomAccessMode = Literal["single_user_private", "multi_user"]
MultiUserJoinRule = Literal["public", "knock"]
RoomJoinRule = Literal["invite", "public", "knock"]
RoomDirectoryVisibility = Literal["public", "private"]
MATRIX_LOCALPART_PATTERN = re.compile(r"^[a-z0-9._=/-]+$")


class MindRoomUserConfig(BaseModel):
    """Configuration for the internal MindRoom user account."""

    username: str = Field(
        default="mindroom_user",
        description="Matrix username localpart for the internal user account (without @ or domain); set before first startup",
    )
    display_name: str = Field(
        default="MindRoomUser",
        description="Display name for the internal user account",
    )

    @field_validator("username")
    @classmethod
    def validate_username(cls, username: str) -> str:
        """Validate and normalize Matrix localpart for the internal user."""
        normalized = username.strip().removeprefix("@")

        if not normalized:
            msg = "mindroom_user.username cannot be empty"
            raise ValueError(msg)

        if "@" in normalized:
            msg = "mindroom_user.username must contain at most one leading @"
            raise ValueError(msg)

        if ":" in normalized:
            msg = "mindroom_user.username must be a Matrix localpart (without domain)"
            raise ValueError(msg)

        if not MATRIX_LOCALPART_PATTERN.fullmatch(normalized):
            msg = (
                "mindroom_user.username contains invalid characters; "
                "allowed: lowercase letters, digits, '.', '_', '=', '-', '/'"
            )
            raise ValueError(msg)

        return normalized


class MatrixRoomAccessConfig(BaseModel):
    """Configuration for managed Matrix room access and discoverability."""

    mode: RoomAccessMode = Field(
        default="single_user_private",
        description=(
            "Room access mode. 'single_user_private' preserves invite-only/private behavior. "
            "'multi_user' applies configured join rules and directory visibility."
        ),
    )
    multi_user_join_rule: MultiUserJoinRule = Field(
        default="public",
        description="Default join rule for managed rooms in multi_user mode",
    )
    publish_to_room_directory: bool = Field(
        default=False,
        description="Whether managed rooms should be published to the room directory in multi_user mode",
    )
    invite_only_rooms: list[str] = Field(
        default_factory=list,
        description=("Managed room keys/aliases/IDs that must remain invite-only and private, even in multi_user mode"),
    )
    reconcile_existing_rooms: bool = Field(
        default=False,
        description=(
            "Whether to reconcile existing managed rooms to match current mode/join rule/directory settings "
            "on startup and config reload"
        ),
    )

    @field_validator("invite_only_rooms")
    @classmethod
    def validate_unique_invite_only_rooms(cls, invite_only_rooms: list[str]) -> list[str]:
        """Ensure each invite-only room identifier appears at most once."""
        if len(invite_only_rooms) != len(set(invite_only_rooms)):
            seen: set[str] = set()
            duplicates = {r for r in invite_only_rooms if r in seen or seen.add(r)}
            msg = f"Duplicate invite_only_rooms are not allowed: {', '.join(sorted(duplicates))}"
            raise ValueError(msg)
        return invite_only_rooms

    def is_multi_user_mode(self) -> bool:
        """Return whether multi-user room access mode is enabled."""
        return self.mode == "multi_user"

    def is_invite_only_room(
        self,
        room_key: str,
        room_id: str | None = None,
        room_alias: str | None = None,
    ) -> bool:
        """Check whether a managed room should remain invite-only."""
        identifiers = {room_key}
        if room_id:
            identifiers.add(room_id)
        if room_alias:
            identifiers.add(room_alias)
            localpart = room_alias_localpart(room_alias)
            if localpart:
                identifiers.add(localpart)
                managed_room_key = managed_room_key_from_alias_localpart(localpart)
                if managed_room_key:
                    identifiers.add(managed_room_key)
        return any(identifier in self.invite_only_rooms for identifier in identifiers)

    def get_target_join_rule(
        self,
        room_key: str,
        room_id: str | None = None,
        room_alias: str | None = None,
    ) -> RoomJoinRule | None:
        """Get the configured target join rule for a managed room."""
        if not self.is_multi_user_mode():
            return None
        if self.is_invite_only_room(room_key, room_id=room_id, room_alias=room_alias):
            return "invite"
        return self.multi_user_join_rule

    def get_target_directory_visibility(
        self,
        room_key: str,
        room_id: str | None = None,
        room_alias: str | None = None,
    ) -> RoomDirectoryVisibility | None:
        """Get the configured target room directory visibility for a managed room."""
        if not self.is_multi_user_mode():
            return None
        if self.is_invite_only_room(room_key, room_id=room_id, room_alias=room_alias):
            return "private"
        return "public" if self.publish_to_room_directory else "private"
