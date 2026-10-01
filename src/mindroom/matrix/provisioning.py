"""Provisioning helpers for hosted local-MindRoom registration flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, NoReturn

import httpx

from mindroom.http_error_detail import error_detail_from_response
from mindroom.matrix.client_session import matrix_startup_error
from mindroom.matrix.identity import parse_current_matrix_user_id
from mindroom.matrix.provisioning_env import (
    local_client_headers,
    local_pairing_required,
    local_provisioning_client_credentials_from_env,
)

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths


def required_local_provisioning_client_credentials_for_registration(
    *,
    provisioning_url: str | None,
    registration_token: str | None,
    runtime_paths: RuntimePaths,
) -> tuple[str, str] | None:
    """Resolve required local provisioning credentials when using hosted registration."""
    if registration_token or not provisioning_url:
        return None

    creds = local_provisioning_client_credentials_from_env(runtime_paths)
    # Unpaired installs with a shared secret register through it, matching the run's decision to skip pairing.
    if creds is None and local_pairing_required(runtime_paths):
        msg = "MINDROOM_PROVISIONING_URL is set but local client credentials are missing. Run `mindroom connect` first."
        raise matrix_startup_error(msg, permanent=True)
    return creds


@dataclass(frozen=True)
class _ProvisioningRegisterResult:
    """Result returned by the provisioning register-agent endpoint."""

    status: Literal["created", "user_in_use"]
    user_id: str
    # One-time password the service registered a created account with; the caller replaces it immediately.
    password: str | None


# Kept in sync with scripts/local_mindroom_provisioning_service.py by a contract
# test. The service's other credential failures ("Missing/Invalid local client
# credentials") always use HTTP 401, so only the revoked detail matters for 403.
_CONNECTION_REVOKED_DETAIL = "Connection revoked"
_NAMESPACE_MISMATCH_DETAIL = "Requested username is outside this local connection namespace"


def local_client_credentials_rejected(response: httpx.Response) -> bool:
    """Return whether the provisioning service rejected this install's credentials as invalid or revoked."""
    return response.status_code == 401 or (
        response.status_code == 403 and error_detail_from_response(response) == _CONNECTION_REVOKED_DETAIL
    )


def _raise_for_register_agent_error(response: httpx.Response, *, username: str) -> NoReturn:
    """Raise the appropriate error for a failed register-agent response."""
    detail = error_detail_from_response(response)
    if local_client_credentials_rejected(response):
        msg = f"Provisioning credentials are invalid or revoked (server said: {detail}). Run `mindroom connect` again."
        raise matrix_startup_error(msg, permanent=True)
    if response.status_code == 403:
        msg = f"Provisioning service refused to register agent user {username!r}: {detail}"
        if detail == _NAMESPACE_MISMATCH_DETAIL:
            msg += (
                ". Usernames must match mindroom_<entity>_<namespace>; check that MINDROOM_NAMESPACE "
                "matches this connection's namespace or re-run `mindroom connect`."
            )
        raise matrix_startup_error(msg, permanent=True)
    if response.status_code == 404:
        msg = "Provisioning service does not support /register-agent yet. Deploy the latest local provisioning service."
        raise matrix_startup_error(msg, permanent=True)
    if response.status_code in {400, 422}:
        msg = f"Provisioning service rejected the register-agent request (HTTP {response.status_code}): {detail}"
        raise matrix_startup_error(msg, permanent=True)
    if 300 <= response.status_code < 400:
        location = response.headers.get("location", "unknown")
        msg = (
            f"Provisioning service URL redirects (HTTP {response.status_code} to {location}). "
            "Update MINDROOM_PROVISIONING_URL to the final URL."
        )
        raise matrix_startup_error(msg, permanent=True)
    msg = f"Provisioning service returned HTTP {response.status_code}: {detail}"
    raise ValueError(msg)


async def register_user_via_provisioning_service(
    *,
    provisioning_url: str,
    client_id: str,
    client_secret: str,
    homeserver: str,
    username: str,
    display_name: str,
) -> _ProvisioningRegisterResult:
    """Register an agent account via provisioning service server-side flow."""
    url = f"{provisioning_url}/v1/local-mindroom/register-agent"
    headers = local_client_headers(client_id, client_secret)
    payload = {
        "homeserver": homeserver.rstrip("/"),
        "username": username,
        "display_name": display_name,
    }
    try:
        # The response carries the agent's one-time password, so TLS is verified whatever MATRIX_SSL_VERIFY says.
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        msg = f"Could not reach provisioning service ({provisioning_url}): {exc}"
        raise ValueError(msg) from exc

    if not response.is_success:
        _raise_for_register_agent_error(response, username=username)

    try:
        body = response.json()
    except ValueError as exc:
        msg = "Provisioning service returned invalid JSON while registering agent."
        raise matrix_startup_error(msg, permanent=True) from exc

    if not isinstance(body, dict):
        msg = "Provisioning service returned invalid register-agent payload."
        raise matrix_startup_error(msg, permanent=True)

    status = body.get("status")
    user_id = body.get("user_id")
    if status not in {"created", "user_in_use"}:
        msg = "Provisioning service response missing valid status for register-agent."
        raise matrix_startup_error(msg, permanent=True)
    if not isinstance(user_id, str) or not user_id.strip():
        msg = "Provisioning service response missing user_id for register-agent."
        raise matrix_startup_error(msg, permanent=True)
    try:
        parsed_user_id = parse_current_matrix_user_id(user_id.strip())
    except ValueError as exc:
        msg = "Provisioning service response returned invalid user_id for register-agent."
        raise matrix_startup_error(msg, permanent=True) from exc

    password = None
    if status == "created":
        password = body.get("password")
        if not isinstance(password, str) or not password:
            msg = "Provisioning service response missing one-time password for created register-agent account."
            raise matrix_startup_error(msg, permanent=True)

    return _ProvisioningRegisterResult(status=status, user_id=parsed_user_id, password=password)
