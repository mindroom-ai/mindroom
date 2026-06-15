"""Runtime MCP session manager owned by the orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, cast

import mcp.types as mcp_types
from mcp import ClientSession

from mindroom.credentials import get_runtime_credentials_manager, load_scoped_credentials, save_scoped_credentials
from mindroom.logging_config import get_logger
from mindroom.mcp.config import (
    MCPServerConfig,
    mcp_oauth_bridge_function_names,
    resolved_mcp_tool_prefix,
    validate_mcp_function_name,
)
from mindroom.mcp.errors import MCPConnectionError, MCPError, MCPProtocolError, MCPTimeoutError, MCPToolCallError
from mindroom.mcp.oauth import mcp_oauth_provider
from mindroom.mcp.registry import mcp_server_id_from_tool_name, mcp_tool_name
from mindroom.mcp.results import tool_result_from_call_result
from mindroom.mcp.transports import build_transport_handle
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog, MCPServerState
from mindroom.oauth.providers import OAuthConnectionRequired, OAuthProviderError
from mindroom.oauth.service import build_oauth_connect_instruction, oauth_connect_url, oauth_credentials_usable
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded, get_tool_by_name
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from agno.tools.function import ToolResult
    from mcp.client.session import MessageHandlerFnT

    from mindroom.config.main import Config
    from mindroom.config.models import EffectiveToolConfig
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)

# The cap matches STARTUP_RETRY_MAX_DELAY_SECONDS so a recovered required server
# unblocks its dependent agents no slower than the bot-start retry loop did.
_DISCOVERY_RETRY_INITIAL_DELAY_SECONDS = 5.0
_DISCOVERY_RETRY_MAX_DELAY_SECONDS = 60.0


def _discovery_retry_delay_seconds(consecutive_failures: int) -> float:
    """Return the exponential-backoff delay before the next discovery retry."""
    # Clamp the exponent so a long outage cannot overflow float conversion.
    exponent = min(max(consecutive_failures - 1, 0), 10)
    return min(
        _DISCOVERY_RETRY_INITIAL_DELAY_SECONDS * 2**exponent,
        _DISCOVERY_RETRY_MAX_DELAY_SECONDS,
    )


@dataclass(frozen=True)
class _MCPSessionKey:
    """Requester-scoped MCP session cache key."""

    server_id: str
    worker_scope: str
    worker_key: str


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
        self._scoped_states: dict[_MCPSessionKey, MCPServerState] = {}
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
            if state.last_error is not None or (state.config.auth is None and state.catalog is None)
        }

    def failed_required_server_ids(self) -> set[str]:
        """Return failed servers configured to block dependent agent startup."""
        return {server_id for server_id in self.failed_server_ids() if self._states[server_id].config.required}

    def get_catalog(self, server_id: str) -> MCPServerCatalog:
        """Return the cached catalog for one server."""
        state = self._require_state(server_id)
        if state.catalog is not None:
            return state.catalog
        if state.last_error is not None:
            raise state.last_error
        msg = f"MCP server '{server_id}' is not connected"
        raise MCPConnectionError(server_id, msg)

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
                await self._cancel_refresh_task(state)
                async with state.lock:
                    await self._disconnect_state_when_idle(state)
                    await self._remove_scoped_server_states(server_id)
                    state.config = server_config
                    state.catalog = None
                    state.last_error = None
                    state.stale = True
                    state.consecutive_failures = 0
                    state.semaphore = asyncio.Semaphore(server_config.max_concurrent_calls)

            if server_config.auth is not None:
                state.stale = False
                continue

            retry_pending = state.refresh_task is not None and not state.refresh_task.done()
            if (
                (state.catalog is None or state.stale or state.last_error is not None or not state.connected)
                and not retry_pending
                and await self._refresh_server_catalog(state, notify=False)
            ):
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
            await self._cancel_refresh_task(state)
            await self._disconnect_state_when_idle(state)
        for state in list(self._scoped_states.values()):
            await self._cancel_refresh_task(state)
            await self._disconnect_state_when_idle(state)
        self._states.clear()
        self._scoped_states.clear()

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> ToolResult:
        """Call one remote MCP tool through the cached session."""
        state = self._require_state(server_id)
        if state.config.auth is not None:
            request_state, auth_headers = await self._request_state_and_headers(
                server_id,
                credentials_manager=credentials_manager,
                worker_target=worker_target,
            )
            if (
                request_state.catalog is None
                or request_state.session is None
                or request_state.stale
                or request_state.last_error is not None
                or not request_state.connected
            ):
                await self._refresh_server_catalog(request_state, notify=False, auth_headers=auth_headers)
            self._require_catalog_tool(request_state, remote_tool_name)
            return await self._call_tool_once_or_reconnect(
                request_state,
                remote_tool_name,
                arguments,
                timeout_seconds=timeout_seconds or request_state.config.call_timeout_seconds,
                auth_headers=auth_headers,
            )

        if state.catalog is None or state.session is None or not state.connected:
            await self._refresh_server_catalog(state, notify=False)
        self._require_catalog_tool(state, remote_tool_name)
        return await self._call_tool_once_or_reconnect(
            state,
            remote_tool_name,
            arguments,
            timeout_seconds=timeout_seconds or state.config.call_timeout_seconds,
        )

    async def get_request_catalog(
        self,
        server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
    ) -> MCPServerCatalog:
        """Return a requester-scoped catalog for one OAuth-backed MCP server."""
        state, auth_headers = await self._request_state_and_headers(
            server_id,
            credentials_manager=credentials_manager,
            worker_target=worker_target,
        )
        if state.catalog is None or state.stale or state.last_error is not None or not state.connected:
            await self._refresh_server_catalog(state, notify=False, auth_headers=auth_headers)
        if state.catalog is not None:
            return state.catalog
        if state.last_error is not None:
            raise state.last_error
        msg = f"MCP server '{server_id}' is not connected"
        raise MCPConnectionError(server_id, msg)

    def cached_request_catalog(
        self,
        server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
    ) -> MCPServerCatalog | None:
        """Return an already-discovered requester-scoped catalog without network or credential I/O."""
        base_state = self._states.get(server_id)
        if base_state is None or base_state.config.auth is None:
            return None
        try:
            key = self._request_session_key(base_state, worker_target)
        except OAuthConnectionRequired:
            return None
        state = self._scoped_states.get(key)
        if state is None or state.catalog is None or state.stale or state.last_error is not None:
            return None
        return state.catalog

    async def disconnect_request_session(
        self,
        server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
    ) -> None:
        """Close a requester-scoped OAuth MCP session, if one is active."""
        base_state = self._states.get(server_id)
        if base_state is None or base_state.config.auth is None:
            return
        try:
            key = self._request_session_key(base_state, worker_target)
        except OAuthConnectionRequired:
            return
        state = self._scoped_states.pop(key, None)
        if state is None:
            return
        await self._cancel_refresh_task(state)
        await self._disconnect_state_when_idle(state)

    def _oauth_connection_required(
        self,
        state: MCPServerState,
        worker_target: ResolvedWorkerTarget | None,
    ) -> OAuthConnectionRequired:
        provider = mcp_oauth_provider(state.server_id, state.config)
        connect_url = oauth_connect_url(provider, self.runtime_paths, worker_target=worker_target)
        return OAuthConnectionRequired(
            build_oauth_connect_instruction(provider, connect_url),
            provider_id=provider.id,
            connect_url=connect_url,
        )

    def _request_session_key(
        self,
        state: MCPServerState,
        worker_target: ResolvedWorkerTarget | None,
    ) -> _MCPSessionKey:
        worker_scope = worker_target.worker_scope if worker_target is not None else None
        worker_key = worker_target.worker_key if worker_target is not None else None
        if worker_scope in {"user", "user_agent"} and not worker_key:
            raise self._oauth_connection_required(state, worker_target)
        return _MCPSessionKey(
            server_id=state.server_id,
            worker_scope=worker_scope or "unscoped",
            worker_key=worker_key or "global",
        )

    async def _oauth_access_token(
        self,
        state: MCPServerState,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
    ) -> str:
        provider = mcp_oauth_provider(state.server_id, state.config)
        manager = credentials_manager or get_runtime_credentials_manager(self.runtime_paths)
        credentials = load_scoped_credentials(
            provider.credential_service,
            credentials_manager=manager,
            worker_target=worker_target,
        )
        if not oauth_credentials_usable(provider, self.runtime_paths, credentials):
            raise self._oauth_connection_required(state, worker_target)
        assert credentials is not None
        try:
            refreshed_credentials = await provider.refresh_token_data(credentials, self.runtime_paths)
        except OAuthProviderError as exc:
            raise self._oauth_connection_required(state, worker_target) from exc
        if refreshed_credentials is not None:
            save_scoped_credentials(
                provider.credential_service,
                refreshed_credentials,
                credentials_manager=manager,
                worker_target=worker_target,
            )
            credentials = refreshed_credentials
        token = credentials.get("token") or credentials.get("access_token")
        if not isinstance(token, str) or not token:
            raise self._oauth_connection_required(state, worker_target)
        return token

    async def _request_state_and_headers(
        self,
        server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
    ) -> tuple[MCPServerState, Mapping[str, str]]:
        base_state = self._require_state(server_id)
        if base_state.config.auth is None:
            msg = f"MCP server '{server_id}' is not OAuth-backed"
            raise MCPConnectionError(server_id, msg)
        if base_state.last_error is not None:
            raise base_state.last_error
        key = self._request_session_key(base_state, worker_target)
        state = self._scoped_states.get(key)
        if state is None:
            state = MCPServerState(server_id=server_id, config=base_state.config)
            self._scoped_states[key] = state

        async with state.lock:
            access_token = await self._oauth_access_token(
                base_state,
                credentials_manager=credentials_manager,
                worker_target=worker_target,
            )
            token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
            if state.oauth_access_token_hash != token_hash:
                async with state.call_lock.write():
                    await self._disconnect_state(state)
                    state.catalog = None
                    state.last_error = None
                    state.stale = True
                    state.oauth_access_token_hash = token_hash
        return state, {"Authorization": f"Bearer {access_token}"}

    async def _call_tool_once_or_reconnect(
        self,
        state: MCPServerState,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float,
        auth_headers: Mapping[str, str] | None = None,
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

        await self._refresh_server_catalog(
            state,
            notify=True,
            expected_refresh_revision=refresh_revision,
            auth_headers=auth_headers,
        )
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
                arguments=arguments,
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
        auth_headers: Mapping[str, str] | None = None,
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
                    catalog = await self._connect_and_discover(state, auth_headers=auth_headers)
                except MCPError as exc:
                    repeated_error = state.last_error is not None and str(state.last_error) == str(exc)
                    state.last_error = exc
                    state.connected = False
                    state.catalog = None
                    state.consecutive_failures += 1
                    log = logger.debug if repeated_error else logger.warning
                    log(
                        "MCP server discovery failed",
                        server_id=state.server_id,
                        transport=state.config.transport,
                        error=str(exc),
                        required=state.config.required,
                        affected_entities=sorted(self._entities_referencing_server(state.server_id)),
                        consecutive_failures=state.consecutive_failures,
                    )
                    self._schedule_refresh_task(
                        state,
                        delay_seconds=_discovery_retry_delay_seconds(state.consecutive_failures),
                    )
                    return False

                state.catalog = catalog
                state.connected = True
                state.last_error = None
                state.consecutive_failures = 0
                changed = previous_hash != catalog.catalog_hash
                should_notify_catalog_change = notify and changed and self._on_catalog_change is not None
        invalid_server_ids = await self._validate_global_function_names()
        if state.server_id in invalid_server_ids:
            return False
        if should_notify_catalog_change and self._on_catalog_change is not None:
            await self._on_catalog_change(state.server_id)
        if state.config.auth is None and state.stale and state.refresh_task is None and not self._shutdown:
            self._schedule_refresh_task(state)
        return changed

    async def _connect_and_discover(
        self,
        state: MCPServerState,
        *,
        auth_headers: Mapping[str, str] | None = None,
    ) -> MCPServerCatalog:
        handle = build_transport_handle(state.server_id, state.config, self.runtime_paths, extra_headers=auth_headers)
        ready: asyncio.Future[tuple[ClientSession, MCPServerCatalog]] = asyncio.get_running_loop().create_future()
        close_event = asyncio.Event()

        async def session_owner() -> None:
            # MCP/AnyIO session contexts must exit in the same task that entered them.
            exit_stack = AsyncExitStack()
            try:
                read_stream, write_stream = await exit_stack.enter_async_context(handle.opener())
                session = await exit_stack.enter_async_context(
                    ClientSession(
                        read_stream,
                        write_stream,
                        read_timeout_seconds=timedelta(seconds=state.config.call_timeout_seconds),
                        message_handler=self._build_message_handler(state),
                    ),
                )
                initialize_result = await session.initialize()
                catalog = await self._discover_catalog(state.server_id, state.config, session, initialize_result)
                if not ready.done():
                    ready.set_result((session, catalog))
                await close_event.wait()
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
                raise
            except BaseException as exc:
                if not ready.done():
                    ready.set_exception(exc)
                else:
                    logger.warning(
                        "MCP server session owner failed",
                        server_id=state.server_id,
                        transport=state.config.transport,
                        error=self._runtime_exception_message(exc),
                    )
                raise
            finally:
                await exit_stack.aclose()

        owner_task = asyncio.create_task(session_owner(), name=f"mcp_session:{state.server_id}")

        try:
            session, catalog = await asyncio.wait_for(
                asyncio.shield(ready),
                timeout=state.config.startup_timeout_seconds,
            )
        except asyncio.CancelledError:
            await self._cancel_session_owner_task(owner_task)
            raise
        except Exception as exc:
            await self._cancel_session_owner_task(owner_task)
            if isinstance(exc, TimeoutError | asyncio.TimeoutError):
                msg = f"MCP startup timed out after {state.config.startup_timeout_seconds} seconds"
                raise MCPTimeoutError(state.server_id, msg) from exc
            raise self._wrap_runtime_exception(state.server_id, exc) from exc

        state.exit_stack = None
        state.session = session
        state.session_owner_task = owner_task
        state.session_close_event = close_event
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
            instructions=initialize_result.instructions,
            catalog_hash=catalog_hash,
        )

    def _build_message_handler(self, state: MCPServerState) -> MessageHandlerFnT:
        async def handle_message(message: object) -> None:
            if isinstance(message, Exception):
                logger.warning(
                    "MCP server emitted message handler exception",
                    server_id=state.server_id,
                    error=str(message),
                )
                return
            if not isinstance(message, mcp_types.ServerNotification):
                return
            if not isinstance(message.root, mcp_types.ToolListChangedNotification):
                return
            state.stale = True
            if state.config.auth is None:
                self._schedule_refresh_task(state)

        return cast("MessageHandlerFnT", handle_message)

    def _entities_referencing_server(self, server_id: str) -> set[str]:
        """Return configured entities whose tools reference one MCP server."""
        config = self._config
        if config is None:
            return set()
        return config.get_entities_referencing_tools({mcp_tool_name(server_id)})

    def _schedule_refresh_task(self, state: MCPServerState, *, delay_seconds: float = 0.0) -> None:
        if self._shutdown or state.config.auth is not None:
            return
        existing_task = state.refresh_task
        if existing_task is not None and not existing_task.done() and existing_task is not asyncio.current_task():
            return

        async def refresh() -> None:
            current_task = asyncio.current_task()
            cancelled = False
            try:
                if delay_seconds > 0:
                    await asyncio.sleep(delay_seconds)
                changed = await self._refresh_server_catalog(state, notify=True)
                if changed:
                    logger.info(
                        "MCP server catalog changed",
                        server_id=state.server_id,
                        transport=state.config.transport,
                    )
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                logger.warning(
                    "MCP server catalog refresh failed",
                    server_id=state.server_id,
                    transport=state.config.transport,
                    error=str(exc),
                )
            finally:
                # A failed refresh schedules its own backoff retry from within this
                # task, so only clear or reschedule when no replacement exists.
                if state.refresh_task is current_task:
                    state.refresh_task = None
                    if state.stale and not cancelled:
                        self._schedule_refresh_task(state)

        state.refresh_task = asyncio.create_task(refresh(), name=f"mcp_catalog_refresh:{state.server_id}")

    async def _remove_server(self, server_id: str) -> None:
        state = self._states.pop(server_id, None)
        if state is None:
            return
        await self._cancel_refresh_task(state)
        await self._remove_scoped_server_states(server_id)
        await self._disconnect_state_when_idle(state)

    async def _remove_scoped_server_states(self, server_id: str) -> None:
        scoped_keys = [key for key in self._scoped_states if key.server_id == server_id]
        for key in scoped_keys:
            state = self._scoped_states.pop(key)
            await self._cancel_refresh_task(state)
            await self._disconnect_state_when_idle(state)

    @staticmethod
    async def _cancel_refresh_task(state: MCPServerState) -> None:
        task = state.refresh_task
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if state.refresh_task is task:
            state.refresh_task = None

    async def _disconnect_state_when_idle(self, state: MCPServerState) -> None:
        async with state.call_lock.write():
            await self._disconnect_state(state)

    async def _disconnect_state(self, state: MCPServerState) -> None:
        close_error: BaseException | None = None
        owner_task = state.session_owner_task
        close_event = state.session_close_event
        state.session_owner_task = None
        state.session_close_event = None
        if owner_task is not None:
            try:
                if close_event is not None:
                    close_event.set()
                await owner_task
            except BaseException as exc:
                close_error = exc
        elif state.exit_stack is not None:
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

    @staticmethod
    async def _cancel_session_owner_task(owner_task: asyncio.Task[None]) -> None:
        owner_task.cancel()
        await asyncio.gather(owner_task, return_exceptions=True)

    def _require_state(self, server_id: str) -> MCPServerState:
        state = self._states.get(server_id)
        if state is None:
            msg = f"Unknown MCP server '{server_id}'"
            raise KeyError(msg)
        return state

    def _require_catalog_tool(self, state: MCPServerState, remote_tool_name: str) -> None:
        catalog = state.catalog if state.catalog is not None else self.get_catalog(state.server_id)
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

    def _visible_function_server_ids(self) -> set[str]:
        """Return MCP servers that currently expose provider-visible function names."""
        server_ids: set[str] = set()
        for state in self._states.values():
            if state.last_error is not None:
                continue
            if state.config.auth is not None or state.catalog is not None:
                server_ids.add(state.server_id)
        for key, state in self._scoped_states.items():
            if state.catalog is not None and state.last_error is None:
                server_ids.add(key.server_id)
        return server_ids

    @staticmethod
    def _normalized_tool_filter(value: object) -> set[str]:
        """Normalize MCP per-assignment remote tool filters."""
        if value is None:
            return set()
        if isinstance(value, str):
            return {part.strip() for part in value.replace("\n", ",").split(",") if part.strip()}
        if isinstance(value, list):
            return {part.strip() for part in value if isinstance(part, str) and part.strip()}
        return set()

    def _catalog_function_names_for_tool_config(
        self,
        catalog: MCPServerCatalog,
        tool_config: EffectiveToolConfig,
    ) -> set[str]:
        """Return catalog function names after one agent MCP assignment's filters."""
        include_tools = self._normalized_tool_filter(tool_config.tool_config_overrides.get("include_tools"))
        exclude_tools = self._normalized_tool_filter(tool_config.tool_config_overrides.get("exclude_tools"))
        return {
            tool.function_name
            for tool in catalog.tools
            if (not exclude_tools or tool.remote_name not in exclude_tools)
            and (not include_tools or tool.remote_name in include_tools)
        }

    def _server_visible_function_surface(
        self,
        server_id: str,
        tool_config: EffectiveToolConfig,
    ) -> tuple[set[str], set[str]]:
        """Return visible function names and real same-server collisions for one MCP server."""
        state = self._states.get(server_id)
        if state is None or state.last_error is not None:
            return set(), set()
        base_function_names: set[str] = set()
        duplicate_function_names: set[str] = set()
        if state.config.auth is not None:
            base_function_names.update(mcp_oauth_bridge_function_names(server_id, state.config))
        if state.catalog is not None:
            catalog_function_names = self._catalog_function_names_for_tool_config(state.catalog, tool_config)
            duplicate_function_names.update(base_function_names & catalog_function_names)
            base_function_names.update(catalog_function_names)
        scoped_function_names: set[str] = set()
        for key, scoped_state in self._scoped_states.items():
            if key.server_id != server_id or scoped_state.catalog is None or scoped_state.last_error is not None:
                continue
            catalog_function_names = self._catalog_function_names_for_tool_config(scoped_state.catalog, tool_config)
            duplicate_function_names.update(base_function_names & catalog_function_names)
            scoped_function_names.update(catalog_function_names)
        return base_function_names | scoped_function_names, duplicate_function_names

    def _agent_collision_messages(
        self,
        agent_name: str,
        visible_function_server_ids: set[str],
        *,
        loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
    ) -> dict[str, list[str]]:
        """Return one agent's MCP function-name collisions against its visible surface."""
        configured_local_function_names, configured_mcp_tool_configs = self._configured_function_surface(
            agent_name,
            loaded_tools=loaded_tools,
        )
        visible_server_ids = set(configured_mcp_tool_configs) & visible_function_server_ids
        if not visible_server_ids:
            return {}

        server_ids_by_function_name: dict[str, set[str]] = {}
        errors_by_server: dict[str, list[str]] = {}
        for server_id in visible_server_ids:
            for tool_config in configured_mcp_tool_configs[server_id]:
                visible_function_names, duplicate_function_names = self._server_visible_function_surface(
                    server_id,
                    tool_config,
                )
                for function_name in sorted(visible_function_names):
                    server_ids_by_function_name.setdefault(function_name, set()).add(server_id)
                for function_name in duplicate_function_names:
                    errors_by_server.setdefault(server_id, []).append(
                        f"MCP function name '{function_name}' collides within server '{server_id}'",
                    )
        if not server_ids_by_function_name:
            return errors_by_server
        for server_id, messages in self._function_name_collision_messages(
            server_ids_by_function_name,
            configured_local_function_names,
        ).items():
            errors_by_server.setdefault(server_id, []).extend(messages)
        return errors_by_server

    async def _apply_function_name_collision_errors(self, errors_by_server: dict[str, set[str]]) -> None:
        """Disconnect and mark servers that failed provider-visible name validation."""
        for server_id, messages in errors_by_server.items():
            state = self._require_state(server_id)
            error_message = "\n".join(sorted(messages))
            async with state.lock:
                await self._disconnect_state_when_idle(state)
                await self._mark_scoped_states_failed(server_id, error_message)
                state.catalog = None
                state.last_error = MCPProtocolError(server_id, error_message)
                state.stale = False

    async def _mark_scoped_states_failed(self, server_id: str, error_message: str) -> None:
        """Disconnect requester-scoped states after server-level function-name validation fails."""
        for key, state in list(self._scoped_states.items()):
            if key.server_id != server_id:
                continue
            async with state.lock:
                await self._disconnect_state_when_idle(state)
                state.catalog = None
                state.last_error = MCPProtocolError(server_id, error_message)
                state.stale = False

    async def _validate_global_function_names(self) -> set[str]:
        async with self._catalog_validation_lock:
            visible_function_server_ids = self._visible_function_server_ids()
            if not visible_function_server_ids:
                return set()

            errors_by_server: dict[str, set[str]] = {}
            for agent_name in sorted(self._config.agents) if self._config is not None else ():
                for server_id, messages in self._agent_collision_messages(
                    agent_name,
                    visible_function_server_ids,
                    loaded_tools=[],
                ).items():
                    errors_by_server.setdefault(server_id, set()).update(messages)
            if not errors_by_server:
                return set()

            await self._apply_function_name_collision_errors(errors_by_server)
            return set(errors_by_server)

    def _configured_tool_configs(
        self,
        agent_name: str,
        *,
        loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
    ) -> tuple[EffectiveToolConfig, ...]:
        """Return provider-visible tool configs for one agent surface."""
        config = cast("Config", self._config)
        return visible_tool_surface(
            agent_name=agent_name,
            config=config,
            loaded_tools=loaded_tools,
            enable_dynamic_tools_manager=True,
        ).runtime_tool_configs

    def _mcp_server_id_from_tool_config_name(self, tool_name: str) -> str | None:
        """Return the MCP server id for a tool name visible in this manager's active config."""
        config = self._config
        if config is not None:
            for server_id in config.mcp_servers:
                if tool_name == mcp_tool_name(server_id):
                    return server_id
        return mcp_server_id_from_tool_name(tool_name)

    def _partition_tool_configs(
        self,
        tool_configs: tuple[EffectiveToolConfig, ...],
    ) -> tuple[list[EffectiveToolConfig], dict[str, tuple[EffectiveToolConfig, ...]]]:
        """Split tool configs into local tool configs and visible MCP server ids."""
        local_tool_configs: list[EffectiveToolConfig] = []
        mcp_tool_configs: dict[str, list[EffectiveToolConfig]] = {}
        for tool_config in tool_configs:
            if server_id := self._mcp_server_id_from_tool_config_name(tool_config.name):
                mcp_tool_configs.setdefault(server_id, []).append(tool_config)
                continue
            local_tool_configs.append(tool_config)
        return local_tool_configs, {server_id: tuple(configs) for server_id, configs in mcp_tool_configs.items()}

    @staticmethod
    def _metadata_only_tool_function_names(tool_name: str, *, config: Config, agent_name: str) -> set[str]:
        """Return provider-visible names for context-built tools declared in metadata."""
        metadata = TOOL_METADATA.get(tool_name)
        if metadata is None or metadata.factory is not None:
            return set()
        if tool_name == "memory" and config.get_agent_memory_backend(agent_name) == "none":
            return set()
        return set(metadata.function_names)

    def _metadata_only_tool_function_names_for_surface(
        self,
        tool_names: set[str],
        *,
        config: Config,
        agent_name: str,
    ) -> set[str]:
        """Return provider-visible function names for metadata-only configured tools."""
        function_names: set[str] = set()
        for tool_name in sorted(tool_names):
            function_names.update(
                self._metadata_only_tool_function_names(tool_name, config=config, agent_name=agent_name),
            )
        return function_names

    def _tool_function_names_for_local_tools(
        self,
        tool_configs: list[EffectiveToolConfig],
        *,
        get_tool_by_name: Callable[..., object],
    ) -> set[str]:
        """Return provider-visible function names exposed by one set of local tools."""
        function_names: set[str] = set()
        for tool_config in sorted(tool_configs, key=lambda entry: entry.name):
            try:
                toolkit = get_tool_by_name(
                    tool_config.name,
                    self.runtime_paths,
                    worker_target=None,
                    tool_config_overrides=dict(tool_config.tool_config_overrides),
                )
            except Exception as exc:
                logger.debug(
                    "Skipping local tool during MCP function-name validation",
                    tool_name=tool_config.name,
                    error=str(exc),
                )
                continue
            function_names.update(self._toolkit_function_names(toolkit))
        return function_names

    def _configured_function_surface(
        self,
        agent_name: str,
        *,
        loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None,
    ) -> tuple[set[str], dict[str, tuple[EffectiveToolConfig, ...]]]:
        """Return one agent's provider-visible local functions and MCP servers."""
        config = self._config
        if config is None:
            return set(), {}

        ensure_tool_registry_loaded(self.runtime_paths, config)
        local_tool_configs, mcp_tool_configs = self._partition_tool_configs(
            self._configured_tool_configs(agent_name, loaded_tools=loaded_tools),
        )
        local_tool_names = {entry.name for entry in local_tool_configs}
        function_names = self._metadata_only_tool_function_names_for_surface(
            local_tool_names,
            config=config,
            agent_name=agent_name,
        )
        function_names.update(
            self._tool_function_names_for_local_tools(
                [
                    entry
                    for entry in local_tool_configs
                    if not self._metadata_only_tool_function_names(
                        entry.name,
                        config=config,
                        agent_name=agent_name,
                    )
                ],
                get_tool_by_name=get_tool_by_name,
            ),
        )
        return function_names, mcp_tool_configs

    def mcp_tool_unavailable_messages_for_loaded_tools(
        self,
        agent_name: str,
        loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    ) -> list[str]:
        """Return unavailable non-OAuth MCP server messages for a candidate loaded dynamic-tool state."""
        config = self._config
        if config is None:
            return []

        _local_tool_configs, mcp_tool_configs = self._partition_tool_configs(
            self._configured_tool_configs(agent_name, loaded_tools=loaded_tools),
        )
        messages: list[str] = []
        for server_id in sorted(mcp_tool_configs):
            server_config = config.mcp_servers.get(server_id)
            state = self._states.get(server_id)
            if server_config is not None and server_config.auth is not None:
                continue
            if state is not None and state.config.auth is not None:
                continue
            if state is None:
                messages.append(f"MCP server '{server_id}' is not configured or has not been synchronized.")
                continue
            if state.last_error is not None:
                messages.append(f"MCP server '{server_id}' is unavailable: {state.last_error}")
                continue
            if state.catalog is None or state.session is None or not state.connected:
                messages.append(f"MCP server '{server_id}' is not connected.")
        return messages

    def function_name_collision_messages_for_loaded_tools(
        self,
        agent_name: str,
        loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    ) -> list[str]:
        """Return collision messages for a candidate loaded dynamic-tool state."""
        visible_function_server_ids = self._visible_function_server_ids()
        if not visible_function_server_ids:
            return []
        errors_by_server = self._agent_collision_messages(
            agent_name,
            visible_function_server_ids,
            loaded_tools=loaded_tools,
        )
        return sorted({message for messages in errors_by_server.values() for message in messages})

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

    @classmethod
    def _runtime_exception_message(cls, exc: BaseException) -> str:
        if isinstance(exc, BaseExceptionGroup):
            nested_messages = [cls._runtime_exception_message(nested) for nested in exc.exceptions]
            nested_text = "; ".join(message for message in nested_messages if message)
            if nested_text:
                return f"{exc.message}: {nested_text}"
        return str(exc)

    def _wrap_runtime_exception(self, server_id: str, exc: Exception) -> MCPError:
        if isinstance(exc, MCPError):
            return exc
        message = self._runtime_exception_message(exc)
        if isinstance(exc, TimeoutError | asyncio.TimeoutError):
            return MCPTimeoutError(server_id, f"MCP operation timed out: {message}")
        return MCPConnectionError(server_id, f"MCP operation failed: {message}")
