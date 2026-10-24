"""Borrow the Kimi Code CLI's OAuth login for the Kimi route.

The access token is refreshed under the CLI's file lock when it is about to expire, so MindRoom and a
running Kimi Code CLI never write the credentials file at the same time.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from mindroom.file_locks import advisory_file_lock
from mindroom.providers.errors import CliLoginError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["KIMI_BASE_URL", "KimiAuthError", "borrow_kimi_token", "normalize_kimi_model_id"]

KIMI_BASE_URL = "https://api.kimi.com/coding/v1"
_KIMI_REFRESH_URL = "https://auth.kimi.com/api/oauth/token"
_KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
_KIMI_REFRESH_SKEW_SECONDS = 30
_KIMI_MODEL_PREFIX = "kimi-code/"
_KIMI_CREDENTIALS_RELATIVE_PATH = Path("credentials") / "kimi-code.json"


class KimiAuthError(CliLoginError):
    """Raised when the local Kimi Code CLI OAuth state cannot provide a usable token."""


def normalize_kimi_model_id(model_id: str) -> str:
    """Return the Kimi endpoint model slug from either bare or CLI-config-style IDs."""
    return model_id.strip().removeprefix(_KIMI_MODEL_PREFIX)


def borrow_kimi_token(*, kimi_home: str | Path | None = None) -> str:
    """Return a valid Kimi Code CLI OAuth access token, refreshing it when expired."""
    credentials_path = _kimi_credentials_path(kimi_home=kimi_home)
    if (usable_token := _usable_access_token(_read_kimi_credentials(credentials_path))) is not None:
        return usable_token

    with advisory_file_lock(credentials_path.with_name(f"{credentials_path.name}.lock")):
        credentials = _read_kimi_credentials(credentials_path)
        if (usable_token := _usable_access_token(credentials)) is not None:
            return usable_token

        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            msg = "No Kimi Code refresh token found. Run `kimi` and `/login` to re-authenticate."
            raise KimiAuthError(msg)

        refreshed = _refresh_kimi_tokens(str(refresh_token))
        if not refreshed.get("access_token"):
            msg = "Kimi token refresh response did not include an access token."
            raise KimiAuthError(msg)

        _update_credentials(credentials, refreshed)
        _write_kimi_credentials(credentials_path, credentials)
        return str(credentials["access_token"])


def _kimi_credentials_path(*, kimi_home: str | Path | None) -> Path:
    credentials_path = _kimi_home_path(kimi_home=kimi_home) / _KIMI_CREDENTIALS_RELATIVE_PATH
    if not credentials_path.exists():
        msg = f"Kimi Code credentials not found at {credentials_path}. Run `kimi` and `/login` first."
        raise KimiAuthError(msg)
    return credentials_path


def _kimi_home_path(*, kimi_home: str | Path | None) -> Path:
    configured_home = kimi_home if kimi_home is not None else os.environ.get("KIMI_CODE_HOME", "~/.kimi-code")
    return Path(configured_home).expanduser()


def _read_kimi_credentials(credentials_path: Path) -> dict[str, Any]:
    with credentials_path.open(encoding="utf-8") as credentials_file:
        credentials = json.load(credentials_file)
    if not isinstance(credentials, dict) or not credentials.get("access_token"):
        msg = "No Kimi Code access token found in credentials. Run `kimi` and `/login` first."
        raise KimiAuthError(msg)
    return credentials


def _usable_access_token(credentials: Mapping[str, Any]) -> str | None:
    access_token = str(credentials["access_token"])
    expires_at = credentials.get("expires_at")
    if not isinstance(expires_at, int | float) or time.time() >= expires_at - _KIMI_REFRESH_SKEW_SECONDS:
        return None
    return access_token


def _write_kimi_credentials(credentials_path: Path, credentials: dict[str, Any]) -> None:
    temp_path = credentials_path.with_name(f"{credentials_path.name}.tmp")
    temp_path.unlink(missing_ok=True)
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        temp_file.write(json.dumps(credentials, indent=2))
    temp_path.replace(credentials_path)
    credentials_path.chmod(0o600)


def _refresh_kimi_tokens(refresh_token: str) -> dict[str, Any]:
    payload = {
        "client_id": _KIMI_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    try:
        response = httpx.post(_KIMI_REFRESH_URL, data=payload, timeout=10)
    except httpx.HTTPError as exc:
        msg = f"Kimi token refresh failed: {exc}"
        raise KimiAuthError(msg) from exc

    if not response.is_success:
        msg = (
            f"Kimi token refresh failed (HTTP {response.status_code}): {response.text}. "
            "Run `kimi` and `/login` to re-authenticate if the refresh token expired."
        )
        raise KimiAuthError(msg, status_code=response.status_code) from None

    try:
        refreshed = response.json()
    except json.JSONDecodeError as exc:
        msg = "Kimi token refresh returned invalid JSON."
        raise KimiAuthError(msg) from exc
    if not isinstance(refreshed, dict):
        msg = "Kimi token refresh returned an unexpected payload."
        raise KimiAuthError(msg)
    return refreshed


def _update_credentials(credentials: dict[str, Any], refreshed: dict[str, Any]) -> None:
    for key in ("access_token", "refresh_token", "token_type", "scope"):
        if refreshed.get(key):
            credentials[key] = refreshed[key]
    if isinstance(refreshed.get("expires_at"), int | float):
        credentials["expires_at"] = refreshed["expires_at"]
    elif isinstance(refreshed.get("expires_in"), int | float):
        credentials["expires_in"] = refreshed["expires_in"]
        credentials["expires_at"] = int(time.time()) + int(refreshed["expires_in"])
