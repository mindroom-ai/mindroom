"""Configuration of invites by email."""

from __future__ import annotations

from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from mindroom.origins import is_local_origin, normalized_origin


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
        """Accept an https origin, or an http origin on a local address, with no path."""
        if normalized_origin(value) != value or (urlsplit(value).scheme != "https" and not is_local_origin(value)):
            msg = "email_invites.chat_url must be an https origin, or an http origin on a local address, with no path"
            raise ValueError(msg)
        return value

    @field_validator("approvers")
    @classmethod
    def require_user_ids(cls, approvers: list[str]) -> list[str]:
        """Accept only Matrix user IDs."""
        for approver in approvers:
            if not approver.startswith("@") or ":" not in approver:
                msg = f"email_invites.approvers entry is not a Matrix user ID: {approver}"
                raise ValueError(msg)
        return approvers
