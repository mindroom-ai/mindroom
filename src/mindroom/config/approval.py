"""Tool approval configuration models."""

from __future__ import annotations

import math
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ApprovalAction = Literal["auto_approve", "require_approval"]
_ALLOWED_APPROVAL_ACTIONS = {"auto_approve", "require_approval"}


def _coerce_positive_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _validate_positive_timeout_days(timeout_days: float | None, *, field_name: str) -> None:
    if timeout_days is None:
        return
    if not math.isfinite(timeout_days) or timeout_days <= 0:
        msg = f"{field_name} must be a finite number greater than 0"
        raise ValueError(msg)


def _validate_tool_approval_default(tool_approval: dict[str, object]) -> None:
    default_action = tool_approval.get("default")
    if isinstance(default_action, str) and default_action not in _ALLOWED_APPROVAL_ACTIONS:
        msg = "tool_approval.default must be 'auto_approve' or 'require_approval'"
        raise ValueError(msg)

    timeout_days = _coerce_positive_float(tool_approval.get("timeout_days"))
    _validate_positive_timeout_days(timeout_days, field_name="tool_approval.timeout_days")


def _validate_tool_approval_rule(
    rule: dict[str, object],
    *,
    index: int,
) -> None:
    match_value = rule.get("match")
    if isinstance(match_value, str) and not match_value.strip():
        msg = f"tool_approval.rules[{index}].match must not be empty"
        raise ValueError(msg)

    action = rule.get("action")
    script = rule.get("script")
    action_is_set = action is not None
    script_is_set = script is not None
    if action_is_set == script_is_set:
        msg = f"tool_approval.rules[{index}] must set exactly one of action or script"
        raise ValueError(msg)
    if isinstance(action, str) and action not in _ALLOWED_APPROVAL_ACTIONS:
        msg = f"tool_approval.rules[{index}].action must be 'auto_approve' or 'require_approval'"
        raise ValueError(msg)
    if isinstance(script, str) and not script.strip():
        msg = f"tool_approval.rules[{index}].script must not be empty"
        raise ValueError(msg)

    rule_timeout_days = _coerce_positive_float(rule.get("timeout_days"))
    _validate_positive_timeout_days(
        rule_timeout_days,
        field_name=f"tool_approval.rules[{index}].timeout_days",
    )


def validate_raw_tool_approval_config(data: dict[str, object]) -> None:
    """Validate raw tool-approval input before Pydantic builds nested models."""
    raw_tool_approval = data.get("tool_approval")
    if raw_tool_approval is None or not isinstance(raw_tool_approval, dict):
        return

    tool_approval = cast("dict[str, object]", raw_tool_approval)
    _validate_tool_approval_default(tool_approval)

    rules = tool_approval.get("rules")
    if not isinstance(rules, list):
        return

    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            continue
        _validate_tool_approval_rule(cast("dict[str, object]", rule), index=index)


class ApprovalRuleConfig(BaseModel):
    """One ordered tool-approval rule."""

    model_config = ConfigDict(extra="forbid")

    match: str
    action: ApprovalAction | None = None
    script: str | None = None
    timeout_days: float | None = None

    @field_validator("match")
    @classmethod
    def validate_match(cls, value: str) -> str:
        """Reject blank match globs for direct model construction too."""
        if not value.strip():
            msg = "tool_approval.rules[].match must not be empty"
            raise ValueError(msg)
        return value

    @field_validator("action", mode="before")
    @classmethod
    def validate_action(cls, value: object) -> object:
        """Keep invalid action values on the model path aligned with config validation."""
        if isinstance(value, str) and value not in _ALLOWED_APPROVAL_ACTIONS:
            msg = "tool_approval.rules[].action must be 'auto_approve' or 'require_approval'"
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
    def normalize_timeout_days(cls, value: object) -> object:
        """Preserve numeric-string support while rejecting bools up front."""
        if value is None:
            return None
        timeout_days = _coerce_positive_float(value)
        if timeout_days is None:
            if isinstance(value, bool):
                msg = "tool_approval.rules[].timeout_days must be a finite number greater than 0"
                raise ValueError(msg)
            return value
        return timeout_days

    @field_validator("timeout_days")
    @classmethod
    def validate_timeout_days(cls, value: float | None) -> float | None:
        """Reject non-finite and non-positive rule overrides."""
        _validate_positive_timeout_days(value, field_name="tool_approval.rules[].timeout_days")
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

    default: ApprovalAction = Field(default="auto_approve")
    timeout_days: float = Field(default=7.0)
    rules: list[ApprovalRuleConfig] = Field(default_factory=list)

    @field_validator("default", mode="before")
    @classmethod
    def validate_default(cls, value: object) -> object:
        """Keep top-level action errors consistent across construction paths."""
        if isinstance(value, str) and value not in _ALLOWED_APPROVAL_ACTIONS:
            msg = "tool_approval.default must be 'auto_approve' or 'require_approval'"
            raise ValueError(msg)
        return value

    @field_validator("timeout_days", mode="before")
    @classmethod
    def normalize_timeout_days(cls, value: object) -> object:
        """Preserve numeric-string support while rejecting bools up front."""
        if value is None:
            return value
        timeout_days = _coerce_positive_float(value)
        if timeout_days is None:
            if isinstance(value, bool):
                msg = "tool_approval.timeout_days must be a finite number greater than 0"
                raise ValueError(msg)
            return value
        return timeout_days

    @field_validator("timeout_days")
    @classmethod
    def validate_timeout_days(cls, value: float) -> float:
        """Reject non-finite and non-positive approval timeouts."""
        _validate_positive_timeout_days(value, field_name="tool_approval.timeout_days")
        return value
