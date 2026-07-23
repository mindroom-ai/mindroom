"""MindRoom toolkit wrapper for cached MCP catalogs."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from agno.tools import Toolkit
from agno.tools.function import Function

from mindroom.mcp.config import resolved_mcp_tool_prefix
from mindroom.oauth.providers import OAuthConnectionRequired, oauth_connection_required_payload

if TYPE_CHECKING:
    from agno.tools.function import ToolResult

    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.mcp.config import MCPServerConfig
    from mindroom.mcp.manager import MCPServerManager
    from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

_ACTIVE_MCP_SERVER_MANAGER: MCPServerManager | None = None


def bind_mcp_server_manager(manager: MCPServerManager | None) -> None:
    """Bind the active runtime manager used by dynamic registry factories."""
    global _ACTIVE_MCP_SERVER_MANAGER
    _ACTIVE_MCP_SERVER_MANAGER = manager


def require_mcp_server_manager() -> MCPServerManager | None:
    """Return the active runtime manager when one is bound."""
    return _ACTIVE_MCP_SERVER_MANAGER


def _normalize_tool_name_filter(value: list[str] | str | None) -> list[str] | None:
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
        server_config: MCPServerConfig | None = None,
        include_tools: list[str] | str | None = None,
        exclude_tools: list[str] | str | None = None,
        call_timeout_seconds: float | None = None,
        runtime_paths: RuntimePaths | None = None,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> None:
        super().__init__(
            name=catalog.tool_name if catalog is not None else (tool_name or server_id),
            auto_register=False,
        )
        self.server_id = server_id
        self.manager = manager
        self.catalog = catalog
        self.server_config = server_config
        self.runtime_paths = runtime_paths
        self.credentials_manager = credentials_manager
        self.worker_target = worker_target
        self.call_timeout_seconds = float(call_timeout_seconds) if call_timeout_seconds is not None else None
        self.include_tools = _normalize_tool_name_filter(include_tools)
        self.exclude_tools = _normalize_tool_name_filter(exclude_tools)
        if self._is_oauth_backed():
            self._register_oauth_bridge_tools()
            if self.manager is not None:
                cached_catalog = self.manager.cached_request_catalog(
                    self.server_id,
                    worker_target=self.worker_target,
                )
                if cached_catalog is not None:
                    self.catalog = cached_catalog
                    self._register_catalog_tools(self._filtered_tools())
            return
        if self.manager is None or self.catalog is None:
            return
        filtered_tools = self._filtered_tools()
        self._register_catalog_tools(filtered_tools)

    def _is_oauth_backed(self) -> bool:
        return self.server_config is not None and self.server_config.auth is not None

    def _filtered_catalog_tools(self, catalog: MCPServerCatalog) -> list[MCPDiscoveredTool]:
        filtered: list[MCPDiscoveredTool] = []
        include_tools = set(self.include_tools or [])
        exclude_tools = set(self.exclude_tools or [])
        for tool in catalog.tools:
            if exclude_tools and tool.remote_name in exclude_tools:
                continue
            if include_tools and tool.remote_name not in include_tools:
                continue
            filtered.append(tool)
        return filtered

    def _filtered_tools(self) -> list[MCPDiscoveredTool]:
        if self.catalog is None:
            return []
        return self._filtered_catalog_tools(self.catalog)

    def _register_catalog_tools(self, tools: list[MCPDiscoveredTool]) -> None:
        seen: set[str] = set(self.async_functions)
        for tool in tools:
            if tool.function_name in seen:
                msg = f"Duplicate MCP function name '{tool.function_name}' for server '{self.server_id}'"
                raise ValueError(msg)
            seen.add(tool.function_name)
            self.async_functions[tool.function_name] = self._build_function(tool)

    def _register_oauth_bridge_tools(self) -> None:
        if self.server_config is None:
            return
        tool_prefix = resolved_mcp_tool_prefix(self.server_id, self.server_config)
        # Before the requester signs in, these bridge functions are the only
        # model-visible surface for the server, so the configured description
        # is the model's only hint about what connecting would unlock.
        suffix = f" {self.server_config.description}" if self.server_config.description else ""
        self.async_functions[f"{tool_prefix}_connection_status"] = Function(
            name=f"{tool_prefix}_connection_status",
            description=f"Check whether MCP server '{self.server_id}' is connected for the current requester.{suffix}",
            parameters={"type": "object", "properties": {}},
            entrypoint=self._oauth_connection_status,
            skip_entrypoint_processing=True,
        )
        self.async_functions[f"{tool_prefix}_list_tools"] = Function(
            name=f"{tool_prefix}_list_tools",
            description=f"List remote tools exposed by MCP server '{self.server_id}' for the current requester.{suffix}",
            parameters={"type": "object", "properties": {}},
            entrypoint=self._oauth_list_tools,
            skip_entrypoint_processing=True,
        )
        self.async_functions[f"{tool_prefix}_call_tool"] = Function(
            name=f"{tool_prefix}_call_tool",
            description=f"Call one remote tool on MCP server '{self.server_id}' for the current requester.{suffix}",
            parameters={
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Remote MCP tool name to call.",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Arguments to pass to the remote MCP tool.",
                    },
                },
                "required": ["tool_name", "arguments"],
            },
            entrypoint=self._oauth_call_tool,
            skip_entrypoint_processing=True,
        )

    def _oauth_payload(self, exc: OAuthConnectionRequired) -> str:
        return json.dumps(oauth_connection_required_payload(exc))

    async def _oauth_request_catalog(self) -> MCPServerCatalog:
        if self.manager is None:
            msg = f"MCP server '{self.server_id}' is not connected"
            raise RuntimeError(msg)
        return await self.manager.get_request_catalog(
            self.server_id,
            credentials_manager=self.credentials_manager,
            worker_target=self.worker_target,
        )

    async def _oauth_connection_status(self) -> str:
        try:
            catalog = await self._oauth_request_catalog()
        except OAuthConnectionRequired as exc:
            return self._oauth_payload(exc)
        return json.dumps(
            {
                "connected": True,
                "server_id": self.server_id,
                "tool_count": len(self._filtered_catalog_tools(catalog)),
            },
        )

    def _catalog_payload(self, catalog: MCPServerCatalog) -> dict[str, object]:
        return {
            "server_id": catalog.server_id,
            "instructions": catalog.instructions,
            "tools": [
                {
                    "name": tool.remote_name,
                    "description": tool.description,
                    "input_schema": tool.input_schema,
                    "output_schema": tool.output_schema,
                    "title": tool.title,
                }
                for tool in self._filtered_catalog_tools(catalog)
            ],
        }

    async def _oauth_list_tools(self) -> str:
        try:
            catalog = await self._oauth_request_catalog()
        except OAuthConnectionRequired as exc:
            return self._oauth_payload(exc)
        return json.dumps(self._catalog_payload(catalog))

    async def _oauth_call_tool(self, *, tool_name: str, arguments: dict[str, object] | None = None) -> ToolResult | str:
        try:
            catalog = await self._oauth_request_catalog()
        except OAuthConnectionRequired as exc:
            return self._oauth_payload(exc)

        tools_by_name = {tool.remote_name: tool for tool in self._filtered_catalog_tools(catalog)}
        if tool_name not in tools_by_name:
            return json.dumps(
                {
                    "error": f"MCP tool '{tool_name}' is not available for server '{self.server_id}'",
                    "available_tools": sorted(tools_by_name),
                },
            )
        if self.manager is None:
            msg = f"MCP server '{self.server_id}' is not connected"
            raise RuntimeError(msg)
        try:
            return await self.manager.call_tool(
                self.server_id,
                tool_name,
                dict(arguments or {}),
                timeout_seconds=self.call_timeout_seconds,
                credentials_manager=self.credentials_manager,
                worker_target=self.worker_target,
            )
        except OAuthConnectionRequired as exc:
            return self._oauth_payload(exc)

    def _build_function(self, tool: MCPDiscoveredTool) -> Function:
        async def _call_tool(**kwargs: object) -> ToolResult:
            assert self.manager is not None
            return await self.manager.call_tool(
                self.server_id,
                tool.remote_name,
                dict(kwargs),
                timeout_seconds=self.call_timeout_seconds,
                credentials_manager=self.credentials_manager,
                worker_target=self.worker_target,
            )

        return Function(
            name=tool.function_name,
            description=tool.description,
            parameters=tool.input_schema,
            entrypoint=_call_tool,
            skip_entrypoint_processing=True,
        )
