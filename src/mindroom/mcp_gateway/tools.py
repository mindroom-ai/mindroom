"""Bounded, requester-scoped tool discovery and direct execution."""

from __future__ import annotations

import asyncio
import inspect
import json
from contextlib import suppress
from typing import TYPE_CHECKING, Any, Never, cast

from agno.tools.function import FunctionCall
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

from mindroom.credentials import get_runtime_credentials_manager
from mindroom.hooks import HookRegistry
from mindroom.mcp.toolkit import MindRoomMCPToolkit
from mindroom.oauth.providers import OAuthConnectionRequired
from mindroom.tool_approval import tool_may_require_approval
from mindroom.tool_schema_cache import cached_processed_schema
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.runtime_context import (
    ToolDispatchContext,
    WorkerRuntimeContext,
    tool_runtime_context,
    worker_runtime_context,
)
from mindroom.tool_system.tool_hooks import (
    SyncToolCompletionTracker,
    build_tool_hook_bridge,
    prepend_tool_hook_bridge,
    track_sync_tool_completion,
)
from mindroom.tool_system.worker_proxy_client import to_json_compatible
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from agno.tools import Toolkit
    from agno.tools.function import Function, ToolResult

    from mindroom.api.personal_agent import PersonalAgentContext
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.mcp.manager import MCPServerManager

_CLEANUP_TASKS: set[asyncio.Task[None]] = set()
_MESSAGES = {
    "connection_required": "Connect this service to continue.",
    "tool_not_found": "This tool is not assigned to your personal agent.",
    "approval_required": "This tool requires approval and cannot run through this gateway.",
    "tool_unavailable": "This tool is currently unavailable.",
    "invalid_arguments": "Tool arguments are invalid or exceed the allowed size.",
    "result_too_large": "The tool result exceeds the allowed size.",
    "schema_too_large": "The tool schema exceeds the allowed size.",
}


class _GatewayError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _error(context: PersonalAgentContext, code: str) -> dict[str, Any]:
    error = {"code": code, "message": _MESSAGES[code]}
    if code == "connection_required":
        origin = (context.runtime_paths.env_value("MINDROOM_PUBLIC_URL") or "").rstrip("/")
        error["connection_url"] = f"{origin}/connections"
    return {"error": error}


def _json_size(value: object) -> int:
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def _handle(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128


def _entries(context: PersonalAgentContext) -> dict[str, EffectiveToolConfig]:
    ensure_tool_registry_loaded(context.runtime_paths, context.config)
    entity = context.config.resolve_entity(context.agent_name)
    authored = {entry.name for entry in entity.authored_tool_configs}
    return {
        entry.name: entry
        for entry in visible_tool_surface(
            agent_name=context.agent_name,
            config=context.config,
            loaded_tools=[entry.name for entry in entity.authored_deferred_tool_configs],
            enable_dynamic_tools_manager=False,
        ).runtime_tool_configs
        if entry.authored_name in authored
    }


def _require_entry(context: PersonalAgentContext, name: str) -> EffectiveToolConfig:
    if not _handle(name):
        raise _GatewayError(code="tool_not_found")
    entry = _entries(context).get(name)
    if entry is None:
        raise _GatewayError(code="tool_not_found")
    return entry


def _blocked(context: PersonalAgentContext, function: Function) -> bool:
    return function.requires_confirmation is True or tool_may_require_approval(context.config, function.name)


def _function(context: PersonalAgentContext, toolkit: Toolkit, name: str) -> Function:
    function = {**toolkit.functions, **toolkit.async_functions}.get(name)
    if function is None:
        raise _GatewayError(code="tool_not_found")
    if _blocked(context, function):
        raise _GatewayError(code="approval_required")
    return function


def _schema(function: Function) -> dict[str, Any]:
    if function.skip_entrypoint_processing or function.entrypoint is None:
        return function.parameters
    strict = function.strict is True
    snapshot = cached_processed_schema(function, strict=strict)
    if snapshot is not None:
        return snapshot.parameters
    prepared = function.model_copy(deep=True)
    prepared.process_entrypoint(strict=strict)
    return prepared.parameters


def _schema_payload(toolkit: str, function: Function) -> dict[str, Any]:
    payload = {
        "toolkit": toolkit,
        "function": function.name,
        "description": function.description or "",
        "inputSchema": _schema(function),
    }
    if _json_size(payload) > 32768:
        raise _GatewayError(code="schema_too_large")
    return payload


def _no_remote_schema(uri: str) -> Never:
    raise NoSuchResource(ref=uri)


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, object]) -> None:
    try:
        Draft202012Validator(schema, registry=Registry(retrieve=_no_remote_schema)).validate(arguments)
    except Exception as exc:
        raise _GatewayError(code="invalid_arguments") from exc


