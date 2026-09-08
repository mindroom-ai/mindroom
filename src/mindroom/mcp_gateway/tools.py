"""Bounded, requester-scoped tool discovery and direct execution."""

from __future__ import annotations

import inspect
import json
from contextlib import suppress
from functools import wraps
from typing import TYPE_CHECKING, Any, Never

from agno.tools.function import FunctionCall
from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

from mindroom.hooks import HookRegistry
from mindroom.mcp_gateway.execution import run_gateway_sync
from mindroom.mcp_gateway.toolkits import run_toolkit_operation
from mindroom.mcp_gateway.types import (
    GatewayError,
    GatewayErrorCode,
    GatewayErrorDetail,
    GatewayErrorResponse,
    GatewaySuccessResponse,
    InvocationResponse,
    InvocationResult,
    SearchItem,
    SearchResponse,
    SearchResult,
    ToolSchemaResponse,
    ToolSchemaResult,
)
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
from mindroom.tool_system.tool_hooks import build_tool_hook_bridge, prepend_tool_hook_bridge
from mindroom.tool_system.worker_proxy_client import to_json_compatible
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.tools import Toolkit
    from agno.tools.function import Function

    from mindroom.api.personal_agent import PersonalAgentContext
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.mcp.manager import MCPServerManager

_MESSAGES: dict[GatewayErrorCode, str] = {
    GatewayErrorCode.CONNECTION_REQUIRED: "Connect this service to continue.",
    GatewayErrorCode.TOOL_NOT_FOUND: "This tool is not assigned to your personal agent.",
    GatewayErrorCode.APPROVAL_REQUIRED: "This tool requires approval and cannot run through this gateway.",
    GatewayErrorCode.TOOL_UNAVAILABLE: "This tool is currently unavailable.",
    GatewayErrorCode.INVALID_ARGUMENTS: "Tool arguments are invalid or exceed the allowed size.",
    GatewayErrorCode.RESULT_TOO_LARGE: "The tool result exceeds the allowed size.",
    GatewayErrorCode.SCHEMA_TOO_LARGE: "The tool schema exceeds the allowed size.",
}


def _error(context: PersonalAgentContext, code: GatewayErrorCode) -> GatewayErrorResponse:
    error: GatewayErrorDetail = {"code": code, "message": _MESSAGES[code]}
    if code is GatewayErrorCode.CONNECTION_REQUIRED:
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
        raise GatewayError(code=GatewayErrorCode.TOOL_NOT_FOUND)
    entry = _entries(context).get(name)
    if entry is None:
        raise GatewayError(code=GatewayErrorCode.TOOL_NOT_FOUND)
    return entry


def _blocked(context: PersonalAgentContext, function: Function) -> bool:
    return function.requires_confirmation is True or tool_may_require_approval(context.config, function.name)


def _function(context: PersonalAgentContext, toolkit: Toolkit, name: str) -> Function:
    function = {**toolkit.functions, **toolkit.async_functions}.get(name)
    if function is None:
        raise GatewayError(code=GatewayErrorCode.TOOL_NOT_FOUND)
    if _blocked(context, function):
        raise GatewayError(code=GatewayErrorCode.APPROVAL_REQUIRED)
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


def _schema_payload(toolkit: str, function: Function) -> ToolSchemaResponse:
    payload: ToolSchemaResponse = {
        "toolkit": toolkit,
        "function": function.name,
        "description": function.description or "",
        "inputSchema": _schema(function),
    }
    if _json_size(payload) > 32768:
        raise GatewayError(code=GatewayErrorCode.SCHEMA_TOO_LARGE)
    return payload


def _no_remote_schema(uri: str) -> Never:
    raise NoSuchResource(ref=uri)


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, object]) -> None:
    try:
        Draft202012Validator(schema, registry=Registry(retrieve=_no_remote_schema)).validate(arguments)
    except Exception as exc:
        raise GatewayError(code=GatewayErrorCode.INVALID_ARGUMENTS) from exc


async def _selected_operation[T](
    context: PersonalAgentContext,
    name: str,
    manager: MCPServerManager | None,
    operation: Callable[[Toolkit], Awaitable[T]],
) -> T:
    async def selected() -> T:
        entry = await run_gateway_sync(_require_entry, context, name)
        return await run_toolkit_operation(context, entry, manager, operation)

    with (
        tool_runtime_context(None),
        worker_runtime_context(WorkerRuntimeContext(runtime_paths=context.runtime_paths, config=context.config)),
    ):
        return await run_with_tool_execution_identity(
            context.execution_identity,
            operation=selected,
        )


async def _guard[T: GatewaySuccessResponse](
    context: PersonalAgentContext,
    operation: Awaitable[T],
) -> T | GatewayErrorResponse:
    try:
        return await operation
    except GatewayError as exc:
        return _error(context, exc.code)
    except OAuthConnectionRequired:
        return _error(context, GatewayErrorCode.CONNECTION_REQUIRED)
    except Exception:
        return _error(context, GatewayErrorCode.TOOL_UNAVAILABLE)


