"""Construction and lifecycle ownership for selected gateway toolkits."""

from __future__ import annotations

import asyncio
import inspect
from contextlib import suppress
from typing import TYPE_CHECKING, Any, cast

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.mcp_gateway.execution import retain_execution_task, run_gateway_sync
from mindroom.mcp_gateway.types import GatewayError, GatewayErrorCode
from mindroom.tool_system.catalog import TOOL_METADATA
from mindroom.tool_system.tool_hooks import SyncToolCompletionTracker, track_sync_tool_completion

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from agno.tools import Toolkit
    from agno.tools.function import ToolResult

    from mindroom.api.personal_agent import PersonalAgentContext
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.mcp.manager import MCPServerManager

_CLEANUP_TASKS: set[asyncio.Task[None]] = set()
logger = get_logger(__name__)


async def _lifecycle(operation: Callable[[], object]) -> None:
    result = operation() if inspect.iscoroutinefunction(operation) else await run_gateway_sync(operation)
    if inspect.isawaitable(result):
        await result


async def _close(toolkit: Toolkit) -> None:
    if toolkit.requires_connect:
        await _lifecycle(toolkit.close)


def _retain(cleanup: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.create_task(cleanup, name="mcp-gateway-tool-cleanup")
    retain_execution_task(task)
    _CLEANUP_TASKS.add(task)

    def finished(completed: asyncio.Task[None]) -> None:
        _CLEANUP_TASKS.discard(completed)
        if completed.cancelled():
            return
        if (error := completed.exception()) is not None:
            logger.warning("mcp_gateway_tool_cleanup_failed", error_type=type(error).__name__)

    task.add_done_callback(finished)
    return task


async def drain_gateway_tool_cleanup() -> None:
    """Drain retained tool owners before shutting down the gateway's MCP manager."""
    while _CLEANUP_TASKS:
        await asyncio.gather(*(asyncio.shield(task) for task in tuple(_CLEANUP_TASKS)), return_exceptions=True)


async def _close_after(pending: asyncio.Task[Any], toolkit: Toolkit | None = None) -> None:
    with suppress(BaseException):
        result = await pending
        if toolkit is None:
            toolkit = result
    if toolkit is not None:
        await _close(toolkit)


def _build_native(context: PersonalAgentContext, entry: EffectiveToolConfig) -> Toolkit:
    from mindroom.agents import build_agent_toolkit, resolve_runtime_worker_tools  # noqa: PLC0415
    from mindroom.runtime_resolution import resolve_agent_runtime  # noqa: PLC0415

    metadata = TOOL_METADATA.get(entry.name)
    if metadata is not None and metadata.requires_room_context:
        raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)
    runtime = resolve_agent_runtime(
        context.agent_name,
        context.config,
        context.runtime_paths,
        execution_identity=context.execution_identity,
        create=True,
    )
    worker_tools = resolve_runtime_worker_tools(
        context.agent_name,
        context.config,
        context.runtime_paths,
        [entry.name],
        tool_registry_preloaded=True,
    )
    toolkit = build_agent_toolkit(
        entry.name,
        agent_name=context.agent_name,
        config=context.config,
        runtime_paths=context.runtime_paths,
        worker_tools=worker_tools,
        runtime_overrides=context.config.resolve_entity(context.agent_name).tool_runtime_overrides(entry.name),
        agent_runtime=runtime,
        tool_config_overrides=entry.tool_config_overrides,
        execution_identity=context.execution_identity,
    )
    if toolkit is None:
        raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)
    return toolkit


class _GatewayMCPToolkit(MindRoomMCPToolkit):
    """Keep the exact request configuration attached to every upstream dispatch."""

    context: PersonalAgentContext

    async def _call_tool_with_error_payload(self, tool_name: str, arguments: dict[str, object]) -> ToolResult:
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
        )


async def _build_selected(
    context: PersonalAgentContext,
    entry: EffectiveToolConfig,
    manager: MCPServerManager | None,
) -> Toolkit:
    server_id = entry.name.removeprefix("mcp_")
    server = context.config.mcp_servers.get(server_id) if entry.name.startswith("mcp_") else None
    if server is not None:
        if not server.enabled or manager is None:
            raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)
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
        # Generic OAuth bridge dispatch cannot carry per-function approval policy.
        typed_names = {tool.function_name for tool in catalog.tools}
        toolkit.async_functions = {
            name: function for name, function in toolkit.async_functions.items() if name in typed_names
        }
        return toolkit
    task = asyncio.create_task(asyncio.to_thread(_build_native, context, entry))
    retain_execution_task(task)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        _retain(_close_after(task))
        raise


async def run_toolkit_operation[T](
    context: PersonalAgentContext,
    entry: EffectiveToolConfig,
    manager: MCPServerManager | None,
    operation: Callable[[Toolkit], Awaitable[T]],
) -> T:
    """Build, connect, operate on, and close one selected gateway toolkit."""
    toolkit = await _build_selected(context, entry, manager)
    tracker = SyncToolCompletionTracker()
    pending: asyncio.Task[Any] | None = None
    cancelled = False
    try:
        if toolkit.requires_connect:
            pending = asyncio.create_task(_lifecycle(toolkit.connect))
            await asyncio.shield(pending)
            pending = None
        with track_sync_tool_completion(tracker):
            return await operation(toolkit)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        pending = pending or tracker.started_task()
        cleanup = _close_after(pending, toolkit) if pending is not None else _close(toolkit)
        close_task = _retain(cleanup)
        if not cancelled and (pending is None or pending.done()):
            await asyncio.shield(close_task)