async def _lifecycle(operation: Callable[[], object]) -> None:
    result = operation() if inspect.iscoroutinefunction(operation) else await asyncio.to_thread(operation)
    if inspect.isawaitable(result):
        await result


async def _close(toolkit: Toolkit) -> None:
    if toolkit.requires_connect:
        await _lifecycle(toolkit.close)


def _retain(cleanup: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    task = asyncio.create_task(cleanup, name="mcp-gateway-tool-cleanup")
    _CLEANUP_TASKS.add(task)

    def finished(completed: asyncio.Task[None]) -> None:
        _CLEANUP_TASKS.discard(completed)
        if not completed.cancelled():
            completed.exception()

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
        raise _GatewayError(code="tool_unavailable")
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
        raise _GatewayError(code="tool_unavailable")
    return toolkit


class _GatewayMCPToolkit(MindRoomMCPToolkit):
    """Keep the exact request configuration attached to every upstream dispatch."""

    context: PersonalAgentContext

    async def _call_tool_with_error_payload(self, tool_name: str, arguments: dict[str, object]) -> ToolResult:
        if self.manager is None:
            raise _GatewayError(code="tool_unavailable")
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
            raise _GatewayError(code="tool_unavailable")
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
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        _retain(_close_after(task))
        raise


async def _selected_operation(
    context: PersonalAgentContext,
    name: str,
    manager: MCPServerManager | None,
    operation: Callable[[Toolkit], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    with (
        tool_runtime_context(None),
        worker_runtime_context(WorkerRuntimeContext(runtime_paths=context.runtime_paths, config=context.config)),
    ):
        return await run_with_tool_execution_identity(
            context.execution_identity,
            operation=lambda: _run_selected_operation(context, name, manager, operation),
        )


async def _run_selected_operation(
    context: PersonalAgentContext,
    name: str,
    manager: MCPServerManager | None,
    operation: Callable[[Toolkit], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    entry = await asyncio.to_thread(_require_entry, context, name)
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


async def _guard(context: PersonalAgentContext, operation: Awaitable[dict[str, Any]]) -> dict[str, Any]:
    try:
        return await operation
    except _GatewayError as exc:
        return _error(context, exc.code)
    except OAuthConnectionRequired:
        return _error(context, "connection_required")
    except Exception:
        return _error(context, "tool_unavailable")


def _search_results(items: list[dict[str, Any]], query: str, limit: int) -> dict[str, Any]:
    words = query.lower().split()
    ranked = [
        item for item in items if all(word in " ".join(str(value) for value in item.values()).lower() for word in words)
    ]
    result = {"results": ranked[:limit]}
    while result["results"] and _json_size(result) > 16384:
        result["results"].pop()
    return result


async def search_tools(
    context: PersonalAgentContext,
    *,
    query: str = "",
    toolkit: str | None = None,
    limit: int = 5,
    manager: MCPServerManager | None = None,
) -> dict[str, Any]:
    """Search assigned toolkit metadata or one selected function catalog without schemas."""
    if not isinstance(query, str) or len(query) > 256 or not isinstance(limit, int) or isinstance(limit, bool):
        return _error(context, "invalid_arguments")
    limit = max(1, min(limit, 10))

    async def selected(built: Toolkit) -> dict[str, Any]:
        items = [
            {"toolkit": toolkit, "function": function.name, "description": (function.description or "")[:256]}
            for function in {**built.functions, **built.async_functions}.values()
            if _handle(function.name) and not _blocked(context, function)
        ]
        return _search_results(items, query, limit)

    async def search() -> dict[str, Any]:
        if toolkit is not None:
            return await _selected_operation(context, toolkit, manager, selected)
        entries = await asyncio.to_thread(_entries, context)
        items = []
        for name in entries:
            if not _handle(name):
                continue
            metadata = TOOL_METADATA.get(name)
            server = context.config.mcp_servers.get(name.removeprefix("mcp_")) if name.startswith("mcp_") else None
            description = (
                (server.description or "Remote tools")
                if server is not None
                else (metadata.description if metadata else name)
            )
            items.append(
                {
                    "toolkit": name,
                    "description": description[:256],
                    "next": "Search this toolkit to see its functions.",
                },
            )
        return _search_results(items, query, limit)

    return await _guard(context, search())


async def get_tool(
    context: PersonalAgentContext,
    *,
    toolkit: str,
    function: str,
    manager: MCPServerManager | None = None,
) -> dict[str, Any]:
    """Return the bounded schema for one currently assigned, ungated function."""
    if not _handle(function):
        return _error(context, "tool_not_found")

    async def selected(built: Toolkit) -> dict[str, Any]:
        return _schema_payload(toolkit, _function(context, built, function))

    return await _guard(context, _selected_operation(context, toolkit, manager, selected))


def _connection_required(result: object) -> bool:
    if isinstance(result, str):
        with suppress(ValueError, TypeError):
            result = json.loads(result)
    return isinstance(result, dict) and cast("dict[str, object]", result).get("oauth_connection_required") is True


async def invoke_tool(
    context: PersonalAgentContext,
    *,
    toolkit: str,
    function: str,
    arguments: dict[str, object],
    manager: MCPServerManager | None = None,
) -> dict[str, Any]:
    """Invoke one selected function with canonical routing, hooks, and fresh credentials."""
    if not _handle(function):
        return _error(context, "tool_not_found")
    try:
        if not isinstance(arguments, dict) or _json_size(arguments) > 65536:
            return _error(context, "invalid_arguments")
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return _error(context, "invalid_arguments")

    async def selected(built: Toolkit) -> dict[str, Any]:
        target = _function(context, built, function)
        if inspect.isgeneratorfunction(target.entrypoint) or inspect.isasyncgenfunction(target.entrypoint):
            raise _GatewayError(code="tool_unavailable")
        schema = _schema_payload(toolkit, target)["inputSchema"]
        _validate_arguments(schema, arguments)
        plugins = await asyncio.to_thread(load_plugins, context.config, context.runtime_paths, set_skill_roots=False)
        bridge = build_tool_hook_bridge(
            HookRegistry.from_plugins(plugins),
            agent_name=context.agent_name,
            dispatch_context=ToolDispatchContext(execution_identity=context.execution_identity),
            config=context.config,
            runtime_paths=context.runtime_paths,
        )
        prepend_tool_hook_bridge(built, bridge)
        target.cache_results = False
        execution = await run_with_tool_execution_identity(
            context.execution_identity,
            operation=lambda: FunctionCall(function=target, arguments=arguments).aexecute(),
        )
        if execution.status != "success":
            raise _GatewayError(code="tool_unavailable")
        result = to_json_compatible(execution.result)
        if _connection_required(result):
            raise _GatewayError(code="connection_required")
        payload = {"result": result}
        if _json_size(payload) > 65536:
            raise _GatewayError(code="result_too_large")
        return payload

    return await _guard(context, _selected_operation(context, toolkit, manager, selected))
