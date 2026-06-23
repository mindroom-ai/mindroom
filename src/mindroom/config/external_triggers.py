"""External trigger configuration models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _non_empty_stripped(value: str, *, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        msg = f"{field_name} must not be empty"
        raise ValueError(msg)
    return stripped


class ExternalTriggerTargetConfig(BaseModel):
    """Destination for one externally signed trigger."""

    model_config = ConfigDict(extra="forbid")

    room_id: str = Field(description="Matrix room ID that receives the trigger message")
    thread_id: str | None = Field(default=None, description="Optional Matrix thread ID to append to")
    agent: str = Field(description="Agent or team name that should handle this trigger")
    new_thread: bool = Field(default=False, description="Whether the trigger should start a new thread")

    @field_validator("room_id")
    @classmethod
    def validate_room_id(cls, value: str) -> str:
        """Reject empty Matrix room IDs."""
        return _non_empty_stripped(value, field_name="room_id")

    @field_validator("agent")
    @classmethod
    def validate_agent(cls, value: str) -> str:
        """Reject empty agent or team targets."""
        return _non_empty_stripped(value, field_name="agent")


class ExternalTriggerConfig(BaseModel):
    """Configuration for one externally signed trigger ingress."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Whether this external trigger endpoint is active")
    description: str = Field(default="", description="Human-readable trigger purpose")
    auth: Literal["ed25519"] = Field(default="ed25519", description="External trigger signature scheme")
    key_id: str = Field(default="default", description="Key identifier expected in trigger signatures")
    public_key: str = Field(description="Base64-encoded Ed25519 public key")
    target: ExternalTriggerTargetConfig = Field(description="Destination for accepted trigger messages")
    allowed_kinds: tuple[str, ...] = Field(default=(), description="Allowed trigger kind values")
    replay_window_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Maximum accepted signature timestamp skew in seconds",
    )
    max_body_bytes: int = Field(
        default=65536,
        ge=1024,
        le=262144,
        description="Maximum signed trigger body size in bytes",
    )

    @field_validator("public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        """Reject empty public keys."""
        return _non_empty_stripped(value, field_name="public_key")

    @field_validator("key_id")
    @classmethod
    def validate_key_id(cls, value: str) -> str:
        """Reject empty key identifiers."""
        return _non_empty_stripped(value, field_name="key_id")
