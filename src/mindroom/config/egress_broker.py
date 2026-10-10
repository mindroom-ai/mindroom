"""Egress broker configuration models."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from mindroom.credential_policy import is_oauth_client_config_service, is_oauth_token_service

_AuthType = Literal["bearer", "basic", "header", "query"]

# Reserved env names that cannot be used as placeholder names
_RESERVED_EXACT = {"PATH", "HOME"}
_RESERVED_PREFIXES = {"MINDROOM_", "GIT_CONFIG_"}
_RESERVED_PROXY_VARS = {
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "no_proxy",
}
_RESERVED_CA_VARS = {
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
    "GIT_SSL_CAINFO",
    "NODE_EXTRA_CA_CERTS",
}


class EgressAuth(BaseModel):
    """Authentication configuration for an egress rule."""

    model_config = ConfigDict(extra="forbid")

    type: _AuthType = Field(description="Authentication type")
    username: str | None = Field(
        default=None,
        description="Username for basic auth; required when type=basic",
    )
    name: str | None = Field(
        default=None,
        description="Header or query param name; required for header/query types",
    )
    template: str = Field(
        default="{secret}",
        description="Template for header/query value; must contain {secret} exactly once",
    )

    @field_validator("template")
    @classmethod
    def validate_template(cls, value: str) -> str:
        """Ensure template contains {secret} exactly once."""
        count = value.count("{secret}")
        if count != 1:
            msg = "template must contain {secret} exactly once"
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def validate_type_requirements(self) -> EgressAuth:
        """Validate type-specific field requirements."""
        if self.type == "basic" and self.username is None:
            msg = "basic auth requires username"
            raise ValueError(msg)
        if self.type in ("header", "query") and self.name is None:
            msg = f"{self.type} auth requires name"
            raise ValueError(msg)
        return self


class EgressRule(BaseModel):
    """One egress routing rule."""

    model_config = ConfigDict(extra="forbid")

    host: str = Field(description="Host to match (exact, IP, or *.domain.com)")
    port: int | None = Field(
        default=None,
        description="Port to match; None matches any port",
    )
    path_prefix: str = Field(
        default="/",
        description="Path prefix to match",
    )
    auth: EgressAuth = Field(description="Authentication config for this rule")

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        """Validate and normalize host."""
        # Lowercase the host
        value = value.lower()

        # Strip one trailing dot (FQDN notation)
        if value.endswith(".") and not value.endswith(".."):
            value = value[:-1]

        # Check for invalid patterns
        if "://" in value:
            msg = "host must not contain scheme"
            raise ValueError(msg)
        if ":" in value:
            msg = "host must not contain port"
            raise ValueError(msg)
        if "/" in value:
            msg = "host must not contain path"
            raise ValueError(msg)

        # Wildcard validation: only single leading *.
        if value.startswith("*."):
            parts = value.split(".")
            if parts.count("*") > 1:
                msg = "host can only have one wildcard label"
                raise ValueError(msg)
            # Ensure there's a base domain after *.
            if len(parts) < 3:  # Must be *.domain.tld minimum
                msg = "wildcard host must have at least one domain part after *."
                raise ValueError(msg)
        elif "*" in value:
            msg = "wildcard must be a single leading *. label"
            raise ValueError(msg)

        return value

    @field_validator("path_prefix")
    @classmethod
    def validate_path_prefix(cls, value: str) -> str:
        """Ensure path_prefix starts with /."""
        if not value.startswith("/"):
            msg = "path_prefix must start with /"
            raise ValueError(msg)
        return value


class EgressService(BaseModel):
    """One egress service configuration."""

    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(
        default=None,
        description="Human-readable service name",
    )
    description: str = Field(
        default="",
        description="Service description",
    )
    rules: list[EgressRule] = Field(
        ...,
        min_length=1,
        description="Routing rules for this service",
    )
    placeholder_env: dict[str, str] = Field(
        default_factory=dict,
        description="Placeholder environment variables",
    )

    @field_validator("placeholder_env")
    @classmethod
    def validate_placeholder_env(cls, value: dict[str, str]) -> dict[str, str]:
        """Validate placeholder env names."""
        pattern = re.compile(r"^[A-Z_][A-Z0-9_]*$")
        reserved_all = _RESERVED_EXACT | _RESERVED_PROXY_VARS | _RESERVED_CA_VARS

        for name in value:
            # Must match pattern
            if not pattern.match(name):
                msg = f"placeholder_env name '{name}' must match ^[A-Z_][A-Z0-9_]*$"
                raise ValueError(msg)

            # Check exact reserved names
            if name in reserved_all:
                msg = f"placeholder_env name '{name}' is reserved"
                raise ValueError(msg)

            # Check reserved prefixes
            for prefix in _RESERVED_PREFIXES:
                if name.startswith(prefix):
                    msg = f"placeholder_env name '{name}' starts with reserved prefix {prefix}"
                    raise ValueError(msg)

        return value


class EgressBrokerConfig(BaseModel):
    """Top-level egress broker configuration."""

    model_config = ConfigDict(extra="forbid")

    unmatched_hosts: Literal["passthrough", "deny"] = Field(
        default="passthrough",
        description="Action for hosts with no matching rules",
    )
    services: dict[str, EgressService] = Field(
        default_factory=dict,
        description="Service configurations by name",
    )

    @field_validator("services")
    @classmethod
    def validate_service_names(cls, value: dict[str, EgressService]) -> dict[str, EgressService]:
        """Validate service names."""
        pattern = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
        for name in value:
            if not pattern.match(name):
                msg = f"service name '{name}' must match ^[a-z0-9][a-z0-9_-]{{0,62}}$"
                raise ValueError(msg)
            # Secrets are stored as `egress_<name>`; OAuth suffixes there would read as OAuth services.
            credential_service = f"egress_{name}"
            if is_oauth_token_service(credential_service) or is_oauth_client_config_service(credential_service):
                msg = f"service name '{name}' must not end in '_oauth' or '_oauth_client' (reserved for OAuth)"
                raise ValueError(msg)
        return value
