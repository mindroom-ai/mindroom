"""Configuration of invites by email."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.config.access import validate_concrete_matrix_user_ids
from mindroom.origins import parse_browser_origin


class EmailInvitesConfig(BaseModel):
    """Settings of the `email_invite` tool: the Chat link, the expiry, and the approvers."""

    model_config = ConfigDict(extra="forbid")

    chat_url: str = Field(
        default="https://chat.mindroom.chat",
        description="Origin of the MindRoom Chat web app the invite link opens, without a path",
    )
    expiry_days: int = Field(default=7, ge=1, le=30, description="Days an invite link stays valid")
    approvers: list[str] = Field(
        default_factory=list,
        description="Matrix user IDs who may approve email invites in any room",
    )

    @field_validator("chat_url")
    @classmethod
    def require_origin(cls, value: str) -> str:
        """Accept an https origin, or an http origin on a local address, written exactly as its origin."""
        if parse_browser_origin(value) != value:
            msg = f"email_invites.chat_url must be written as its origin: {parse_browser_origin(value)}"
            raise ValueError(msg)
        return value

    @field_validator("approvers")
    @classmethod
    def require_user_ids(cls, approvers: list[str]) -> list[str]:
        """Accept only unique, concrete Matrix user IDs."""
        return validate_concrete_matrix_user_ids(approvers, field_name="email_invites.approvers")
