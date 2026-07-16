"""Request and response models for one-shot callbacks."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.config.validation import non_empty_stripped

CallbackStatus = Literal["done", "failed", "blocked", "progress"]


class CallbackFirePayload(BaseModel):
    """Bearer-authenticated callback request body."""

    model_config = ConfigDict(extra="forbid")

    status: CallbackStatus = "done"
    message: str
    data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        """Reject empty callback messages."""
        return non_empty_stripped(value, field_name="message")


class CallbackAcceptedResponse(BaseModel):
    """API response for an accepted callback fire."""

    accepted: bool
    callback_id: str
    uses_left: int
    matrix_event_id: str | None = None
