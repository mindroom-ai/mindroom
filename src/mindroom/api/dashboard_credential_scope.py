"""Dashboard credential scope resolution and authorization checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException, Request

from mindroom.agent_policy import ResolvedAgentPolicy, resolve_agent_policy_from_data
from mindroom.authorization import is_sender_allowed_for_agent_credential_management
from mindroom.matrix.identity import try_parse_historical_matrix_user_id
from mindroom.tool_system.worker_routing import ToolExecutionIdentity, WorkerScope

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths

_OWNER_MATRIX_USER_ID_ENV = "MINDROOM_OWNER_USER_ID"


@dataclass(frozen=True)
class _DashboardAgentExecutionScopeResolution:
    """Resolved dashboard scope request for one agent selection."""

    agent_name: str | None
    persisted_policy: ResolvedAgentPolicy | None
    persisted_execution_scope: WorkerScope | None
    requested_execution_scope: WorkerScope | None
    execution_scope_override_provided: bool
    draft_scope_preview: bool


def _request_auth_user(request: Request) -> dict[str, Any] | None:
    auth_user = request.scope.get("auth_user")
    return auth_user if isinstance(auth_user, dict) else None


def require_auth_user_id(request: Request) -> str:
    """Return the authenticated dashboard user id or raise 401."""
    auth_user = _request_auth_user(request) or {}
    user_id = auth_user.get("user_id")
    if isinstance(user_id, str) and user_id:
        return user_id
    raise HTTPException(status_code=401, detail="Missing or invalid credentials")


def _dashboard_requester_id_for_request(request: Request, runtime_paths: RuntimePaths) -> str | None:
    """Return the requester identity dashboard-scoped worker credentials should use."""
    auth_user = _request_auth_user(request) or {}
    if auth_user.get("auth_source") == "trusted_upstream":
        matrix_user_id = auth_user.get("matrix_user_id")
        return try_parse_historical_matrix_user_id(matrix_user_id) if isinstance(matrix_user_id, str) else None
    owner_user_id = runtime_paths.env_value(_OWNER_MATRIX_USER_ID_ENV)
    if owner_user_id:
        return owner_user_id
    user_id = auth_user.get("user_id")
    return user_id if isinstance(user_id, str) and user_id else None


def reject_unbound_private_dashboard_requester(
    execution_scope: WorkerScope,
    execution_identity: ToolExecutionIdentity,
) -> None:
    """Reject private-scope dashboard requests without a Matrix requester identity."""
    if execution_scope not in {"user", "user_agent"}:
        return
    if try_parse_historical_matrix_user_id(execution_identity.requester_id):
        return
    raise HTTPException(
        status_code=400,
        detail=(
            "Dashboard credential management for private user scopes requires a Matrix requester identity. "
            "Set MINDROOM_OWNER_USER_ID to your Matrix user ID, or run MindRoom under Matrix authentication."
        ),
    )


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
        requester_id=_dashboard_requester_id_for_request(request, runtime_paths),
        room_id=None,
        thread_id=None,
        resolved_thread_id=None,
        session_id=None,
        tenant_id=tenant_id,
        account_id=account_id,
    )


def dashboard_scope_label(
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
) -> _DashboardAgentExecutionScopeResolution:
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
        return _DashboardAgentExecutionScopeResolution(
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
        requested_scope_label = dashboard_scope_label(
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
    return _DashboardAgentExecutionScopeResolution(
        agent_name=agent_name,
        persisted_policy=persisted_policy,
        persisted_execution_scope=persisted_execution_scope,
        requested_execution_scope=requested_execution_scope,
        execution_scope_override_provided=execution_scope_override_provided,
        draft_scope_preview=draft_scope_preview,
    )


def require_agent_credential_management_authorized(
    request: Request,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    agent_name: str,
) -> ToolExecutionIdentity:
    """Require the dashboard requester to be allowed to manage one agent's credentials."""
    execution_identity = build_dashboard_execution_identity(
        request,
        agent_name,
        runtime_paths=runtime_paths,
    )
    requester_id = execution_identity.requester_id
    if requester_id is None or not is_sender_allowed_for_agent_credential_management(
        requester_id,
        agent_name=agent_name,
        config=config,
    ):
        raise HTTPException(status_code=403, detail=f"Not authorized to manage credentials for agent '{agent_name}'")
    return execution_identity
