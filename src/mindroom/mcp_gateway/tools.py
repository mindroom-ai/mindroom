"""Bounded, requester-scoped tool discovery and direct execution."""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from functools import wraps
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from mindroom.approval.tool_approval import tool_may_require_approval
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
from mindroom.tool_system.agent_tools import resolve_agent_tools
from mindroom.tool_system.agno_toolkits import adapt_function, adapt_toolkit
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded
from mindroom.tool_system.invocation import (
    DETACHED_SESSION_ID,
    CallResult,
    ToolCallContext,
    client_tool_result,
    run_tool_call,
)
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.runtime_context import ToolDispatchContext, tool_runtime_context
from mindroom.tool_system.tool_access import search_tool_metadata, validate_tool_arguments
from mindroom.tool_system.tool_hooks import build_tool_hook_middleware
from mindroom.tool_system.worker_proxy_client import to_json_compatible
from mindroom.tool_system.worker_routing import run_with_tool_execution_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from mindroom.api.connection_agents import AgentToolContext
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.mcp.manager import MCPServerManager
    from mindroom.tool_system.agno_toolkits import Function, Toolkit
    from mindroom.tool_system.tool_function import ToolFunction

_MESSAGES: dict[GatewayErrorCode, str] = {
    GatewayErrorCode.CONNECTION_REQUIRED: "Connect this service to continue.",
    GatewayErrorCode.TOOL_NOT_FOUND: "This tool is not assigned to the selected agent.",
    GatewayErrorCode.UNAUTHORIZED: "Agent access has changed. Check your selection in Connections.",
    GatewayErrorCode.APPROVAL_REQUIRED: "This tool requires approval and cannot run through this gateway.",
    GatewayErrorCode.TOOL_UNAVAILABLE: "This tool is currently unavailable.",
    GatewayErrorCode.TOOL_DECLINED: "A MindRoom plugin declined this call, so it did not run.",
    GatewayErrorCode.TOOL_FAILED: "The tool reported that this call failed.",
    GatewayErrorCode.INVALID_ARGUMENTS: "Tool arguments are invalid or exceed the allowed size.",
    GatewayErrorCode.RESULT_TOO_LARGE: "The tool result exceeds the allowed size.",
    GatewayErrorCode.SCHEMA_TOO_LARGE: "The tool schema exceeds the allowed size.",
}


def _error(context: AgentToolContext, code: GatewayErrorCode, message: str | None = None) -> GatewayErrorResponse:
    error: GatewayErrorDetail = {"code": code, "message": message or _MESSAGES[code]}
    if code is GatewayErrorCode.CONNECTION_REQUIRED:
        origin = (context.runtime_paths.env_value("MINDROOM_PUBLIC_URL") or "").rstrip("/")
        error["connection_url"] = f"{origin}/connections"
    return {"error": error}


def _json_size(value: object) -> int:
    return len(json.dumps(value, allow_nan=False).encode("utf-8"))


