"""Borrow the Codex CLI's ChatGPT OAuth login for the Codex route.

The access token is refreshed under the CLI's file lock when it is about to expire, so MindRoom and a
running Codex CLI never write the auth file at the same time.
"""

from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx

from mindroom.file_locks import advisory_file_lock
from mindroom.model_defaults import CODEX_GPT_ALIAS, CODEX_GPT_ENDPOINT
from mindroom.providers.errors import CliLoginError

__all__ = [
    "CODEX_BASE_URL",
    "CodexAuthError",
    "borrow_codex_key",
    "codex_installation_metadata",
    "codex_session_headers",
    "normalize_codex_model_id",
]

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_REFRESH_SKEW_SECONDS = 30
_CODEX_MODEL_PREFIX = "openai-codex/"
_CODEX_MODEL_ALIASES = {CODEX_GPT_ALIAS: CODEX_GPT_ENDPOINT}
_CODEX_INSTALLATION_ID_HEADER = "x-codex-installation-id"
_CODEX_WINDOW_ID_HEADER = "x-codex-window-id"


class CodexAuthError(CliLoginError):
    """Raised when the local Codex CLI OAuth state cannot provide a usable token."""


def normalize_codex_model_id(model_id: str) -> str:
    """Return the Codex endpoint model slug from either bare or LLM-plugin-style IDs."""
    normalized = model_id.strip()
    if normalized.startswith(_CODEX_MODEL_PREFIX):
        normalized = normalized.removeprefix(_CODEX_MODEL_PREFIX)
    return _CODEX_MODEL_ALIASES.get(normalized, normalized)


def borrow_codex_key(*, codex_home: str | Path | None = None) -> tuple[str, str | None]:
    """Return a valid Codex CLI ChatGPT access token and optional account id."""
    auth_path = _codex_auth_path(codex_home=codex_home)
    _, tokens = _read_codex_auth(auth_path)
    usable_token = _usable_access_token(tokens)
    if usable_token is not None:
        return usable_token

    with advisory_file_lock(auth_path.with_name(f"{auth_path.name}.lock")):
        auth, tokens = _read_codex_auth(auth_path)
        usable_token = _usable_access_token(tokens)
        if usable_token is not None:
            return usable_token

        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            msg = "No Codex refresh token found. Run `codex login` to re-authenticate."
            raise CodexAuthError(msg)

        account_id = str(tokens["account_id"]) if tokens.get("account_id") else None
        refreshed = _refresh_codex_tokens(str(refresh_token))
        if not refreshed.get("access_token"):
            msg = "Codex token refresh response did not include an access token."
            raise CodexAuthError(msg)

        _update_tokens(tokens, refreshed)
        auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        _write_codex_auth(auth_path, auth)
        return str(tokens["access_token"]), account_id


def _codex_auth_path(*, codex_home: str | Path | None) -> Path:
    home = _codex_home_path(codex_home=codex_home)
    auth_path = home / "auth.json"
    if not auth_path.exists():
        msg = f"Codex auth file not found at {auth_path}. Run `codex login` first."
        raise CodexAuthError(msg)
    return auth_path


def _codex_home_path(*, codex_home: str | Path | None) -> Path:
    configured_home = codex_home if codex_home is not None else os.environ.get("CODEX_HOME", "~/.codex")
    return Path(configured_home).expanduser()


def _read_codex_auth(auth_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the ChatGPT login in ``auth.json`` and its tokens, which hold at least an access token."""
    with auth_path.open(encoding="utf-8") as auth_file:
        auth = json.load(auth_file)
    if auth.get("auth_mode") != "chatgpt":
        msg = "Codex auth.json must use ChatGPT OAuth auth_mode. Run `codex login` first."
        raise CodexAuthError(msg)
    tokens = auth.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        msg = "No ChatGPT access token found in Codex auth.json. Run `codex login` first."
        raise CodexAuthError(msg)
    return auth, tokens


def _usable_access_token(tokens: dict[str, Any]) -> tuple[str, str | None] | None:
    access_token = str(tokens["access_token"])
    expires_at = _jwt_exp(access_token)
    if expires_at is None or time.time() >= expires_at - _CODEX_REFRESH_SKEW_SECONDS:
        return None
    account_id = str(tokens["account_id"]) if tokens.get("account_id") else None
    return access_token, account_id


def _write_codex_auth(auth_path: Path, auth: dict[str, Any]) -> None:
    temp_path = auth_path.with_name(f"{auth_path.name}.tmp")
    temp_path.unlink(missing_ok=True)
    fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
        temp_file.write(json.dumps(auth, indent=2))
    temp_path.replace(auth_path)
    auth_path.chmod(0o600)


def _jwt_exp(token: str) -> int | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload)
        exp = json.loads(decoded).get("exp")
    except (IndexError, ValueError, json.JSONDecodeError):
        return None
    return int(exp) if isinstance(exp, int) else None


def _refresh_codex_tokens(refresh_token: str) -> dict[str, Any]:
    payload = {
        "client_id": _CODEX_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    try:
        response = httpx.post(_CODEX_REFRESH_URL, json=payload, timeout=10)
    except httpx.HTTPError as exc:
        msg = f"Codex token refresh failed: {exc}"
        raise CodexAuthError(msg) from exc

    if not response.is_success:
        error_body = response.text
        error_code = _refresh_error_code(error_body)
        if error_code in {"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"}:
            msg = f"Codex refresh token is no longer valid ({error_code}). Run `codex login` again."
        else:
            msg = f"Codex token refresh failed (HTTP {response.status_code}): {error_body}"
        raise CodexAuthError(msg, status_code=response.status_code) from None

    try:
        refreshed = response.json()
    except json.JSONDecodeError as exc:
        msg = "Codex token refresh returned invalid JSON."
        raise CodexAuthError(msg) from exc
    if not isinstance(refreshed, dict):
        msg = "Codex token refresh returned an unexpected payload."
        raise CodexAuthError(msg)
    return refreshed


def _refresh_error_code(error_body: str) -> str | None:
    try:
        error = json.loads(error_body)
    except json.JSONDecodeError:
        return None
    code = error.get("error") if isinstance(error, dict) else None
    return code if isinstance(code, str) else None


def _update_tokens(tokens: dict[str, Any], refreshed: dict[str, Any]) -> None:
    for key in ("access_token", "id_token", "refresh_token"):
        if refreshed.get(key):
            tokens[key] = refreshed[key]


def codex_session_headers(session_id: str) -> dict[str, str]:
    """Return the headers that route one conversation's requests to the same Codex backend session."""
    return {
        "session_id": session_id,
        "x-client-request-id": session_id,
        _CODEX_WINDOW_ID_HEADER: f"{session_id}:0",
    }


def codex_installation_metadata(*, codex_home: str | Path | None) -> dict[str, str]:
    """Return the ``client_metadata`` naming this Codex installation, or nothing when the CLI never wrote one."""
    installation_path = _codex_home_path(codex_home=codex_home) / "installation_id"
    if not installation_path.is_file():
        return {}
    installation_id = installation_path.read_text(encoding="utf-8").strip()
    return {_CODEX_INSTALLATION_ID_HEADER: installation_id} if installation_id else {}
