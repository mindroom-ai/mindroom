"""Matrix-specific configuration models."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.runtime_env_policy import is_runtime_database_url_env_name

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

_MatrixSyncMode = Literal["sliding", "classic"]
RoomJoinRule = Literal["invite", "public", "knock"]
RoomDirectoryVisibility = Literal["public", "private"]
_MATRIX_LOCALPART_PATTERN = re.compile(r"^[a-z0-9._=/-]+$")


class MatrixSyncConfig(BaseModel):
    """Configuration for Matrix event sync transport."""

    model_config = ConfigDict(extra="forbid")

    mode: _MatrixSyncMode = Field(
        default="classic",
        description=(
            "Matrix sync transport. 'classic' uses /v3/sync and 'sliding' opts into MSC4186 Simplified Sliding"
            " Sync, which requires a homeserver advertising org.matrix.simplified_msc3575."
        ),
    )
    sliding_timeline_limit: int = Field(
        default=100,
        ge=1,
        description=(
            "Timeline event limit for each room requested through Simplified Sliding Sync. Sliding positions are"
            " connection-scoped, so this also bounds how many per-room events a restarted connection can replay."
        ),
    )


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


class EventJournalConfig(BaseModel):
    """Where this runtime's durable event journal lives."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["sqlite", "postgres"] = Field(
        default="sqlite",
        description="Storage backend for the durable Matrix event journal.",
    )
    database_url: str | None = Field(
        default=None,
        description=(
            "PostgreSQL connection URL for the durable Matrix event journal. Prefer database_url_env for secrets."
        ),
    )
    database_url_env: str = Field(
        default="MINDROOM_EVENT_CACHE_DATABASE_URL",
        description=(
            "Runtime env var that contains the PostgreSQL event-journal connection URL. "
            "Must be DATABASE_URL or end with _DATABASE_URL so runtime secret filters withhold it."
        ),
    )

    @field_validator("database_url_env")
    @classmethod
    def validate_database_url_env(cls, env_name: str) -> str:
        """Require custom DSN env names to match runtime secret-filter conventions."""
        normalized = env_name.strip()
        if normalized and not is_runtime_database_url_env_name(normalized):
            msg = "event_journal.database_url_env must be DATABASE_URL or end with _DATABASE_URL"
            raise ValueError(msg)
        return normalized

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
            "PostgreSQL event journal requires event_journal.database_url or "
            f"{self.database_url_env} in the runtime environment"
        )
        raise ValueError(msg)
