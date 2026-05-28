"""Configuration helpers for MCP client servers."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MCPTransport = Literal["stdio", "sse", "streamable-http"]
_MCPOAuthDiscoveryMode = Literal["auto", "manual"]
_MCPOAuthTokenEndpointAuthMethod = Literal["none", "client_secret_post", "client_secret_basic"]
_MCP_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_MCP_FUNCTION_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MAX_MCP_FUNCTION_NAME_LENGTH = 64


def _validate_mcp_identifier(value: str, *, subject: str) -> str:
    """Validate one MCP config identifier used in tool and function names."""
    normalized = value.strip()
    if not normalized:
        msg = f"{subject} must not be empty"
        raise ValueError(msg)
    if not _MCP_IDENTIFIER_PATTERN.fullmatch(normalized):
        msg = f"{subject} must contain only letters, numbers, and underscores"
        raise ValueError(msg)
    return normalized


def validate_mcp_function_name(value: str, *, subject: str) -> str:
    """Validate one provider-visible MCP function name."""
    if not value:
        msg = f"{subject} must not be empty"
        raise ValueError(msg)
    if len(value) > _MAX_MCP_FUNCTION_NAME_LENGTH:
        msg = f"{subject} must be at most {_MAX_MCP_FUNCTION_NAME_LENGTH} characters"
        raise ValueError(msg)
    if not _MCP_FUNCTION_NAME_PATTERN.fullmatch(value):
        msg = f"{subject} must contain only letters, numbers, underscores, and dashes"
        raise ValueError(msg)
    return value


def validate_mcp_tool_filter_overlap(include_tools: list[str], exclude_tools: list[str], *, message: str) -> None:
    """Reject tool names that appear in both include and exclude filters."""
    if overlap := sorted(set(include_tools) & set(exclude_tools)):
        msg = f"{message}: {', '.join(overlap)}"
        raise ValueError(msg)


def normalize_mcp_server_id(server_id: str) -> str:
    """Validate and normalize one MCP server id."""
    return _validate_mcp_identifier(server_id, subject="MCP server id")


class MCPOAuthConfig(BaseModel):
    """OAuth settings for requester-scoped remote MCP servers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["oauth"]
    provider_id: str | None = Field(default=None, description="OAuth provider id; defaults to mcp_<server_id>")
    display_name: str | None = Field(default=None, description="Human-readable OAuth provider name")
    resource: str | None = Field(default=None, description="OAuth protected resource identifier")
    discovery: _MCPOAuthDiscoveryMode = Field(default="auto", description="OAuth metadata discovery mode")
    authorization_server: str | None = Field(default=None, description="Authorization server issuer/base URL")
    authorization_url: str | None = Field(default=None, description="Manual OAuth authorization endpoint")
    token_url: str | None = Field(default=None, description="Manual OAuth token endpoint")
    registration_url: str | None = Field(default=None, description="Dynamic client registration endpoint")
    dynamic_client_registration: bool = Field(default=True, description="Whether dynamic registration may be used")
    token_endpoint_auth_method: _MCPOAuthTokenEndpointAuthMethod = Field(
        default="none",
        description="OAuth token endpoint client authentication method",
    )
    pkce_code_challenge_method: Literal["S256"] | None = Field(default="S256", description="PKCE challenge method")
    scopes: list[str] = Field(default_factory=list, description="OAuth scopes")
    extra_auth_params: dict[str, str] = Field(default_factory=dict, description="Extra authorization request params")
    extra_token_params: dict[str, str] = Field(default_factory=dict, description="Extra token request params")
    client_config_services: list[str] = Field(default_factory=list, description="Provider-specific client config")
    shared_client_config_services: list[str] = Field(default_factory=list, description="Shared client config services")

    @field_validator("provider_id")
    @classmethod
    def validate_provider_id(cls, value: str | None) -> str | None:
        """Validate explicit generated OAuth provider ids."""
        if value is None:
            return None
        return _validate_mcp_identifier(value, subject="MCP OAuth provider_id")

    @field_validator("scopes", mode="before")
    @classmethod
    def normalize_scopes(cls, value: object) -> object:
        """Strip blank scope entries while preserving empty scope lists."""
        if value is None or not isinstance(value, list):
            return value
        normalized: list[str] = []
        for entry in value:
            if not isinstance(entry, str):
                msg = "MCP OAuth scopes must be strings"
                raise TypeError(msg)
            stripped = entry.strip()
            if stripped:
                normalized.append(stripped)
        return normalized


