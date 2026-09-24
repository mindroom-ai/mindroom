"""Strict bounded JSON models for the turn-owned agent CLI."""

from __future__ import annotations

from typing import Annotated, Literal, cast
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, JsonValue, TypeAdapter, model_validator

from mindroom.agent_cli.json_io import canonical_json as _canonical_json

_SELECTOR = Annotated[str, Field(min_length=1, max_length=128)]
# Strict models still accept the canonical JSON UUID string.
_WIRE_UUID = Annotated[UUID, BeforeValidator(lambda value: UUID(value) if isinstance(value, str) else value)]


class _Operation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    @model_validator(mode="after")
    def _validate_bounded_json(self) -> _Operation:
        _canonical_json(self.model_dump(mode="json"))
        return self


class ToolListOperation(_Operation):
    """List lightweight metadata with explicit pagination."""

    operation: Literal["tools.list"]
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=100, ge=1, le=1000)


class ToolSearchOperation(_Operation):
    """Search lightweight tool metadata."""

    operation: Literal["tools.search"]
    query: str = Field(min_length=1, max_length=1024)
    toolkit: _SELECTOR | None = None
    limit: int = Field(default=20, ge=1, le=100)


class ToolDescribeOperation(_Operation):
    """Load one exact toolkit/function descriptor."""

    operation: Literal["tools.describe"]
    toolkit: _SELECTOR
    function: _SELECTOR


class ToolCallOperation(_Operation):
    """Claim one exact tool invocation with caller-generated identity."""

    operation: Literal["tools.call"]
    call_id: _WIRE_UUID
    toolkit: _SELECTOR
    function: _SELECTOR
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @property
    def canonical_arguments_json(self) -> str:
        """Return the exact JSON representation retained by the live response owner."""
        return _canonical_json(self.arguments)


class ContextListOperation(_Operation):
    """List readable context resources with explicit pagination."""

    operation: Literal["context.list"]
    cursor: str | None = Field(default=None, max_length=1024)
    limit: int = Field(default=100, ge=1, le=1000)


class ContextReadOperation(_Operation):
    """Read one bounded context-resource range."""

    operation: Literal["context.read"]
    name: _SELECTOR
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8000, ge=1, le=65536)


class ToolCallReceipt(_Operation):
    """Bounded caller-safe projection of one live response-owned call."""

    call_id: _WIRE_UUID
    toolkit: _SELECTOR
    function: _SELECTOR
    status: Literal["queued", "running", "waiting", "completed", "failed", "cancelled"]
    parent_bash_call_id: str | None = Field(default=None, max_length=128)
    outcome: JsonValue = None
    attachments: list[dict[str, JsonValue]] = Field(default_factory=list)


AgentCliOperation = Annotated[
    ToolListOperation
    | ToolSearchOperation
    | ToolDescribeOperation
    | ToolCallOperation
    | ContextListOperation
    | ContextReadOperation,
    Field(discriminator="operation"),
]
_OPERATION_ADAPTER = TypeAdapter(AgentCliOperation)


def parse_operation(payload: object) -> AgentCliOperation:
    """Validate one untrusted operation without adding caller identity."""
    return cast("AgentCliOperation", _OPERATION_ADAPTER.validate_python(payload))
