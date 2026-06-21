"""Dynamic MindRoom tool registry entries for configured MCP servers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.logging_config import get_logger
from mindroom.mcp.config import mcp_oauth_bridge_function_names, validate_mcp_tool_filter_overlap
from mindroom.mcp.errors import MCPError
from mindroom.mcp.oauth import mcp_oauth_provider_id
from mindroom.mcp.toolkit import MindRoomMCPToolkit, require_mcp_server_manager
from mindroom.tool_system.catalog import (
    TOOL_METADATA,
    ConfigField,
    SetupType,
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolManagedInitArg,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registry_state import TOOL_REGISTRY, reconcile_dynamic_tool_state

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.mcp.config import MCPServerConfig
    from mindroom.mcp.manager import MCPServerManager
    from mindroom.mcp.types import MCPServerCatalog
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

_MCP_TOOL_PREFIX = "mcp_"
_MCP_TOOL_NAMES: set[str] = set()
_MCP_OAUTH_TOOL_NAMES: set[str] = set()
_MCP_TOOL_FACTORY_MARKER = "__mindroom_mcp_tool_factory__"
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


def mcp_server_id_from_tool_name(tool_name: str) -> str | None:
    """Return the server id for an MCP registry tool name."""
    if not tool_name.startswith(_MCP_TOOL_PREFIX):
        return None
    factory = TOOL_REGISTRY.get(tool_name)
    if tool_name not in _MCP_TOOL_NAMES and not getattr(factory, _MCP_TOOL_FACTORY_MARKER, False):
        return None
    server_id = tool_name.removeprefix(_MCP_TOOL_PREFIX)
    return server_id or None


def mcp_tool_name_is_oauth_backed(tool_name: str) -> bool:
    """Return whether one registry-owned MCP tool uses requester-scoped OAuth."""
    return tool_name in _MCP_OAUTH_TOOL_NAMES


def _registered_mcp_tool_names() -> set[str]:
    """Return tool names that are actually owned by the dynamic MCP registry."""
    return {
        *_MCP_TOOL_NAMES,
        *(
            tool_name
            for tool_name, factory in TOOL_REGISTRY.items()
            if getattr(factory, _MCP_TOOL_FACTORY_MARKER, False)
        ),
    }


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
    transport_label = server_config.transport.replace("-", " ")
    is_oauth = server_config.auth is not None
    manager = require_mcp_server_manager()
    catalog = None
    if not is_oauth and manager is not None and manager.has_server(server_id):
        try:
            catalog = manager.get_catalog(server_id)
        except MCPError:
            catalog = None
    auth_provider = None
    function_names: tuple[str, ...]
    if is_oauth:
        auth_provider = mcp_oauth_provider_id(server_id, server_config.auth)
        function_names = mcp_oauth_bridge_function_names(server_id, server_config)
    else:
        function_names = tuple(tool.function_name for tool in catalog.tools) if catalog is not None else ()
    return ToolMetadata(
        name=tool_name,
        display_name=f"MCP {server_id.replace('_', ' ').title()}",
        description=f"MCP server '{server_id}' tools over {transport_label}.",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.REQUIRES_CONFIG if is_oauth else ToolStatus.AVAILABLE,
        setup_type=SetupType.OAUTH if is_oauth else SetupType.NONE,
        auth_provider=auth_provider,
        config_fields=_tool_override_fields(),
        agent_override_fields=_tool_override_fields(),
        authored_override_validator=ToolAuthoredOverrideValidator.MCP,
        function_names=function_names,
        managed_init_args=_MCP_MANAGED_INIT_ARGS,
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

    setattr(factory, _MCP_TOOL_FACTORY_MARKER, True)
    return factory


def _desired_server_entries(config: Config | None) -> dict[str, MCPServerConfig]:
    if config is None:
        return {}
    return {
        server_id: server_config for server_id, server_config in config.mcp_servers.items() if server_config.enabled
    }


def sync_mcp_tool_registry(config: Config | None) -> None:
    """Reconcile the dynamic registry entries for configured MCP servers."""
    desired_registry, desired_metadata = resolved_mcp_tool_state(config)
    desired_tool_names = reconcile_dynamic_tool_state(
        TOOL_REGISTRY,
        TOOL_METADATA,
        desired_registry,
        desired_metadata,
        owned_tool_names=_registered_mcp_tool_names(),
        collision_error=lambda tool_name: ValueError(
            f"MCP tool '{tool_name}' conflicts with an existing registered tool",
        ),
    )
    _MCP_TOOL_NAMES.clear()
    _MCP_TOOL_NAMES.update(desired_tool_names)
    _MCP_OAUTH_TOOL_NAMES.clear()
    if config is not None:
        _MCP_OAUTH_TOOL_NAMES.update(
            mcp_tool_name(server_id)
            for server_id, server_config in config.mcp_servers.items()
            if server_config.enabled
            and server_config.auth is not None
            and mcp_tool_name(server_id) in desired_tool_names
        )


def resolved_mcp_tool_state(
    config: Config | None,
) -> tuple[dict[str, Callable[[], type[Toolkit]]], dict[str, ToolMetadata]]:
    """Return the MCP tool registry entries implied by one config without mutating globals."""
    registry: dict[str, Callable[[], type[Toolkit]]] = {}
    metadata: dict[str, ToolMetadata] = {}
    for server_id, server_config in _desired_server_entries(config).items():
        tool_name = mcp_tool_name(server_id)
        registry[tool_name] = _tool_factory(server_id, server_config)
        metadata[tool_name] = _tool_metadata(server_id, server_config)
    return registry, metadata
