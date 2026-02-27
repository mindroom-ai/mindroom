"""Provisioning helpers for hosted local-MindRoom registration flows."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import httpx

from mindroom.constants import MATRIX_SSL_VERIFY


def provisioning_url_from_env() -> str | None:
    """Get hosted provisioning API base URL from environment if configured."""
    url = os.getenv("MINDROOM_PROVISIONING_URL", "").strip()
    return url.rstrip("/") or None


def registration_token_from_env() -> str | None:
    """Get MATRIX_REGISTRATION_TOKEN from environment if configured."""
    token = os.getenv("MATRIX_REGISTRATION_TOKEN", "").strip()
    return token or None


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


def required_local_provisioning_client_credentials_for_registration(
    *,
    provisioning_url: str | None,
    registration_token: str | None,
) -> tuple[str, str] | None:
    """Resolve required local provisioning credentials when using hosted registration."""
    if registration_token or not provisioning_url:
        return None

    creds = local_provisioning_client_credentials_from_env()
    if creds is None:
        msg = (
            "MINDROOM_PROVISIONING_URL is set but local client credentials are missing. "
            "Run `mindroom connect --pair-code ...` first."
        )
        raise ValueError(msg)
    return creds


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
        async with httpx.AsyncClient(timeout=10, verify=MATRIX_SSL_VERIFY) as client:
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
