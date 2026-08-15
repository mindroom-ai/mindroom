"""Runtime MCP session manager owned by the orchestrator."""

from __future__ import annotations

import asyncio
import hashlib
import json
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, cast
from weakref import WeakValueDictionary

import mcp.types as mcp_types
from httpx import HTTPStatusError
from mcp import ClientSession

from mindroom.background_tasks import run_coroutine_until_complete
from mindroom.credentials import get_runtime_credentials_manager
from mindroom.logging_config import get_logger
from mindroom.mcp.config import (
    MCPServerConfig,
    mcp_oauth_bridge_function_names,
    resolved_mcp_tool_prefix,
    validate_mcp_function_name,
)
from mindroom.mcp.errors import (
    MCPConnectionError,
    MCPError,
    MCPProtocolError,
    MCPTimeoutError,
    MCPToolCallError,
    MCPToolUnavailableError,
)
from mindroom.mcp.oauth import mcp_oauth_provider, mcp_oauth_provider_id
from mindroom.mcp.registry import mcp_server_id_from_tool_name, mcp_tool_name
from mindroom.mcp.results import tool_result_from_call_result
from mindroom.mcp.transports import build_transport_handle
from mindroom.mcp.types import MCPDiscoveredTool, MCPServerCatalog, MCPServerState
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_credentials_snapshot,
    oauth_credentials_usable,
    refresh_oauth_credentials_with_result,
    resolve_oauth_credential_context,
)
from mindroom.oauth.providers import OAuthConnectionRequired, OAuthProviderError, OAuthRefreshRejectedError
from mindroom.oauth.service import (
    OAUTH_ACCESS_REJECTED_REASON,
    OAUTH_REFRESH_REJECTED_REASON,
    oauth_connection_required,
)
from mindroom.tool_system.catalog import TOOL_METADATA, ensure_tool_registry_loaded, get_tool_by_name
from mindroom.tool_system.dynamic_toolkits import visible_tool_surface

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping

    from agno.tools.function import ToolResult
    from mcp.client.session import MessageHandlerFnT

    from mindroom.config.auth import AuthorizationConfig
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
class _MCPOAuthRequestKey:
    """Provider and requester identity shared across server config generations."""

    provider_id: str
    worker_scope: str
    worker_key: str


@dataclass(frozen=True)
class _MCPSessionKey:
    """Requester-scoped MCP session cache key."""

    server_id: str
    config_generation: int
    provider_id: str
    worker_scope: str
    worker_key: str

    @property
    def oauth_request_key(self) -> _MCPOAuthRequestKey:
        """Return the provider/requester identity used by reset retirement."""
        return _MCPOAuthRequestKey(
            provider_id=self.provider_id,
            worker_scope=self.worker_scope,
            worker_key=self.worker_key,
        )


@dataclass(frozen=True)
class _MCPAuthorizationLease:
    """Authorization material and identity for one requester operation."""

    headers: Mapping[str, str]
    token_hash: str
    credential_context: OAuthCredentialContext
    credential_generation: str
    session_key: _MCPSessionKey


class _MCPAuthorizationChangedError(RuntimeError):
    """Signal that a requester must reacquire authoritative authorization."""


class _MCPConfigurationChangedError(RuntimeError):
    """Signal that a requester resolved against a retired config generation."""


