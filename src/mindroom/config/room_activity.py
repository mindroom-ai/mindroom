"""Opt-in note telling an agent which other threads of its room had activity since its last reply in a thread."""

from pydantic import BaseModel, ConfigDict, Field


class RoomActivityConfig(BaseModel):
    """Whether, and how far back, an agent's thread replies hear about activity in the room's other threads."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(
        default=False,
        description="Add a short note listing the room's other threads with new messages to the agent's thread replies",
    )
    window_minutes: int = Field(
        default=120,
        ge=1,
        le=1440,
        description="How far back the note looks, in minutes, and at most since the agent's last reply in the thread",
    )
    max_threads: int = Field(default=5, ge=1, le=20, description="Most threads one note lists, the most recent first")


class AgentRoomActivityConfig(BaseModel):
    """Per-agent override of ``defaults.room_activity``; each unset field keeps the default."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = Field(default=None, description="Per-agent override of room_activity.enabled")
    window_minutes: int | None = Field(
        default=None,
        ge=1,
        le=1440,
        description="Per-agent override of room_activity.window_minutes",
    )
    max_threads: int | None = Field(
        default=None,
        ge=1,
        le=20,
        description="Per-agent override of room_activity.max_threads",
    )
