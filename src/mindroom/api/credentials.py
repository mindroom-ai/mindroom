"""Unified credentials management API."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from mindroom.agent_policy import (
    ResolvedAgentPolicy,
    dashboard_credentials_supported_for_scope,
    resolve_agent_policy_from_data,
)
from mindroom.api import config_lifecycle
from mindroom.api.auth import request_uses_trusted_upstream_auth, trusted_upstream_matrix_user_id_for_request
from mindroom.config.main import Config
from mindroom.credential_policy import (
    OAUTH_CREDENTIAL_FIELDS,
    credential_service_policy,
    dashboard_may_edit_oauth_service,
    filter_oauth_credential_fields,
    looks_like_oauth_credentials,
)
from mindroom.credentials import (
    CredentialsManager,
    delete_scoped_credentials,
    get_runtime_credentials_manager,
    list_worker_grantable_shared_services,
    load_scoped_credentials,
    load_worker_grantable_shared_credentials,
    save_scoped_credentials,
    validate_service_name,
)
from mindroom.matrix.identity import MatrixID
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.registry import load_oauth_providers_for_snapshot
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    require_worker_key_for_scope,
    resolve_worker_target,
    unsupported_shared_only_integration_message,
    unsupported_shared_only_integration_names,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.oauth.providers import OAuthProvider
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget

router = APIRouter(prefix="/api/credentials", tags=["credentials"])
_OWNER_MATRIX_USER_ID_ENV = "MINDROOM_OWNER_USER_ID"
_PENDING_OAUTH_STATE_TTL_SECONDS = 600
_OAUTH_TOKEN_CREDENTIALS_ERROR = "OAuth token credentials must be managed through the OAuth connect flow."  # noqa: S105
_PENDING_OAUTH_STATE_KIND = "dashboard_oauth_state"


@dataclass(frozen=True)
class _PendingOAuthState:
    """Pending OAuth connect request bound to one authenticated dashboard user."""

    service: str
    user_id: str
    agent_name: str | None
    execution_scope_override_provided: bool
    execution_scope_override: WorkerScope | None
    payload: dict[str, str] | None
    created_at: float


def _filter_internal_keys(credentials: dict[str, Any]) -> dict[str, Any]:
    """Remove internal metadata keys (prefixed with _) from credentials."""
    return {k: v for k, v in credentials.items() if not k.startswith("_")}


def _filter_credentials_for_response(credentials: dict[str, Any], *, is_oauth_service: bool) -> dict[str, Any]:
    """Return credentials safe for dashboard config responses."""
    filtered = _filter_internal_keys(credentials)
    if not is_oauth_service and not looks_like_oauth_credentials(credentials):
        return filtered
    return filter_oauth_credential_fields(credentials)


def _validated_service(service: str) -> str:
    try:
        return validate_service_name(service)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@dataclass(frozen=True)
class RequestCredentialsTarget:
    """Resolved credential target for one dashboard/API request."""

    runtime_paths: RuntimePaths
    base_manager: CredentialsManager
    target_manager: CredentialsManager
    worker_scope: WorkerScope | None
    agent_name: str | None
    execution_identity: ToolExecutionIdentity | None
    allowed_shared_services: frozenset[str] | None = None


@dataclass(frozen=True)
class DashboardAgentExecutionScopeResolution:
    """Resolved dashboard scope request for one agent selection."""

    agent_name: str | None
    persisted_policy: ResolvedAgentPolicy | None
    persisted_execution_scope: WorkerScope | None
    requested_execution_scope: WorkerScope | None
    execution_scope_override_provided: bool
    draft_scope_preview: bool


@dataclass(frozen=True)
class OAuthCredentialServiceMatch:
    """OAuth provider service role for one credentials API service name."""

    provider: OAuthProvider
    token_service: bool
    tool_config_service: bool


@dataclass(frozen=True)
class OAuthCredentialServices:
    """Classify dashboard credential services registered by OAuth providers."""

    providers: dict[str, OAuthProvider]

    def match(self, service: str) -> OAuthCredentialServiceMatch | None:
        """Return the OAuth role for one credential service, if registered."""
        for provider in self.providers.values():
            token_service = provider.credential_service == service
            tool_config_service = provider.tool_config_service == service
            if token_service or tool_config_service:
                return OAuthCredentialServiceMatch(
                    provider=provider,
                    token_service=token_service,
                    tool_config_service=tool_config_service,
                )
        return None

    def reject_non_editable_services(self, services: tuple[str, ...]) -> None:
        """Reject direct dashboard access to non-editable OAuth credential services."""
        for service in services:
            _reject_oauth_token_service(self.match(service))

    def allows_private_scope_for(self, service: str) -> bool:
        """Return whether this OAuth tool config service can target a private scope."""
        match = self.match(service)
        return _dashboard_may_edit_oauth_match(match)

    def dashboard_may_show_service(self, service: str) -> bool:
        """Return whether a service may appear in dashboard credential listings."""
        match = self.match(service)
        return match is None or _dashboard_may_edit_oauth_match(match)


def _request_auth_user(request: Request) -> dict[str, Any] | None:
    auth_user = request.scope.get("auth_user")
    return auth_user if isinstance(auth_user, dict) else None


def _require_auth_user_id(request: Request) -> str:
    auth_user = _request_auth_user(request) or {}
    user_id = auth_user.get("user_id")
    if isinstance(user_id, str) and user_id:
        return user_id
    raise HTTPException(status_code=401, detail="Missing or invalid credentials")


def dashboard_requester_id_for_request(request: Request, runtime_paths: RuntimePaths) -> str | None:
    """Return the requester identity dashboard-scoped worker credentials should use."""
    trusted_matrix_user_id = trusted_upstream_matrix_user_id_for_request(request)
    if trusted_matrix_user_id:
        return trusted_matrix_user_id
    if request_uses_trusted_upstream_auth(request):
        return None
    owner_user_id = runtime_paths.env_value(_OWNER_MATRIX_USER_ID_ENV)
    if owner_user_id:
        return owner_user_id
    auth_user = _request_auth_user(request) or {}
    user_id = auth_user.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


def _reject_unbound_private_dashboard_requester(
    execution_scope: WorkerScope,
    execution_identity: ToolExecutionIdentity,
) -> None:
    if execution_scope not in {"user", "user_agent"}:
        return
    if execution_identity.requester_id is not None:
        try:
            MatrixID.parse(execution_identity.requester_id)
        except ValueError:
            pass
        else:
            return
    raise HTTPException(
        status_code=400,
        detail=(
            "Dashboard credential management for private user scopes requires a Matrix requester identity. "
            "Set MINDROOM_OWNER_USER_ID to your Matrix user ID, or run MindRoom under Matrix authentication."
        ),
    )


def issue_pending_oauth_state(
    request: Request,
    service: str,
    agent_name: str | None = None,
    *,
    payload: dict[str, str] | None = None,
) -> str:
    """Create an opaque OAuth state bound to the current user and target."""
    user_id = _require_auth_user_id(request)
    execution_scope_override_provided, execution_scope_override = resolve_dashboard_execution_scope_override(request)
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    return issue_opaque_oauth_state(
        runtime_paths,
        kind=_PENDING_OAUTH_STATE_KIND,
        ttl_seconds=_PENDING_OAUTH_STATE_TTL_SECONDS,
        data={
            "service": service,
            "user_id": user_id,
            "agent_name": agent_name or "",
            "execution_scope_override_provided": execution_scope_override_provided,
            "execution_scope_override": execution_scope_override or "",
            "payload": payload or {},
        },
    )


def _consume_pending_oauth_request(request: Request, service: str, state: str) -> _PendingOAuthState:
    """Consume and validate a previously issued dashboard OAuth state token."""
    user_id = _require_auth_user_id(request)
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    try:
        data = read_opaque_oauth_state(runtime_paths, kind=_PENDING_OAUTH_STATE_KIND, token=state)
    except OAuthProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if data.get("service") != service:
        raise HTTPException(status_code=400, detail="OAuth state does not match this integration")
    if data.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="OAuth state does not belong to the current user")
    try:
        consume_opaque_oauth_state(runtime_paths, kind=_PENDING_OAUTH_STATE_KIND, token=state)
    except OAuthProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    execution_scope_raw = data.get("execution_scope_override")
    execution_scope_override = execution_scope_raw if execution_scope_raw in {"shared", "user", "user_agent"} else None
    payload = data.get("payload")
    return _PendingOAuthState(
        service=service,
        user_id=user_id,
        agent_name=data.get("agent_name") or None,
        execution_scope_override_provided=data.get("execution_scope_override_provided") is True,
        execution_scope_override=cast("WorkerScope | None", execution_scope_override),
        payload=payload if isinstance(payload, dict) else None,
        created_at=0,
    )


def consume_pending_oauth_request(
    request: Request,
    service: str,
    state: str,
) -> _PendingOAuthState:
    """Return the validated pending OAuth request for a callback."""
    return _consume_pending_oauth_request(request, service, state)


def build_dashboard_execution_identity(
    request: Request,
    agent_name: str,
    *,
    runtime_paths: RuntimePaths,
) -> ToolExecutionIdentity:
    """Build one dashboard-scoped execution identity for API credential and tool lookups.

    This is a boundary helper for dashboard/API requests only.
    It uses the authenticated dashboard user as the requester, not any Matrix sender,
    and it exists solely so dashboard previews hit the same scoped-runtime seams as
    live requests once an execution scope is chosen.
    """
    tenant_id = runtime_paths.env_value("CUSTOMER_ID")
    account_id = runtime_paths.env_value("ACCOUNT_ID")
    return ToolExecutionIdentity(
        channel="matrix",
        agent_name=agent_name,
        requester_id=dashboard_requester_id_for_request(request, runtime_paths),
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=tenant_id,
        account_id=account_id,
    )


def _dashboard_scope_label(
    *,
    config_labeled_scope: str,
    execution_scope: WorkerScope | None,
    execution_scope_override_provided: bool,
) -> str:
    """Return the user-facing scope label for one dashboard request."""
    if execution_scope_override_provided:
        if execution_scope is None:
            return "execution_scope=unscoped"
        return f"execution_scope={execution_scope}"
    return config_labeled_scope


def resolve_dashboard_execution_scope_override(
    request: Request,
) -> tuple[bool, WorkerScope | None]:
    """Return the explicit dashboard execution-scope override, if one was provided."""
    raw_execution_scope = request.query_params.get("execution_scope")
    if raw_execution_scope is None or raw_execution_scope == "":
        return False, None
    if raw_execution_scope == "unscoped":
        return True, None
    if raw_execution_scope in {"shared", "user", "user_agent"}:
        return True, cast("WorkerScope", raw_execution_scope)
    raise HTTPException(
        status_code=400,
        detail=("Query parameter 'execution_scope' must be one of 'shared', 'user', 'user_agent', or 'unscoped'."),
    )


def resolve_dashboard_agent_execution_scope_request(
    *,
    config: Config,
    agent_name: str | None,
    execution_scope_override_provided: bool,
    execution_scope_override: WorkerScope | None,
    allow_draft_override: bool,
) -> DashboardAgentExecutionScopeResolution:
    """Resolve one dashboard execution-scope request against persisted agent config.

    Tools may preview draft execution scopes, but persistent credential writes must
    stay bound to the saved config. This helper keeps that policy in one place.
    """
    if agent_name is None:
        if execution_scope_override_provided:
            raise HTTPException(
                status_code=400,
                detail="Query parameter 'execution_scope' requires agent_name on the dashboard API.",
            )
        return DashboardAgentExecutionScopeResolution(
            agent_name=None,
            persisted_policy=None,
            persisted_execution_scope=None,
            requested_execution_scope=None,
            execution_scope_override_provided=False,
            draft_scope_preview=False,
        )

    if agent_name not in config.agents:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent_name}")

    persisted_policy = resolve_agent_policy_from_data(
        agent_name,
        config.agents[agent_name],
        default_worker_scope=config.defaults.worker_scope,
        private_knowledge_base_id_prefix=config.PRIVATE_KNOWLEDGE_BASE_ID_PREFIX,
    )
    persisted_execution_scope = persisted_policy.effective_execution_scope
    requested_execution_scope = (
        execution_scope_override if execution_scope_override_provided else persisted_execution_scope
    )
    draft_scope_preview = execution_scope_override_provided and requested_execution_scope != persisted_execution_scope
    if draft_scope_preview and not allow_draft_override:
        requested_scope_label = _dashboard_scope_label(
            config_labeled_scope=persisted_policy.scope_label,
            execution_scope=requested_execution_scope,
            execution_scope_override_provided=True,
        )
        persisted_scope_label = persisted_policy.scope_label
        raise HTTPException(
            status_code=409,
            detail=(
                f"Save the configuration before managing credentials for agent '{agent_name}' with "
                f"{requested_scope_label}. Persisted scope is {persisted_scope_label}."
            ),
        )
    return DashboardAgentExecutionScopeResolution(
        agent_name=agent_name,
        persisted_policy=persisted_policy,
        persisted_execution_scope=persisted_execution_scope,
        requested_execution_scope=requested_execution_scope,
        execution_scope_override_provided=execution_scope_override_provided,
        draft_scope_preview=draft_scope_preview,
    )


def _reject_raw_worker_targeting(request: Request) -> None:
    for param_name in ("worker_key", "source_worker_key"):
        if request.query_params.get(param_name):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Query parameter '{param_name}' is not supported on the dashboard credentials API. "
                    "Use agent_name to resolve the scoped worker target."
                ),
            )


def resolve_request_credentials_target(
    request: Request,
    *,
    agent_name: str | None = None,
    credentials_manager: CredentialsManager | None = None,
    service_names: tuple[str, ...] = (),
    execution_scope_override_provided: bool | None = None,
    execution_scope_override: WorkerScope | None = None,
    allow_private_scopes: bool = False,
) -> RequestCredentialsTarget:
    """Resolve the credential storage target for one authenticated dashboard request."""
    _reject_raw_worker_targeting(request)
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths

    base_manager = credentials_manager or get_runtime_credentials_manager(runtime_paths)
    if execution_scope_override_provided is None:
        execution_scope_override_provided, execution_scope_override = resolve_dashboard_execution_scope_override(
            request,
        )

    # Plain dashboard credential reads/writes with no agent selection remain global and
    # must not start depending on a persisted config file.
    if agent_name is None and not execution_scope_override_provided:
        return RequestCredentialsTarget(
            runtime_paths=runtime_paths,
            base_manager=base_manager,
            target_manager=base_manager,
            worker_scope=None,
            agent_name=None,
            execution_identity=None,
            allowed_shared_services=None,
        )

    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    scope_request = resolve_dashboard_agent_execution_scope_request(
        config=config,
        agent_name=agent_name,
        execution_scope_override_provided=execution_scope_override_provided,
        execution_scope_override=execution_scope_override,
        allow_draft_override=False,
    )
    if scope_request.agent_name is None:
        return RequestCredentialsTarget(
            runtime_paths=runtime_paths,
            base_manager=base_manager,
            target_manager=base_manager,
            worker_scope=None,
            agent_name=None,
            execution_identity=None,
            allowed_shared_services=None,
        )
    execution_scope = scope_request.requested_execution_scope
    if execution_scope is None:
        return RequestCredentialsTarget(
            runtime_paths=runtime_paths,
            base_manager=base_manager,
            target_manager=base_manager,
            worker_scope=None,
            agent_name=scope_request.agent_name,
            execution_identity=None,
            allowed_shared_services=None,
        )

    scope_label = _dashboard_scope_label(
        config_labeled_scope=(
            scope_request.persisted_policy.scope_label if scope_request.persisted_policy is not None else "unscoped"
        ),
        execution_scope=execution_scope,
        execution_scope_override_provided=execution_scope_override_provided,
    )
    if not allow_private_scopes and not dashboard_credentials_supported_for_scope(execution_scope):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Dashboard credential management does not support {scope_label} "
                f"for agent '{scope_request.agent_name}'."
            ),
        )

    unsupported_services = unsupported_shared_only_integration_names(list(service_names), execution_scope)
    if unsupported_services:
        raise HTTPException(
            status_code=400,
            detail=unsupported_shared_only_integration_message(
                unsupported_services[0],
                execution_scope,
                agent_name=scope_request.agent_name,
                scope_label=scope_label,
            ),
        )

    execution_identity = build_dashboard_execution_identity(
        request,
        scope_request.agent_name,
        runtime_paths=runtime_paths,
    )
    _reject_unbound_private_dashboard_requester(execution_scope, execution_identity)
    worker_key = require_worker_key_for_scope(
        execution_scope,
        execution_identity=execution_identity,
        agent_name=scope_request.agent_name,
        failure_message=f"Could not resolve worker credentials for agent '{scope_request.agent_name}'.",
    )
    return RequestCredentialsTarget(
        runtime_paths=runtime_paths,
        base_manager=base_manager,
        target_manager=base_manager.for_worker(worker_key),
        worker_scope=execution_scope,
        agent_name=scope_request.agent_name,
        execution_identity=execution_identity,
        allowed_shared_services=config.get_worker_grantable_credentials(),
    )


def load_credentials_for_target(service: str, target: RequestCredentialsTarget) -> dict[str, Any] | None:
    """Load credentials for the resolved target, including scoped overlays when needed."""
    if target.worker_scope is None:
        return target.target_manager.load_credentials(service)
    if _service_uses_primary_runtime_store(service, target):
        return load_scoped_credentials(
            service,
            credentials_manager=target.base_manager,
            worker_target=_worker_target_for_credentials_target(target),
            allowed_shared_services=target.allowed_shared_services,
        )

    shared_manager = target.base_manager.shared_manager()
    shared_credentials = load_worker_grantable_shared_credentials(
        service,
        shared_manager=shared_manager,
        allowed_services=target.allowed_shared_services or frozenset(),
    )
    worker_credentials = target.target_manager.load_credentials(service)
    if not shared_credentials and not isinstance(worker_credentials, dict):
        return None
    merged_credentials = dict(shared_credentials or {})
    if isinstance(worker_credentials, dict):
        merged_credentials.update(worker_credentials)
    return merged_credentials or None


def _service_uses_primary_runtime_store(service: str, target: RequestCredentialsTarget) -> bool:
    policy = credential_service_policy(service, target.worker_scope)
    return policy.uses_primary_runtime_scoped_credentials or policy.uses_local_shared_credentials


def _worker_target_for_credentials_target(target: RequestCredentialsTarget) -> ResolvedWorkerTarget | None:
    if target.worker_scope is None:
        return None
    return resolve_worker_target(
        target.worker_scope,
        target.agent_name,
        execution_identity=target.execution_identity,
    )


def _save_credentials_for_target(service: str, credentials: dict[str, Any], target: RequestCredentialsTarget) -> None:
    if target.worker_scope is None or not _service_uses_primary_runtime_store(service, target):
        target.target_manager.save_credentials(service, credentials)
        return
    save_scoped_credentials(
        service,
        credentials,
        credentials_manager=target.base_manager,
        worker_target=_worker_target_for_credentials_target(target),
    )


def _primary_runtime_scoped_services_for_target(target: RequestCredentialsTarget) -> set[str]:
    if target.worker_scope not in {"user", "user_agent"}:
        return set()
    if target.execution_identity is None or target.execution_identity.requester_id is None:
        return set()
    agent_name = target.agent_name if target.worker_scope == "user_agent" else None
    scoped_manager = target.base_manager.for_primary_runtime_scope(
        target.execution_identity.requester_id,
        agent_name,
    )
    return {
        service
        for service in scoped_manager.list_services()
        if credential_service_policy(service, target.worker_scope).uses_primary_runtime_scoped_credentials
    }


def _delete_credentials_for_target(service: str, target: RequestCredentialsTarget) -> None:
    if target.worker_scope is None or not _service_uses_primary_runtime_store(service, target):
        target.target_manager.delete_credentials(service)
        return
    delete_scoped_credentials(
        service,
        credentials_manager=target.base_manager,
        worker_target=_worker_target_for_credentials_target(target),
    )


def _request_may_target_scoped_credentials(request: Request, agent_name: str | None) -> bool:
    return agent_name is not None or bool(request.query_params.get("execution_scope"))


def _oauth_providers_for_request(request: Request) -> dict[str, OAuthProvider]:
    snapshot = config_lifecycle.bind_current_request_snapshot(request)
    if snapshot.runtime_config is None and not snapshot.config_data:
        snapshot.runtime_config = Config.model_validate({})
    return load_oauth_providers_for_snapshot(snapshot)


def _oauth_services_for_request(request: Request) -> OAuthCredentialServices:
    return OAuthCredentialServices(providers=_oauth_providers_for_request(request))


def _oauth_service_match(request: Request, service: str) -> OAuthCredentialServiceMatch | None:
    return _oauth_services_for_request(request).match(service)


def _reject_oauth_token_service(
    oauth_service_match: OAuthCredentialServiceMatch | None,
) -> None:
    if oauth_service_match is None or _dashboard_may_edit_oauth_match(oauth_service_match):
        return
    raise HTTPException(status_code=400, detail=_OAUTH_TOKEN_CREDENTIALS_ERROR)


def _dashboard_may_edit_oauth_match(oauth_service_match: OAuthCredentialServiceMatch | None) -> bool:
    if oauth_service_match is None:
        return False
    return dashboard_may_edit_oauth_service(
        token_service=oauth_service_match.token_service,
        tool_config_service=oauth_service_match.tool_config_service,
    )


def _reject_oauth_credentials_document(credentials: dict[str, Any]) -> None:
    if not looks_like_oauth_credentials(credentials):
        return
    raise HTTPException(status_code=400, detail=_OAUTH_TOKEN_CREDENTIALS_ERROR)


def _reject_oauth_api_key_field(
    oauth_service_match: OAuthCredentialServiceMatch | None,
    *,
    key_name: str,
) -> None:
    if not _dashboard_may_edit_oauth_match(oauth_service_match):
        return
    if key_name not in OAUTH_CREDENTIAL_FIELDS:
        return
    raise HTTPException(
        status_code=400,
        detail=f"OAuth field '{key_name}' must be managed through the OAuth connect flow.",
    )


def _dashboard_credentials_for_save(
    config_values: dict[str, Any],
    *,
    strip_oauth_fields: bool,
) -> dict[str, Any]:
    credentials = dict(config_values)
    if strip_oauth_fields:
        credentials = filter_oauth_credential_fields(credentials)
    credentials["_source"] = "ui"
    return credentials


@dataclass(frozen=True)
class DashboardCredentialAccess:
    """Credential storage access for one dashboard request target."""

    target: RequestCredentialsTarget
    oauth_services: OAuthCredentialServices

    @classmethod
    def resolve(
        cls,
        request: Request,
        *,
        agent_name: str | None,
        service_names: tuple[str, ...] = (),
        allow_private_scopes: bool = False,
    ) -> DashboardCredentialAccess:
        """Resolve dashboard credential access for one request."""
        oauth_services = _oauth_services_for_request(request)
        oauth_services.reject_non_editable_services(service_names)
        allow_oauth_private_scopes = any(
            oauth_services.allows_private_scope_for(service) for service in service_names
        ) and _request_may_target_scoped_credentials(request, agent_name)
        target = resolve_request_credentials_target(
            request,
            agent_name=agent_name,
            service_names=service_names,
            allow_private_scopes=allow_private_scopes or allow_oauth_private_scopes,
        )
        return cls(target=target, oauth_services=oauth_services)

    def match(self, service: str) -> OAuthCredentialServiceMatch | None:
        """Return the OAuth role for one credential service, if registered."""
        return self.oauth_services.match(service)

    def reject_token_service(self, service: str) -> None:
        """Reject direct dashboard access to OAuth token credentials."""
        _reject_oauth_token_service(self.match(service))

    def reject_stored_oauth_credentials(self, credentials: dict[str, Any]) -> None:
        """Reject stored OAuth token documents returned through generic routes."""
        _reject_oauth_credentials_document(credentials)

    def load(self, service: str) -> dict[str, Any] | None:
        """Load dashboard-visible credentials for one service."""
        self.reject_token_service(service)
        return load_credentials_for_target(service, self.target)

    def save(self, service: str, credentials: dict[str, Any]) -> None:
        """Save dashboard-visible credentials for one service."""
        self.reject_token_service(service)
        _save_credentials_for_target(service, credentials, self.target)

    def delete(self, service: str) -> None:
        """Delete dashboard-visible credentials for one service."""
        self.reject_token_service(service)
        _delete_credentials_for_target(service, self.target)

    def response_credentials(self, service: str, credentials: dict[str, Any]) -> dict[str, Any]:
        """Return credentials filtered for dashboard responses."""
        return _filter_credentials_for_response(
            credentials,
            is_oauth_service=_dashboard_may_edit_oauth_match(self.match(service)),
        )

    def credentials_for_save(self, service: str, config_values: dict[str, Any]) -> dict[str, Any]:
        """Return user-submitted credentials normalized for storage."""
        return _dashboard_credentials_for_save(
            config_values,
            strip_oauth_fields=_dashboard_may_edit_oauth_match(self.match(service)),
        )

    def list_services(self) -> list[str]:
        """List dashboard-visible services for the resolved target."""
        if self.target.worker_scope is None:
            return [
                service
                for service in self.target.target_manager.list_services()
                if self.oauth_services.dashboard_may_show_service(service)
            ]
        worker_services = set(self.target.target_manager.list_services())
        primary_runtime_services = _primary_runtime_scoped_services_for_target(self.target)
        shared_manager = self.target.base_manager.shared_manager()
        shared_services = set(
            list_worker_grantable_shared_services(
                shared_manager=shared_manager,
                allowed_services=self.target.allowed_shared_services or frozenset(),
            ),
        )
        if self.target.worker_scope == "shared":
            shared_services |= {
                service
                for service in shared_manager.list_services()
                if credential_service_policy(service, self.target.worker_scope).uses_local_shared_credentials
            }
        services = worker_services | primary_runtime_services | shared_services
        services -= set(unsupported_shared_only_integration_names(sorted(services), self.target.worker_scope))
        return sorted(service for service in services if self.oauth_services.dashboard_may_show_service(service))


class SetApiKeyRequest(BaseModel):
    """Request to set an API key."""

    service: str
    api_key: str
    key_name: str = "api_key"


class CredentialStatus(BaseModel):
    """Status of a service's credentials."""

    service: str
    has_credentials: bool
    key_names: list[str] | None = None


