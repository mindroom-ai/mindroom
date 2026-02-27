"""Provisioning helpers for hosted local-MindRoom registration flows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import httpx


def _matrix_ssl_verify_enabled() -> bool:
    return os.getenv("MATRIX_SSL_VERIFY", "true").lower() != "false"


def provisioning_url_from_env() -> str | None:
    """Get hosted provisioning API base URL from environment if configured."""
    url = os.getenv("MINDROOM_PROVISIONING_URL", "").strip()
    return url.rstrip("/") or None


def local_provisioning_client_credentials_from_env() -> tuple[str, str] | None:
    """Get local provisioning client credentials from environment if configured."""
    client_id = os.getenv("MINDROOM_LOCAL_CLIENT_ID", "").strip()
    client_secret = os.getenv("MINDROOM_LOCAL_CLIENT_SECRET", "").strip()
    if not client_id and not client_secret:
        return None
    if not client_id or not client_secret:
        msg = (
            "Provisioning credentials are incomplete. "
            "Set both MINDROOM_LOCAL_CLIENT_ID and MINDROOM_LOCAL_CLIENT_SECRET, "
            "or run `mindroom connect --pair-code ...` again."
        )
        raise ValueError(msg)
    return client_id, client_secret


@dataclass(frozen=True)
class ProvisioningRegisterResult:
    """Result returned by the provisioning register-agent endpoint."""

    status: Literal["created", "user_in_use"]
    user_id: str


async def register_user_via_provisioning_service(
    *,
    provisioning_url: str,
    client_id: str,
    client_secret: str,
    homeserver: str,
    username: str,
    password: str,
    display_name: str,
) -> ProvisioningRegisterResult:
    """Register an agent account via provisioning service server-side flow."""
    url = f"{provisioning_url}/v1/local-mindroom/register-agent"
    headers = {
        "X-Local-MindRoom-Client-Id": client_id,
        "X-Local-MindRoom-Client-Secret": client_secret,
    }
    payload = {
        "homeserver": homeserver.rstrip("/"),
        "username": username,
        "password": password,
        "display_name": display_name,
    }
    try:
        async with httpx.AsyncClient(timeout=10, verify=_matrix_ssl_verify_enabled()) as client:
            response = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        msg = f"Could not reach provisioning service ({provisioning_url}): {exc}"
        raise ValueError(msg) from exc

    if not response.is_success:
        detail = response.text.strip() or "unknown error"
        if response.status_code in {401, 403}:
            msg = "Provisioning credentials are invalid or revoked. Run `mindroom connect --pair-code ...` again."
            raise ValueError(msg)
        if response.status_code == 404:
            msg = (
                "Provisioning service does not support /register-agent yet. "
                "Deploy the latest local provisioning service."
            )
            raise ValueError(msg)
        msg = f"Provisioning service returned HTTP {response.status_code}: {detail}"
        raise ValueError(msg)

    try:
        body = response.json()
    except ValueError as exc:
        msg = "Provisioning service returned invalid JSON while registering agent."
        raise ValueError(msg) from exc

    if not isinstance(body, dict):
        msg = "Provisioning service returned invalid register-agent payload."
        raise TypeError(msg)

    status = body.get("status")
    user_id = body.get("user_id")
    if status not in {"created", "user_in_use"}:
        msg = "Provisioning service response missing valid status for register-agent."
        raise ValueError(msg)
    if not isinstance(user_id, str) or not user_id.strip():
        msg = "Provisioning service response missing user_id for register-agent."
        raise ValueError(msg)

    return ProvisioningRegisterResult(status=status, user_id=user_id.strip())
