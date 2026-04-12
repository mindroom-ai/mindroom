"""Credential connection configuration models."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.credentials import validate_service_name

ConnectionAuthKind = Literal["api_key", "google_adc", "oauth_client", "none"]
_RESERVED_CONNECTION_SERVICES = frozenset({"google"})


class ConnectionConfig(BaseModel):
    """One named credential connection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(description="Provider that consumes this connection")
    service: str | None = Field(
        default=None,
        description="Slash-free shared CredentialsManager service name",
    )
    auth_kind: ConnectionAuthKind = Field(description="Credential payload shape for this connection")

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: str) -> str:
        """Reject blank provider names."""
        normalized = value.strip()
        if not normalized:
            msg = "provider must not be empty"
            raise ValueError(msg)
        return normalized

    @field_validator("service")
    @classmethod
    def validate_service(cls, value: str | None) -> str | None:
        """Reject invalid credential service names."""
        if value is None:
            return None
        if "/" in value:
            msg = "service must not contain '/'"
            raise ValueError(msg)
        normalized = validate_service_name(value)
        if normalized in _RESERVED_CONNECTION_SERVICES:
            msg = f"service '{normalized}' is reserved for backend-managed Google token storage"
            raise ValueError(msg)
        return normalized

    @model_validator(mode="after")
    def validate_service_requirement(self) -> Self:
        """Require a backing service unless auth is intentionally disabled."""
        if self.auth_kind == "none":
            if self.service is not None:
                msg = "service must be null when auth_kind is 'none'"
                raise ValueError(msg)
            return self
        if self.service is None:
            msg = f"service is required when auth_kind is '{self.auth_kind}'"
            raise ValueError(msg)
        return self
