"""Credential storage target resolution and store routing for dashboard requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException, Request

from mindroom.agent_policy import dashboard_credentials_supported_for_scope
from mindroom.api import config_lifecycle
from mindroom.api.dashboard_credential_scope import (
    dashboard_scope_label,
    reject_unbound_private_dashboard_requester,
    require_agent_credential_management_authorized,
    resolve_dashboard_agent_execution_scope_request,
    resolve_dashboard_execution_scope_override,
)
from mindroom.credential_policy import credential_service_policy
from mindroom.credentials import (
    CredentialsManager,
    delete_scoped_credentials,
    get_runtime_credentials_manager,
    load_scoped_credentials,
    load_worker_grantable_shared_credentials,
    save_scoped_credentials,
)
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
    from mindroom.tool_system.worker_routing import ResolvedWorkerTarget


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
    execution_identity = require_agent_credential_management_authorized(
        request,
        config=config,
        runtime_paths=runtime_paths,
        agent_name=scope_request.agent_name,
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
    if _service_uses_primary_runtime_global_store(service, target):
        return target.base_manager.load_credentials(service)
    if target.worker_scope is None:
        return target.target_manager.load_credentials(service)
    if _service_uses_primary_runtime_store(service, target):
        return load_scoped_credentials(
            service,
            credentials_manager=target.base_manager,
            worker_target=worker_target_for_credentials_target(target),
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
    return (
        policy.uses_primary_runtime_global_credentials
        or policy.uses_primary_runtime_scoped_credentials
        or policy.uses_local_shared_credentials
    )


def _service_uses_primary_runtime_global_store(service: str, target: RequestCredentialsTarget) -> bool:
    return credential_service_policy(service, target.worker_scope).uses_primary_runtime_global_credentials


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
    if _service_uses_primary_runtime_global_store(service, target):
        target.base_manager.save_credentials(service, credentials)
        return
    if target.worker_scope is None or not _service_uses_primary_runtime_store(service, target):
        target.target_manager.save_credentials(service, credentials)
        return
    save_scoped_credentials(
        service,
        credentials,
        credentials_manager=target.base_manager,
        worker_target=worker_target_for_credentials_target(target),
    )


def delete_credentials_for_target(service: str, target: RequestCredentialsTarget) -> None:
    """Delete credentials from the store the resolved target routes to."""
    if _service_uses_primary_runtime_global_store(service, target):
        target.base_manager.delete_credentials(service)
        return
    if target.worker_scope is None or not _service_uses_primary_runtime_store(service, target):
        target.target_manager.delete_credentials(service)
        return
    delete_scoped_credentials(
        service,
        credentials_manager=target.base_manager,
        worker_target=worker_target_for_credentials_target(target),
    )


def primary_runtime_scoped_services_for_target(target: RequestCredentialsTarget) -> set[str]:
    """List services stored in the primary runtime scoped store for one target."""
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