class SetCredentialsRequest(BaseModel):
    """Request to set multiple credentials for a service."""

    credentials: dict[str, Any]  # Can be strings, booleans, numbers, etc.


@router.get("/list")
async def list_services(
    request: Request,
    agent_name: str | None = None,
) -> list[str]:
    """List all services with stored credentials."""
    access = DashboardCredentialAccess.resolve(request, agent_name=agent_name, allow_private_scopes=True)
    return access.list_services()


@router.get("/{service}/status")
async def get_credential_status(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> CredentialStatus:
    """Get the status of credentials for a service."""
    service = _validated_service(service)
    access = DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service,),
    )
    credentials = access.load(service)

    if credentials:
        access.reject_stored_oauth_credentials(credentials)
        filtered = access.response_credentials(service, credentials)
        return CredentialStatus(
            service=service,
            has_credentials=True,
            key_names=list(filtered.keys()) if filtered else None,
        )

    return CredentialStatus(service=service, has_credentials=False)


@router.post("/{service}")
async def set_credentials(
    service: str,
    http_request: Request,
    payload: SetCredentialsRequest,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Set multiple credentials for a service."""
    service = _validated_service(service)
    _reject_oauth_credentials_document(payload.credentials)
    access = DashboardCredentialAccess.resolve(
        http_request,
        agent_name=agent_name,
        service_names=(service,),
    )
    existing_credentials = access.load(service)
    if existing_credentials:
        access.reject_stored_oauth_credentials(existing_credentials)

    creds = access.credentials_for_save(service, payload.credentials)
    access.save(service, creds)

    return {"status": "success", "message": f"Credentials saved for {service}"}


@router.post("/{service}/api-key")
async def set_api_key(
    service: str,
    http_request: Request,
    payload: SetApiKeyRequest,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Set an API key for a service."""
    service = _validated_service(service)
    request_service = _validated_service(payload.service)
    if request_service != service:
        raise HTTPException(status_code=400, detail="Service mismatch in request")
    oauth_service_match = _oauth_service_match(http_request, service)
    _reject_oauth_token_service(oauth_service_match)
    _reject_oauth_api_key_field(oauth_service_match, key_name=payload.key_name)

    target = resolve_request_credentials_target(http_request, agent_name=agent_name, service_names=(service,))
    credentials = load_credentials_for_target(service, target) or {}
    _reject_oauth_credentials_document(credentials)
    credentials[payload.key_name] = payload.api_key
    credentials["_source"] = "ui"
    _save_credentials_for_target(service, credentials, target)

    return {"status": "success", "message": f"API key set for {service}"}


@router.get("/{service}/api-key")
async def get_api_key(
    service: str,
    request: Request,
    key_name: str = "api_key",
    include_value: bool = False,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Get API key metadata for a service, and optionally the full key value."""
    service = _validated_service(service)
    oauth_service_match = _oauth_service_match(request, service)
    _reject_oauth_token_service(oauth_service_match)
    _reject_oauth_api_key_field(oauth_service_match, key_name=key_name)
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=(service,))
    credentials = load_credentials_for_target(service, target) or {}
    _reject_oauth_credentials_document(credentials)
    api_key = credentials.get(key_name)

    if api_key:
        source = credentials.get("_source")
        response = {
            "service": service,
            "has_key": True,
            "key_name": key_name,
            # Return masked version
            "masked_key": f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else "****",
            "source": source,
        }
        if include_value:
            response["api_key"] = api_key
        return response

    return {"service": service, "has_key": False, "key_name": key_name}


@router.get("/{service}")
async def get_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Get credentials for a service (for editing)."""
    service = _validated_service(service)
    access = DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service,),
    )
    credentials = access.load(service)

    if not credentials:
        return {"service": service, "credentials": {}}
    access.reject_stored_oauth_credentials(credentials)

    return {
        "service": service,
        "credentials": access.response_credentials(service, credentials),
    }


