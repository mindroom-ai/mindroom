"""Request and response models for external triggers."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _non_empty_stripped(value: str, *, field_name: str) -> str:
    stripped = value.strip()
    if not stripped:
        msg = f"{field_name} must not be empty"
        raise ValueError(msg)
    return stripped


class ExternalTriggerPayload(BaseModel):
    """Signed external trigger request body."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    message: str
    event_id: str | None = None
    title: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def validate_kind(cls, value: str) -> str:
        """Reject empty trigger kinds."""
        return _non_empty_stripped(value, field_name="kind")

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """Reject empty trigger messages."""
        return _non_empty_stripped(value, field_name="message")


class ExternalTriggerAcceptedResponse(BaseModel):
    """API response for an accepted external trigger."""

    accepted: bool
    duplicate: bool = False
    trigger_id: str
    event_id: str
    matrix_event_id: str | None = None