class _MCPFunctionValidationError(MCPProtocolError):
    """Signal that one candidate catalog conflicts with the provider-visible surface."""


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
        self._retiring_states: dict[int, MCPServerState] = {}
        self._request_retirement_locks: WeakValueDictionary[_MCPOAuthRequestKey, asyncio.Lock] = WeakValueDictionary()
        self._retired_request_keys: set[_MCPOAuthRequestKey] = set()
        self._catalog_validation_lock = asyncio.Lock()
        self._state_lifecycle_lock = asyncio.Lock()
        self._sync_lock = asyncio.Lock()
        self._on_catalog_change = on_catalog_change
        self._config: Config | None = None
        self._last_config_generation = 0
        self._shutdown = False
        self._shutdown_complete = asyncio.Event()

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
        if state.last_error is not None:
            raise state.last_error
        if state.catalog is not None:
            return state.catalog
        msg = f"MCP server '{server_id}' is not connected"
        raise MCPConnectionError(server_id, msg)

    async def sync_servers(self, config: Config) -> set[str]:
        """Reconcile live server sessions against the active config."""
        async with self._sync_lock:
            desired_servers = {
                server_id: server_config
                for server_id, server_config in config.mcp_servers.items()
                if server_config.enabled
            }
            retired_states = await self._publish_server_config(config, desired_servers)
            if retired_states is None:
                return set()
            await run_coroutine_until_complete(self._drain_retired_states(tuple(retired_states)))
            await self._clear_function_validation_errors()
            changed_server_ids: set[str] = set()
            for server_id, server_config in desired_servers.items():
                state = self._states[server_id]
                if state.retired:
                    continue
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

            async with self._state_lifecycle_lock:
                if self._shutdown:
                    return set()
            invalid_server_ids = await self._validate_global_function_names()
            changed_server_ids.difference_update(invalid_server_ids)
            changed_server_ids.difference_update(self.failed_server_ids())
            return changed_server_ids

    async def _publish_server_config(
        self,
        config: Config,
        desired_servers: Mapping[str, MCPServerConfig],
    ) -> list[MCPServerState] | None:
        """Atomically replace changed base generations and detach their scoped sessions."""
        retired_states: list[MCPServerState] = []

        def retire_state(state: MCPServerState) -> None:
            if state.retired:
                return
            state.retired = True
            self._retiring_states[id(state)] = state
            retired_states.append(state)

        async with self._state_lifecycle_lock:
            if self._shutdown:
                return None
            for server_id, state in tuple(self._states.items()):
                server_config = desired_servers.get(server_id)
                if server_config is not None and state.config == server_config:
                    continue
                self._states.pop(server_id)
                retire_state(state)
                for key, scoped_state in tuple(self._scoped_states.items()):
                    if key.server_id == server_id:
                        self._scoped_states.pop(key)
                        retire_state(scoped_state)
            for server_id, server_config in desired_servers.items():
                if server_id in self._states:
                    continue
                self._last_config_generation += 1
                provider_id = (
                    mcp_oauth_provider_id(server_id, server_config.auth) if server_config.auth is not None else None
                )
                self._states[server_id] = MCPServerState(
                    server_id=server_id,
                    config=server_config,
                    config_generation=self._last_config_generation,
                    oauth_provider_id=provider_id,
                )
            self._config = config
        return retired_states

    async def shutdown(self) -> None:
        """Close all tracked sessions and background refresh tasks."""
        async with self._state_lifecycle_lock:
            if self._shutdown:
                shutdown_complete = self._shutdown_complete
                shutdown_states: tuple[MCPServerState, ...] | None = None
            else:
                self._shutdown = True
                self._config = None
                shutdown_complete = self._shutdown_complete
                seen_state_ids: set[int] = set()
                captured_states: list[MCPServerState] = []
                for state in (
                    *self._states.values(),
                    *self._scoped_states.values(),
                    *self._retiring_states.values(),
                ):
                    if id(state) in seen_state_ids:
                        continue
                    seen_state_ids.add(id(state))
                    captured_states.append(state)
                shutdown_states = tuple(captured_states)
                for state in shutdown_states:
                    state.retired = True
        if shutdown_states is None:
            await shutdown_complete.wait()
            return
        await run_coroutine_until_complete(self._drain_shutdown_states(shutdown_states))

    async def _drain_shutdown_states(self, states: tuple[MCPServerState, ...]) -> None:
        """Drain every captured state before publishing terminal manager shutdown."""
        try:
            for state in states:
                try:
                    await self._cancel_refresh_task(state)
                except (asyncio.CancelledError, Exception) as exc:
                    logger.warning(
                        "MCP shutdown refresh cleanup failed",
                        server_id=state.server_id,
                        error_type=type(exc).__name__,
                    )
                try:
                    await self._disconnect_state_when_idle(state)
                except (asyncio.CancelledError, Exception) as exc:
                    logger.warning(
                        "MCP shutdown session cleanup failed",
                        server_id=state.server_id,
                        error_type=type(exc).__name__,
                    )
        finally:
            async with self._state_lifecycle_lock:
                self._states.clear()
                self._scoped_states.clear()
                self._retiring_states.clear()
                self._request_retirement_locks.clear()
                self._retired_request_keys.clear()
                self._shutdown_complete.set()

    async def call_tool(
        self,
        server_id: str,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float | None = None,
        credentials_manager: CredentialsManager | None = None,
        worker_target: ResolvedWorkerTarget | None = None,
        authorization: AuthorizationConfig | None = None,
        include_tools: Collection[str] | None = None,
        exclude_tools: Collection[str] | None = None,
    ) -> ToolResult:
        """Call one remote MCP tool through the cached session."""
        state = self._require_state(server_id)
        if state.config.auth is not None:
            while True:
                request_state, authorization_lease = await self._request_state_and_headers(
                    server_id,
                    credentials_manager=credentials_manager,
                    worker_target=worker_target,
                    authorization=authorization,
                )
                try:
                    if (
                        request_state.catalog is None
                        or request_state.session is None
                        or request_state.stale
                        or request_state.last_error is not None
                        or not request_state.connected
                    ):
                        await self._refresh_server_catalog(
                            request_state,
                            notify=False,
                            auth_headers=authorization_lease.headers,
                            authorization_lease=authorization_lease,
                        )
                    return await self._call_tool_once_or_reconnect(
                        request_state,
                        remote_tool_name,
                        arguments,
                        timeout_seconds=timeout_seconds or request_state.config.call_timeout_seconds,
                        auth_headers=authorization_lease.headers,
                        authorization_lease=authorization_lease,
                        include_tools=include_tools,
                        exclude_tools=exclude_tools,
                    )
                except _MCPAuthorizationChangedError:
                    continue

        if state.catalog is None or state.session is None or not state.connected:
            await self._refresh_server_catalog(state, notify=False)
        return await self._call_tool_once_or_reconnect(
            state,
            remote_tool_name,
            arguments,
            timeout_seconds=timeout_seconds or state.config.call_timeout_seconds,
            include_tools=include_tools,
            exclude_tools=exclude_tools,
        )

    async def get_request_catalog(
        self,
        server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None = None,
    ) -> MCPServerCatalog:
        """Return a requester-scoped catalog for one OAuth-backed MCP server."""
        while True:
            state, authorization_lease = await self._request_state_and_headers(
                server_id,
                credentials_manager=credentials_manager,
                worker_target=worker_target,
                authorization=authorization,
            )
            try:
                if state.catalog is None or state.stale or state.last_error is not None or not state.connected:
                    await self._refresh_server_catalog(
                        state,
                        notify=False,
                        auth_headers=authorization_lease.headers,
                        authorization_lease=authorization_lease,
                    )
                return await self._request_catalog_with_lock(state, authorization_lease)
            except _MCPAuthorizationChangedError:
                continue
            except MCPError as exc:
                try:
                    rejection = await self._oauth_transport_rejection(state, authorization_lease, exc)
                except _MCPAuthorizationChangedError:
                    continue
                if rejection is None:
                    raise
                await run_coroutine_until_complete(
                    self._disconnect_rejected_oauth_request_state(authorization_lease.session_key, state),
                )
                raise rejection from exc

    def cached_request_catalog(
        self,
        server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None = None,
    ) -> MCPServerCatalog | None:
        """Return an already-discovered worker-scoped catalog without network or credential I/O."""
        base_state = self._states.get(server_id)
        if base_state is None or base_state.config.auth is None:
            return None
        try:
            credential_context = self._oauth_credential_context(
                base_state,
                worker_target=worker_target,
                authorization=authorization,
            )
            key = self._request_session_key(base_state, credential_context.worker_target)
        except OAuthConnectionRequired:
            return None
        state = self._scoped_states.get(key)
        if state is None or state.retired or state.catalog is None or state.stale or state.last_error is not None:
            return None
        return state.catalog

    @asynccontextmanager
    async def retire_request_session(
        self,
        *,
        credential_context: OAuthCredentialContext,
        expected_connection_generation: str | None = None,
    ) -> AsyncIterator[None]:
        """Fence one provider/requester lineage across every server config generation."""
        worker_target = credential_context.worker_target
        request_key = _MCPOAuthRequestKey(
            provider_id=credential_context.provider.id,
            worker_scope=(worker_target.worker_scope if worker_target is not None else None) or "unscoped",
            worker_key=(worker_target.worker_key if worker_target is not None else None) or "global",
        )
        async with self._state_lifecycle_lock:
            retirement_lock = self._request_retirement_locks.setdefault(request_key, asyncio.Lock())
        async with retirement_lock:
            async with self._state_lifecycle_lock:
                self._retired_request_keys.add(request_key)
            try:
                snapshot = await load_oauth_credentials_snapshot(credential_context)
                if (
                    expected_connection_generation is not None
                    and snapshot.connection_generation != expected_connection_generation
                ):
                    yield
                    return
                async with self._state_lifecycle_lock:
                    states: list[MCPServerState] = []
                    seen_state_ids: set[int] = set()
                    for key, state in tuple(self._scoped_states.items()):
                        if key.oauth_request_key != request_key:
                            continue
                        self._scoped_states.pop(key)
                        state.retired = True
                        self._retiring_states[id(state)] = state
                        states.append(state)
                        seen_state_ids.add(id(state))
                    for state in self._retiring_states.values():
                        if (
                            id(state) in seen_state_ids
                            or state.oauth_provider_id != request_key.provider_id
                            or state.oauth_request_scope != (request_key.worker_scope, request_key.worker_key)
                        ):
                            continue
                        state.retired = True
                        states.append(state)
                        seen_state_ids.add(id(state))
                for state in states:
                    await self._cancel_refresh_task(state)
                    async with state.lock:
                        await self._disconnect_state_when_idle(state)
                    async with self._state_lifecycle_lock:
                        self._retiring_states.pop(id(state), None)
                yield
            finally:
                self._retired_request_keys.discard(request_key)

    def _oauth_credential_context(
        self,
        state: MCPServerState,
        *,
        worker_target: ResolvedWorkerTarget | None,
        credentials_manager: CredentialsManager | None = None,
        authorization: AuthorizationConfig | None = None,
    ) -> OAuthCredentialContext:
        return resolve_oauth_credential_context(
            mcp_oauth_provider(state.server_id, state.config),
            self.runtime_paths,
            credentials_manager or get_runtime_credentials_manager(self.runtime_paths),
            worker_target,
            authorization=authorization,
        )

    def _request_session_key(
        self,
        state: MCPServerState,
        worker_target: ResolvedWorkerTarget | None,
    ) -> _MCPSessionKey:
        worker_scope = worker_target.worker_scope if worker_target is not None else None
        worker_key = worker_target.worker_key if worker_target is not None else None
        identity = worker_target.execution_identity if worker_target is not None else None
        if (
            worker_scope not in {"user", "user_agent"}
            or not worker_key
            or identity is None
            or not identity.requester_id
        ):
            provider_id = state.oauth_provider_id or mcp_oauth_provider_id(state.server_id, state.config.auth)
            msg = f"MCP OAuth provider '{provider_id}' requires a requester identity"
            raise OAuthConnectionRequired(msg, provider_id=provider_id)
        return _MCPSessionKey(
            server_id=state.server_id,
            config_generation=state.config_generation,
            provider_id=(state.oauth_provider_id or mcp_oauth_provider_id(state.server_id, state.config.auth)),
            worker_scope=worker_scope or "unscoped",
            worker_key=worker_key or "global",
        )

    def _log_oauth_refresh_failure(
        self,
        state: MCPServerState,
        provider_id: str,
        credentials: Mapping[str, object],
        exc: OAuthProviderError,
    ) -> None:
        refresh_token = credentials.get("refresh_token")
        raw_expires_at = credentials.get("expires_at")
        expires_at = (
            float(raw_expires_at)
            if not isinstance(raw_expires_at, bool) and isinstance(raw_expires_at, int | float)
            else None
        )
        has_refresh_token = isinstance(refresh_token, str) and bool(refresh_token)
        if isinstance(exc, OAuthRefreshRejectedError):
            if exc.refresh_had_token is not None:
                has_refresh_token = exc.refresh_had_token
            if exc.refresh_expires_at is not None:
                expires_at = exc.refresh_expires_at
        logger.warning(
            "MCP OAuth token refresh failed",
            provider_id=provider_id,
            server_id=state.server_id,
            has_refresh_token=has_refresh_token,
            expires_at=expires_at,
            error_type=type(exc).__name__,
            refresh_rejected=isinstance(exc, OAuthRefreshRejectedError),
        )

    @staticmethod
    def _oauth_refreshed_expires_at(credentials: Mapping[str, object]) -> float | None:
        expires_at = credentials.get("expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int | float):
            return None
        return float(expires_at)

    async def _oauth_authorization_material(
        self,
        state: MCPServerState,
        *,
        credential_context: OAuthCredentialContext,
    ) -> tuple[str, str]:
        """Return the usable access token and exact committed credential generation."""
        context = credential_context
        provider = context.provider
        try:
            refresh_result = await refresh_oauth_credentials_with_result(context)
            credentials = refresh_result.credentials
        except OAuthProviderError as exc:
            failed_credentials = (await load_oauth_credentials_snapshot(context)).credentials
            self._log_oauth_refresh_failure(state, provider.id, failed_credentials or {}, exc)
            reason = OAUTH_REFRESH_REJECTED_REASON if isinstance(exc, OAuthRefreshRejectedError) else None
            raise oauth_connection_required(context, reason=reason) from exc
        if not oauth_credentials_usable(provider, self.runtime_paths, credentials):
            raise oauth_connection_required(context)
        assert credentials is not None
        if refresh_result.refreshed:
            logger.info(
                "MCP OAuth token refreshed",
                provider_id=provider.id,
                server_id=state.server_id,
                expires_at=self._oauth_refreshed_expires_at(credentials),
            )
        token = credentials.get("token") or credentials.get("access_token")
        if not isinstance(token, str) or not token:
            raise oauth_connection_required(context)
        return token, refresh_result.generation

    async def _request_state_and_headers(
        self,
        server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None = None,
    ) -> tuple[MCPServerState, _MCPAuthorizationLease]:
        while True:
            try:
                return await self._request_state_and_headers_once(
                    server_id,
                    credentials_manager=credentials_manager,
                    worker_target=worker_target,
                    authorization=authorization,
                )
            except _MCPConfigurationChangedError:
                continue

    async def _request_state_and_headers_once(
        self,
        server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
        authorization: AuthorizationConfig | None,
    ) -> tuple[MCPServerState, _MCPAuthorizationLease]:
        """Resolve one requester session against an exact published config generation."""
        base_state = self._require_state(server_id)
        if base_state.config.auth is None:
            msg = f"MCP server '{server_id}' is not OAuth-backed"
            raise MCPConnectionError(server_id, msg)
        if base_state.last_error is not None:
            raise base_state.last_error
        credential_context = self._oauth_credential_context(
            base_state,
            worker_target=worker_target,
            credentials_manager=credentials_manager,
            authorization=authorization,
        )
        worker_target = credential_context.worker_target
        key = self._request_session_key(base_state, worker_target)
        request_key = key.oauth_request_key
        async with self._state_lifecycle_lock:
            if self._shutdown:
                msg = f"MCP server manager shut down while resolving '{server_id}'"
                raise MCPConnectionError(server_id, msg)
            if (
                self._states.get(server_id) is not base_state
                or base_state.config_generation != key.config_generation
                or base_state.oauth_provider_id != key.provider_id
                or base_state.retired
            ):
                raise _MCPConfigurationChangedError
            if request_key in self._retired_request_keys:
                raise oauth_connection_required(credential_context)
            state = self._scoped_states.get(key)
            if state is None:
                state = MCPServerState(
                    server_id=server_id,
                    config=base_state.config,
                    config_generation=key.config_generation,
                    oauth_provider_id=key.provider_id,
                    oauth_request_scope=(key.worker_scope, key.worker_key),
                )
                self._scoped_states[key] = state

        try:
            async with state.lock:
                self._require_current_request_state(
                    key,
                    state,
                    credential_context=credential_context,
                )
                access_token, credential_generation = await self._oauth_authorization_material(
                    base_state,
                    credential_context=credential_context,
                )
                self._require_current_request_state(
                    key,
                    state,
                    credential_context=credential_context,
                )
                token_hash = hashlib.sha256(access_token.encode("utf-8")).hexdigest()
                if (
                    state.oauth_access_token_hash != token_hash
                    or state.oauth_credential_generation != credential_generation
                ):
                    async with state.call_lock.write():
                        await self._disconnect_state(state)
                        state.catalog = None
                        state.last_error = None
                        state.stale = True
                        state.oauth_access_token_hash = token_hash
                        state.oauth_credential_generation = credential_generation
        except OAuthConnectionRequired:
            await run_coroutine_until_complete(self._disconnect_rejected_oauth_request_state(key, state))
            raise
        return state, _MCPAuthorizationLease(
            headers={"Authorization": f"Bearer {access_token}"},
            token_hash=token_hash,
            credential_context=credential_context,
            credential_generation=credential_generation,
            session_key=key,
        )

    def _require_current_request_state(
        self,
        key: _MCPSessionKey,
        state: MCPServerState,
        *,
        credential_context: OAuthCredentialContext,
    ) -> None:
        """Distinguish reset retirement from ordinary config-generation replacement."""
        if key.oauth_request_key in self._retired_request_keys:
            raise oauth_connection_required(credential_context)
        if state.retired or self._scoped_states.get(key) is not state:
            raise _MCPConfigurationChangedError

    async def _disconnect_rejected_oauth_request_state(
        self,
        key: _MCPSessionKey,
        state: MCPServerState,
    ) -> None:
        """Retire cached bearer state after credentials become unusable or rejected."""
        state.retired = True
        async with self._state_lifecycle_lock:
            if self._scoped_states.get(key) is state:
                self._scoped_states.pop(key)
            self._retiring_states[id(state)] = state
        drained = False
        try:
            async with state.call_lock.write():
                try:
                    await self._disconnect_state(state)
                except asyncio.CancelledError:
                    current_task = asyncio.current_task()
                    if current_task is not None and current_task.cancelling():
                        raise
                    logger.warning(
                        "MCP OAuth rejected-session disconnect failed",
                        server_id=state.server_id,
                        error_type="CancelledError",
                    )
                except Exception as exc:
                    logger.warning(
                        "MCP OAuth rejected-session disconnect failed",
                        server_id=state.server_id,
                        error_type=type(exc).__name__,
                    )
                finally:
                    state.catalog = None
                    state.last_error = None
                    state.stale = True
                    state.oauth_access_token_hash = None
                    state.oauth_credential_generation = None
            drained = True
        finally:
            if drained:
                async with self._state_lifecycle_lock:
                    self._retiring_states.pop(id(state), None)

    async def _call_tool_once_or_reconnect(
        self,
        state: MCPServerState,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float,
        auth_headers: Mapping[str, str] | None = None,
        authorization_lease: _MCPAuthorizationLease | None = None,
        include_tools: Collection[str] | None = None,
        exclude_tools: Collection[str] | None = None,
    ) -> ToolResult:
        self._require_active_state(state)
        if authorization_lease is not None:
            rejection = await self._oauth_transport_rejection(state, authorization_lease)
            if rejection is not None:
                await run_coroutine_until_complete(
                    self._disconnect_rejected_oauth_request_state(authorization_lease.session_key, state),
                )
                raise rejection
        refresh_revision = state.refresh_revision
        ambiguous_dispatch_error: MCPConnectionError | MCPTimeoutError | None = None
        try:
            return await self._call_tool_with_lock(
                state,
                remote_tool_name,
                arguments,
                timeout_seconds=timeout_seconds,
                authorization_lease=authorization_lease,
                include_tools=include_tools,
                exclude_tools=exclude_tools,
            )
        except (MCPToolCallError, MCPProtocolError):
            raise
        except (MCPConnectionError, MCPTimeoutError) as dispatch_error:
            if authorization_lease is not None:
                rejection = await self._oauth_transport_rejection(
                    state,
                    authorization_lease,
                    dispatch_error,
                )
                if rejection is not None:
                    await run_coroutine_until_complete(
                        self._disconnect_rejected_oauth_request_state(
                            authorization_lease.session_key,
                            state,
                        ),
                    )
                    raise rejection from dispatch_error
            if state.last_error is not None or not state.config.auto_reconnect:
                raise
            ambiguous_dispatch_error = dispatch_error
        except MCPError:
            raise

        try:
            await self._refresh_server_catalog(
                state,
                notify=True,
                expected_refresh_revision=refresh_revision,
                auth_headers=auth_headers,
                authorization_lease=authorization_lease,
            )
        except _MCPAuthorizationChangedError as exc:
            msg = f"MCP server '{state.server_id}' authorization changed after remote dispatch; retry manually"
            raise MCPConnectionError(state.server_id, msg) from exc
        assert ambiguous_dispatch_error is not None
        msg = f"MCP server '{state.server_id}' remote call outcome is unknown; retry manually"
        raise MCPConnectionError(state.server_id, msg) from ambiguous_dispatch_error

    async def _call_tool_with_lock(
        self,
        state: MCPServerState,
        remote_tool_name: str,
        arguments: dict[str, object],
        *,
        timeout_seconds: float,
        authorization_lease: _MCPAuthorizationLease | None = None,
        include_tools: Collection[str] | None = None,
        exclude_tools: Collection[str] | None = None,
    ) -> ToolResult:
        async with state.semaphore, state.call_lock.read():
            self._require_active_state(state)
            self._require_desired_oauth_lease(state, authorization_lease)
            if state.last_error is not None:
                raise state.last_error
            await self._validate_authoritative_oauth_lease(state, authorization_lease)
            self._require_session_oauth_lease(state, authorization_lease)
            if state.session is None or state.catalog is None or not state.connected:
                msg = f"MCP server '{state.server_id}' is not connected"
                raise MCPConnectionError(state.server_id, msg)
            self._require_catalog_tool(
                state,
                remote_tool_name,
                include_tools=include_tools,
                exclude_tools=exclude_tools,
            )
            return await self._call_tool_once(
                state,
                remote_tool_name,
                arguments,
                timeout_seconds=timeout_seconds,
            )

    async def _request_catalog_with_lock(
        self,
        state: MCPServerState,
        authorization_lease: _MCPAuthorizationLease,
    ) -> MCPServerCatalog:
        """Return catalog only while its connected authorization lease is current."""
        async with state.call_lock.read():
            self._require_active_state(state)
            self._require_desired_oauth_lease(state, authorization_lease)
            if state.last_error is not None:
                raise state.last_error
            await self._validate_authoritative_oauth_lease(state, authorization_lease)
            self._require_session_oauth_lease(state, authorization_lease)
            if state.catalog is not None and state.connected:
                return state.catalog
            msg = f"MCP server '{state.server_id}' is not connected"
            raise MCPConnectionError(state.server_id, msg)

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
        authorization_lease: _MCPAuthorizationLease | None = None,
    ) -> bool:
        self._require_active_state(state)
        should_notify_catalog_change = False
        async with state.lock:
            self._require_active_state(state)
            if expected_refresh_revision is not None and state.refresh_revision != expected_refresh_revision:
                return False
            self._require_desired_oauth_lease(state, authorization_lease)
            state.refresh_revision += 1
            state.stale = False
            async with state.call_lock.write():
                previous_hash = state.catalog.catalog_hash if state.catalog is not None else None
                try:
                    await self._disconnect_state(state)
                except Exception:
                    message = f"MCP server '{state.server_id}' reconnect teardown failed"
                    await self._record_discovery_failure(
                        state,
                        MCPConnectionError(state.server_id, message),
                    )
                    return False
                self._require_desired_oauth_lease(state, authorization_lease)
                try:
                    catalog = await self._connect_and_publish_catalog(
                        state,
                        auth_headers=auth_headers,
                        authorization_lease=authorization_lease,
                    )
                except _MCPAuthorizationChangedError:
                    await self._disconnect_state(state)
                    raise
                except MCPError as exc:
                    await self._record_discovery_failure(state, exc)
                    return False

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

    async def _connect_and_publish_catalog(
        self,
        state: MCPServerState,
        *,
        auth_headers: Mapping[str, str] | None,
        authorization_lease: _MCPAuthorizationLease | None,
    ) -> MCPServerCatalog:
        """Discover, revalidate, and atomically publish one candidate catalog."""
        await self._validate_authoritative_oauth_lease(state, authorization_lease)
        catalog = await self._connect_and_discover(state, auth_headers=auth_headers)
        try:
            await self._validate_authoritative_oauth_lease(state, authorization_lease)
            self._require_desired_oauth_lease(state, authorization_lease)
            if state.retired:
                await self._disconnect_state(state)
                self._require_active_state(state)
            async with self._catalog_validation_lock:
                self._require_active_state(state)
                collision_error = self._candidate_function_validation_error(state, catalog)
                if collision_error is not None:
                    raise collision_error  # noqa: TRY301
                state.oauth_session_access_token_hash = (
                    authorization_lease.token_hash if authorization_lease is not None else None
                )
                state.oauth_session_credential_generation = (
                    authorization_lease.credential_generation if authorization_lease is not None else None
                )
                state.catalog = catalog
                state.connected = True
                state.last_error = None
                state.function_validation_error = False
        except BaseException:
            try:
                await run_coroutine_until_complete(self._disconnect_state(state))
            except BaseException as close_error:
                logger.warning(
                    "MCP unpublished-session disconnect failed",
                    server_id=state.server_id,
                    error_type=type(close_error).__name__,
                )
            raise
        return catalog

    async def _record_discovery_failure(self, state: MCPServerState, error: MCPError) -> None:
        """Hide and drain one failed candidate while preserving its owning error."""
        try:
            await self._disconnect_state(state)
        except Exception as close_error:
            logger.warning(
                "MCP failed-session disconnect failed",
                server_id=state.server_id,
                error_type=type(close_error).__name__,
            )
        repeated_error = state.last_error is not None and str(state.last_error) == str(error)
        state.connected = False
        state.catalog = None
        state.function_validation_error = isinstance(error, _MCPFunctionValidationError)
        state.consecutive_failures += 1
        state.last_error = error
        log = logger.debug if repeated_error else logger.warning
        log(
            "MCP server discovery failed",
            server_id=state.server_id,
            transport=state.config.transport,
            error=str(error),
            required=state.config.required,
            affected_entities=sorted(self._entities_referencing_server(state.server_id)),
            consecutive_failures=state.consecutive_failures,
        )
        if state.config.auth is None:
            self._schedule_refresh_task(
                state,
                delay_seconds=_discovery_retry_delay_seconds(state.consecutive_failures),
            )

    async def _connect_and_discover(
        self,
        state: MCPServerState,
        *,
        auth_headers: Mapping[str, str] | None = None,
    ) -> MCPServerCatalog:
        self._require_active_state(state)
        handle = build_transport_handle(state.server_id, state.config, self.runtime_paths, extra_headers=auth_headers)
        state.oauth_transport_authorization_rejected = handle.authorization_rejected
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
        if self._shutdown or state.retired or state.config.auth is not None:
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

    async def _drain_retired_states(self, states: tuple[MCPServerState, ...]) -> None:
        """Close atomically detached config generations outside the lifecycle mutex."""
        for state in states:
            try:
                await self._cancel_refresh_task(state)
            except (asyncio.CancelledError, Exception) as exc:
                logger.warning(
                    "MCP retired-state refresh cleanup failed",
                    server_id=state.server_id,
                    error_type=type(exc).__name__,
                )
            try:
                async with state.lock:
                    await self._disconnect_state_when_idle(state)
            except (asyncio.CancelledError, Exception) as exc:
                logger.warning(
                    "MCP retired-state session cleanup failed",
                    server_id=state.server_id,
                    error_type=type(exc).__name__,
                )
            async with self._state_lifecycle_lock:
                self._retiring_states.pop(id(state), None)

    async def _clear_function_validation_errors(self) -> None:
        """Make collision-owned failures eligible for validation under the new config surface."""
        for state in (*tuple(self._states.values()), *tuple(self._scoped_states.values())):
            if not state.function_validation_error:
                continue
            async with state.lock:
                state.last_error = None
                state.function_validation_error = False
                state.stale = True

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
            if owner_task.done() and owner_task.cancelled():
                pass
            else:
                try:
                    if close_event is None:
                        await self._cancel_session_owner_task(owner_task)
                        close_error = RuntimeError(
                            f"MCP server '{state.server_id}' session owner is missing close event",
                        )
                    else:
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
        state.oauth_session_access_token_hash = None
        state.oauth_session_credential_generation = None
        state.oauth_transport_authorization_rejected = None
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

    def _require_catalog_tool(
        self,
        state: MCPServerState,
        remote_tool_name: str,
        *,
        include_tools: Collection[str] | None,
        exclude_tools: Collection[str] | None,
    ) -> None:
        self._require_active_state(state)
        catalog = state.catalog
        if catalog is None:
            msg = f"MCP server '{state.server_id}' is not connected"
            raise MCPConnectionError(state.server_id, msg)
        included = set(include_tools or ())
        excluded = set(exclude_tools or ())
        available_tools = tuple(
            sorted(
                tool.remote_name
                for tool in catalog.tools
                if (not included or tool.remote_name in included) and (not excluded or tool.remote_name not in excluded)
            ),
        )
        if remote_tool_name not in available_tools:
            raise MCPToolUnavailableError(state.server_id, remote_tool_name, available_tools)

    def _require_desired_oauth_lease(
        self,
        state: MCPServerState,
        authorization_lease: _MCPAuthorizationLease | None,
    ) -> None:
        if state.config.auth is not None and authorization_lease is None:
            raise _MCPAuthorizationChangedError
        if authorization_lease is not None and (
            state.oauth_access_token_hash != authorization_lease.token_hash
            or state.oauth_credential_generation != authorization_lease.credential_generation
            or state.config_generation != authorization_lease.session_key.config_generation
            or state.oauth_provider_id != authorization_lease.session_key.provider_id
            or state.oauth_request_scope
            != (
                authorization_lease.session_key.worker_scope,
                authorization_lease.session_key.worker_key,
            )
            or self._scoped_states.get(authorization_lease.session_key) is not state
            or authorization_lease.session_key.oauth_request_key in self._retired_request_keys
        ):
            raise _MCPAuthorizationChangedError

    def _require_session_oauth_lease(
        self,
        state: MCPServerState,
        authorization_lease: _MCPAuthorizationLease | None,
    ) -> None:
        self._require_desired_oauth_lease(state, authorization_lease)
        if authorization_lease is not None and (
            state.oauth_session_access_token_hash != authorization_lease.token_hash
            or state.oauth_session_credential_generation != authorization_lease.credential_generation
        ):
            raise _MCPAuthorizationChangedError

    async def _validate_authoritative_oauth_lease(
        self,
        state: MCPServerState,
        authorization_lease: _MCPAuthorizationLease | None,
    ) -> None:
        """Revalidate durable authorization immediately before publication or remote use."""
        if authorization_lease is None:
            return
        self._require_desired_oauth_lease(state, authorization_lease)
        try:
            snapshot = await load_oauth_credentials_snapshot(authorization_lease.credential_context)
        except OAuthProviderError as exc:
            raise _MCPAuthorizationChangedError from exc
        self._require_desired_oauth_lease(state, authorization_lease)
        credentials = snapshot.credentials or {}
        access_token = credentials.get("token") or credentials.get("access_token")
        if (
            snapshot.generation != authorization_lease.credential_generation
            or not isinstance(access_token, str)
            or hashlib.sha256(access_token.encode("utf-8")).hexdigest() != authorization_lease.token_hash
        ):
            raise _MCPAuthorizationChangedError

    @staticmethod
    def _require_active_state(state: MCPServerState) -> None:
        if state.retired:
            msg = f"MCP server '{state.server_id}' requester session generation is retired"
            raise MCPConnectionError(state.server_id, msg)

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
        *,
        requester_surface: tuple[str, str] | None,
        candidate_state: MCPServerState | None = None,
        candidate_catalog: MCPServerCatalog | None = None,
    ) -> tuple[set[str], set[str]]:
        """Return visible function names and real same-server collisions for one MCP server."""
        state = self._states.get(server_id)
        if state is None or (state.last_error is not None and state is not candidate_state):
            return set(), set()
        base_function_names: set[str] = set()
        duplicate_function_names: set[str] = set()
        if state.config.auth is not None:
            base_function_names.update(mcp_oauth_bridge_function_names(server_id, state.config))
        state_catalog = candidate_catalog if state is candidate_state else state.catalog
        if state_catalog is not None:
            catalog_function_names = self._catalog_function_names_for_tool_config(state_catalog, tool_config)
            duplicate_function_names.update(base_function_names & catalog_function_names)
            base_function_names.update(catalog_function_names)
        scoped_function_names: set[str] = set()
        for key, scoped_state in self._scoped_states.items():
            scoped_catalog = candidate_catalog if scoped_state is candidate_state else scoped_state.catalog
            if (
                key.server_id != server_id
                or requester_surface is None
                or (key.worker_scope, key.worker_key) != requester_surface
                or scoped_catalog is None
                or (scoped_state.last_error is not None and scoped_state is not candidate_state)
            ):
                continue
            catalog_function_names = self._catalog_function_names_for_tool_config(scoped_catalog, tool_config)
            duplicate_function_names.update(base_function_names & catalog_function_names)
            scoped_function_names.update(catalog_function_names)
        return base_function_names | scoped_function_names, duplicate_function_names

    def _agent_collision_messages(
        self,
        agent_name: str,
        visible_function_server_ids: set[str],
        *,
        loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
        requester_surface: tuple[str, str] | None = None,
        candidate_state: MCPServerState | None = None,
        candidate_catalog: MCPServerCatalog | None = None,
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
                    requester_surface=requester_surface,
                    candidate_state=candidate_state,
                    candidate_catalog=candidate_catalog,
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

    def _candidate_function_validation_error(
        self,
        state: MCPServerState,
        catalog: MCPServerCatalog,
    ) -> _MCPFunctionValidationError | None:
        """Validate a discovered catalog before publishing it to concurrent callers."""
        visible_server_ids = self._visible_function_server_ids() | {state.server_id}
        requester_surface = next(
            (
                (key.worker_scope, key.worker_key)
                for key, scoped_state in self._scoped_states.items()
                if scoped_state is state
            ),
            None,
        )
        messages: set[str] = set()
        for agent_name in sorted(self._config.agents) if self._config is not None else ():
            errors = self._agent_collision_messages(
                agent_name,
                visible_server_ids,
                loaded_tools=[],
                requester_surface=requester_surface,
                candidate_state=state,
                candidate_catalog=catalog,
            )
            messages.update(errors.get(state.server_id, ()))
        if not messages:
            return None
        return _MCPFunctionValidationError(state.server_id, "\n".join(sorted(messages)))

    @staticmethod
    def _mark_function_name_collision_errors(
        errors_by_state: dict[int, tuple[MCPServerState, set[str]]],
    ) -> tuple[MCPServerState, ...]:
        """Hide invalid catalogs atomically before their sessions drain."""
        marked_states: list[MCPServerState] = []
        for state, messages in errors_by_state.values():
            server_id = state.server_id
            error_message = "\n".join(sorted(messages))
            state.catalog = None
            state.last_error = MCPProtocolError(server_id, error_message)
            state.function_validation_error = True
            state.stale = False
            marked_states.append(state)
        return tuple(marked_states)

    def _function_validation_states_for_surface(
        self,
        server_id: str,
        requester_surface: tuple[str, str] | None,
    ) -> tuple[MCPServerState, ...]:
        """Return only states whose visible function surface owns one collision."""
        if requester_surface is None:
            state = self._states.get(server_id)
            return (state,) if state is not None else ()
        return tuple(
            state
            for key, state in self._scoped_states.items()
            if key.server_id == server_id and (key.worker_scope, key.worker_key) == requester_surface
        )

    async def _disconnect_function_validation_states(self, states: tuple[MCPServerState, ...]) -> None:
        """Drain sessions whose catalogs were already hidden by validation."""
        for state in states:
            async with state.lock:
                if not state.function_validation_error:
                    continue
                await self._disconnect_state_when_idle(state)

    async def _validate_global_function_names(self) -> set[str]:
        async with self._catalog_validation_lock:
            visible_function_server_ids = self._visible_function_server_ids()
            if not visible_function_server_ids:
                return set()

            requester_surfaces = {
                (key.worker_scope, key.worker_key)
                for key, state in self._scoped_states.items()
                if state.catalog is not None and state.last_error is None
            }
            errors_by_state: dict[int, tuple[MCPServerState, set[str]]] = {}
            for requester_surface in (None, *sorted(requester_surfaces)):
                for agent_name in sorted(self._config.agents) if self._config is not None else ():
                    for server_id, messages in self._agent_collision_messages(
                        agent_name,
                        visible_function_server_ids,
                        loaded_tools=[],
                        requester_surface=requester_surface,
                    ).items():
                        for state in self._function_validation_states_for_surface(server_id, requester_surface):
                            entry = errors_by_state.setdefault(id(state), (state, set()))
                            entry[1].update(messages)
            if not errors_by_state:
                return set()
            marked_states = self._mark_function_name_collision_errors(errors_by_state)
        await self._disconnect_function_validation_states(marked_states)
        return {state.server_id for state in marked_states}

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
            include_matrix_room_runtime_tools=True,
        ).runtime_tool_configs

    def _mcp_server_id_from_tool_config_name(self, tool_name: str) -> str | None:
        """Return the MCP server id for a tool name visible in this manager's active config."""
        config = self._config
        if config is not None:
            for server_id, server_config in config.mcp_servers.items():
                if server_config.enabled and tool_name == mcp_tool_name(server_id):
                    return server_id
        registered_server_id = mcp_server_id_from_tool_name(tool_name)
        return registered_server_id if registered_server_id in self._states else None

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
        if tool_name == "memory" and config.resolve_entity(agent_name).memory_backend == "none":
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
        authorization: AuthorizationConfig,
    ) -> set[str]:
        """Return provider-visible function names exposed by one set of local tools."""
        function_names: set[str] = set()
        for tool_config in sorted(tool_configs, key=lambda entry: entry.name):
            try:
                toolkit = get_tool_by_name(
                    tool_config.name,
                    self.runtime_paths,
                    worker_target=None,
                    authorization=authorization,
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
                authorization=config.authorization,
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

    async def _oauth_transport_rejection(
        self,
        state: MCPServerState,
        authorization_lease: _MCPAuthorizationLease,
        exc: BaseException | None = None,
    ) -> OAuthConnectionRequired | None:
        """Return reconnect-required only for a same-generation structured bearer rejection."""
        rejected = (
            state.oauth_transport_authorization_rejected is not None and state.oauth_transport_authorization_rejected()
        ) or (exc is not None and self._runtime_exception_has_http_status(exc, 401))
        if not rejected:
            return None
        await self._validate_authoritative_oauth_lease(state, authorization_lease)
        return oauth_connection_required(
            authorization_lease.credential_context,
            reason=OAUTH_ACCESS_REJECTED_REASON,
        )

    @classmethod
    def _runtime_exception_has_http_status(cls, exc: BaseException, status_code: int) -> bool:
        """Return whether a nested structured transport failure carries one HTTP status."""
        if isinstance(exc, HTTPStatusError) and exc.response.status_code == status_code:
            return True
        if isinstance(exc, BaseExceptionGroup) and any(
            cls._runtime_exception_has_http_status(nested, status_code) for nested in exc.exceptions
        ):
            return True
        cause = exc.__cause__
        return cause is not None and cls._runtime_exception_has_http_status(cause, status_code)

    def _wrap_runtime_exception(self, server_id: str, exc: Exception) -> MCPError:
        if isinstance(exc, MCPError):
            return exc
        message = self._runtime_exception_message(exc)
        if isinstance(exc, TimeoutError | asyncio.TimeoutError):
            return MCPTimeoutError(server_id, f"MCP operation timed out: {message}")
        return MCPConnectionError(server_id, f"MCP operation failed: {message}")
