"""Tool approval configuration models."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ApprovalAction = Literal["auto_approve", "require_approval"]
_MAX_TIMEOUT_DAYS = 36500.0
_TimeoutDays = Annotated[float, Field(gt=0, le=_MAX_TIMEOUT_DAYS, allow_inf_nan=False)]


class ApprovalRuleConfig(BaseModel):
    """One ordered tool-approval rule."""

    model_config = ConfigDict(extra="forbid")

    match: str
    action: _ApprovalAction | None = None
    script: str | None = None
    timeout_days: _TimeoutDays | None = None

    @field_validator("match")
    @classmethod
    def validate_match(cls, value: str) -> str:
        """Reject blank match globs for direct model construction too."""
        if not value.strip():
            msg = "tool_approval.rules[].match must not be empty"
            raise ValueError(msg)
        return value

    @field_validator("script")
    @classmethod
    def validate_script(cls, value: str | None) -> str | None:
        """Reject blank script paths for direct model construction too."""
        if value is not None and not value.strip():
            msg = "tool_approval.rules[].script must not be empty"
            raise ValueError(msg)
        return value

    @field_validator("timeout_days", mode="before")
    @classmethod
    def reject_bool_timeout_days(cls, value: object) -> object:
        """Reject bools before Pydantic coerces them as numbers."""
        if isinstance(value, bool):
            msg = "timeout_days must be a number, not a boolean"
            raise ValueError(msg)  # noqa: TRY004 - keep Pydantic validation errors structured
        return value

    @model_validator(mode="after")
    def validate_action_or_script(self) -> ApprovalRuleConfig:
        """Require exactly one approval action source."""
        action_is_set = self.action is not None
        script_is_set = self.script is not None
        if action_is_set == script_is_set:
            msg = "tool_approval.rules[] must set exactly one of action or script"
            raise ValueError(msg)
        return self


class ToolApprovalConfig(BaseModel):
    """Top-level tool-approval settings."""

    model_config = ConfigDict(extra="forbid")

    default: _ApprovalAction = Field(default="auto_approve")
    timeout_days: _TimeoutDays = Field(default=7.0)
    rules: list[ApprovalRuleConfig] = Field(default_factory=list)

    @field_validator("timeout_days", mode="before")
    @classmethod
    def reject_bool_timeout_days(cls, value: object) -> object:
        """Reject bools before Pydantic coerces them as numbers."""
        if isinstance(value, bool):
            msg = "timeout_days must be a number, not a boolean"
            raise ValueError(msg)  # noqa: TRY004 - keep Pydantic validation errors structured
        return value
