"""Plugin configuration models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator


class HookOverrideConfig(BaseModel):
    """Per-hook deployer override configuration."""

    enabled: bool = True
    priority: int | None = None
    timeout_ms: int | None = None


class PluginEntryConfig(BaseModel):
    """Normalized plugin entry from the root config."""

    path: str
    enabled: bool = True
    settings: dict[str, Any] = Field(default_factory=dict)
    hooks: dict[str, HookOverrideConfig] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Reject empty plugin paths after trimming whitespace."""
        normalized = value.strip()
        if not normalized:
            msg = "Plugin path must not be empty"
            raise ValueError(msg)
        return normalized
