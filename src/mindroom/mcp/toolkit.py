"""MindRoom toolkit wrapper for cached MCP catalogs."""

from __future__ import annotations

from typing import TYPE_CHECKING

from agno.tools import Toolkit
from agno.tools.function import Function

if TYPE_CHECKING:
    from agno.tools.function import ToolResult

    from mindroom.mcp.manager import MCPServerManager
    from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog

_ACTIVE_MCP_SERVER_MANAGER: MCPServerManager | None = None


def bind_mcp_server_manager(manager: MCPServerManager | None) -> None:
    """Bind the active runtime manager used by dynamic registry factories."""
    global _ACTIVE_MCP_SERVER_MANAGER
    _ACTIVE_MCP_SERVER_MANAGER = manager


def require_mcp_server_manager() -> MCPServerManager | None:
    """Return the active runtime manager when one is bound."""
    return _ACTIVE_MCP_SERVER_MANAGER


def normalize_tool_name_filter(value: list[str] | str | None) -> list[str] | None:
    """Normalize filter values passed from tool config and runtime overrides."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = [part.strip() for part in value.replace("\n", ",").split(",") if part.strip()]
        return normalized or None
    normalized = [part.strip() for part in value if part.strip()]
    return normalized or None


class MindRoomMCPToolkit(Toolkit):
    """Toolkit that exposes cached MCP tools as async Agno functions."""

    def __init__(
        self,
        *,
        server_id: str,
        manager: MCPServerManager | None,
        catalog: MCPServerCatalog | None,
        tool_name: str | None = None,
        include_tools: list[str] | str | None = None,
        exclude_tools: list[str] | str | None = None,
        call_timeout_seconds: float | None = None,
    ) -> None:
        super().__init__(
            name=catalog.tool_name if catalog is not None else (tool_name or server_id),
            auto_register=False,
        )
        self.server_id = server_id
        self.manager = manager
        self.catalog = catalog
        self.call_timeout_seconds = float(call_timeout_seconds) if call_timeout_seconds is not None else None
        self.include_tools = normalize_tool_name_filter(include_tools)
        self.exclude_tools = normalize_tool_name_filter(exclude_tools)
        if self.manager is None or self.catalog is None:
            return
        filtered_tools = self._filtered_tools()
        self._register_catalog_tools(filtered_tools)

    def _filtered_tools(self) -> list[MCPDiscoveredTool]:
        if self.catalog is None:
            return []
        filtered: list[MCPDiscoveredTool] = []
        include_tools = set(self.include_tools or [])
        exclude_tools = set(self.exclude_tools or [])
        for tool in self.catalog.tools:
            if exclude_tools and tool.remote_name in exclude_tools:
                continue
            if include_tools and tool.remote_name not in include_tools:
                continue
            filtered.append(tool)
        return filtered

    def _register_catalog_tools(self, tools: list[MCPDiscoveredTool]) -> None:
        seen: set[str] = set()
        for tool in tools:
            if tool.function_name in seen:
                msg = f"Duplicate MCP function name '{tool.function_name}' for server '{self.server_id}'"
                raise ValueError(msg)
            seen.add(tool.function_name)
            self.async_functions[tool.function_name] = self._build_function(tool)

    def _build_function(self, tool: MCPDiscoveredTool) -> Function:
        async def _call_tool(**kwargs: object) -> ToolResult:
            assert self.manager is not None
            return await self.manager.call_tool(
                self.server_id,
                tool.remote_name,
                dict(kwargs),
                timeout_seconds=self.call_timeout_seconds,
            )

        return Function(
            name=tool.function_name,
            description=tool.description,
            parameters=tool.input_schema,
            entrypoint=_call_tool,
            skip_entrypoint_processing=True,
        )
