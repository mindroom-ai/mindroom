"""MindRoom tool registry entries for configured MCP servers."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger
from mindroom.mcp.config import mcp_oauth_bridge_function_names, validate_mcp_tool_filter_overlap
from mindroom.mcp.errors import MCPError
from mindroom.mcp.oauth import mcp_oauth_provider_id
from mindroom.mcp.toolkit import MindRoomMCPToolkit, require_mcp_server_manager
from mindroom.tool_system.catalog import (
    ConfigField,
    SetupType,
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolFileAccess,
    ToolManagedInitArg,
    ToolMetadata,
    ToolStatus,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from agno.tools import Toolkit

    from mindroom.constants import RuntimePaths
    from mindroom.credentials.manager import CredentialsManager
    from mindroom.mcp.config import MCPServerConfig
    from mindroom.mcp.manager import MCPServerManager
    from mindroom.mcp.types import MCPServerCatalog
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_MCP_TOOL_PREFIX = "mcp_"
# MindRoomMCPToolkit declares these constructor args for every MCP tool; metadata
# mirrors that contract even though credentials are used only by OAuth-backed servers.
_MCP_MANAGED_INIT_ARGS = (
    ToolManagedInitArg.RUNTIME_PATHS,
    ToolManagedInitArg.CREDENTIALS_MANAGER,
    ToolManagedInitArg.WORKER_TARGET,
)


def mcp_tool_name(server_id: str) -> str:
    """Return the MindRoom tool name for one MCP server."""
    return f"{_MCP_TOOL_PREFIX}{server_id}"


def _tool_override_fields() -> list[ConfigField]:
    return [
        ConfigField(
            name="include_tools",
            label="Include Tools",
            type="string[]",
            required=False,
            default=None,
            description="Optional allowlist of remote tool names for this assignment.",
        ),
        ConfigField(
            name="exclude_tools",
            label="Exclude Tools",
            type="string[]",
            required=False,
            default=None,
            description="Optional denylist of remote tool names for this assignment.",
        ),
        ConfigField(
            name="call_timeout_seconds",
            label="Call Timeout Seconds",
            type="number",
            required=False,
            default=None,
            description="Optional per-assignment timeout override for MCP tool calls.",
        ),
    ]


def validate_mcp_agent_overrides(tool_name: str, overrides: dict[str, object]) -> None:
    """Validate normalized per-agent overrides for one MCP registry tool."""
    if not overrides:
        return

    include_tools = cast("list[str]", overrides.get("include_tools", []))
    exclude_tools = cast("list[str]", overrides.get("exclude_tools", []))
    message = f"Invalid per-agent override for '{tool_name}': include_tools and exclude_tools overlap"
    validate_mcp_tool_filter_overlap(include_tools, exclude_tools, message=message)

    timeout_seconds = overrides.get("call_timeout_seconds")
    if timeout_seconds is not None and (
        not isinstance(timeout_seconds, int | float) or isinstance(timeout_seconds, bool) or float(timeout_seconds) <= 0
    ):
        msg = f"Invalid per-agent override for '{tool_name}.call_timeout_seconds': expected a number greater than 0"
        raise ValueError(msg)


def _tool_metadata(server_id: str, server_config: MCPServerConfig) -> ToolMetadata:
    tool_name = mcp_tool_name(server_id)
    provider_name = server_config.auth.display_name if server_config.auth is not None else None
    is_oauth = server_config.auth is not None
    auth_provider = mcp_oauth_provider_id(server_id, server_config.auth) if server_config.auth is not None else None
    return ToolMetadata(
        name=tool_name,
        display_name=server_config.display_name or provider_name or f"MCP {server_id.replace('_', ' ').title()}",
        description=server_config.summary or "Tools provided by this MCP server",
        icon=server_config.icon,
        category=ToolCategory.DEVELOPMENT,
        # A local stdio server is an operator-launched program that file_access cannot confine.
        file_access=ToolFileAccess.UNCONFINED if server_config.transport == "stdio" else ToolFileAccess.NONE,
        # Worker configs carry no MCP servers, so only the primary can reach them.
        requires_primary_runtime=True,
        status=ToolStatus.REQUIRES_CONFIG if is_oauth else ToolStatus.AVAILABLE,
        setup_type=SetupType.OAUTH if is_oauth else SetupType.NONE,
        auth_provider=auth_provider,
        config_fields=_tool_override_fields(),
        agent_override_fields=_tool_override_fields(),
        authored_override_validator=ToolAuthoredOverrideValidator.MCP,
        # An OAuth server's functions are its bridge; another server's come from its live catalog.
        function_names=mcp_oauth_bridge_function_names(server_id, server_config) if is_oauth else (),
        managed_init_args=_MCP_MANAGED_INIT_ARGS,
        factory=_tool_factory(server_id, server_config),
        mcp_server_id=server_id,
    )


def _available_catalog(
    server_id: str,
    server_config: MCPServerConfig,
    manager: MCPServerManager,
) -> MCPServerCatalog | None:
    """Return the cached catalog, degrading to None when an optional server is unavailable."""
    try:
        return manager.get_catalog(server_id)
    except MCPError as exc:
        if server_config.required:
            raise
        logger.debug(
            "MCP server unavailable; building toolkit without its tools",
            server_id=server_id,
            error=str(exc),
        )
        return None


def _tool_factory(server_id: str, server_config: MCPServerConfig) -> Callable[[], type[Toolkit]]:
    def factory() -> type[Toolkit]:
        class BoundMindRoomMCPToolkit(MindRoomMCPToolkit):
            def __init__(
                self,
                include_tools: list[str] | str | None = None,
                exclude_tools: list[str] | str | None = None,
                call_timeout_seconds: float | None = None,
                runtime_paths: RuntimePaths | None = None,
                credentials_manager: CredentialsManager | None = None,
                worker_target: ResolvedWorkerTarget | None = None,
            ) -> None:
                manager = require_mcp_server_manager()
                is_oauth = server_config.auth is not None
                super().__init__(
                    server_id=server_id,
                    manager=manager,
                    catalog=(
                        _available_catalog(server_id, server_config, manager)
                        if manager is not None and not is_oauth
                        else None
                    ),
                    tool_name=mcp_tool_name(server_id),
                    server_config=server_config,
                    include_tools=include_tools,
                    exclude_tools=exclude_tools,
                    call_timeout_seconds=call_timeout_seconds,
                    runtime_paths=runtime_paths,
                    credentials_manager=credentials_manager,
                    worker_target=worker_target,
                )

        BoundMindRoomMCPToolkit.__name__ = f"MindRoomMCPToolkit_{server_id}"
        return BoundMindRoomMCPToolkit

    return factory


def mcp_tool_entries(mcp_servers: Mapping[str, MCPServerConfig]) -> dict[str, ToolMetadata]:
    """Return the registry entries of the enabled MCP servers among ``mcp_servers``."""
    return {
        mcp_tool_name(server_id): _tool_metadata(server_id, server_config)
        for server_id, server_config in mcp_servers.items()
        if server_config.enabled
    }


def with_catalog_function_names(tool_metadata: Mapping[str, ToolMetadata]) -> dict[str, ToolMetadata]:
    """Return ``tool_metadata`` with each non-OAuth MCP server's function names read from its live catalog."""
    manager = require_mcp_server_manager()
    listed = dict(tool_metadata)
    for tool_name, metadata in tool_metadata.items():
        server_id = metadata.mcp_server_id
        if (
            server_id is None
            or metadata.auth_provider is not None
            or manager is None
            or not manager.has_server(server_id)
        ):
            continue
        try:
            catalog = manager.get_catalog(server_id)
        except MCPError:
            continue
        listed[tool_name] = replace(metadata, function_names=tuple(tool.function_name for tool in catalog.tools))
    return listed
