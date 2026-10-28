"""Slack integration settings."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mindroom.matrix.identity import validate_email_to_matrix_mapping


class SlackConfig(BaseModel):
    """How Slack workspace members map to Matrix users."""

    model_config = ConfigDict(extra="forbid")

    email_domain: str | None = Field(
        default=None,
        description="Email domain of the Slack workspace's members, for example `example.com`; set with matrix_user_id_template",
    )
    matrix_user_id_template: str | None = Field(
        default=None,
        description="Matrix user ID a member's email localpart maps to, with one `{localpart}`, for example `@{localpart}:example.org`; set with email_domain",
    )

    @model_validator(mode="after")
    def validate_identity_mapping(self) -> SlackConfig:
        """Require both mapping fields or neither, and a valid pair."""
        if (self.email_domain is None) != (self.matrix_user_id_template is None):
            msg = "Set email_domain and matrix_user_id_template together"
            raise ValueError(msg)
        if self.matrix_user_id_template is not None:
            validate_email_to_matrix_mapping(self.matrix_user_id_template, self.email_domain)
        return self
