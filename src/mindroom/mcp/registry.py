"""Dynamic MindRoom tool registry entries for configured MCP servers."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from mindroom.mcp.toolkit import (
    MindRoomMCPToolkit,
    require_mcp_server_manager,
)
from mindroom.tool_system.catalog import (
    TOOL_METADATA,
    ConfigField,
    SetupType,
    ToolAuthoredOverrideValidator,
    ToolCategory,
    ToolMetadata,
    ToolStatus,
)
from mindroom.tool_system.registry_state import _TOOL_REGISTRY

if TYPE_CHECKING:
    from collections.abc import Callable

    from agno.tools import Toolkit

    from mindroom.config.main import Config
    from mindroom.mcp.config import MCPServerConfig

_MCP_TOOL_PREFIX = "mcp_"
_MCP_TOOL_NAMES: set[str] = set()
_MCP_TOOL_FACTORY_MARKER = "__mindroom_mcp_tool_factory__"


def mcp_tool_name(server_id: str) -> str:
    """Return the MindRoom tool name for one MCP server."""
    return f"{_MCP_TOOL_PREFIX}{server_id}"


def mcp_server_id_from_tool_name(tool_name: str) -> str | None:
    """Return the server id for an MCP registry tool name."""
    if not tool_name.startswith(_MCP_TOOL_PREFIX):
        return None
    factory = _TOOL_REGISTRY.get(tool_name)
    if tool_name not in _MCP_TOOL_NAMES and not getattr(factory, _MCP_TOOL_FACTORY_MARKER, False):
        return None
    server_id = tool_name.removeprefix(_MCP_TOOL_PREFIX)
    return server_id or None


def mcp_registry_tool_names() -> set[str]:
    """Return all active dynamic MCP tool names."""
    return set(_MCP_TOOL_NAMES)


def _registered_mcp_tool_names() -> set[str]:
    """Return tool names that are actually owned by the dynamic MCP registry."""
    return {
        *_MCP_TOOL_NAMES,
        *(
            tool_name
            for tool_name, factory in _TOOL_REGISTRY.items()
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
    overlap = sorted(set(include_tools) & set(exclude_tools))
    if overlap:
        msg = f"Invalid per-agent override for '{tool_name}': include_tools and exclude_tools overlap: {', '.join(overlap)}"
        raise ValueError(msg)

    timeout_seconds = overrides.get("call_timeout_seconds")
    if timeout_seconds is not None and (
        not isinstance(timeout_seconds, int | float) or isinstance(timeout_seconds, bool) or float(timeout_seconds) <= 0
    ):
        msg = f"Invalid per-agent override for '{tool_name}.call_timeout_seconds': expected a number greater than 0"
        raise ValueError(msg)


def _tool_metadata(server_id: str, server_config: MCPServerConfig) -> ToolMetadata:
    tool_name = mcp_tool_name(server_id)
    transport_label = server_config.transport.replace("-", " ")
    return ToolMetadata(
        name=tool_name,
        display_name=f"MCP {server_id.replace('_', ' ').title()}",
        description=f"MCP server '{server_id}' tools over {transport_label}.",
        category=ToolCategory.DEVELOPMENT,
        status=ToolStatus.AVAILABLE,
        setup_type=SetupType.NONE,
        config_fields=_tool_override_fields(),
        agent_override_fields=_tool_override_fields(),
        authored_override_validator=ToolAuthoredOverrideValidator.MCP,
    )


def _tool_factory(server_id: str) -> Callable[[], type[Toolkit]]:
    def factory() -> type[Toolkit]:
        class BoundMindRoomMCPToolkit(MindRoomMCPToolkit):
            def __init__(
                self,
                include_tools: list[str] | str | None = None,
                exclude_tools: list[str] | str | None = None,
                call_timeout_seconds: float | None = None,
            ) -> None:
                manager = require_mcp_server_manager()
                super().__init__(
                    server_id=server_id,
                    manager=manager,
                    catalog=manager.get_catalog(server_id) if manager is not None else None,
                    tool_name=mcp_tool_name(server_id),
                    include_tools=include_tools,
                    exclude_tools=exclude_tools,
                    call_timeout_seconds=call_timeout_seconds,
                )

        BoundMindRoomMCPToolkit.__name__ = f"MindRoomMCPToolkit_{server_id}"
        return BoundMindRoomMCPToolkit

    setattr(factory, _MCP_TOOL_FACTORY_MARKER, True)
    return factory


def register_mcp_tool(server_id: str, server_config: MCPServerConfig) -> None:
    """Register one dynamic MCP tool entry."""
    tool_name = mcp_tool_name(server_id)
    if tool_name not in _registered_mcp_tool_names() and (tool_name in TOOL_METADATA or tool_name in _TOOL_REGISTRY):
        msg = f"MCP tool '{tool_name}' conflicts with an existing registered tool"
        raise ValueError(msg)
    TOOL_METADATA[tool_name] = _tool_metadata(server_id, server_config)
    _TOOL_REGISTRY[tool_name] = _tool_factory(server_id)
    _MCP_TOOL_NAMES.add(tool_name)


def unregister_mcp_tool(tool_name: str) -> None:
    """Remove one dynamic MCP tool entry."""
    TOOL_METADATA.pop(tool_name, None)
    _TOOL_REGISTRY.pop(tool_name, None)
    _MCP_TOOL_NAMES.discard(tool_name)


def _desired_server_entries(config: Config | None) -> dict[str, MCPServerConfig]:
    if config is None:
        return {}
    return {
        server_id: server_config for server_id, server_config in config.mcp_servers.items() if server_config.enabled
    }


def sync_mcp_tool_registry(config: Config | None) -> None:
    """Reconcile the dynamic registry entries for configured MCP servers."""
    desired_registry, desired_metadata = resolved_mcp_tool_state(config)
    desired_tool_names = set(desired_registry)
    registered_mcp_tool_names = _registered_mcp_tool_names()
    existing_non_mcp_tool_names = {*TOOL_METADATA, *_TOOL_REGISTRY} - registered_mcp_tool_names
    conflicting_tool_names = sorted(desired_tool_names & existing_non_mcp_tool_names)
    if conflicting_tool_names:
        msg = f"MCP tool '{conflicting_tool_names[0]}' conflicts with an existing registered tool"
        raise ValueError(msg)

    for tool_name in sorted(registered_mcp_tool_names - desired_tool_names):
        unregister_mcp_tool(tool_name)

    for tool_name in desired_tool_names:
        TOOL_METADATA[tool_name] = desired_metadata[tool_name]
        _TOOL_REGISTRY[tool_name] = desired_registry[tool_name]

    _MCP_TOOL_NAMES.clear()
    _MCP_TOOL_NAMES.update(desired_tool_names)


def resolved_mcp_tool_state(
    config: Config | None,
) -> tuple[dict[str, Callable[[], type[Toolkit]]], dict[str, ToolMetadata]]:
    """Return the MCP tool registry entries implied by one config without mutating globals."""
    registry: dict[str, Callable[[], type[Toolkit]]] = {}
    metadata: dict[str, ToolMetadata] = {}
    for server_id, server_config in _desired_server_entries(config).items():
        tool_name = mcp_tool_name(server_id)
        registry[tool_name] = _tool_factory(server_id)
        metadata[tool_name] = _tool_metadata(server_id, server_config)
    return registry, metadata
