"""Plugin configuration models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from mindroom.config.validation import non_empty_stripped


class HookOverrideConfig(BaseModel):
    """Per-hook deployer override configuration."""

    enabled: bool = Field(default=True, description="Run this hook; false disables it without removing the plugin")
    priority: int | None = Field(default=None, description="Override the hook's default execution priority")
    timeout_ms: int | None = Field(default=None, description="Override the hook's default timeout in milliseconds")


class PluginEntryConfig(BaseModel):
    """Normalized plugin entry from the root config."""

    path: str = Field(description="Plugin directory, config-relative path, or Python package spec")
    enabled: bool = Field(default=True, description="Load the plugin; false disables it without removing the entry")
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Free-form settings passed to the plugin at load time",
    )
    hooks: dict[str, HookOverrideConfig] = Field(
        default_factory=dict,
        description="Per-hook overrides keyed by hook function name",
    )

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        """Reject empty plugin paths after trimming whitespace."""
        return non_empty_stripped(value, field_name="Plugin path")
