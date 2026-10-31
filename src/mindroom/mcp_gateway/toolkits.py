"""Construction and lifecycle ownership for selected gateway toolkits."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from functools import partial
from typing import TYPE_CHECKING, cast

from mindroom.credentials.manager import get_runtime_credentials_manager
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.mcp_gateway.execution import retain_execution_task, run_gateway_sync
from mindroom.mcp_gateway.types import GatewayError, GatewayErrorCode
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import configure_toolkit_functions
from mindroom.tool_system.sandbox_proxy import tool_placement
from mindroom.tool_system.toolkit_connections import (
    RetainedToolkitCleanups,
    release_toolkit,
    toolkit_call_lifetime,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.api.connection_agents import AgentToolContext
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.mcp.manager import MCPServerManager
    from mindroom.tool_system.agno_toolkits import AnyToolkit
    from mindroom.tool_system.tool_function import ToolValue

_CLEANUPS = RetainedToolkitCleanups(failure_event="mcp_gateway_tool_cleanup_failed", adopt=retain_execution_task)
# Toolkit construction, connect, and close, synchronous tool bodies, and the worker proxy requests of
# async ones hold their thread until they return, even after a timeout or cancel. A separate pool keeps
# them off the default executor that authentication, OAuth storage, and the Matrix runtime share; chat
# tool calls give each worker proxy request its own thread instead. A call holds one thread for each such
# body or request it runs at once, its own and those of tools it calls, so the per-user call limit bounds a
# user's threads only for calls that run one at a time.
_TOOL_EXECUTOR = ThreadPoolExecutor(max_workers=32, thread_name_prefix="mindroom-mcp-gateway-tool")


async def _on_tool_pool[T](operation: Callable[[], T]) -> T:
    """Run blocking toolkit work on the gateway pool in the caller's context, like ``asyncio.to_thread``."""
    return await asyncio.get_running_loop().run_in_executor(_TOOL_EXECUTOR, copy_context().run, operation)


async def drain_gateway_tool_cleanup() -> None:
    """Drain retained tool owners before shutting down the gateway's MCP manager."""
    await _CLEANUPS.drain()


async def _close_once_built(building: asyncio.Task[AnyToolkit]) -> None:
    """Close the toolkit a cancelled call was building, once its construction returns."""
    try:
        toolkit = await building
    except Exception:
        return
    await release_toolkit(toolkit, executor=_TOOL_EXECUTOR)


def _build_native(context: AgentToolContext, entry: EffectiveToolConfig) -> AnyToolkit:
    from mindroom.agents import build_agent_toolkit  # noqa: PLC0415
    from mindroom.runtime_resolution import resolve_agent_runtime  # noqa: PLC0415

    runtime = resolve_agent_runtime(
        context.agent_name,
        context.config,
        context.runtime_paths,
        execution_identity=context.execution_identity,
        create=True,
    )
    # The registry the request selected the entry from, so the build cannot meet another plugin generation.
    agent_tools = resolve_agent_tools(context.config, context.agent_name, context.plugins.tools)
    placement = tool_placement(context.runtime_paths, agent_tools.tools)
    toolkit = build_agent_toolkit(
        entry.name,
        agent_tools=agent_tools,
        placement=placement,
        runtime_paths=context.runtime_paths,
        workspace_in_worker=agent_tools.workspace_in_worker([entry.name], placement),
        agent_runtime=runtime,
        tool_config_overrides=entry.tool_config_overrides,
        execution_identity=context.execution_identity,
    )
    if toolkit is None:
        raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)
    return toolkit


class _GatewayMCPToolkit(MindRoomMCPToolkit):
    """Keep the exact request configuration attached to every upstream dispatch."""

    context: AgentToolContext
    require_current_access: Callable[[], None] | None

    async def _before_dispatch(self) -> None:
        if self.require_current_access is not None:
            await run_gateway_sync(self.require_current_access)

    async def _call_tool_with_error_payload(self, tool_name: str, arguments: dict[str, object]) -> ToolValue:
        if self.manager is None:
            raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)
        return await self.manager.call_tool(
            self.server_id,
            tool_name,
            arguments,
            timeout_seconds=self.call_timeout_seconds,
            credentials_manager=self.credentials_manager,
            worker_target=self.worker_target,
            include_tools=self.include_tools,
            exclude_tools=self.exclude_tools,
            expected_config=self.context.config,
            before_dispatch=self._before_dispatch,
        )


async def _build_selected(
    context: AgentToolContext,
    entry: EffectiveToolConfig,
    manager: MCPServerManager | None,
    require_current_access: Callable[[], None] | None,
) -> AnyToolkit:
    server_id = context.plugins.tools.mcp_server_id(entry.name)
    if server_id is not None:
        if manager is None:
            raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)
        server = context.config.mcp_servers[server_id]
        credentials = get_runtime_credentials_manager(context.runtime_paths)
        catalog = await manager.get_request_catalog(
            server_id,
            credentials_manager=credentials,
            worker_target=context.worker_target,
            expected_config=context.config,
        )
        toolkit = _GatewayMCPToolkit(
            server_id=server_id,
            manager=manager,
            catalog=catalog,
            server_config=server,
            runtime_paths=context.runtime_paths,
            credentials_manager=credentials,
            worker_target=context.worker_target,
            include_tools=cast("list[str] | str | None", entry.tool_config_overrides.get("include_tools")),
            exclude_tools=cast("list[str] | str | None", entry.tool_config_overrides.get("exclude_tools")),
            call_timeout_seconds=cast("float | None", entry.tool_config_overrides.get("call_timeout_seconds")),
        )
        toolkit.context = context
        toolkit.require_current_access = require_current_access
        # Generic OAuth bridge dispatch cannot carry per-function approval policy.
        return configure_toolkit_functions(toolkit, offered={tool.function_name for tool in catalog.tools})
    task = asyncio.create_task(_on_tool_pool(partial(_build_native, context, entry)))
    retain_execution_task(task)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        _CLEANUPS.retain(asyncio.create_task(_close_once_built(task), name="mcp-gateway-tool-cleanup"))
        raise


async def run_toolkit_operation[T](
    context: AgentToolContext,
    entry: EffectiveToolConfig,
    manager: MCPServerManager | None,
    operation: Callable[[AnyToolkit], Awaitable[T]],
    require_current_access: Callable[[], None] | None = None,
) -> T:
    """Build, connect, operate on, and close one selected gateway toolkit."""
    toolkit = await _build_selected(context, entry, manager, require_current_access)
    async with toolkit_call_lifetime(toolkit, _CLEANUPS, executor=_TOOL_EXECUTOR):
        return await operation(toolkit)
