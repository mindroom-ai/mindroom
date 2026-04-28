"""Matrix-specific configuration models."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.constants import (
    is_runtime_database_url_env_name,
    resolve_config_relative_path,
    runtime_mindroom_namespace,
)
from mindroom.matrix_identifiers import managed_room_key_from_alias_localpart, room_alias_localpart

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths

_RoomAccessMode = Literal["single_user_private", "multi_user"]
_MultiUserJoinRule = Literal["public", "knock"]
RoomJoinRule = Literal["invite", "public", "knock"]
RoomDirectoryVisibility = Literal["public", "private"]
_MATRIX_LOCALPART_PATTERN = re.compile(r"^[a-z0-9._=/-]+$")


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

        if not _MATRIX_LOCALPART_PATTERN.fullmatch(normalized):
            msg = (
                "mindroom_user.username contains invalid characters; "
                "allowed: lowercase letters, digits, '.', '_', '=', '-', '/'"
            )
            raise ValueError(msg)

        return normalized


class MatrixSpaceConfig(BaseModel):
    """Configuration for the optional root Matrix Space."""

    enabled: bool = Field(
        default=True,
        description="Whether to create and maintain a root Matrix Space for managed MindRoom rooms",
    )
    name: str = Field(
        default="MindRoom",
        description="Display name for the root Matrix Space when enabled",
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, name: str) -> str:
        """Validate and normalize the root Space display name."""
        normalized = name.strip()
        if not normalized:
            msg = "matrix_space.name cannot be empty"
            raise ValueError(msg)
        return normalized


class MatrixRoomAccessConfig(BaseModel):
    """Configuration for managed Matrix room access and discoverability."""

    mode: _RoomAccessMode = Field(
        default="single_user_private",
        description=(
            "Room access mode. 'single_user_private' preserves invite-only/private behavior. "
            "'multi_user' applies configured join rules and directory visibility."
        ),
    )
    multi_user_join_rule: _MultiUserJoinRule = Field(
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
        runtime_paths: RuntimePaths,
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
                managed_room_key = managed_room_key_from_alias_localpart(localpart, runtime_paths)
                if managed_room_key:
                    identifiers.add(managed_room_key)
        return any(identifier in self.invite_only_rooms for identifier in identifiers)

    def get_target_join_rule(
        self,
        room_key: str,
        runtime_paths: RuntimePaths,
        room_id: str | None = None,
        room_alias: str | None = None,
    ) -> RoomJoinRule | None:
        """Get the configured target join rule for a managed room."""
        if not self.is_multi_user_mode():
            return None
        if self.is_invite_only_room(room_key, runtime_paths, room_id=room_id, room_alias=room_alias):
            return "invite"
        return self.multi_user_join_rule

    def get_target_directory_visibility(
        self,
        room_key: str,
        runtime_paths: RuntimePaths,
        room_id: str | None = None,
        room_alias: str | None = None,
    ) -> RoomDirectoryVisibility | None:
        """Get the configured target room directory visibility for a managed room."""
        if not self.is_multi_user_mode():
            return None
        if self.is_invite_only_room(room_key, runtime_paths, room_id=room_id, room_alias=room_alias):
            return "private"
        return "public" if self.publish_to_room_directory else "private"


class CacheConfig(BaseModel):
    """Startup configuration for the always-on Matrix event cache."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["sqlite", "postgres"] = Field(
        default="sqlite",
        description="Storage backend for the always-on Matrix event cache.",
    )
    db_path: str | None = Field(
        default=None,
        description=(
            "SQLite database path for the always-on Matrix event cache. "
            "Defaults to <storage>/event_cache.db when omitted. "
            "Changing this path requires a restart because hot reload intentionally keeps the active cache file."
        ),
    )
    database_url: str | None = Field(
        default=None,
        description=(
            "PostgreSQL connection URL for the always-on Matrix event cache. Prefer database_url_env for secrets."
        ),
    )
    database_url_env: str = Field(
        default="MINDROOM_EVENT_CACHE_DATABASE_URL",
        description=(
            "Runtime env var that contains the PostgreSQL event-cache connection URL. "
            "Must be DATABASE_URL or end with _DATABASE_URL so runtime secret filters withhold it."
        ),
    )
    namespace: str | None = Field(
        default=None,
        description=(
            "Logical namespace for PostgreSQL event-cache rows. "
            "Defaults to MINDROOM_NAMESPACE when set, otherwise 'default'."
        ),
    )

    @field_validator("database_url_env")
    @classmethod
    def validate_database_url_env(cls, env_name: str) -> str:
        """Require custom DSN env names to match runtime secret-filter conventions."""
        normalized = env_name.strip()
        if normalized and not is_runtime_database_url_env_name(normalized):
            msg = "cache.database_url_env must be DATABASE_URL or end with _DATABASE_URL"
            raise ValueError(msg)
        return normalized

    def resolve_db_path(self, runtime_paths: RuntimePaths) -> Path:
        """Resolve the configured database path for the active runtime startup."""
        if self.db_path is None:
            return runtime_paths.storage_root / "event_cache.db"
        return resolve_config_relative_path(self.db_path, runtime_paths)

    def resolve_postgres_database_url(self, runtime_paths: RuntimePaths) -> str:
        """Resolve the configured PostgreSQL connection URL for the active runtime."""
        configured_url = (self.database_url or "").strip()
        if configured_url:
            return configured_url
        env_name = self.database_url_env.strip()
        if env_name:
            env_url = (runtime_paths.env_value(env_name) or "").strip()
            if env_url:
                return env_url
        msg = (
            f"PostgreSQL event cache requires cache.database_url or {self.database_url_env} in the runtime environment"
        )
        raise ValueError(msg)

    def resolve_namespace(self, runtime_paths: RuntimePaths) -> str:
        """Resolve the logical cache namespace for shared PostgreSQL databases."""
        configured_namespace = (self.namespace or "").strip()
        if configured_namespace:
            return configured_namespace
        runtime_namespace = runtime_mindroom_namespace(runtime_paths)
        if runtime_namespace is not None:
            return runtime_namespace
        return "default"
