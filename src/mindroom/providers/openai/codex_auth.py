"""Borrow the Codex CLI's ChatGPT OAuth login for the Codex route, refreshing its access token when about to expire."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mindroom.providers.errors import CliLoginError
from mindroom.providers.openai.cli_login import borrow_under_lock, post_token_refresh, write_private_json

if TYPE_CHECKING:
    import httpx

__all__ = [
    "CODEX_BASE_URL",
    "CodexAuthError",
    "borrow_codex_key",
    "codex_installation_metadata",
    "codex_session_headers",
]

CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
_CODEX_REFRESH_URL = "https://auth.openai.com/oauth/token"
_CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
_CODEX_REFRESH_SKEW_SECONDS = 30
_CODEX_INSTALLATION_ID_HEADER = "x-codex-installation-id"
_CODEX_WINDOW_ID_HEADER = "x-codex-window-id"


class CodexAuthError(CliLoginError):
    """Raised when the local Codex CLI OAuth state cannot provide a usable token."""


def borrow_codex_key(*, codex_home: str | Path | None) -> tuple[str, str | None]:
    """Return a valid Codex CLI ChatGPT access token and optional account id."""
    auth_path = _codex_auth_path(codex_home=codex_home)
    return borrow_under_lock(
        auth_path,
        usable=lambda: _usable_access_token(_read_codex_auth(auth_path)[1]),
        refresh=lambda: _refresh_codex_auth(auth_path),
    )


def _refresh_codex_auth(auth_path: Path) -> tuple[str, str | None]:
    auth, tokens = _read_codex_auth(auth_path)
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        msg = "No Codex refresh token found. Run `codex login` to re-authenticate."
        raise CodexAuthError(msg)

    refreshed = _refresh_codex_tokens(str(refresh_token))
    if not refreshed.get("access_token"):
        msg = "Codex token refresh response did not include an access token."
        raise CodexAuthError(msg)

    _update_tokens(tokens, refreshed)
    auth["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    write_private_json(auth_path, auth)
    return str(tokens["access_token"]), _account_id(tokens)


def _codex_auth_path(*, codex_home: str | Path | None) -> Path:
    home = _codex_home_path(codex_home=codex_home)
    auth_path = home / "auth.json"
    if not auth_path.exists():
        msg = f"Codex auth file not found at {auth_path}. Run `codex login` first."
        raise CodexAuthError(msg)
    return auth_path


def _codex_home_path(*, codex_home: str | Path | None) -> Path:
    return Path(codex_home if codex_home is not None else "~/.codex").expanduser()


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
    return access_token, _account_id(tokens)


def _account_id(tokens: dict[str, Any]) -> str | None:
    return str(tokens["account_id"]) if tokens.get("account_id") else None


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
    return post_token_refresh(
        _CODEX_REFRESH_URL,
        service="Codex",
        error=CodexAuthError,
        refused=_refused_refresh_message,
        json_body={"client_id": _CODEX_CLIENT_ID, "grant_type": "refresh_token", "refresh_token": refresh_token},
    )


def _refused_refresh_message(response: httpx.Response) -> str:
    error_code = _refresh_error_code(response.text)
    if error_code in {"refresh_token_expired", "refresh_token_reused", "refresh_token_invalidated"}:
        return f"Codex refresh token is no longer valid ({error_code}). Run `codex login` again."
    return f"Codex token refresh failed (HTTP {response.status_code}): {response.text}"


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