def _handle(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 128


def _entries(context: AgentToolContext) -> dict[str, EffectiveToolConfig]:
    ensure_tool_registry_loaded(context.runtime_paths, context.config)
    agent_tools = resolve_agent_tools(context.config, context.agent_name)
    authored = {entry.name for entry in agent_tools.facts.authored}
    return {
        entry.name: entry
        for entry in agent_tools.candidates()
        if entry.authored_name in authored
        and ((metadata := TOOL_METADATA.get(entry.name)) is None or metadata.mcp_available)
    }


def _require_entry(context: AgentToolContext, name: str) -> EffectiveToolConfig:
    if not _handle(name):
        raise GatewayError(code=GatewayErrorCode.TOOL_NOT_FOUND)
    entry = _entries(context).get(name)
    if entry is None:
        raise GatewayError(code=GatewayErrorCode.TOOL_NOT_FOUND)
    return entry


def _blocked(context: AgentToolContext, function: Function | ToolFunction) -> bool:
    return function.requires_confirmation is True or tool_may_require_approval(context.config, function.name)


def _function(context: AgentToolContext, toolkit: Toolkit, name: str) -> Function:
    function = toolkit.get_async_functions().get(name)
    if function is None:
        raise GatewayError(code=GatewayErrorCode.TOOL_NOT_FOUND)
    if _blocked(context, function):
        raise GatewayError(code=GatewayErrorCode.APPROVAL_REQUIRED)
    return function


def _schema_payload(agent: str, toolkit: str, function: ToolFunction) -> ToolSchemaResponse:
    payload: ToolSchemaResponse = {
        "agent": agent,
        "toolkit": toolkit,
        "function": function.name,
        "description": function.description or "",
        "inputSchema": dict(function.parameters),
    }
    if _json_size(payload) > 32768:
        raise GatewayError(code=GatewayErrorCode.SCHEMA_TOO_LARGE)
    return payload


def _validate_arguments(schema: dict[str, Any], arguments: dict[str, object]) -> None:
    try:
        validate_tool_arguments(schema, arguments)
    except ValueError as exc:
        raise GatewayError(code=GatewayErrorCode.INVALID_ARGUMENTS) from exc


async def _selected_operation[T](
    context: AgentToolContext,
    name: str,
    manager: MCPServerManager | None,
    operation: Callable[[Toolkit], Awaitable[T]],
    require_current_access: Callable[[], None] | None = None,
) -> T:
    async def authorized(built: Toolkit) -> T:
        if require_current_access is not None:
            await run_gateway_sync(require_current_access)
        return await operation(built)

    async def selected() -> T:
        entry = await run_gateway_sync(_require_entry, context, name)
        if require_current_access is not None:
            await run_gateway_sync(require_current_access)
        return await run_toolkit_operation(context, entry, manager, authorized, require_current_access)

    with tool_runtime_context(None):
        return await run_with_tool_execution_identity(
            context.execution_identity,
            operation=selected,
        )


async def _guard[T: GatewaySuccessResponse](
    context: AgentToolContext,
    operation: Awaitable[T],
) -> T | GatewayErrorResponse:
    try:
        return await operation
    except GatewayError as exc:
        return _error(context, exc.code, exc.message)
    except OAuthConnectionRequired:
        return _error(context, GatewayErrorCode.CONNECTION_REQUIRED)
    except Exception:
        return _error(context, GatewayErrorCode.TOOL_UNAVAILABLE)


def _search_results(items: list[SearchItem], query: str, limit: int) -> SearchResponse:
    result: SearchResponse = {"results": search_tool_metadata(items, query, limit)}
    while result["results"] and _json_size(result) > 16384:
        result["results"].pop()
    return result


async def search_tools(
    context: AgentToolContext,
    *,
    query: str = "",
    toolkit: str | None = None,
    limit: int = 5,
    manager: MCPServerManager | None = None,
    require_current_access: Callable[[], None] | None = None,
    allowed_toolkits: tuple[str, ...] | None = None,
) -> SearchResult:
    """Search assigned toolkit metadata or one selected function catalog without schemas."""
    if not isinstance(query, str) or len(query) > 256 or not isinstance(limit, int) or isinstance(limit, bool):
        return _error(context, GatewayErrorCode.INVALID_ARGUMENTS)
    limit = max(1, min(limit, 10))

    async def search() -> SearchResponse:
        if toolkit is not None:
            selected_toolkit = toolkit

            async def selected(built: Toolkit) -> SearchResponse:
                # Adapted, each function carries the description ``get_tool`` shows, which Agno derives from its
                # docstring when the tool sets none.
                items: list[SearchItem] = [
                    {
                        "agent": context.agent_name,
                        "toolkit": selected_toolkit,
                        "function": function.name,
                        "description": (function.description or "")[:256],
                    }
                    for function in adapt_toolkit(built).functions
                    if _handle(function.name) and not _blocked(context, function)
                ]
                return _search_results(items, query, limit)

            return await _selected_operation(context, selected_toolkit, manager, selected, require_current_access)
        entries = await run_gateway_sync(_entries, context)
        items: list[SearchItem] = []
        for name in entries:
            if not _handle(name) or (allowed_toolkits is not None and name not in allowed_toolkits):
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
                    "agent": context.agent_name,
                    "toolkit": name,
                    "description": description[:256],
                    "next": "Search this agent and toolkit to see its functions.",
                },
            )
        if require_current_access is not None:
            await run_gateway_sync(require_current_access)
        return _search_results(items, query, limit)

    return await _guard(context, search())


