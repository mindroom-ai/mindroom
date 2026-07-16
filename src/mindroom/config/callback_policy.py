"""Callback policy configuration."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CallbackPolicyConfig(BaseModel):
    """Global policy for tool-minted one-shot agent completion callbacks."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=True, description="Whether the callback API and mint_callback tool are enabled")
    default_ttl_seconds: int = Field(
        default=86400,
        ge=60,
        le=604800,
        description="Default lifetime for newly minted callbacks",
    )
    max_ttl_seconds: int = Field(
        default=604800,
        ge=60,
        le=604800,
        description="Maximum lifetime any callback may request",
    )
    max_uses_cap: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="Maximum number of uses any callback may request",
    )
    max_active_per_owner: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="Maximum live callback records one owner may hold",
    )
    max_body_bytes: int = Field(
        default=65536,
        ge=1024,
        le=262144,
        description="Maximum callback request body size",
    )

    @model_validator(mode="after")
    def validate_defaults_fit_caps(self) -> CallbackPolicyConfig:
        """Ensure defaults never exceed policy caps."""
        if self.default_ttl_seconds > self.max_ttl_seconds:
            msg = "default_ttl_seconds must not exceed max_ttl_seconds"
            raise ValueError(msg)
        return self
