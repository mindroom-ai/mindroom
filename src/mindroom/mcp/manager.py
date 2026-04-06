"""Runtime MCP session manager owned by the orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast

import mcp.types as mcp_types
from mcp import ClientSession

from mindroom.logging_config import get_logger
from mindroom.mcp.config import MCPServerConfig, resolved_mcp_tool_prefix, validate_mcp_function_name
from mindroom.mcp.errors import MCPConnectionError, MCPError, MCPProtocolError, MCPTimeoutError, MCPToolCallError
from mindroom.mcp.registry import mcp_server_id_from_tool_name, mcp_tool_name
from mindroom.mcp.results import tool_result_from_call_result
from mindroom.mcp.transports import build_transport_handle
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog, MCPServerState

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from agno.tools.function import ToolResult
    from mcp.client.session import MessageHandlerFnT

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

logger = get_logger(__name__)


class MCPServerManager:
    """Own one live MCP session per configured server."""

    def __init__(
        self,
        runtime_paths: RuntimePaths,
        *,
        on_catalog_change: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self.runtime_paths = runtime_paths
        self._states: dict[str, MCPServerState] = {}
        self._catalog_validation_lock = asyncio.Lock()
        self._on_catalog_change = on_catalog_change
        self._config: Config | None = None
        self._shutdown = False

    def has_server(self, server_id: str) -> bool:
        """Return whether one configured server is tracked."""
        return server_id in self._states

    def failed_server_ids(self) -> set[str]:
        """Return servers that do not currently have a usable catalog."""
        return {
            server_id
            for server_id, state in self._states.items()
            if state.catalog is None or state.last_error is not None
        }

    def get_catalog(self, server_id: str) -> MCPServerCatalog:
        """Return the cached catalog for one server."""
        state = self._require_state(server_id)
        if state.catalog is not None:
            return state.catalog
        if state.last_error is not None:
            raise state.last_error
        msg = f"MCP server '{server_id}' is not connected"
        raise MCPConnectionError(server_id, msg)

    def get_catalog_for_tool(self, tool_name: str) -> MCPServerCatalog:
        """Return the cached catalog for one dynamic MindRoom MCP tool."""
        server_id = mcp_server_id_from_tool_name(tool_name)
        if server_id is None:
            msg = f"Tool '{tool_name}' is not an MCP tool"
            raise ValueError(msg)
        return self.get_catalog(server_id)

    async def sync_servers(self, config: Config) -> set[str]:
        """Reconcile live server sessions against the active config."""
        self._config = config
        changed_server_ids: set[str] = set()
        desired_servers = {
            server_id: server_config for server_id, server_config in config.mcp_servers.items() if server_config.enabled
        }

        for server_id in sorted(set(self._states) - set(desired_servers)):
            await self._remove_server(server_id)

        for server_id, server_config in desired_servers.items():
            state = self._states.get(server_id)
            if state is None:
                state = MCPServerState(server_id=server_id, config=server_config)
                self._states[server_id] = state
            elif state.config != server_config:
                async with state.lock:
                    await self._disconnect_state_when_idle(state)
                    state.config = server_config
                    state.catalog = None
                    state.last_error = None
                    state.stale = True
                    state.semaphore = asyncio.Semaphore(server_config.max_concurrent_calls)

            if (
                state.catalog is None or state.stale or state.last_error is not None or not state.connected
            ) and await self._refresh_server_catalog(state, notify=False):
                changed_server_ids.add(server_id)

        invalid_server_ids = await self._validate_global_function_names()
        changed_server_ids.difference_update(invalid_server_ids)
        changed_server_ids.difference_update(self.failed_server_ids())
        return changed_server_ids

    async def shutdown(self) -> None:
        """Close all tracked sessions and background refresh tasks."""
        self._shutdown = True
        self._config = None
        for state in list(self._states.values()):
            task = state.refresh_task
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                state.refresh_task = None
            await self._disconnect_state_when_idle(state)
        self._states.clear()

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
    ) -> ToolResult:
        """Call one remote MCP tool through the cached session."""
        state = self._require_state(server_id)
        if state.catalog is None or state.session is None or not state.connected:
            await self._refresh_server_catalog(state, notify=False)
        self._require_catalog_tool(state, remote_tool_name)
        return await self._call_tool_once_or_reconnect(
            state,
            remote_tool_name,
            arguments,
            timeout_seconds=timeout_seconds or state.config.call_timeout_seconds,
        )

    async def _call_tool_once_or_reconnect(
        self,
        state: MCPServerState,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> ToolResult:
        refresh_revision = state.refresh_revision
        try:
            return await self._call_tool_with_lock(state, remote_tool_name, arguments, timeout_seconds=timeout_seconds)
        except (MCPToolCallError, MCPProtocolError):
            raise
        except (MCPConnectionError, MCPTimeoutError):
            if not state.config.auto_reconnect:
                raise
        except MCPError:
            raise

        await self._refresh_server_catalog(state, notify=True, expected_refresh_revision=refresh_revision)
        self._require_catalog_tool(state, remote_tool_name)
        return await self._call_tool_with_lock(state, remote_tool_name, arguments, timeout_seconds=timeout_seconds)

    async def _call_tool_with_lock(
        self,
        state: MCPServerState,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> ToolResult:
        async with state.semaphore, state.call_lock.read():
            if state.session is None or state.catalog is None or not state.connected:
                if state.last_error is not None:
                    raise state.last_error
                msg = f"MCP server '{state.server_id}' is not connected"
                raise MCPConnectionError(state.server_id, msg)
            return await self._call_tool_once(
                state,
                remote_tool_name,
                arguments,
                timeout_seconds=timeout_seconds,
            )

    async def _call_tool_once(
        self,
        state: MCPServerState,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float,
    ) -> ToolResult:
        session = state.session
        if session is None:
            msg = f"MCP server '{state.server_id}' is not connected"
            raise MCPConnectionError(state.server_id, msg)
        try:
            result = await session.call_tool(
                remote_tool_name,
                arguments=arguments or None,
                read_timeout_seconds=timedelta(seconds=timeout_seconds),
            )
        except Exception as exc:
            raise self._wrap_runtime_exception(state.server_id, exc) from exc
        return tool_result_from_call_result(state.server_id, result)

    async def _refresh_server_catalog(
        self,
        state: MCPServerState,
        *,
        notify: bool,
        expected_refresh_revision: int | None = None,
    ) -> bool:
        should_notify_catalog_change = False
        async with state.lock:
            if expected_refresh_revision is not None and state.refresh_revision != expected_refresh_revision:
                return False
            state.refresh_revision += 1
            state.stale = False
            async with state.call_lock.write():
                previous_hash = state.catalog.catalog_hash if state.catalog is not None else None
                await self._disconnect_state(state)
                try:
                    catalog = await self._connect_and_discover(state)
                except MCPError as exc:
                    state.last_error = exc
                    state.connected = False
                    state.catalog = None
                    logger.warning(
                        "MCP server discovery failed",
                        server_id=state.server_id,
                        transport=state.config.transport,
                        error=str(exc),
                    )
                    return False

                state.catalog = catalog
                state.connected = True
                state.last_error = None
                changed = previous_hash != catalog.catalog_hash
                should_notify_catalog_change = notify and changed and self._on_catalog_change is not None
        invalid_server_ids = await self._validate_global_function_names()
        if state.server_id in invalid_server_ids:
            return False
        if should_notify_catalog_change and self._on_catalog_change is not None:
            await self._on_catalog_change(state.server_id)
        if state.stale and state.refresh_task is None and not self._shutdown:
            self._schedule_refresh_task(state)
        return changed

    async def _connect_and_discover(self, state: MCPServerState) -> MCPServerCatalog:
        handle = build_transport_handle(state.server_id, state.config, self.runtime_paths)
        exit_stack = AsyncExitStack()

        async def open_session_and_discover() -> tuple[ClientSession, MCPServerCatalog]:
            read_stream, write_stream = await exit_stack.enter_async_context(handle.opener())
            session = await exit_stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=timedelta(seconds=state.config.call_timeout_seconds),
                    message_handler=self._build_message_handler(state.server_id),
                ),
            )
            initialize_result = await session.initialize()
            catalog = await self._discover_catalog(state.server_id, state.config, session, initialize_result)
            return session, catalog

        try:
            session, catalog = await asyncio.wait_for(
                open_session_and_discover(),
                timeout=state.config.startup_timeout_seconds,
            )
        except asyncio.CancelledError:
            await exit_stack.aclose()
            raise
        except Exception as exc:
            await exit_stack.aclose()
            if isinstance(exc, TimeoutError | asyncio.TimeoutError):
                msg = f"MCP startup timed out after {state.config.startup_timeout_seconds} seconds"
                raise MCPTimeoutError(state.server_id, msg) from exc
            raise self._wrap_runtime_exception(state.server_id, exc) from exc

        state.exit_stack = exit_stack
        state.session = session
        logger.info(
            "MCP server connected",
            server_id=state.server_id,
            transport=state.config.transport,
            tool_count=len(catalog.tools),
        )
        return catalog

    async def _discover_catalog(
        self,
        server_id: str,
        server_config: MCPServerConfig,
        session: ClientSession,
        initialize_result: mcp_types.InitializeResult,
    ) -> MCPServerCatalog:
        discovered_tools: list[mcp_types.Tool] = []
        cursor: str | None = None
        while True:
            result = await session.list_tools(cursor=cursor)
            discovered_tools.extend(result.tools)
            cursor = result.nextCursor
            if cursor is None:
                break

        tool_prefix = resolved_mcp_tool_prefix(server_id, server_config)
        include_tools = set(server_config.include_tools)
        exclude_tools = set(server_config.exclude_tools)
        filtered_tools: list[MCPDiscoveredTool] = []
        function_names: set[str] = set()
        for tool in discovered_tools:
            if exclude_tools and tool.name in exclude_tools:
                continue
            if include_tools and tool.name not in include_tools:
                continue
            try:
                function_name = validate_mcp_function_name(
                    f"{tool_prefix}_{tool.name}",
                    subject=f"MCP function name for server '{server_id}'",
                )
            except ValueError as exc:
                raise MCPProtocolError(server_id, str(exc)) from exc
            if function_name in function_names:
                msg = f"MCP server '{server_id}' exposes duplicate function name '{function_name}'"
                raise MCPProtocolError(server_id, msg)
            function_names.add(function_name)
            filtered_tools.append(
                MCPDiscoveredTool(
                    remote_name=tool.name,
                    function_name=function_name,
                    description=tool.description,
                    input_schema=tool.inputSchema,
                    output_schema=tool.outputSchema,
                    title=(tool.annotations.title if tool.annotations is not None else tool.title),
                ),
            )

        catalog_payload = [
            {
                "remote_name": tool.remote_name,
                "function_name": tool.function_name,
                "description": tool.description,
                "input_schema": tool.input_schema,
                "output_schema": tool.output_schema,
            }
            for tool in filtered_tools
        ]
        catalog_hash = hashlib.sha256(json.dumps(catalog_payload, sort_keys=True).encode("utf-8")).hexdigest()
        return MCPServerCatalog(
            server_id=server_id,
            tool_name=mcp_tool_name(server_id),
            tool_prefix=tool_prefix,
            tools=tuple(filtered_tools),
            server_info=initialize_result.serverInfo,
            instructions=initialize_result.instructions,
            catalog_hash=catalog_hash,
            discovered_at=datetime.now(UTC),
        )

    def _build_message_handler(self, server_id: str) -> MessageHandlerFnT:
        async def handle_message(message: object) -> None:
            if isinstance(message, Exception):
                logger.warning("MCP server emitted message handler exception", server_id=server_id, error=str(message))
                return
            if not isinstance(message, mcp_types.ServerNotification):
                return
            if not isinstance(message.root, mcp_types.ToolListChangedNotification):
                return
            state = self._states.get(server_id)
            if state is None:
                return
            state.stale = True
            self._schedule_refresh_task(state)

        return cast("MessageHandlerFnT", handle_message)

    def _schedule_refresh_task(self, state: MCPServerState) -> None:
        existing_task = state.refresh_task
        if self._shutdown:
            return
        if existing_task is not None and not existing_task.done():
            return

        async def refresh() -> None:
            try:
                changed = await self._refresh_server_catalog(state, notify=True)
                if changed:
                    logger.info(
                        "MCP server catalog changed",
                        server_id=state.server_id,
                        transport=state.config.transport,
                    )
            except Exception as exc:
                logger.warning(
                    "MCP server catalog refresh failed",
                    server_id=state.server_id,
                    transport=state.config.transport,
                    error=str(exc),
                )
            finally:
                state.refresh_task = None
                if state.stale:
                    self._schedule_refresh_task(state)

        state.refresh_task = asyncio.create_task(refresh(), name=f"mcp_catalog_refresh:{state.server_id}")

    async def _remove_server(self, server_id: str) -> None:
        state = self._states.pop(server_id, None)
        if state is None:
            return
        task = state.refresh_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await self._disconnect_state_when_idle(state)

    async def _disconnect_state_when_idle(self, state: MCPServerState) -> None:
        async with state.call_lock.write():
            await self._disconnect_state(state)

    async def _disconnect_state(self, state: MCPServerState) -> None:
        close_error: BaseException | None = None
        if state.exit_stack is not None:
            try:
                await state.exit_stack.aclose()
            except BaseException as exc:
                close_error = exc
            finally:
                state.exit_stack = None
        if state.connected:
            logger.info(
                "MCP server disconnected",
                server_id=state.server_id,
                transport=state.config.transport,
            )
        state.session = None
        state.connected = False
        if close_error is not None:
            raise close_error

    def _require_state(self, server_id: str) -> MCPServerState:
        state = self._states.get(server_id)
        if state is None:
            msg = f"Unknown MCP server '{server_id}'"
            raise KeyError(msg)
        return state

    def _require_catalog_tool(self, state: MCPServerState, remote_tool_name: str) -> None:
        catalog = self.get_catalog(state.server_id)
        if remote_tool_name not in {tool.remote_name for tool in catalog.tools}:
            msg = f"MCP tool '{remote_tool_name}' is not in the cached catalog for server '{state.server_id}'"
            raise MCPProtocolError(state.server_id, msg)

    @staticmethod
    def _function_name_collision_messages(
        server_ids_by_function_name: dict[str, set[str]],
        configured_local_function_names: set[str],
    ) -> dict[str, list[str]]:
        """Build validation errors for conflicting provider-visible function names."""
        errors_by_server: dict[str, list[str]] = {}
        for function_name, server_ids in server_ids_by_function_name.items():
            if function_name in configured_local_function_names:
                message = f"MCP function name '{function_name}' collides with an existing MindRoom tool function"
                for server_id in server_ids:
                    errors_by_server.setdefault(server_id, []).append(message)
            if len(server_ids) < 2:
                continue
            server_list = ", ".join(sorted(server_ids))
            message = f"MCP function name '{function_name}' collides across servers: {server_list}"
            for server_id in server_ids:
                errors_by_server.setdefault(server_id, []).append(message)
        return errors_by_server

    async def _validate_global_function_names(self) -> set[str]:
        async with self._catalog_validation_lock:
            server_ids_by_function_name: dict[str, set[str]] = {}
            for state in self._states.values():
                if state.catalog is None or state.last_error is not None:
                    continue
                for tool in state.catalog.tools:
                    server_ids_by_function_name.setdefault(tool.function_name, set()).add(state.server_id)
            if not server_ids_by_function_name:
                return set()

            configured_local_function_names = self._configured_local_function_names()
            errors_by_server = self._function_name_collision_messages(
                server_ids_by_function_name,
                configured_local_function_names,
            )

            for server_id, messages in errors_by_server.items():
                state = self._require_state(server_id)
                error_message = "\n".join(messages)
                async with state.lock:
                    await self._disconnect_state_when_idle(state)
                    state.catalog = None
                    state.last_error = MCPProtocolError(server_id, error_message)
                    state.stale = False
            return set(errors_by_server)

    def _configured_local_function_names(self) -> set[str]:
        """Return provider-visible function names from the current non-MCP tool surface."""
        config = self._config
        if config is None:
            return set()

        from mindroom.mcp.registry import _MCP_TOOL_FACTORY_MARKER  # noqa: PLC0415
        from mindroom.tool_system.metadata import (  # noqa: PLC0415
            _TOOL_REGISTRY,
            ensure_tool_registry_loaded,
            get_tool_by_name,
        )

        ensure_tool_registry_loaded(self.runtime_paths, config)
        function_names: set[str] = set()
        tool_names: set[str] = set()
        for agent_name, agent_config in config.agents.items():
            tool_names.update(
                tool_name
                for tool_name in config.get_agent_tools(agent_name)
                if not getattr(_TOOL_REGISTRY.get(tool_name), _MCP_TOOL_FACTORY_MARKER, False)
            )
            for toolkit_name in set(agent_config.allowed_toolkits) | set(agent_config.initial_toolkits):
                tool_names.update(
                    toolkit_entry.name
                    for toolkit_entry in config.get_toolkit_tool_configs(toolkit_name)
                    if not getattr(_TOOL_REGISTRY.get(toolkit_entry.name), _MCP_TOOL_FACTORY_MARKER, False)
                )
            if agent_config.delegate_to:
                function_names.add("delegate_task")
            allow_self_config = (
                agent_config.allow_self_config
                if agent_config.allow_self_config is not None
                else config.defaults.allow_self_config
            )
            if allow_self_config:
                function_names.update({"get_own_config", "update_own_config"})
            if agent_config.allowed_toolkits:
                function_names.update({"list_toolkits", "load_tools", "unload_tools"})

        for tool_name in sorted(tool_names):
            try:
                toolkit = get_tool_by_name(tool_name, self.runtime_paths, worker_target=None)
            except Exception as exc:
                logger.debug(
                    "Skipping local tool during MCP function-name validation",
                    tool_name=tool_name,
                    error=str(exc),
                )
                continue
            function_names.update(self._toolkit_function_names(toolkit))

        return function_names

    @staticmethod
    def _toolkit_function_names(toolkit: object) -> set[str]:
        """Return provider-visible function names exposed by one toolkit instance."""
        toolkit_functions = getattr(toolkit, "functions", {})
        toolkit_async_functions = getattr(toolkit, "async_functions", {})
        names = {name for name in {*toolkit_functions, *toolkit_async_functions} if isinstance(name, str) and name}
        if names:
            return names

        for raw_tool in getattr(toolkit, "tools", ()):
            function_name = getattr(raw_tool, "name", None)
            if isinstance(function_name, str) and function_name:
                names.add(function_name)
        return names

    def _wrap_runtime_exception(self, server_id: str, exc: Exception) -> MCPError:
        if isinstance(exc, MCPError):
            return exc
        if isinstance(exc, TimeoutError | asyncio.TimeoutError):
            return MCPTimeoutError(server_id, f"MCP operation timed out: {exc}")
        return MCPConnectionError(server_id, f"MCP operation failed: {exc}")
