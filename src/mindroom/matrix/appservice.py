"""Passwordless Matrix application-service authentication for managed accounts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NamedTuple, NoReturn

import httpx

from mindroom.constants import RuntimePaths, runtime_env_path, runtime_matrix_ssl_verify
from mindroom.matrix.client_session import create_authenticated_client, matrix_startup_error
from mindroom.matrix.identity import parse_current_matrix_user_id
from mindroom.runtime_env_policy import (
    MATRIX_APPSERVICE_TOKEN_ENV,
    MATRIX_APPSERVICE_TOKEN_FILE_ENV,
    MATRIX_MANAGED_ACCOUNT_AUTH_ENV,
)

if TYPE_CHECKING:
    import nio

_ManagedAccountAuthMode = Literal["password", "appservice"]

_APPSERVICE_LOGIN_TYPE = "m.login.application_service"
_TRANSIENT_APPSERVICE_STATUS_CODES = frozenset({408, 425, 429})


@dataclass(frozen=True)
class ManagedAccountAuth:
    """Resolved authentication method for Matrix accounts managed by MindRoom."""

    mode: _ManagedAccountAuthMode
    appservice_token: str | None = None


class _ConfiguredToken(NamedTuple):
    token: str
    source_env: str


def _appservice_token_from_env(runtime_paths: RuntimePaths) -> _ConfiguredToken | None:
    token = (runtime_paths.env_value(MATRIX_APPSERVICE_TOKEN_ENV) or "").strip()
    file_path = runtime_env_path(runtime_paths, MATRIX_APPSERVICE_TOKEN_FILE_ENV)
    if token and file_path is not None:
        msg = f"Set only one of {MATRIX_APPSERVICE_TOKEN_ENV} or {MATRIX_APPSERVICE_TOKEN_FILE_ENV}"
        raise matrix_startup_error(msg, permanent=True)
    if token:
        return _ConfiguredToken(token, MATRIX_APPSERVICE_TOKEN_ENV)
    if file_path is None:
        return None
    try:
        file_token = file_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        msg = f"{MATRIX_APPSERVICE_TOKEN_FILE_ENV} is not readable: {file_path}"
        raise matrix_startup_error(msg, permanent=True) from exc
    if not file_token:
        msg = f"{MATRIX_APPSERVICE_TOKEN_FILE_ENV} points to an empty file: {file_path}"
        raise matrix_startup_error(msg, permanent=True)
    return _ConfiguredToken(file_token, MATRIX_APPSERVICE_TOKEN_FILE_ENV)


def resolve_managed_account_auth(runtime_paths: RuntimePaths) -> ManagedAccountAuth:
    """Resolve explicit password or application-service account authentication."""
    raw_mode = (runtime_paths.env_value(MATRIX_MANAGED_ACCOUNT_AUTH_ENV) or "password").strip().lower()
    if raw_mode not in {"password", "appservice"}:
        msg = f"{MATRIX_MANAGED_ACCOUNT_AUTH_ENV} must be 'password' or 'appservice', got {raw_mode!r}"
        raise matrix_startup_error(msg, permanent=True)

    configured = _appservice_token_from_env(runtime_paths)
    if raw_mode == "password":
        if configured is not None:
            msg = f"{configured.source_env} is set but {MATRIX_MANAGED_ACCOUNT_AUTH_ENV} is not 'appservice'"
            raise matrix_startup_error(msg, permanent=True)
        return ManagedAccountAuth(mode="password")

    if configured is None:
        msg = (
            f"{MATRIX_MANAGED_ACCOUNT_AUTH_ENV}=appservice requires "
            f"{MATRIX_APPSERVICE_TOKEN_ENV} or {MATRIX_APPSERVICE_TOKEN_FILE_ENV}"
        )
        raise matrix_startup_error(msg, permanent=True)
    return ManagedAccountAuth(mode="appservice", appservice_token=configured.token)


def _response_error(response: httpx.Response) -> tuple[str | None, str]:
    try:
        body = response.json()
    except ValueError:
        return None, response.text.strip() or f"HTTP {response.status_code}"
    if not isinstance(body, dict):
        return None, f"HTTP {response.status_code}"
    errcode = body.get("errcode")
    error = body.get("error")
    return (
        errcode if isinstance(errcode, str) else None,
        error if isinstance(error, str) and error else f"HTTP {response.status_code}",
    )


def _success_response_body(action: str, response: httpx.Response) -> dict[str, object]:
    try:
        body = response.json()
    except ValueError as exc:
        msg = f"Matrix application-service {action} returned invalid JSON"
        raise matrix_startup_error(msg, permanent=True) from exc
    if not isinstance(body, dict):
        msg = f"Matrix application-service {action} returned a non-object JSON response"
        raise matrix_startup_error(msg, permanent=True)
    return body


async def _post(
    homeserver: str,
    path: str,
    *,
    token: str,
    payload: dict[str, object],
    runtime_paths: RuntimePaths,
) -> httpx.Response:
    async with httpx.AsyncClient(
        timeout=10,
        verify=runtime_matrix_ssl_verify(runtime_paths=runtime_paths),
    ) as client:
        return await client.post(
            f"{homeserver.rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )


def _raise_appservice_error(action: str, response: httpx.Response) -> NoReturn:
    errcode, detail = _response_error(response)
    message = f"Matrix application-service {action} failed: {errcode or response.status_code}: {detail}"
    permanent = response.status_code < 500 and response.status_code not in _TRANSIENT_APPSERVICE_STATUS_CODES
    raise matrix_startup_error(message, permanent=permanent)


async def register_appservice_user(
    homeserver: str,
    *,
    username: str,
    expected_user_id: str,
    token: str,
    runtime_paths: RuntimePaths,
) -> str:
    """Register a passwordless user and return the server-assigned user ID.

    Mirrors password-mode registration: the caller adopts whatever user ID the
    homeserver assigns (e.g. its real server name when ``MATRIX_SERVER_NAME``
    is unset). ``expected_user_id`` is only the fallback when the account
    already exists.
    """
    response = await _post(
        homeserver,
        "/_matrix/client/v3/register",
        token=token,
        payload={
            "type": _APPSERVICE_LOGIN_TYPE,
            "username": username,
            "inhibit_login": True,
        },
        runtime_paths=runtime_paths,
    )
    if not response.is_success:
        errcode, _ = _response_error(response)
        if errcode == "M_USER_IN_USE":
            return expected_user_id
        _raise_appservice_error("registration", response)

    body = _success_response_body("registration", response)
    returned_user_id = body.get("user_id")
    msg = f"Matrix application-service registration returned an invalid user ID: {returned_user_id!r}"
    if not isinstance(returned_user_id, str):
        raise matrix_startup_error(msg, permanent=True)
    try:
        return parse_current_matrix_user_id(returned_user_id)
    except ValueError as exc:
        raise matrix_startup_error(msg, permanent=True) from exc


async def login_appservice_user(
    homeserver: str,
    *,
    user_id: str,
    token: str,
    runtime_paths: RuntimePaths,
) -> nio.AsyncClient:
    """Create an ordinary per-user Matrix device through application-service login."""
    response = await _post(
        homeserver,
        "/_matrix/client/v3/login",
        token=token,
        payload={
            "type": _APPSERVICE_LOGIN_TYPE,
            "identifier": {"type": "m.id.user", "user": user_id},
            "initial_device_display_name": "MindRoom",
        },
        runtime_paths=runtime_paths,
    )
    if not response.is_success:
        _raise_appservice_error("login", response)

    body = _success_response_body("login", response)
    returned_user_id = body.get("user_id")
    access_token = body.get("access_token")
    device_id = body.get("device_id")
    if returned_user_id != user_id or not isinstance(access_token, str) or not isinstance(device_id, str):
        msg = f"Matrix application-service login returned incomplete credentials for {user_id}"
        raise matrix_startup_error(msg, permanent=True)

    return create_authenticated_client(
        homeserver,
        user_id,
        device_id,
        access_token,
        runtime_paths=runtime_paths,
    )
