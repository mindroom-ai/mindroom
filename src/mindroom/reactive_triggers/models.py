"""Pydantic data models for reactive triggers."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from uuid import uuid4

import regex
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

TRIGGER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SourceType = Literal["room_message", "thread_tag", "external_event"]
FiringStatus = Literal["pending", "triaged_no", "claimed", "delivered", "failed", "skipped"]
DisabledReason = Literal["manual", "once_consumed"]


def generate_trigger_id() -> str:
    """Return a newly generated route-safe reactive trigger identifier."""
    return f"tr_{uuid4().hex[:12]}"


def _strip_non_empty(value: str, *, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        msg = f"{field_name} must be a non-empty string"
        raise ValueError(msg)
    return stripped


def _strip_optional_non_empty(value: str | None, *, field_name: str) -> str | None:
    if value is None:
        return None
    return _strip_non_empty(value, field_name=field_name)


class RoomMessageSourceSpec(BaseModel):
    """Watch new visible messages in one resolved Matrix room."""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["room_message"] = "room_message"
    room_id: str
    thread_id: str | None = None
    senders: list[str] = Field(default_factory=list)
    pattern: str | None = Field(default=None, max_length=256)
    include_bots: bool = False

    @field_validator("room_id")
    @classmethod
    def validate_room_id(cls, value: str) -> str:
        """Require a non-empty resolved room ID."""
        return _strip_non_empty(value, field_name="room_id")

    @field_validator("thread_id")
    @classmethod
    def validate_thread_id(cls, value: str | None) -> str | None:
        """Reject empty thread IDs."""
        return _strip_optional_non_empty(value, field_name="thread_id")

    @field_validator("senders")
    @classmethod
    def validate_senders(cls, values: list[str]) -> list[str]:
        """Reject empty sender entries."""
        return [_strip_non_empty(sender, field_name="senders entry") for sender in values]

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str | None) -> str | None:
        """Validate that an optional regex pattern is non-empty and compiles."""
        if value is None:
            return None
        if not value:
            msg = "pattern must not be empty"
            raise ValueError(msg)
        try:
            regex.compile(value, regex.IGNORECASE)
        except regex.error as exc:
            msg = f"Invalid regular expression pattern: {exc}"
            raise ValueError(msg) from exc
        return value


class ThreadTagSourceSpec(BaseModel):
    """Watch a Matrix thread tag state transition."""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["thread_tag"] = "thread_tag"
    room_id: str
    thread_id: str
    tag: str = "resolved"
    transition: Literal["added", "removed"] = "added"

    @field_validator("room_id", "thread_id", "tag")
    @classmethod
    def validate_non_empty_fields(cls, value: str, info: ValidationInfo) -> str:
        """Require non-empty room_id, thread_id, and tag values."""
        return _strip_non_empty(value, field_name=str(info.field_name))


class ExternalEventSourceSpec(BaseModel):
    """Subscribe to one named external event source and kind."""

    model_config = ConfigDict(extra="forbid")

    source_type: Literal["external_event"] = "external_event"
    source_id: str
    kind: str
    match: dict[str, str | bool | int] = Field(default_factory=dict)

    @field_validator("source_id", "kind")
    @classmethod
    def validate_non_empty_fields(cls, value: str, info: ValidationInfo) -> str:
        """Require non-empty source_id and kind values."""
        return _strip_non_empty(value, field_name=str(info.field_name))


TriggerSourceSpec = Annotated[
    RoomMessageSourceSpec | ThreadTagSourceSpec | ExternalEventSourceSpec,
    Field(discriminator="source_type"),
]


class TriageSpec(BaseModel):
    """Optional tool-less cheap triage specification."""

    model_config = ConfigDict(extra="forbid")

    instructions: str = Field(min_length=1, max_length=1000)
    provider: Literal["llm", "typesafe", "openai_decisions"] = "llm"
    model: str | None = None
    threshold: float | None = Field(default=None, ge=0.0, le=1.0, allow_inf_nan=False)
    timeout_seconds: float | None = Field(default=None, gt=0.0, le=30.0, allow_inf_nan=False)

    @field_validator("instructions")
    @classmethod
    def validate_instructions(cls, value: str) -> str:
        """Require non-empty triage instructions."""
        return _strip_non_empty(value, field_name="instructions")

    @field_validator("model")
    @classmethod
    def validate_model(cls, value: str | None) -> str | None:
        """Reject empty model strings."""
        return _strip_optional_non_empty(value, field_name="model")

    @model_validator(mode="after")
    def validate_provider_options(self) -> TriageSpec:
        """Enforce provider-specific model and threshold rules."""
        if self.provider == "llm":
            if self.model is None:
                msg = "model is required when triage provider is 'llm'"
                raise ValueError(msg)
            if self.threshold is not None:
                msg = "threshold is not supported when triage provider is 'llm'"
                raise ValueError(msg)
        else:
            if self.model is not None:
                msg = "model must be None when triage provider is not 'llm'"
                raise ValueError(msg)
            if self.threshold is None:
                self.threshold = 0.8
        return self


class TriggerTargetSpec(BaseModel):
    """Wake-agent delivery target for a reactive trigger."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    room_id: str
    thread_id: str | None = None
    new_thread: bool = False
    thread_key: str | None = Field(default=None, max_length=256)
    reopen_on_fire: bool = False
    prompt: str = Field(min_length=1, max_length=4000)

    @field_validator("agent", "room_id", "prompt")
    @classmethod
    def validate_required_text(cls, value: str, info: ValidationInfo) -> str:
        """Require non-empty agent, room_id, and prompt values."""
        return _strip_non_empty(value, field_name=str(info.field_name))

    @field_validator("thread_id", "thread_key")
    @classmethod
    def validate_optional_text(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Reject empty optional thread fields."""
        return _strip_optional_non_empty(value, field_name=str(info.field_name))

    @model_validator(mode="after")
    def validate_thread_placement(self) -> TriggerTargetSpec:
        """Reject conflicting thread placement options."""
        if self.new_thread and self.thread_id is not None:
            msg = "thread_id and new_thread are mutually exclusive"
            raise ValueError(msg)
        if self.thread_key is not None and not self.new_thread:
            msg = "thread_key requires new_thread=True"
            raise ValueError(msg)
        return self


class ReactiveTriggerRecord(BaseModel):
    """Durable reactive trigger rule owned by one Matrix user."""

    model_config = ConfigDict(extra="forbid")

    trigger_id: str
    rule_uid: str = Field(default_factory=lambda: uuid4().hex)
    owner_user_id: str
    description: str = ""
    source: TriggerSourceSpec
    triage: TriageSpec | None = None
    target: TriggerTargetSpec
    enabled: bool = True
    disabled_reason: DisabledReason | None = None
    once: bool = False
    max_fires_per_hour: int = Field(default=10, ge=1, le=600)
    cooldown_seconds: int = Field(default=0, ge=0, le=86400)
    expires_at: float | None = Field(default=None, allow_inf_nan=False)
    fire_count: int = Field(default=0, ge=0)
    last_fired_at: float | None = Field(default=None, allow_inf_nan=False)
    created_at: float = Field(allow_inf_nan=False)
    updated_at: float = Field(allow_inf_nan=False)

    @field_validator("trigger_id")
    @classmethod
    def validate_trigger_id(cls, value: str) -> str:
        """Require a route-safe trigger_id matching TRIGGER_ID_PATTERN."""
        stripped = _strip_non_empty(value, field_name="trigger_id")
        if TRIGGER_ID_PATTERN.fullmatch(stripped) is None:
            msg = "trigger_id must match ^[A-Za-z0-9_-]{1,64}$"
            raise ValueError(msg)
        return stripped

    @field_validator("rule_uid")
    @classmethod
    def validate_rule_uid(cls, value: str) -> str:
        """Require a non-empty immutable rule UID."""
        return _strip_non_empty(value, field_name="rule_uid")

    @field_validator("owner_user_id")
    @classmethod
    def validate_owner_user_id(cls, value: str) -> str:
        """Require a non-empty owner Matrix user ID."""
        return _strip_non_empty(value, field_name="owner_user_id")

    @model_validator(mode="after")
    def validate_origin_target(self) -> ReactiveTriggerRecord:
        """Allow target.thread_id == 'origin' only for same-room room_message triggers."""
        if self.target.thread_id == "origin":
            if self.source.source_type != "room_message":
                msg = "target.thread_id='origin' is only allowed for room_message triggers"
                raise ValueError(msg)
            if self.source.room_id != self.target.room_id:
                msg = "target.thread_id='origin' requires target.room_id to match source.room_id"
                raise ValueError(msg)
        return self


class TriggerEvent(BaseModel):
    """Normalized event snapshot flowing through the reactive trigger pipeline."""

    model_config = ConfigDict(extra="forbid")

    source_type: SourceType
    source_id: str
    kind: str
    event_id: str
    timestamp: float = Field(allow_inf_nan=False)
    room_id: str | None = None
    thread_id: str | None = None
    actor: str | None = None
    title: str | None = None
    summary: str | None = None
    text: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


class RoomCursor(BaseModel):
    """Durable journal cursor for a room_message reactive trigger."""

    model_config = ConfigDict(extra="forbid")

    created_ts: int
    logical_event_id: str


class ThreadCheckpoint(BaseModel):
    """Durable state checkpoint for a thread_tag reactive trigger."""

    model_config = ConfigDict(extra="forbid")

    tag_present: bool
    state_event_id: str | None = None
    updated_at: float = Field(allow_inf_nan=False)


class FiringRecord(BaseModel):
    """One durable firing ledger row for a reactive trigger."""

    model_config = ConfigDict(extra="forbid")

    id: int
    trigger_id: str
    rule_uid: str
    dedupe_key: str
    status: FiringStatus
    event: TriggerEvent
    triage_reason: str | None
    matrix_event_id: str | None
    attempt_count: int
    next_retry_at: float | None
    error_message: str | None
    created_at: float
    updated_at: float
