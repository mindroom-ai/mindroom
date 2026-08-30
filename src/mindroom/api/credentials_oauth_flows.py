"""Pending OAuth connect state for dashboard and conversation credential flows.

Owns issue/consume of the opaque OAuth state that binds one OAuth connect
request to its authorization mode and credential target.
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
    """Pending OAuth connect request bound to its initiating authorization mode."""

    service: str
    user_id: str
    browser_user_required: bool
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
    browser_user_required: bool = True,
) -> str:
    """Create opaque OAuth state bound to a browser user or conversation capability."""
    user_id = require_auth_user_id(request) if browser_user_required else ""
    if browser_user_required:
        execution_scope_override_provided, execution_scope_override = resolve_dashboard_execution_scope_override(
            request,
        )
    else:
        execution_scope_override_provided, execution_scope_override = False, None
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    return issue_opaque_oauth_state(
        runtime_paths,
        kind=_PENDING_OAUTH_STATE_KIND,
        ttl_seconds=_PENDING_OAUTH_STATE_TTL_SECONDS,
        data={
            "service": service,
            "user_id": user_id,
            "browser_user_required": browser_user_required,
            "agent_name": agent_name or "",
            "execution_scope_override_provided": execution_scope_override_provided,
            "execution_scope_override": execution_scope_override or "",
            "payload": payload or {},
            "code_verifier": code_verifier or "",
        },
    )


def _read_pending_oauth_request(request: Request, service: str, state: str) -> dict[str, object]:
    """Read and validate pending OAuth state without consuming it."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    try:
        data = read_opaque_oauth_state(runtime_paths, kind=_PENDING_OAUTH_STATE_KIND, token=state)
    except OAuthProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if data.get("service") != service:
        raise HTTPException(status_code=400, detail="OAuth state does not match this integration")
    return data


def pending_oauth_state_requires_browser_user(request: Request, service: str, state: str) -> bool:
    """Return whether pending OAuth state must match an authenticated browser user."""
    data = _read_pending_oauth_request(request, service, state)
    return data.get("browser_user_required") is not False


def consume_pending_oauth_request(request: Request, service: str, state: str) -> _PendingOAuthState:
    """Consume and validate a previously issued OAuth state token."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    data = _read_pending_oauth_request(request, service, state)
    browser_user_required = data.get("browser_user_required") is not False
    user_id = require_auth_user_id(request) if browser_user_required else ""
    if browser_user_required and data.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="OAuth state does not belong to the current user")
    try:
        consume_opaque_oauth_state(runtime_paths, kind=_PENDING_OAUTH_STATE_KIND, token=state)
    except OAuthProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    execution_scope_raw = data.get("execution_scope_override")
    execution_scope_override = execution_scope_raw if execution_scope_raw in {"shared", "user", "user_agent"} else None
    agent_name = data.get("agent_name")
    payload_raw = data.get("payload")
    payload = (
        cast("dict[str, str]", payload_raw)
        if isinstance(payload_raw, dict)
        and all(isinstance(key, str) and isinstance(value, str) for key, value in payload_raw.items())
        else None
    )
    code_verifier = data.get("code_verifier")
    return _PendingOAuthState(
        service=service,
        user_id=user_id,
        browser_user_required=browser_user_required,
        agent_name=agent_name if isinstance(agent_name, str) and agent_name else None,
        execution_scope_override_provided=data.get("execution_scope_override_provided") is True,
        execution_scope_override=cast("WorkerScope | None", execution_scope_override),
        payload=payload,
        code_verifier=code_verifier if isinstance(code_verifier, str) and code_verifier else None,
        created_at=0,
    )
