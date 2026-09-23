"""Kimi model support via the Kimi Code CLI OAuth state."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from agno.utils.http import get_default_async_client, get_default_sync_client
from openai import AsyncOpenAI, OpenAI

from mindroom.file_locks import advisory_file_lock
from mindroom.model_defaults import KIMI_K3
from mindroom.openai_models import MindRoomOpenAIChat

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agno.run.agent import RunOutput
    from agno.run.team import TeamRunOutput
    from pydantic import BaseModel

_KIMI_BASE_URL = "https://api.kimi.com/coding/v1"
_KIMI_REFRESH_URL = "https://auth.kimi.com/api/oauth/token"
_KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"
_KIMI_REFRESH_SKEW_SECONDS = 30
_KIMI_MODEL_PREFIX = "kimi-code/"
_KIMI_CREDENTIALS_RELATIVE_PATH = Path("credentials") / "kimi-code.json"


class _KimiAuthError(ValueError):
    """Raised when the local Kimi Code CLI OAuth state cannot provide a usable token."""


def normalize_kimi_model_id(model_id: str) -> str:
    """Return the Kimi endpoint model slug from either bare or CLI-config-style IDs."""
    normalized = model_id.strip()
    if normalized.startswith(_KIMI_MODEL_PREFIX):
        normalized = normalized.removeprefix(_KIMI_MODEL_PREFIX)
    return normalized


def _borrow_kimi_token(*, kimi_home: str | Path | None = None) -> str:
    """Return a valid Kimi Code CLI OAuth access token, refreshing it when expired."""
    credentials_path = _kimi_credentials_path(kimi_home=kimi_home)
    credentials = _read_kimi_credentials(credentials_path)

    usable_token = _usable_access_token(credentials)
    if usable_token is not None:
        return usable_token

    with advisory_file_lock(credentials_path.with_name(f"{credentials_path.name}.lock")):
        credentials = _read_kimi_credentials(credentials_path)
        usable_token = _usable_access_token(credentials)
        if usable_token is not None:
            return usable_token

        refresh_token = credentials.get("refresh_token")
        if not refresh_token:
            msg = "No Kimi Code refresh token found. Run `kimi` and `/login` to re-authenticate."
            raise _KimiAuthError(msg)

        refreshed = _refresh_kimi_tokens(str(refresh_token))
        if not refreshed.get("access_token"):
            msg = "Kimi token refresh response did not include an access token."
            raise _KimiAuthError(msg)

        _update_credentials(credentials, refreshed)
        _write_kimi_credentials(credentials_path, credentials)
        return str(credentials["access_token"])


def _kimi_credentials_path(*, kimi_home: str | Path | None) -> Path:
    credentials_path = _kimi_home_path(kimi_home=kimi_home) / _KIMI_CREDENTIALS_RELATIVE_PATH
    if not credentials_path.exists():
        msg = f"Kimi Code credentials not found at {credentials_path}. Run `kimi` and `/login` first."
        raise _KimiAuthError(msg)
    return credentials_path


def _kimi_home_path(*, kimi_home: str | Path | None) -> Path:
    configured_home = kimi_home if kimi_home is not None else os.environ.get("KIMI_CODE_HOME", "~/.kimi-code")
    return Path(configured_home).expanduser()


def _read_kimi_credentials(credentials_path: Path) -> dict[str, Any]:
    with credentials_path.open(encoding="utf-8") as credentials_file:
        credentials = json.load(credentials_file)
    if not isinstance(credentials, dict) or not credentials.get("access_token"):
        msg = "No Kimi Code access token found in credentials. Run `kimi` and `/login` first."
        raise _KimiAuthError(msg)
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
        raise _KimiAuthError(msg) from exc

    if not response.is_success:
        msg = (
            f"Kimi token refresh failed (HTTP {response.status_code}): {response.text}. "
            "Run `kimi` and `/login` to re-authenticate if the refresh token expired."
        )
        raise _KimiAuthError(msg) from None

    try:
        refreshed = response.json()
    except json.JSONDecodeError as exc:
        msg = "Kimi token refresh returned invalid JSON."
        raise _KimiAuthError(msg) from exc
    if not isinstance(refreshed, dict):
        msg = "Kimi token refresh returned an unexpected payload."
        raise _KimiAuthError(msg)
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


@dataclass
class KimiChat(MindRoomOpenAIChat):
    """Agno Chat model backed by the local Kimi Code CLI OAuth credentials."""

    id: str = KIMI_K3
    name: str = "KimiChat"
    provider: str = "Kimi"
    base_url: str = _KIMI_BASE_URL
    kimi_home: str | None = None
    prompt_cache_key: str | None = None

    def __post_init__(self) -> None:
        """Normalize CLI-config-style model IDs before Agno uses the model id."""
        self.id = normalize_kimi_model_id(self.id)
        super().__post_init__()
        if self.role_map is None:
            # Agno maps system messages to OpenAI's newer "developer" role, which
            # the Kimi Code endpoint rejects; keep the classic "system" role.
            self.role_map = {**self.default_role_map, "system": "system"}

    def get_request_params(
        self,
        response_format: dict[Any, Any] | type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        run_response: RunOutput | TeamRunOutput | None = None,
    ) -> dict[str, Any]:
        """Pin requests to a stable prompt-cache namespace like the Kimi Code CLI does."""
        request_params = super().get_request_params(
            response_format=response_format,
            tools=tools,
            tool_choice=tool_choice,
            run_response=run_response,
        )
        if self.prompt_cache_key:
            request_params.setdefault("prompt_cache_key", self.prompt_cache_key)
        return request_params

    def _get_client_params(self) -> dict[str, Any]:
        base_params = {
            "api_key": _borrow_kimi_token(kimi_home=self.kimi_home),
            "organization": self.organization,
            "base_url": self.base_url,
            "timeout": self.timeout,
            "max_retries": self.max_retries,
            "default_headers": self.default_headers,
            "default_query": self.default_query,
        }
        client_params = {key: value for key, value in base_params.items() if value is not None}
        if self.client_params:
            client_params.update(self.client_params)
        return client_params

    def get_client(self) -> OpenAI:
        """Return a fresh sync client so expired Kimi tokens are refreshed between requests."""
        client_params = self._get_client_params()
        client_params["http_client"] = (
            self.http_client if isinstance(self.http_client, httpx.Client) else get_default_sync_client()
        )
        return OpenAI(**client_params)

    def get_async_client(self) -> AsyncOpenAI:
        """Return a fresh async client so expired Kimi tokens are refreshed between requests."""
        client_params = self._get_client_params()
        client_params["http_client"] = (
            self.http_client if isinstance(self.http_client, httpx.AsyncClient) else get_default_async_client()
        )
        return AsyncOpenAI(**client_params)
