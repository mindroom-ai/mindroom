"""Pending OAuth connect state for dashboard and conversation credential flows.

Owns issue/consume of the opaque OAuth state that binds one OAuth connect
request to its authorization mode and credential target.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from fastapi import HTTPException, Request

from mindroom.api import config_lifecycle
from mindroom.api.auth_user import require_request_auth_user
from mindroom.api.dashboard_credential_scope import resolve_dashboard_execution_scope_override
from mindroom.oauth.providers import OAuthProviderError
from mindroom.oauth.state import consume_opaque_oauth_state, issue_opaque_oauth_state, read_opaque_oauth_state

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from mindroom.tool_system.worker_routing import WorkerScope

_PENDING_OAUTH_STATE_TTL_SECONDS = 600
_PENDING_OAUTH_STATE_KIND = "dashboard_oauth_state"


@dataclass(frozen=True)
class _PendingOAuthState:
    """Pending OAuth connect request bound to its initiating authorization mode.

    `group_members` holds one credential target per member of a consent group sign-in, empty for any other flow.
    """

    browser_user_required: bool
    agent_name: str | None
    execution_scope_override_provided: bool
    execution_scope_override: WorkerScope | None
    payload: dict[str, str] | None
    code_verifier: str | None
    token_url: str | None
    credential_service: str | None = None
    group_members: tuple[dict[str, str], ...] = ()


def issue_pending_oauth_state(
    request: Request,
    service: str,
    agent_name: str | None = None,
    *,
    payload: dict[str, str] | None = None,
    code_verifier: str | None = None,
    token_url: str | None = None,
    browser_user_required: bool = True,
    opener_origin: str | None = None,
    credential_service: str | None = None,
    group_members: Sequence[Mapping[str, str]] = (),
) -> str:
    """Create opaque OAuth state bound to a browser user or conversation capability.

    `opener_origin` is the already validated origin of the page that started the flow, which the popup's last page
    tells how the flow ended. `credential_service` is the name the callback stores the connection under instead of the
    provider's own, for an Agent Keys service's own account. `group_members` is each member's credential target for a
    consent group sign-in.
    """
    if browser_user_required:
        auth_user = require_request_auth_user(request)
        execution_scope_override_provided, execution_scope_override = resolve_dashboard_execution_scope_override(
            request,
        )
    else:
        auth_user = None
        execution_scope_override_provided, execution_scope_override = False, None
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    return issue_opaque_oauth_state(
        runtime_paths,
        kind=_PENDING_OAUTH_STATE_KIND,
        ttl_seconds=_PENDING_OAUTH_STATE_TTL_SECONDS,
        data={
            "service": service,
            "user_id": auth_user.user_id if auth_user is not None else "",
            "auth_source": auth_user.source.value if auth_user is not None else "",
            "browser_user_required": browser_user_required,
            "agent_name": agent_name or "",
            "execution_scope_override_provided": execution_scope_override_provided,
            "execution_scope_override": execution_scope_override or "",
            "payload": payload or {},
            "code_verifier": code_verifier or "",
            "token_url": token_url or "",
            "opener_origin": opener_origin or "",
            "credential_service": credential_service or "",
            "group_members": [dict(member) for member in group_members],
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


def pending_oauth_state_browser_auth_source(request: Request, service: str, state: str) -> str | None:
    """Return how the browser user that issued pending OAuth state authenticated, or None when none is bound."""
    data = _read_pending_oauth_request(request, service, state)
    if data.get("browser_user_required") is False:
        return None
    auth_source = data.get("auth_source")
    return auth_source if isinstance(auth_source, str) else ""


def pending_oauth_opener_origin(request: Request, service: str, state: str | None) -> str | None:
    """Return the opener origin a pending OAuth state recorded, without consuming it; None for any unusable state."""
    if not state:
        return None
    try:
        data = _read_pending_oauth_request(request, service, state)
    except HTTPException:
        return None
    origin = data.get("opener_origin")
    return origin if isinstance(origin, str) and origin else None


def _string_mapping(value: object) -> dict[str, str] | None:
    """Return `value` when it is a mapping of strings to strings, as pending state stores one."""
    if isinstance(value, dict) and all(isinstance(key, str) and isinstance(item, str) for key, item in value.items()):
        return cast("dict[str, str]", value)
    return None


def _group_members(value: object) -> tuple[dict[str, str], ...]:
    """Return every member a pending state stores, none for a state without members.

    A malformed member list refuses the callback, so a consent group sign-in never stores its grant for fewer services.
    """
    if value is None:
        return ()
    members = [_string_mapping(member) for member in value] if isinstance(value, list) else []
    valid_members = tuple(member for member in members if member is not None)
    if not isinstance(value, list) or len(valid_members) != len(value):
        raise HTTPException(status_code=400, detail="OAuth state does not record its consent group members correctly")
    return valid_members


def consume_pending_oauth_request(request: Request, service: str, state: str) -> _PendingOAuthState:
    """Consume and validate a previously issued OAuth state token."""
    runtime_paths = config_lifecycle.bind_current_request_snapshot(request).runtime_paths
    data = _read_pending_oauth_request(request, service, state)
    browser_user_required = data.get("browser_user_required") is not False
    if browser_user_required and data.get("user_id") != require_request_auth_user(request).user_id:
        raise HTTPException(status_code=403, detail="OAuth state does not belong to the current user")
    try:
        consume_opaque_oauth_state(runtime_paths, kind=_PENDING_OAUTH_STATE_KIND, token=state)
    except OAuthProviderError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    execution_scope_raw = data.get("execution_scope_override")
    execution_scope_override = execution_scope_raw if execution_scope_raw in {"shared", "user", "user_agent"} else None
    agent_name = data.get("agent_name")
    code_verifier = data.get("code_verifier")
    token_url = data.get("token_url")
    credential_service = data.get("credential_service")
    return _PendingOAuthState(
        browser_user_required=browser_user_required,
        agent_name=agent_name if isinstance(agent_name, str) and agent_name else None,
        execution_scope_override_provided=data.get("execution_scope_override_provided") is True,
        execution_scope_override=cast("WorkerScope | None", execution_scope_override),
        payload=_string_mapping(data.get("payload")),
        code_verifier=code_verifier if isinstance(code_verifier, str) and code_verifier else None,
        token_url=token_url if isinstance(token_url, str) and token_url else None,
        credential_service=credential_service if isinstance(credential_service, str) and credential_service else None,
        group_members=_group_members(data.get("group_members")),
    )
