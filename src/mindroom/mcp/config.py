"""Configuration helpers for MCP client servers."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MCPTransport = Literal["stdio", "sse", "streamable-http"]
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


def normalize_mcp_server_id(server_id: str) -> str:
    """Validate and normalize one MCP server id."""
    return _validate_mcp_identifier(server_id, subject="MCP server id")


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
    include_tools: list[str] = Field(default_factory=list, description="Optional remote tool allowlist")
    exclude_tools: list[str] = Field(default_factory=list, description="Optional remote tool denylist")
    startup_timeout_seconds: float = Field(default=20.0, gt=0, description="Startup timeout")
    call_timeout_seconds: float = Field(default=120.0, gt=0, description="Default call timeout")
    max_concurrent_calls: int = Field(default=1, ge=1, description="Maximum concurrent calls")
    idle_ttl_seconds: int = Field(default=900, ge=0, description="Idle timeout for future cleanup")
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

    def _validate_tool_filters(self) -> None:
        include_tools = set(self.include_tools)
        exclude_tools = set(self.exclude_tools)
        overlap = sorted(include_tools & exclude_tools)
        if overlap:
            msg = f"MCP include_tools and exclude_tools overlap: {', '.join(overlap)}"
            raise ValueError(msg)

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
        self._validate_tool_filters()

        if self.transport == "stdio":
            self._validate_stdio_transport()
        else:
            self._validate_remote_transport()

        if self.tool_prefix is not None:
            _validate_mcp_identifier(self.tool_prefix, subject="MCP tool_prefix")
        return self


def resolved_mcp_tool_prefix(server_id: str, server_config: MCPServerConfig) -> str:
    """Return the effective tool prefix for one server."""
    prefix = server_config.tool_prefix if server_config.tool_prefix is not None else normalize_mcp_server_id(server_id)
    return _validate_mcp_identifier(prefix, subject="MCP tool_prefix")
