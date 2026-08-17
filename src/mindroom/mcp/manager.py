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
from mindroom.mcp.registry import mcp_tool_name
from mindroom.mcp.results import tool_result_from_call_result
from mindroom.mcp.surface_projection import (
    MCPFunctionSurfaceContext,
    MCPScopedFunctionState,
    function_collision_messages,
    function_collision_reports,
    mcp_tool_unavailable_messages,
)
from mindroom.mcp.transports import build_transport_handle
from mindroom.mcp.types import (
    MCPDiscoveredTool,
    MCPOAuthLeaseVersion,
    MCPServerCatalog,
    MCPServerState,
)
from mindroom.oauth.credential_lifecycle import (
    OAuthCredentialContext,
    load_oauth_credentials_snapshot,
    load_oauth_reset_connection_generation,
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

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Collection, Mapping

    from agno.tools.function import ToolResult
    from mcp.client.session import MessageHandlerFnT

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.credentials import CredentialsManager
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

logger = get_logger(__name__)
_SANITIZED_OAUTH_REFRESH_ERROR_MESSAGE = "OAuth credential refresh failed"

# The cap matches STARTUP_RETRY_MAX_DELAY_SECONDS so a recovered required server
# unblocks its dependent agents no slower than the bot-start retry loop did.
_DISCOVERY_RETRY_INITIAL_DELAY_SECONDS = 5.0
_DISCOVERY_RETRY_MAX_DELAY_SECONDS = 60.0
# Bound request-local retries when concurrent credential or config publication keeps invalidating leases.
_MAX_REQUEST_STATE_RETRIES = 8


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
    version: MCPOAuthLeaseVersion
    credential_context: OAuthCredentialContext
    session_key: _MCPSessionKey


class _MCPAuthorizationChangedError(RuntimeError):
    """Signal that a requester must reacquire authoritative authorization."""


class _MCPConfigurationChangedError(RuntimeError):
    """Signal that a requester resolved against a retired config generation."""


class _MCPFunctionValidationError(MCPProtocolError):
    """Signal that one candidate catalog conflicts with the provider-visible surface."""

    def __init__(
        self,
        server_id: str,
        message: str,
        invalid_states: tuple[MCPServerState, ...],
    ) -> None:
        super().__init__(server_id, message)
        self.invalid_states = invalid_states


@dataclass(frozen=True, slots=True)
class _DiscoveryRejection:
    """One typed OAuth discovery rejection captured while state locks are held."""

    authorization_lease: _MCPAuthorizationLease
    connection_required: OAuthConnectionRequired
    cause: MCPError


@dataclass(frozen=True, slots=True)
class _CatalogRefreshOutcome:
    """Values computed under refresh locks and consumed after those locks release."""

    changed: bool
    should_notify_catalog_change: bool
    discovery_rejection: _DiscoveryRejection | None
    invalid_function_states: tuple[MCPServerState, ...] | None


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
            async with self._state_lifecycle_lock:
                if self._shutdown:
                    return set()
                desired_states = tuple(
                    (server_id, server_config, self._states.get(server_id))
                    for server_id, server_config in desired_servers.items()
                )
            await self._clear_function_validation_errors()
            changed_server_ids: set[str] = set()
            for server_id, server_config, state in desired_states:
                if state is None or state.retired:
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
                if (
                    server_config is not None
                    and state.config == server_config
                    and (
                        state.config.auth is None
                        or (
                            state.oauth_authorization is not None
                            and state.oauth_authorization.aliases == config.authorization.aliases
                        )
                    )
                ):
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
                    oauth_authorization=(
                        config.authorization.model_copy(deep=True) if provider_id is not None else None
                    ),
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
        include_tools: Collection[str] | None = None,
        exclude_tools: Collection[str] | None = None,
    ) -> ToolResult:
        """Call one remote MCP tool through the cached session."""
        state = self._require_state(server_id)
        if state.config.auth is not None:
            for _attempt in range(_MAX_REQUEST_STATE_RETRIES):
                request_state, authorization_lease = await self._request_state_and_headers(
                    server_id,
                    credentials_manager=credentials_manager,
                    worker_target=worker_target,
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
            msg = f"MCP server '{server_id}' authorization changed repeatedly during tool dispatch"
            raise MCPConnectionError(server_id, msg)

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
    ) -> MCPServerCatalog:
        """Return a requester-scoped catalog for one OAuth-backed MCP server."""
        for _attempt in range(_MAX_REQUEST_STATE_RETRIES):
            state, authorization_lease = await self._request_state_and_headers(
                server_id,
                credentials_manager=credentials_manager,
                worker_target=worker_target,
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
        msg = f"MCP server '{server_id}' authorization changed repeatedly during catalog resolution"
        raise MCPConnectionError(server_id, msg)

    def cached_request_catalog(
        self,
        server_id: str,
        *,
        worker_target: ResolvedWorkerTarget | None,
    ) -> MCPServerCatalog | None:
        """Return an already-discovered worker-scoped catalog without network or credential I/O."""
        base_state = self._states.get(server_id)
        if base_state is None or base_state.config.auth is None:
            return None
        try:
            credential_context = self._oauth_credential_context(
                base_state,
                worker_target=worker_target,
            )
            key = self._request_session_key(
                base_state,
                credential_context.worker_target,
                provider_id=credential_context.provider.id,
            )
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
            self._retired_request_keys.add(request_key)
            try:
                connection_generation = await load_oauth_reset_connection_generation(credential_context)
                if (
                    expected_connection_generation is not None
                    and connection_generation != expected_connection_generation
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
                await run_coroutine_until_complete(self._drain_retired_request_states(tuple(states)))
                yield
            finally:
                self._retired_request_keys.discard(request_key)

    def _oauth_credential_context(
        self,
        state: MCPServerState,
        *,
        worker_target: ResolvedWorkerTarget | None,
        credentials_manager: CredentialsManager | None = None,
    ) -> OAuthCredentialContext:
        return resolve_oauth_credential_context(
            mcp_oauth_provider(state.server_id, state.config),
            self.runtime_paths,
            credentials_manager or get_runtime_credentials_manager(self.runtime_paths),
            worker_target,
            authorization=state.oauth_authorization,
        )

    def _request_session_key(
        self,
        state: MCPServerState,
        worker_target: ResolvedWorkerTarget | None,
        *,
        provider_id: str,
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
            msg = f"MCP OAuth provider '{provider_id}' requires a requester identity"
            raise OAuthConnectionRequired(msg, provider_id=provider_id)
        return _MCPSessionKey(
            server_id=state.server_id,
            config_generation=state.config_generation,
            provider_id=provider_id,
            worker_scope=worker_scope,
            worker_key=worker_key,
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
            if isinstance(exc, OAuthRefreshRejectedError):
                raise oauth_connection_required(context, reason=OAUTH_REFRESH_REJECTED_REASON) from exc
            raise OAuthProviderError(
                _SANITIZED_OAUTH_REFRESH_ERROR_MESSAGE,
                oauth_error=exc.oauth_error,
            ) from None
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
    ) -> tuple[MCPServerState, _MCPAuthorizationLease]:
        for _attempt in range(_MAX_REQUEST_STATE_RETRIES):
            try:
                return await self._request_state_and_headers_once(
                    server_id,
                    credentials_manager=credentials_manager,
                    worker_target=worker_target,
                )
            except _MCPConfigurationChangedError:
                continue
        msg = f"MCP server '{server_id}' configuration changed repeatedly during request resolution"
        raise MCPConnectionError(server_id, msg)

    async def _request_state_and_headers_once(
        self,
        server_id: str,
        *,
        credentials_manager: CredentialsManager | None,
        worker_target: ResolvedWorkerTarget | None,
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
        )
        worker_target = credential_context.worker_target
        key = self._request_session_key(
            base_state,
            worker_target,
            provider_id=credential_context.provider.id,
        )
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
                    oauth_authorization=base_state.oauth_authorization,
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
                lease_version = MCPOAuthLeaseVersion(
                    token_hash=hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
                    credential_generation=credential_generation,
                )
                if state.oauth_lease_version != lease_version:
                    async with state.call_lock.write():
                        await self._disconnect_state(state)
                        state.catalog = None
                        state.last_error = None
                        state.stale = True
                        state.oauth_lease_version = lease_version
        except OAuthConnectionRequired:
            await run_coroutine_until_complete(self._disconnect_rejected_oauth_request_state(key, state))
            raise
        return state, _MCPAuthorizationLease(
            headers={"Authorization": f"Bearer {access_token}"},
            version=lease_version,
            credential_context=credential_context,
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
                    state.oauth_lease_version = None
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
        self._require_desired_oauth_lease(state, authorization_lease)
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
                rejection = await self._post_dispatch_oauth_rejection(
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
            self._require_desired_oauth_lease(state, authorization_lease)
            self._require_active_state(state)
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
            self._require_desired_oauth_lease(state, authorization_lease)
            self._require_active_state(state)
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

    async def _record_catalog_refresh_error(
        self,
        state: MCPServerState,
        authorization_lease: _MCPAuthorizationLease | None,
        error: MCPError,
    ) -> _DiscoveryRejection | None:
        """Record one refresh error and preserve a typed OAuth rejection for post-lock cleanup."""
        connection_required: OAuthConnectionRequired | None = None
        if authorization_lease is not None:
            try:
                connection_required = await self._oauth_transport_rejection(
                    state,
                    authorization_lease,
                    error,
                )
            except _MCPAuthorizationChangedError:
                await self._disconnect_state(state)
                raise
        await self._record_discovery_failure(state, error)
        if authorization_lease is None or connection_required is None:
            return None
        return _DiscoveryRejection(
            authorization_lease=authorization_lease,
            connection_required=connection_required,
            cause=error,
        )

    async def _refresh_server_catalog(
        self,
        state: MCPServerState,
        *,
        notify: bool,
        expected_refresh_revision: int | None = None,
        auth_headers: Mapping[str, str] | None = None,
        authorization_lease: _MCPAuthorizationLease | None = None,
    ) -> bool:
        self._require_desired_oauth_lease(state, authorization_lease)
        self._require_active_state(state)
        changed = False
        should_notify_catalog_change = False
        discovery_rejection: _DiscoveryRejection | None = None
        invalid_function_states: tuple[MCPServerState, ...] | None = None
        async with state.lock:
            self._require_desired_oauth_lease(state, authorization_lease)
            self._require_active_state(state)
            if expected_refresh_revision is not None and state.refresh_revision != expected_refresh_revision:
                return False
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
                except _MCPFunctionValidationError as exc:
                    await self._record_discovery_failure(state, exc)
                    invalid_function_states = tuple(
                        invalid_state for invalid_state in exc.invalid_states if invalid_state is not state
                    )
                except MCPError as exc:
                    discovery_rejection = await self._record_catalog_refresh_error(
                        state,
                        authorization_lease,
                        exc,
                    )
                    if discovery_rejection is None:
                        return False
                else:
                    state.consecutive_failures = 0
                    changed = previous_hash != catalog.catalog_hash
                    should_notify_catalog_change = notify and changed and self._on_catalog_change is not None
        outcome = _CatalogRefreshOutcome(
            changed=changed,
            should_notify_catalog_change=should_notify_catalog_change,
            discovery_rejection=discovery_rejection,
            invalid_function_states=invalid_function_states,
        )
        return await self._finish_catalog_refresh(
            state,
            outcome,
        )

    async def _finish_catalog_refresh(
        self,
        state: MCPServerState,
        outcome: _CatalogRefreshOutcome,
    ) -> bool:
        """Finish refresh cleanup and notifications after state locks are released."""
        if outcome.invalid_function_states is not None:
            await self._disconnect_function_validation_states(outcome.invalid_function_states)
            return False
        if outcome.discovery_rejection is not None:
            rejection = outcome.discovery_rejection
            await run_coroutine_until_complete(
                self._disconnect_rejected_oauth_request_state(
                    rejection.authorization_lease.session_key,
                    state,
                ),
            )
            raise rejection.connection_required from rejection.cause
        invalid_server_ids = await self._validate_global_function_names()
        if state.server_id in invalid_server_ids:
            return False
        if outcome.should_notify_catalog_change and self._on_catalog_change is not None:
            await self._on_catalog_change(state.server_id)
        if state.config.auth is None and state.stale and state.refresh_task is None and not self._shutdown:
            self._schedule_refresh_task(state)
        return outcome.changed

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
            async with self._catalog_validation_lock:
                self._require_desired_oauth_lease(state, authorization_lease)
                self._require_active_state(state)
                collision_error = self._candidate_function_validation_error(state, catalog)
                if collision_error is not None:
                    raise collision_error  # noqa: TRY301
                state.oauth_session_lease_version = (
                    authorization_lease.version if authorization_lease is not None else None
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
        repeated_error = (
            state.consecutive_failures > 0 and state.last_error is not None and str(state.last_error) == str(error)
        )
        state.connected = False
        state.catalog = None
        state.function_validation_error = isinstance(error, _MCPFunctionValidationError)
        state.consecutive_failures += 1
        state.last_error = error
        self._log_discovery_failure(state, error, repeated_error=repeated_error)
        if state.config.auth is None and not state.function_validation_error:
            self._schedule_refresh_task(
                state,
                delay_seconds=_discovery_retry_delay_seconds(state.consecutive_failures),
            )

    def _log_discovery_failure(
        self,
        state: MCPServerState,
        error: MCPError,
        *,
        repeated_error: bool,
    ) -> None:
        """Report one discovery failure without exposing credentials or transport internals."""
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

    async def _drain_retired_request_states(self, states: tuple[MCPServerState, ...]) -> None:
        """Close requester states before reset, failing closed on teardown errors."""
        first_error: BaseException | None = None
        first_error_state: MCPServerState | None = None
        for state in states:
            cleanup_failed = False
            try:
                await self._cancel_refresh_task(state)
            except (asyncio.CancelledError, Exception) as exc:
                cleanup_failed = True
                first_error = first_error or exc
                first_error_state = first_error_state or state
                logger.warning(
                    "MCP requester-state refresh cleanup failed",
                    server_id=state.server_id,
                    error_type=type(exc).__name__,
                )
            try:
                async with state.lock:
                    await self._disconnect_state_when_idle(state)
            except (asyncio.CancelledError, Exception) as exc:
                cleanup_failed = True
                first_error = first_error or exc
                first_error_state = first_error_state or state
                logger.warning(
                    "MCP requester-state session cleanup failed",
                    server_id=state.server_id,
                    error_type=type(exc).__name__,
                )
            if not cleanup_failed:
                async with self._state_lifecycle_lock:
                    self._retiring_states.pop(id(state), None)
        if first_error is not None:
            if isinstance(first_error, asyncio.CancelledError | MCPError):
                raise first_error
            assert first_error_state is not None
            msg = f"MCP requester session cleanup failed for server '{first_error_state.server_id}'"
            raise MCPConnectionError(first_error_state.server_id, msg) from first_error

    async def _clear_function_validation_errors(self) -> None:
        """Make collision-owned failures eligible for validation under the new config surface."""
        for state in (*tuple(self._states.values()), *tuple(self._scoped_states.values())):
            if not state.function_validation_error:
                continue
            async with state.lock:
                state.last_error = None
                state.function_validation_error = False
                state.consecutive_failures = 0
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
        if state.connected:
            logger.info(
                "MCP server disconnected",
                server_id=state.server_id,
                transport=state.config.transport,
            )
        state.session = None
        state.connected = False
        state.oauth_session_lease_version = None
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
            state.retired
            or state.oauth_lease_version != authorization_lease.version
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
        if authorization_lease is not None and (state.oauth_session_lease_version != authorization_lease.version):
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
        if not isinstance(access_token, str):
            raise _MCPAuthorizationChangedError
        authoritative_version = MCPOAuthLeaseVersion(
            token_hash=hashlib.sha256(access_token.encode("utf-8")).hexdigest(),
            credential_generation=snapshot.generation,
        )
        if authoritative_version != authorization_lease.version:
            raise _MCPAuthorizationChangedError

    @staticmethod
    def _require_active_state(state: MCPServerState) -> None:
        if state.retired:
            msg = f"MCP server '{state.server_id}' requester session generation is retired"
            raise MCPConnectionError(state.server_id, msg)

    def _function_surface_context(self) -> MCPFunctionSurfaceContext | None:
        """Capture the manager-owned inputs consumed by surface projection."""
        config = self._config
        if config is None:
            return None
        return MCPFunctionSurfaceContext(
            runtime_paths=self.runtime_paths,
            config=config,
            states=self._states,
            scoped_states=tuple(
                MCPScopedFunctionState(
                    requester_surface=(key.worker_scope, key.worker_key),
                    state=state,
                )
                for key, state in self._scoped_states.items()
            ),
        )

    def _function_name_collision_errors_by_state(
        self,
        *,
        candidate_state: MCPServerState | None = None,
        candidate_catalog: MCPServerCatalog | None = None,
    ) -> dict[int, tuple[MCPServerState, set[str]]]:
        """Collect every state whose provider-visible function surface conflicts."""
        context = self._function_surface_context()
        if context is None:
            return {}
        errors_by_state: dict[int, tuple[MCPServerState, set[str]]] = {}
        for report in function_collision_reports(
            context,
            candidate_state=candidate_state,
            candidate_catalog=candidate_catalog,
        ):
            for state in self._function_validation_states_for_surface(
                report.server_id,
                report.requester_surface,
            ):
                entry = errors_by_state.setdefault(id(state), (state, set()))
                entry[1].update(message for _function_name, message in report.function_name_collisions)
        return errors_by_state

    def _candidate_function_validation_error(
        self,
        state: MCPServerState,
        catalog: MCPServerCatalog,
    ) -> _MCPFunctionValidationError | None:
        """Atomically invalidate every catalog involved in a candidate collision."""
        errors_by_state = self._function_name_collision_errors_by_state(
            candidate_state=state,
            candidate_catalog=catalog,
        )
        candidate_error = errors_by_state.get(id(state))
        if candidate_error is None:
            return None
        marked_states = self._mark_function_name_collision_errors(errors_by_state)
        return _MCPFunctionValidationError(
            state.server_id,
            "\n".join(sorted(candidate_error[1])),
            marked_states,
        )

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
        base_state = self._states.get(server_id)
        if requester_surface is None:
            return (base_state,) if base_state is not None else ()
        states = [base_state] if base_state is not None and base_state.config.auth is None else []
        states.extend(
            state
            for key, state in self._scoped_states.items()
            if key.server_id == server_id and (key.worker_scope, key.worker_key) == requester_surface
        )
        return tuple(states)

    async def _disconnect_function_validation_states(self, states: tuple[MCPServerState, ...]) -> None:
        """Drain sessions whose catalogs were already hidden by validation."""
        for state in states:
            async with state.lock:
                if not state.function_validation_error:
                    continue
                error = state.last_error
                if error is not None:
                    state.consecutive_failures += 1
                    self._log_discovery_failure(state, error, repeated_error=False)
                await self._disconnect_state_when_idle(state)

    async def _validate_global_function_names(self) -> set[str]:
        async with self._catalog_validation_lock:
            errors_by_state = self._function_name_collision_errors_by_state()
            if not errors_by_state:
                return set()
            marked_states = self._mark_function_name_collision_errors(errors_by_state)
        await self._disconnect_function_validation_states(marked_states)
        return {state.server_id for state in marked_states}

    def mcp_tool_unavailable_messages_for_loaded_tools(
        self,
        agent_name: str,
        loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str],
    ) -> list[str]:
        """Return unavailable non-OAuth MCP server messages for a candidate loaded dynamic-tool state."""
        context = self._function_surface_context()
        if context is None:
            return []
        return mcp_tool_unavailable_messages(context, agent_name, loaded_tools)

    def function_name_collision_messages_for_loaded_tools(
        self,
        agent_name: str,
        loaded_tools: list[str] | tuple[str, ...] | set[str] | frozenset[str],
        *,
        worker_target: ResolvedWorkerTarget | None = None,
    ) -> list[str]:
        """Return collision messages for a candidate loaded dynamic-tool state."""
        context = self._function_surface_context()
        if context is None:
            return []
        requester_surfaces: set[tuple[str, str]] = set()
        if worker_target is not None:
            # Generated MCP OAuth providers are requester-scoped, so every
            # configured agent worker scope canonicalizes to one user surface.
            for server_id in sorted(self._states):
                state = self._states[server_id]
                if state.config.auth is None:
                    continue
                canonical_target = self._oauth_credential_context(
                    state,
                    worker_target=worker_target,
                ).worker_target
                if (
                    canonical_target is not None
                    and canonical_target.worker_scope is not None
                    and canonical_target.worker_key is not None
                ):
                    requester_surfaces.add((canonical_target.worker_scope, canonical_target.worker_key))
        return function_collision_messages(
            context,
            agent_name,
            loaded_tools,
            requester_surfaces=requester_surfaces,
        )

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

    async def _post_dispatch_oauth_rejection(
        self,
        state: MCPServerState,
        authorization_lease: _MCPAuthorizationLease,
        dispatch_error: MCPConnectionError | MCPTimeoutError,
    ) -> OAuthConnectionRequired | None:
        """Classify bearer rejection without replaying after authorization drift."""
        try:
            return await self._oauth_transport_rejection(
                state,
                authorization_lease,
                dispatch_error,
            )
        except _MCPAuthorizationChangedError as exc:
            msg = f"MCP server '{state.server_id}' authorization changed after remote dispatch; retry manually"
            raise MCPConnectionError(state.server_id, msg) from exc

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