def _search_results(items: list[SearchItem], query: str, limit: int) -> SearchResponse:
    words = query.lower().split()
    ranked = [
        item for item in items if all(word in " ".join(str(value) for value in item.values()).lower() for word in words)
    ]
    result: SearchResponse = {"results": ranked[:limit]}
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
) -> SearchResult:
    """Search assigned toolkit metadata or one selected function catalog without schemas."""
    if not isinstance(query, str) or len(query) > 256 or not isinstance(limit, int) or isinstance(limit, bool):
        return _error(context, GatewayErrorCode.INVALID_ARGUMENTS)
    limit = max(1, min(limit, 10))

    async def search() -> SearchResponse:
        if toolkit is not None:
            selected_toolkit = toolkit

            async def selected(built: Toolkit) -> SearchResponse:
                items: list[SearchItem] = [
                    {
                        "toolkit": selected_toolkit,
                        "function": function.name,
                        "description": (function.description or "")[:256],
                    }
                    for function in {**built.functions, **built.async_functions}.values()
                    if _handle(function.name) and not _blocked(context, function)
                ]
                return _search_results(items, query, limit)

            return await _selected_operation(context, selected_toolkit, manager, selected)
        entries = await run_gateway_sync(_entries, context)
        items: list[SearchItem] = []
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
) -> ToolSchemaResult:
    """Return the bounded schema for one currently assigned, ungated function."""
    if not _handle(function):
        return _error(context, GatewayErrorCode.TOOL_NOT_FOUND)

    async def selected(built: Toolkit) -> ToolSchemaResponse:
        return _schema_payload(toolkit, _function(context, built, function))

    return await _guard(context, _selected_operation(context, toolkit, manager, selected))


def _connection_required(result: object) -> bool:
    if isinstance(result, str):
        with suppress(ValueError, TypeError):
            result = json.loads(result)
    return isinstance(result, dict) and any(
        key == "oauth_connection_required" and value is True for key, value in result.items()
    )


def _guard_dispatch(function: Function, require_current_config: Callable[[], None]) -> None:
    """Check the publication at the provider entrypoint, after hooks and thread scheduling."""
    entrypoint = function.entrypoint
    if entrypoint is None:
        raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)

    @wraps(entrypoint)
    def guarded_sync(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        require_current_config()
        return entrypoint(*args, **kwargs)

    @wraps(entrypoint)
    async def guarded_async(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        require_current_config()
        return await entrypoint(*args, **kwargs)

    function.entrypoint = guarded_async if inspect.iscoroutinefunction(entrypoint) else guarded_sync


async def invoke_tool(
    context: PersonalAgentContext,
    *,
    toolkit: str,
    function: str,
    arguments: dict[str, object],
    manager: MCPServerManager | None = None,
    require_current_config: Callable[[], None] | None = None,
) -> InvocationResult:
    """Invoke one selected function with canonical routing, hooks, and fresh credentials."""
    if not _handle(function):
        return _error(context, GatewayErrorCode.TOOL_NOT_FOUND)
    try:
        if not isinstance(arguments, dict) or _json_size(arguments) > 65536:
            return _error(context, GatewayErrorCode.INVALID_ARGUMENTS)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        return _error(context, GatewayErrorCode.INVALID_ARGUMENTS)

    async def selected(built: Toolkit) -> InvocationResponse:
        target = _function(context, built, function)
        if inspect.isgeneratorfunction(target.entrypoint) or inspect.isasyncgenfunction(target.entrypoint):
            raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)
        schema = _schema_payload(toolkit, target)["inputSchema"]
        _validate_arguments(schema, arguments)
        plugins = await run_gateway_sync(load_plugins, context.config, context.runtime_paths, set_skill_roots=False)
        bridge = build_tool_hook_bridge(
            HookRegistry.from_plugins(plugins),
            agent_name=context.agent_name,
            dispatch_context=ToolDispatchContext(execution_identity=context.execution_identity),
            config=context.config,
            runtime_paths=context.runtime_paths,
        )
        prepend_tool_hook_bridge(built, bridge)
        target.cache_results = False
        if require_current_config is not None:
            _guard_dispatch(target, require_current_config)
        execution = await run_with_tool_execution_identity(
            context.execution_identity,
            operation=lambda: FunctionCall(function=target, arguments=arguments).aexecute(),
        )
        if execution.status != "success":
            raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE)
        result = to_json_compatible(execution.result)
        if _connection_required(result):
            raise GatewayError(code=GatewayErrorCode.CONNECTION_REQUIRED)
        payload: InvocationResponse = {"result": result}
        if _json_size(payload) > 65536:
            raise GatewayError(code=GatewayErrorCode.RESULT_TOO_LARGE)
        return payload

    return await _guard(context, _selected_operation(context, toolkit, manager, selected))
