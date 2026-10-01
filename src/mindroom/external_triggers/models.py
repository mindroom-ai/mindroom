"""Request and response models for external triggers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.config.validation import non_empty_stripped

# Event ids, thread keys, and signature nonces are retained in a shared store, so keep them short.
_MAX_REPLAY_KEY_LENGTH = 256


class ExternalTriggerPayload(BaseModel):
    """External trigger request body."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    event_id: str | None = Field(default=None, max_length=_MAX_REPLAY_KEY_LENGTH)
    title: str | None = None
    thread_key: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        """Reject empty trigger kinds."""
        return non_empty_stripped(value, field_name="kind")

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """Reject empty trigger messages."""
        return non_empty_stripped(value, field_name="message")

    @field_validator("thread_key")
    @classmethod
    def validate_thread_key(cls, value: str | None) -> str | None:
        """Reject blank or oversized thread keys; ``None`` keeps per-delivery threads."""
        if value is None:
            return None
        stripped = non_empty_stripped(value, field_name="thread_key")
        if len(stripped) > _MAX_REPLAY_KEY_LENGTH:
            msg = f"thread_key must be at most {_MAX_REPLAY_KEY_LENGTH} characters"
            raise ValueError(msg)
        return stripped


class ExternalTriggerAcceptedResponse(BaseModel):
    """API response for an accepted external trigger."""

    accepted: bool
    duplicate: bool = False
    trigger_id: str
    event_id: str
    matrix_event_id: str | None = None
