"""External trigger policy configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExternalTriggerPolicyConfig(BaseModel):
    """Global policy for tool-managed external trigger records."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Whether the external trigger API is enabled")
    default_replay_window_seconds: int = Field(
        default=300,
        ge=30,
        le=3600,
        description="Default accepted signature age for newly created triggers",
    )
    max_replay_window_seconds: int = Field(
        default=3600,
        ge=30,
        le=3600,
        description="Maximum accepted signature age any trigger may request",
    )
    default_max_body_bytes: int = Field(
        default=65536,
        ge=1024,
        le=262144,
        description="Default signed request body size limit for newly created triggers",
    )
    max_body_bytes: int = Field(
        default=262144,
        ge=1024,
        le=262144,
        description="Maximum signed request body size any trigger may request",
    )
    max_triggers_per_owner: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="Maximum trigger records one owner may create",
    )
    admin_users: list[str] = Field(
        default_factory=list,
        description="Matrix users allowed to manage external triggers for other owners",
    )

    @model_validator(mode="after")
    def validate_defaults_fit_caps(self) -> ExternalTriggerPolicyConfig:
        """Ensure defaults never exceed policy caps."""
        if self.default_replay_window_seconds > self.max_replay_window_seconds:
            msg = "default_replay_window_seconds must not exceed max_replay_window_seconds"
            raise ValueError(msg)
        if self.default_max_body_bytes > self.max_body_bytes:
            msg = "default_max_body_bytes must not exceed max_body_bytes"
            raise ValueError(msg)
        return self