async def search_agents(
    contexts: list[AgentToolContext],
    *,
    query: str = "",
    limit: int = 5,
    toolkits_by_agent: dict[str, tuple[str, ...] | None] | None = None,
) -> SearchResult:
    """Aggregate metadata with one global result and byte limit, without constructing tools."""
    items: list[SearchItem] = []
    for context in contexts:
        response = await search_tools(
            context,
            query=query,
            limit=limit,
            allowed_toolkits=toolkits_by_agent.get(context.agent_name, ()) if toolkits_by_agent is not None else None,
        )
        if "error" in response:
            return response
        items.extend(response["results"])
        if len(items) >= limit:
            break
    return _search_results(items, query, limit)


async def get_tool(
    context: AgentToolContext,
    *,
    toolkit: str,
    function: str,
    manager: MCPServerManager | None = None,
    require_current_access: Callable[[], None] | None = None,
) -> ToolSchemaResult:
    """Return the bounded schema for one currently assigned, ungated function."""
    if not _handle(function):
        return _error(context, GatewayErrorCode.TOOL_NOT_FOUND)

    async def selected(built: Toolkit) -> ToolSchemaResponse:
        return _schema_payload(context.agent_name, toolkit, adapt_function(_function(context, built, function)))

    return await _guard(context, _selected_operation(context, toolkit, manager, selected, require_current_access))


def _invocation_response(outcome: CallResult) -> InvocationResponse:
    """Return the gateway's answer for what one call came to, raising the error a client reads for a failed one."""
    if outcome.error is not None:
        raise GatewayError(code=GatewayErrorCode.TOOL_UNAVAILABLE) from outcome.error
    if outcome.status == "declined":
        raise GatewayError(code=GatewayErrorCode.TOOL_DECLINED, message=outcome.failure_text)
    if outcome.status in {"failed", "denied"}:
        raise GatewayError(code=GatewayErrorCode.TOOL_FAILED, message=outcome.failure_text)
    if outcome.status == "connection_required":
        raise GatewayError(code=GatewayErrorCode.CONNECTION_REQUIRED)
    return {"result": to_json_compatible(client_tool_result(outcome.value))}


def _guard_dispatch(function: ToolFunction, require_current_config: Callable[[], None]) -> ToolFunction:
    """Check the publication at the provider entrypoint, after hooks and thread scheduling."""
    entrypoint = function.entrypoint

    @wraps(entrypoint)
    def guarded_sync(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        require_current_config()
        return entrypoint(*args, **kwargs)

    @wraps(entrypoint)
    async def guarded_async(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        await run_gateway_sync(require_current_config)
        return await entrypoint(*args, **kwargs)

    # An async generator function is not a coroutine function, so ``guarded_sync`` guards it: the check runs on the
    # tool thread, which returns the unstarted stream for ``run_tool_call`` to drain on the event loop.
    return replace(function, entrypoint=guarded_async if inspect.iscoroutinefunction(entrypoint) else guarded_sync)


async def invoke_tool(
    context: AgentToolContext,
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
        target = replace(adapt_function(_function(context, built, function)), registered_toolkit=toolkit)
        _validate_arguments(_schema_payload(context.agent_name, toolkit, target)["inputSchema"], arguments)
        plugins = await run_gateway_sync(load_plugins, context.config, context.runtime_paths, set_skill_roots=False)
        policy = build_tool_hook_middleware(
            HookRegistry.from_plugins(plugins),
            agent_name=context.agent_name,
            dispatch_context=ToolDispatchContext(execution_identity=context.execution_identity),
            config=context.config,
            runtime_paths=context.runtime_paths,
        )
        if require_current_config is not None:
            target = _guard_dispatch(target, require_current_config)
        # Each invocation is its own run: it belongs to no conversation and shares no session state.
        call_context = ToolCallContext(
            run_id=f"mcp-gateway:{uuid4().hex}",
            session_id=DETACHED_SESSION_ID,
            user_id=context.requester_id,
        )
        outcome = await run_with_tool_execution_identity(
            context.execution_identity,
            operation=lambda: run_tool_call(target, arguments, call_context, middleware=(policy,)),
        )
        payload = _invocation_response(outcome)
        if _json_size(payload) > 65536:
            raise GatewayError(code=GatewayErrorCode.RESULT_TOO_LARGE)
        return payload

    return await _guard(context, _selected_operation(context, toolkit, manager, selected, require_current_config))
