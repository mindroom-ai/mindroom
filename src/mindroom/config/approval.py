"""Tool approval configuration models."""

from __future__ import annotations

import json
import math
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from collections.abc import Mapping

_ApprovalAction = Literal["auto_approve", "require_approval"]
_MAX_TIMEOUT_DAYS = 36500.0
_TimeoutDays = Annotated[float, Field(gt=0, le=_MAX_TIMEOUT_DAYS, allow_inf_nan=False)]


def _validate_pattern_list(value: str | list[str], field_name: str) -> str | list[str]:
    patterns = (value,) if isinstance(value, str) else value
    if not patterns or any(not pattern.strip() for pattern in patterns):
        msg = f"tool_approval.rules[].{field_name} must not be empty"
        raise ValueError(msg)
    return value


def _is_valid_argument_scalar(value: object) -> bool:
    return value is None or isinstance(value, str | bool | int) or (isinstance(value, float) and math.isfinite(value))


def _argument_scalar_matches(actual: object, expected: object) -> bool:
    if expected is None:
        return actual is None
    if isinstance(expected, bool):
        return type(actual) is bool and actual is expected
    if isinstance(expected, str):
        return isinstance(actual, str) and fnmatchcase(actual, expected)
    if isinstance(expected, int | float):
        return isinstance(actual, int | float) and not isinstance(actual, bool) and actual == expected
    return False


def _canonical_arguments_json(arguments: Mapping[str, Any] | None) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"))


class ApprovalRuleConfig(BaseModel):
    """One ordered tool-approval rule."""

    model_config = ConfigDict(extra="forbid")
    __hash__ = None  # type: ignore[assignment]

    match: str | list[str] = Field(
        description=(
            "Case-sensitive glob or non-empty list of globs over exposed tool function names; "
            "the first matching rule wins"
        ),
    )
    agents: str | list[str] | None = Field(
        default=None,
        description="Optional case-sensitive glob or non-empty list of globs over agent names; unset matches every agent",
    )
    arguments: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional mapping of top-level argument names to scalar matchers or non-empty lists of matchers; "
            "every listed key must match"
        ),
    )
    action: _ApprovalAction | None = Field(
        default=None,
        description="Fixed decision for matching calls; set exactly one of action or script",
    )
    script: str | None = Field(
        default=None,
        description=(
            "Config-relative Python script defining check(tool_name, arguments, agent_name) -> bool; "
            "True requires approval"
        ),
    )
    timeout_days: _TimeoutDays | None = Field(
        default=None,
        description="Approval expiry window in days for this rule; unset uses tool_approval.timeout_days",
    )

    @field_validator("match")
    @classmethod
    def validate_match(cls, value: str | list[str]) -> str | list[str]:
        """Reject blank match globs or empty lists for direct model construction too."""
        return _validate_pattern_list(value, "match")

    @field_validator("agents")
    @classmethod
    def validate_agents(cls, value: str | list[str] | None) -> str | list[str] | None:
        """Reject blank agent globs or empty lists for direct model construction too."""
        return None if value is None else _validate_pattern_list(value, "agents")

    @field_validator("arguments")
    @classmethod
    def validate_arguments(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Accept only non-empty mappings of non-blank keys to valid scalar or non-empty scalar-list matchers."""
        if value is None:
            return None
        if not value:
            msg = "tool_approval.rules[].arguments must not be empty"
            raise ValueError(msg)
        for key, expected in value.items():
            if not key.strip():
                msg = "tool_approval.rules[].arguments keys must not be empty"
                raise ValueError(msg)
            valid = (
                bool(expected) and all(_is_valid_argument_scalar(item) for item in expected)
                if isinstance(expected, list)
                else _is_valid_argument_scalar(expected)
            )
            if not valid:
                msg = (
                    f"tool_approval.rules[].arguments[{key!r}] must be a string, boolean, "
                    "integer, finite float, null, or a non-empty list of those values"
                )
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

    def match_patterns(self) -> tuple[str, ...]:
        """Return the authored tool-name glob patterns as a tuple."""
        return (self.match,) if isinstance(self.match, str) else tuple(self.match)

    def agent_patterns(self) -> tuple[str, ...] | None:
        """Return the authored agent-name glob patterns as a tuple, or None when unrestricted."""
        if self.agents is None:
            return None
        return (self.agents,) if isinstance(self.agents, str) else tuple(self.agents)

    def matches_tool(self, tool_name: str) -> bool:
        """Return whether ``tool_name`` matches any authored ``match`` pattern."""
        return any(fnmatchcase(tool_name, pattern) for pattern in self.match_patterns())

    def matches_agent(self, agent_name: str) -> bool:
        """Return whether ``agent_name`` matches this rule's ``agents`` filter."""
        patterns = self.agent_patterns()
        return patterns is None or any(fnmatchcase(agent_name, pattern) for pattern in patterns)

    def matches_arguments(self, arguments: Mapping[str, Any]) -> bool:
        """Return whether ``arguments`` matches every authored ``arguments`` key matcher."""
        if self.arguments is None:
            return True
        for key, expected in self.arguments.items():
            if key not in arguments:
                return False
            actual = arguments[key]
            matched = (
                any(_argument_scalar_matches(actual, item) for item in expected)
                if isinstance(expected, list)
                else _argument_scalar_matches(actual, expected)
            )
            if not matched:
                return False
        return True

    def __eq__(self, other: object) -> bool:
        """Compare rules with type-sensitive argument equality so ``True`` never equals ``1``."""
        if not isinstance(other, ApprovalRuleConfig):
            return NotImplemented
        return super().__eq__(other) and _canonical_arguments_json(self.arguments) == _canonical_arguments_json(
            other.arguments,
        )


class ToolApprovalConfig(BaseModel):
    """Top-level tool-approval settings."""

    model_config = ConfigDict(extra="forbid")

    default: _ApprovalAction = Field(default="auto_approve", description="Decision for tool calls no rule matches")
    timeout_days: _TimeoutDays = Field(default=7.0, description="Default approval expiry window in days")
    rules: list[ApprovalRuleConfig] = Field(
        default_factory=list,
        description="Ordered approval rules; the first matching rule wins",
    )
    scheduled_any_arguments: bool = Field(
        default=True,
        description=(
            "Let a requester approve a scheduled tool call for any arguments to the same tool, "
            "not only the exact arguments it was scheduled with"
        ),
    )

    @field_validator("timeout_days", mode="before")
    @classmethod
    def reject_bool_timeout_days(cls, value: object) -> object:
        """Reject bools before Pydantic coerces them as numbers."""
        if isinstance(value, bool):
            msg = "timeout_days must be a number, not a boolean"
            raise ValueError(msg)  # noqa: TRY004 - keep Pydantic validation errors structured
        return value