@router.delete("/{service}")
async def delete_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Delete all credentials for a service."""
    service = _validated_service(service)
    access = DashboardCredentialAccess.resolve(
        request,
        agent_name=agent_name,
        service_names=(service,),
    )
    existing_credentials = access.load(service)
    if existing_credentials:
        access.reject_stored_oauth_credentials(existing_credentials)
    access.delete(service)

    return {"status": "success", "message": f"Credentials deleted for {service}"}


@router.post("/{service}/copy-from/{source_service}")
async def copy_credentials(
    service: str,
    source_service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, str]:
    """Copy credentials from one service to another."""
    service = _validated_service(service)
    source_service = _validated_service(source_service)
    _reject_oauth_token_service(_oauth_service_match(request, service))
    _reject_oauth_token_service(_oauth_service_match(request, source_service))
    target = resolve_request_credentials_target(
        request,
        agent_name=agent_name,
        service_names=(service, source_service),
    )
    source_creds = load_credentials_for_target(source_service, target)
    destination_creds = load_credentials_for_target(service, target)

    if not source_creds:
        raise HTTPException(status_code=404, detail=f"No credentials found for {source_service}")
    _reject_oauth_credentials_document(source_creds)
    if destination_creds:
        _reject_oauth_credentials_document(destination_creds)

    # Copy credentials, marking as UI-sourced
    target_creds = {k: v for k, v in source_creds.items() if not k.startswith("_")}
    target_creds["_source"] = "ui"
    _save_credentials_for_target(service, target_creds, target)

    return {"status": "success", "message": f"Credentials copied from {source_service} to {service}"}


@router.post("/{service}/test")
async def validate_credentials(
    service: str,
    request: Request,
    agent_name: str | None = None,
) -> dict[str, Any]:
    """Test if credentials are valid for a service."""
    service = _validated_service(service)
    _reject_oauth_token_service(_oauth_service_match(request, service))
    # This is a placeholder - actual testing would depend on the service
    target = resolve_request_credentials_target(request, agent_name=agent_name, service_names=(service,))
    credentials = load_credentials_for_target(service, target)

    if not credentials:
        raise HTTPException(status_code=404, detail=f"No credentials found for {service}")
    _reject_oauth_credentials_document(credentials)

    # For now, just check if credentials exist
    # In the future, we could implement actual validation per service
    return {
        "service": service,
        "status": "success",
        "message": "Credentials exist (validation not implemented)",
    }