class MCPServerConfig(BaseModel):
    """Config for one MCP server connection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(default=True, description="Whether the server is active")
    transport: MCPTransport = Field(description="Transport type")
    command: str | None = Field(default=None, description="Executable name for stdio transport")
    args: list[str] = Field(default_factory=list, description="Arguments for stdio transport")
    cwd: str | None = Field(default=None, description="Working directory for stdio transport")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables for stdio transport")
    url: str | None = Field(default=None, description="Remote URL for SSE or streamable HTTP")
    headers: dict[str, str] = Field(default_factory=dict, description="HTTP headers for remote transports")
    tool_prefix: str | None = Field(default=None, description="Prefix for model-visible function names")
    auth: MCPOAuthConfig | None = Field(default=None, description="Optional requester-scoped MCP auth")
    include_tools: list[str] = Field(default_factory=list, description="Optional remote tool allowlist")
    exclude_tools: list[str] = Field(default_factory=list, description="Optional remote tool denylist")
    startup_timeout_seconds: float = Field(default=20.0, gt=0, description="Startup timeout")
    call_timeout_seconds: float = Field(default=120.0, gt=0, description="Default call timeout")
    max_concurrent_calls: int = Field(default=1, ge=1, description="Maximum concurrent calls")
    auto_reconnect: bool = Field(default=True, description="Whether to reconnect automatically")

    @field_validator("include_tools", "exclude_tools", mode="before")
    @classmethod
    def normalize_tool_filters(cls, value: object) -> object:
        """Strip tool filter names at parse time so matching stays predictable."""
        if value is None or not isinstance(value, list):
            return value
        normalized: list[str] = []
        for entry in value:
            if not isinstance(entry, str):
                msg = "MCP tool filters must be strings"
                raise TypeError(msg)
            stripped = entry.strip()
            if stripped:
                normalized.append(stripped)
        return normalized

    def _validate_stdio_transport(self) -> None:
        if not self.command or not self.command.strip():
            msg = "stdio MCP servers require a non-empty command"
            raise ValueError(msg)
        if self.url is not None:
            msg = "stdio MCP servers do not allow url"
            raise ValueError(msg)
        if self.headers:
            msg = "stdio MCP servers do not allow headers"
            raise ValueError(msg)
        if self.auth is not None:
            msg = "OAuth-backed MCP servers require remote HTTP transport"
            raise ValueError(msg)

    def _validate_remote_transport(self) -> None:
        if not self.url or not self.url.strip():
            msg = f"{self.transport} MCP servers require a non-empty url"
            raise ValueError(msg)
        if self.command is not None:
            msg = f"{self.transport} MCP servers do not allow command"
            raise ValueError(msg)
        if self.args:
            msg = f"{self.transport} MCP servers do not allow args"
            raise ValueError(msg)
        if self.cwd is not None:
            msg = f"{self.transport} MCP servers do not allow cwd"
            raise ValueError(msg)
        if self.env:
            msg = f"{self.transport} MCP servers do not allow env"
            raise ValueError(msg)

    @model_validator(mode="after")
    def validate_transport_fields(self) -> Self:
        """Validate the transport-specific config shape."""
        validate_mcp_tool_filter_overlap(
            self.include_tools,
            self.exclude_tools,
            message="MCP include_tools and exclude_tools overlap",
        )

        if self.transport == "stdio":
            self._validate_stdio_transport()
        else:
            self._validate_remote_transport()

        if self.auth is not None and self.auth.discovery == "manual":
            if not self.auth.authorization_url or not self.auth.authorization_url.strip():
                msg = "manual MCP OAuth discovery requires authorization_url"
                raise ValueError(msg)
            if not self.auth.token_url or not self.auth.token_url.strip():
                msg = "manual MCP OAuth discovery requires token_url"
                raise ValueError(msg)

        if self.tool_prefix is not None:
            _validate_mcp_identifier(self.tool_prefix, subject="MCP tool_prefix")
        return self


def resolved_mcp_tool_prefix(server_id: str, server_config: MCPServerConfig) -> str:
    """Return the effective tool prefix for one server."""
    prefix = server_config.tool_prefix if server_config.tool_prefix is not None else normalize_mcp_server_id(server_id)
    return _validate_mcp_identifier(prefix, subject="MCP tool_prefix")
