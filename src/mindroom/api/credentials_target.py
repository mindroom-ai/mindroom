"""Credential storage target resolution and store routing for dashboard requests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from mindroom.agent_policy import dashboard_credentials_supported_for_scope
from mindroom.api import config_lifecycle
from mindroom.api.dashboard_credential_scope import (
    build_dashboard_execution_identity,
    dashboard_scope_label,
    reject_unbound_private_dashboard_requester,
    require_agent_credential_management_authorized,
    require_agent_oauth_connection_authorized,
    require_platform_administrator_authorized,
    resolve_dashboard_agent_execution_scope_request,
    resolve_dashboard_execution_scope_override,
)
from mindroom.credentials.manager import (
    CredentialsManager,
    credential_layers,
    delete_scoped_credentials,
    get_runtime_credentials_manager,
    load_scoped_credentials,
    save_scoped_credentials,
)
from mindroom.credentials.policy import is_global_credential_service
from mindroom.tool_system.catalog import ensure_tool_registry_loaded
from mindroom.tool_system.sandbox_proxy import primary_owns_tool_settings
from mindroom.tool_system.worker_routing import (
    ToolExecutionIdentity,
    WorkerScope,
    require_worker_key_for_scope,
    resolve_worker_target,
    unsupported_shared_only_integration_message,
    unsupported_shared_only_integration_names,
)

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


@dataclass(frozen=True)
class RequestCredentialsTarget:
    """Resolved credential target for one dashboard/API request."""

    runtime_paths: RuntimePaths
    base_manager: CredentialsManager
    worker_scope: WorkerScope | None
    agent_name: str | None
    execution_identity: ToolExecutionIdentity | None
    allowed_shared_services: frozenset[str] | None = None


def loaded_runtime_config_for_credentials_request(
    request: Request,
) -> tuple[Config, RuntimePaths] | None:
    """Return the loaded runtime config without making global edits depend on it."""
    try:
        return config_lifecycle.read_committed_runtime_config(request)
    except HTTPException:
        return None


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


def request_may_target_scoped_credentials(request: Request, agent_name: str | None) -> bool:
    """Return whether one request can resolve to a scoped credential target."""
    return agent_name is not None or bool(request.query_params.get("execution_scope"))


def resolve_request_credentials_target(
    request: Request,
    *,
    agent_name: str | None = None,
    service_names: tuple[str, ...] = (),
    execution_scope_override_provided: bool | None = None,
    execution_scope_override: WorkerScope | None = None,
    allow_private_scopes: bool = False,
) -> RequestCredentialsTarget:
    """Resolve the credential storage target for one authenticated dashboard request."""
    _reject_raw_worker_targeting(request)
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths

    base_manager = get_runtime_credentials_manager(runtime_paths)

    def primary_target(target_agent_name: str | None) -> RequestCredentialsTarget:
        """Target the primary runtime's own credential store, unscoped to any worker or requester.

        The target carries the current ``runtime_paths``, which is reassigned once the committed config is read.
        """
        return RequestCredentialsTarget(
            runtime_paths=runtime_paths,
            base_manager=base_manager,
            worker_scope=None,
            agent_name=target_agent_name,
            execution_identity=None,
        )

    if execution_scope_override_provided is None:
        execution_scope_override_provided, execution_scope_override = resolve_dashboard_execution_scope_override(
            request,
        )

    global_config_requested = any(is_global_credential_service(service) for service in service_names)
    if global_config_requested:
        config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
        require_platform_administrator_authorized(
            request,
            config=config,
            runtime_paths=runtime_paths,
        )

    # Requests without an agent or a scope override use the primary runtime's store and need no persisted config.
    if agent_name is None and not execution_scope_override_provided:
        return primary_target(None)

    config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    scope_request = resolve_dashboard_agent_execution_scope_request(
        config=config,
        agent_name=agent_name,
        execution_scope_override_provided=execution_scope_override_provided,
        execution_scope_override=execution_scope_override,
        allow_draft_override=False,
    )
    if scope_request.agent_name is None:
        return primary_target(None)
    execution_scope = scope_request.requested_execution_scope
    allow_requester_scope = (
        allow_private_scopes
        and scope_request.persisted_policy is not None
        and execution_scope in {"user", "user_agent"}
        and not global_config_requested
    )
    if allow_requester_scope:
        execution_identity = require_agent_oauth_connection_authorized(
            request,
            config=config,
            runtime_paths=runtime_paths,
            agent_name=scope_request.agent_name,
            requester_owned=True,
        )
    else:
        execution_identity = require_agent_credential_management_authorized(
            request,
            config=config,
            runtime_paths=runtime_paths,
            agent_name=scope_request.agent_name,
        )
    if execution_scope is None:
        return primary_target(scope_request.agent_name)

    scope_label = dashboard_scope_label(
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

    reject_unbound_private_dashboard_requester(execution_scope, execution_identity)
    require_worker_key_for_scope(
        execution_scope,
        execution_identity=execution_identity,
        agent_name=scope_request.agent_name,
        failure_message=f"Could not resolve worker credentials for agent '{scope_request.agent_name}'.",
    )
    return RequestCredentialsTarget(
        runtime_paths=runtime_paths,
        base_manager=base_manager,
        worker_scope=execution_scope,
        agent_name=scope_request.agent_name,
        execution_identity=execution_identity,
        allowed_shared_services=config.get_worker_grantable_credentials(),
    )


def resolve_requester_credentials_target(
    request: Request,
    *,
    agent_name: str | None,
    service_names: tuple[str, ...] = (),
) -> RequestCredentialsTarget:
    """Resolve credentials that must follow the authenticated requester, independent of worker reuse."""
    base_target = resolve_request_credentials_target(
        request,
        agent_name=None,
        service_names=service_names,
        execution_scope_override_provided=False,
        execution_scope_override=None,
        allow_private_scopes=True,
    )
    if agent_name is not None:
        config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
        execution_identity = require_agent_oauth_connection_authorized(
            request,
            agent_name=agent_name,
            config=config,
            runtime_paths=runtime_paths,
            requester_owned=True,
        )
    else:
        execution_identity = build_dashboard_execution_identity(
            request,
            "oauth",
            config=config_lifecycle.bind_current_request_snapshot(request).runtime_config,
            runtime_paths=base_target.runtime_paths,
        )
    reject_unbound_private_dashboard_requester("user", execution_identity)
    require_worker_key_for_scope(
        "user",
        execution_identity=execution_identity,
        agent_name=agent_name,
        failure_message="Could not resolve requester-scoped OAuth credentials.",
    )
    return replace(base_target, worker_scope="user", agent_name=agent_name, execution_identity=execution_identity)


def load_credentials_for_target(service: str, target: RequestCredentialsTarget) -> dict[str, Any] | None:
    """Load credentials for the resolved target, including scoped overlays when needed."""
    return load_scoped_credentials(
        service,
        credentials_manager=target.base_manager,
        worker_target=worker_target_for_credentials_target(target),
        allowed_shared_services=target.allowed_shared_services,
        primary_built_tool=_target_primary_owns_tool_settings(service, target),
    )


def _target_primary_owns_tool_settings(service: str, target: RequestCredentialsTarget) -> bool:
    """Return whether the primary owns the scoped settings of the tool that this service configures."""
    if target.worker_scope is None or target.agent_name is None:
        return False
    ensure_tool_registry_loaded(target.runtime_paths)
    return primary_owns_tool_settings(service, runtime_paths=target.runtime_paths)


def worker_target_for_credentials_target(target: RequestCredentialsTarget) -> ResolvedWorkerTarget | None:
    """Resolve the worker target represented by one credentials request target."""
    if target.worker_scope is None:
        return None
    return resolve_worker_target(
        target.worker_scope,
        target.agent_name,
        execution_identity=target.execution_identity,
    )


def save_credentials_for_target(service: str, credentials: dict[str, Any], target: RequestCredentialsTarget) -> None:
    """Save credentials to the store the resolved target routes to."""
    save_scoped_credentials(
        service,
        credentials,
        credentials_manager=target.base_manager,
        worker_target=worker_target_for_credentials_target(target),
        primary_built_tool=_target_primary_owns_tool_settings(service, target),
    )


def delete_credentials_for_target(service: str, target: RequestCredentialsTarget) -> None:
    """Delete credentials from the store the resolved target routes to."""
    worker_target = worker_target_for_credentials_target(target)
    primary_built_tool = _target_primary_owns_tool_settings(service, target)
    delete_scoped_credentials(
        service,
        credentials_manager=target.base_manager,
        worker_target=worker_target,
        primary_built_tool=primary_built_tool,
    )
    if primary_built_tool and worker_target is not None and worker_target.worker_key is not None:
        # LEGACY_COMPAT: Worker-store copies of settings the primary now owns.
        # Legacy format: `<service>_credentials.json` in the agent's worker store, where the dashboard saved a scoped agent's tool settings; the dashboard no longer lists or reads that copy.
        # Last legacy release: v2026.10.39 for `openai` and `groq`, v2026.10.8 for `homeassistant` and `spotify`, whose dedicated dashboard routes kept saving there, v2026.9.414 for other tools routed to a worker, and v2026.9.404 for the rest; replacement: v2026.10.40, v2026.10.9, v2026.9.415, and v2026.9.405 respectively save them in the primary's agent- or requester-scoped stores.
        # Handling: deleting the settings also deletes that worker copy, so a deleted value stops reaching worker code.
        # Coverage: tests/test_credentials.py::test_dashboard_delete_also_removes_the_worker_copy_of_tool_settings.
        target.base_manager.for_worker(worker_target.worker_key).delete_credentials(service)


def dashboard_candidate_stores(target: RequestCredentialsTarget) -> list[CredentialsManager]:
    """Return every store this target could read a service from, before checking where each service lives."""
    worker_target = worker_target_for_credentials_target(target)
    base_manager = target.base_manager
    stores = [base_manager]
    if worker_target is not None:
        if worker_target.worker_key is not None:
            stores.append(base_manager.for_worker(worker_target.worker_key))
        requester_id = target.execution_identity.requester_id if target.execution_identity is not None else None
        if target.worker_scope == "shared" and target.agent_name is not None:
            stores.append(base_manager.for_primary_runtime_agent_scope(target.agent_name))
        if requester_id is not None and target.worker_scope == "user":
            stores.append(base_manager.for_primary_runtime_scope(requester_id, None))
        # A requester's store with one agent serves user_agent scopes and Desktop pairings in every scope.
        if requester_id is not None and target.agent_name is not None:
            stores.append(base_manager.for_primary_runtime_scope(requester_id, target.agent_name))
    return stores


def target_reads_service_from(service: str, store: CredentialsManager, target: RequestCredentialsTarget) -> bool:
    """Return whether this target reads a service that ``store`` holds, even when its document cannot be decoded."""
    try:
        layers = credential_layers(
            service,
            credentials_manager=target.base_manager,
            worker_target=worker_target_for_credentials_target(target),
            allowed_shared_services=target.allowed_shared_services,
            primary_built_tool=_target_primary_owns_tool_settings(service, target),
        )
    except ValueError:
        # The service has no store for this target, such as a requester's store without a requester.
        return False
    if layers.store is not None and layers.store.base_path == store.base_path:
        return True
    # A shared document reaches the target only when the grant passes it on.
    return layers.shared is not None and layers.shared.base_path == store.base_path and layers.load(service) is not None
