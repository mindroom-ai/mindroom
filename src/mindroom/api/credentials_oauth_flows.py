"""Pending OAuth connect state for dashboard credential flows.

Owns issue/consume of the opaque OAuth state that binds one dashboard OAuth
connect request to one authenticated user and credential target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException, Request

from mindroom.api import config_lifecycle
from mindroom.api.dashboard_credential_scope import require_auth_user_id, resolve_dashboard_execution_scope_override
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state

if TYPE_CHECKING:
    from mindroom.tool_system.worker_routing import WorkerScope

_PENDING_OAUTH_STATE_TTL_SECONDS = 600
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
    code_verifier: str | None
    created_at: float


def issue_pending_oauth_state(
    request: Request,
    service: str,
    agent_name: str | None = None,
    *,
    payload: dict[str, str] | None = None,
    code_verifier: str | None = None,
) -> str:
    """Create an opaque OAuth state bound to the current user and target."""
    user_id = require_auth_user_id(request)
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
            "code_verifier": code_verifier or "",
        },
    )


def consume_pending_oauth_request(request: Request, service: str, state: str) -> _PendingOAuthState:
    """Consume and validate a previously issued dashboard OAuth state token."""
    user_id = require_auth_user_id(request)
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
    code_verifier = data.get("code_verifier")
    return _PendingOAuthState(
        service=service,
        user_id=user_id,
        agent_name=data.get("agent_name") or None,
        execution_scope_override_provided=data.get("execution_scope_override_provided") is True,
        execution_scope_override=cast("WorkerScope | None", execution_scope_override),
        payload=payload if isinstance(payload, dict) else None,
        code_verifier=code_verifier if isinstance(code_verifier, str) and code_verifier else None,
        created_at=0,
    )
