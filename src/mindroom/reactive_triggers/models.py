"""Pydantic data models for reactive triggers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator, model_validator

TRIGGER_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

SourceType = Literal["room_message", "thread_tag", "external_event"]
FiringStatus = Literal["pending", "triaged_no", "claimed", "delivered", "failed", "skipped"]


def generate_trigger_id() -> str:
    """Return a newly generated route-safe reactive trigger identifier."""
    return f"tr_{uuid4().hex[:12]}"


_UNSAFE_REGEX_MESSAGE = "Pattern is unsafe: nested quantifiers, backreferences, and lookarounds are not allowed"
_BRACE_QUANTIFIER_RE = re.compile(r"\{(?:(\d+)(?:,(\d*))?|,(\d+))\}")
_INLINE_FLAG_CHARS = frozenset("aiLmsux-")


@dataclass(slots=True)
class _RegexGroupFrame:
    """Tracks quantifier and alternation presence inside one regex group."""

    has_quantifier: bool = False
    has_alternation: bool = False


def validate_safe_regex_pattern(pattern: str) -> None:
    """Reject regex constructs prone to catastrophic backtracking (ReDoS)."""
    stack: list[_RegexGroupFrame] = []
    index = 0
    length = len(pattern)
    while index < length:
        index = _step_regex_safety_token(pattern, index, stack)


def _step_regex_safety_token(pattern: str, index: int, stack: list[_RegexGroupFrame]) -> int:
    """Validate and advance past one regex token at index."""
    char = pattern[index]
    if char == "\\":
        if index + 1 < len(pattern) and "1" <= pattern[index + 1] <= "9":
            raise ValueError(_UNSAFE_REGEX_MESSAGE)
        next_index = index + 2
    elif char == "[":
        next_index = _consume_char_class(pattern, index)
    elif char == "(":
        next_index = _consume_group_open(pattern, index, stack)
    elif char == ")":
        next_index = _consume_group_close(pattern, index, stack)
    elif char == "|":
        if stack:
            stack[-1].has_alternation = True
        next_index = index + 1
    else:
        quantifier = _consume_quantifier(pattern, index)
        if quantifier is not None:
            if stack:
                stack[-1].has_quantifier = True
            next_index = quantifier[0]
        else:
            next_index = index + 1
    return next_index


def _consume_char_class(pattern: str, start: int) -> int:
    """Advance past a regex character class [...], treating inner metacharacters literally."""
    index = start + 1
    length = len(pattern)
    if index < length and pattern[index] == "^":
        index += 1
    if index < length and pattern[index] == "]":
        index += 1
    while index < length:
        if pattern[index] == "\\":
            index += 2
            continue
        if pattern[index] == "]":
            return index + 1
        index += 1
    return length


def _consume_quantifier(pattern: str, start: int) -> tuple[int, bool] | None:
    """Parse a quantifier at start, returning (end_index, is_repeating) or None."""
    length = len(pattern)
    if start >= length:
        return None
    char = pattern[start]
    if char in ("+", "*", "?"):
        end = start + 1
        is_repeating = char in ("+", "*")
    elif char == "{":
        match = _BRACE_QUANTIFIER_RE.match(pattern, start)
        if match is None:
            return None
        end = match.end()
        first, second, only_upper = match.groups()
        if only_upper is not None:
            is_repeating = int(only_upper) > 1
        elif second is None:
            is_repeating = int(first) > 1
        elif second == "":
            is_repeating = True
        else:
            is_repeating = int(second) > 1
    else:
        return None
    if end < length and pattern[end] in ("?", "+"):
        end += 1
    return end, is_repeating


def _consume_group_open(pattern: str, start: int, stack: list[_RegexGroupFrame]) -> int:
    """Handle ( and (?...) group openers, rejecting lookarounds and named backreferences."""
    length = len(pattern)
    if start + 1 >= length or pattern[start + 1] != "?":
        stack.append(_RegexGroupFrame())
        return start + 1
    rest = pattern[start + 2 :]
    if rest.startswith(("=", "!", "<=", "<!", "P=", "(")):
        raise ValueError(_UNSAFE_REGEX_MESSAGE)
    if rest.startswith("#"):
        close_idx = pattern.find(")", start + 3)
        return length if close_idx == -1 else close_idx + 1
    if rest.startswith((":", ">")):
        stack.append(_RegexGroupFrame())
        return start + 3
    if rest.startswith("P<"):
        close_idx = pattern.find(">", start + 4)
        stack.append(_RegexGroupFrame())
        return length if close_idx == -1 else close_idx + 1
    return _consume_inline_flag_group(pattern, start + 2, stack)


def _consume_inline_flag_group(pattern: str, flag_start: int, stack: list[_RegexGroupFrame]) -> int:
    """Handle (?ims...) inline flags and (?ims:...) scoped flag groups."""
    index = flag_start
    length = len(pattern)
    while index < length and pattern[index] in _INLINE_FLAG_CHARS:
        if pattern[index] == "x":
            raise ValueError(_UNSAFE_REGEX_MESSAGE)
        index += 1
    if index < length and pattern[index] == ")":
        return index + 1
    if index < length and pattern[index] == ":":
        stack.append(_RegexGroupFrame())
        return index + 1
    stack.append(_RegexGroupFrame())
    return flag_start


def _consume_group_close(pattern: str, start: int, stack: list[_RegexGroupFrame]) -> int:
    """Close a regex group, enforcing nested-quantifier and quantified-alternation bans."""
    index = start + 1
    if not stack:
        return index
    frame = stack.pop()
    quantifier = _consume_quantifier(pattern, index)
    if quantifier is not None:
        index, is_repeating = quantifier
        if is_repeating and (frame.has_quantifier or frame.has_alternation):
            raise ValueError(_UNSAFE_REGEX_MESSAGE)
        if stack:
            stack[-1].has_quantifier = True
            stack[-1].has_alternation = stack[-1].has_alternation or frame.has_alternation
    elif stack:
        stack[-1].has_quantifier = stack[-1].has_quantifier or frame.has_quantifier
        stack[-1].has_alternation = stack[-1].has_alternation or frame.has_alternation
    return index


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
            re.compile(value)
        except re.error as exc:
            msg = f"Invalid regular expression pattern: {exc}"
            raise ValueError(msg) from exc
        validate_safe_regex_pattern(value)
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
    owner_user_id: str
    description: str = ""
    source: TriggerSourceSpec
    triage: TriageSpec | None = None
    target: TriggerTargetSpec
    enabled: bool = True
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
