"""Tests for passwordless Matrix application-service authentication."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Self
from unittest.mock import patch

import httpx
import pytest

from mindroom import constants
from mindroom.matrix.appservice import (
    ManagedAccountAuth,
    login_appservice_credentials,
    register_appservice_user,
    resolve_managed_account_auth,
)
from mindroom.matrix.client_session import PermanentMatrixStartupError

if TYPE_CHECKING:
    from pathlib import Path

APPSERVICE_TOKEN = "as-secret"  # noqa: S105


def _runtime_paths(tmp_path: Path, **env: str) -> constants.RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    return constants.resolve_runtime_paths(config_path=config_path, process_env={**os.environ, **env})


def _recording_client(
    captured: list[tuple[str, dict[str, str], dict[str, object]]],
    response: httpx.Response,
) -> type[object]:
    class _FakeAsyncClient:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def post(
            self,
            url: str,
            *,
            headers: dict[str, str],
            json: dict[str, object],
        ) -> httpx.Response:
            captured.append((url, headers, json))
            return response

    return _FakeAsyncClient


def test_resolve_managed_account_auth_requires_explicit_appservice_mode(tmp_path: Path) -> None:
    """A configured secret must not silently change the selected auth method."""
    runtime_paths = _runtime_paths(tmp_path, MATRIX_APPSERVICE_TOKEN=APPSERVICE_TOKEN)

    with pytest.raises(PermanentMatrixStartupError, match="MATRIX_MANAGED_ACCOUNT_AUTH"):
        resolve_managed_account_auth(runtime_paths)


def test_resolve_managed_account_auth_names_the_configured_file_variable(tmp_path: Path) -> None:
    """The password-mode conflict error names the variable that actually provided the token."""
    token_file = tmp_path / "appservice-token"
    token_file.write_text(f"{APPSERVICE_TOKEN}\n", encoding="utf-8")
    runtime_paths = _runtime_paths(tmp_path, MATRIX_APPSERVICE_TOKEN_FILE=str(token_file))

    with pytest.raises(PermanentMatrixStartupError, match="MATRIX_APPSERVICE_TOKEN_FILE is set"):
        resolve_managed_account_auth(runtime_paths)


def test_resolve_managed_account_auth_rejects_empty_token_file(tmp_path: Path) -> None:
    """An empty mounted secret file is a misconfiguration, not a missing variable."""
    token_file = tmp_path / "appservice-token"
    token_file.write_text("\n", encoding="utf-8")
    runtime_paths = _runtime_paths(
        tmp_path,
        MATRIX_MANAGED_ACCOUNT_AUTH="appservice",
        MATRIX_APPSERVICE_TOKEN_FILE=str(token_file),
    )

    with pytest.raises(PermanentMatrixStartupError, match="empty file"):
        resolve_managed_account_auth(runtime_paths)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"], ids=["plain", "byte-order-mark"])
def test_resolve_managed_account_auth_reads_token_file(tmp_path: Path, encoding: str) -> None:
    """Appservice tokens can come from mounted secret files, saved with or without a byte order mark."""
    token_file = tmp_path / "appservice-token"
    token_file.write_text(f"{APPSERVICE_TOKEN}\n", encoding=encoding)
    runtime_paths = _runtime_paths(
        tmp_path,
        MATRIX_MANAGED_ACCOUNT_AUTH="appservice",
        MATRIX_APPSERVICE_TOKEN_FILE=str(token_file),
    )

    auth = resolve_managed_account_auth(runtime_paths)

    assert auth.mode == "appservice"
    assert auth.appservice_token == APPSERVICE_TOKEN


def test_blank_exported_appservice_settings_do_not_hide_the_env_file_values(tmp_path: Path) -> None:
    """A blank export, as a compose passthrough of an unset variable produces, counts as unset."""
    (tmp_path / ".env").write_text(
        f"MATRIX_MANAGED_ACCOUNT_AUTH=appservice\nMATRIX_APPSERVICE_TOKEN={APPSERVICE_TOKEN}\n",
        encoding="utf-8",
    )
    runtime_paths = _runtime_paths(tmp_path, MATRIX_MANAGED_ACCOUNT_AUTH="", MATRIX_APPSERVICE_TOKEN="")

    assert resolve_managed_account_auth(runtime_paths) == ManagedAccountAuth("appservice", APPSERVICE_TOKEN)


@pytest.mark.asyncio
async def test_register_appservice_user_uses_passwordless_spec_flow(tmp_path: Path) -> None:
    """Registration uses bearer auth and never sends a password."""
    captured: list[tuple[str, dict[str, str], dict[str, object]]] = []
    response = httpx.Response(200, json={"user_id": "@mindroom_agent:example.com"})
    runtime_paths = _runtime_paths(tmp_path)

    with patch(
        "mindroom.matrix.appservice.httpx.AsyncClient",
        _recording_client(captured, response),
    ):
        user_id = await register_appservice_user(
            "https://matrix.example.com",
            username="mindroom_agent",
            expected_user_id="@mindroom_agent:example.com",
            token=APPSERVICE_TOKEN,
            runtime_paths=runtime_paths,
        )

    assert user_id == "@mindroom_agent:example.com"
    assert captured == [
        (
            "https://matrix.example.com/_matrix/client/v3/register",
            {"Authorization": f"Bearer {APPSERVICE_TOKEN}"},
            {
                "type": "m.login.application_service",
                "username": "mindroom_agent",
                "inhibit_login": True,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_register_appservice_user_adopts_server_assigned_user_id(tmp_path: Path) -> None:
    """The server's user ID wins when it differs from the locally derived one."""
    response = httpx.Response(200, json={"user_id": "@mindroom_agent:example.com"})

    with patch(
        "mindroom.matrix.appservice.httpx.AsyncClient",
        _recording_client([], response),
    ):
        user_id = await register_appservice_user(
            "https://matrix.example.com",
            username="mindroom_agent",
            expected_user_id="@mindroom_agent:matrix.internal.cluster",
            token=APPSERVICE_TOKEN,
            runtime_paths=_runtime_paths(tmp_path),
        )

    assert user_id == "@mindroom_agent:example.com"


@pytest.mark.asyncio
async def test_register_appservice_user_rejects_malformed_user_id(tmp_path: Path) -> None:
    """A structurally invalid returned user ID must fail permanently, not retry forever."""
    response = httpx.Response(200, json={"user_id": "@broken"})

    with (
        patch(
            "mindroom.matrix.appservice.httpx.AsyncClient",
            _recording_client([], response),
        ),
        pytest.raises(PermanentMatrixStartupError, match="invalid user ID"),
    ):
        await register_appservice_user(
            "https://matrix.example.com",
            username="mindroom_agent",
            expected_user_id="@mindroom_agent:example.com",
            token=APPSERVICE_TOKEN,
            runtime_paths=_runtime_paths(tmp_path),
        )


@pytest.mark.asyncio
async def test_register_appservice_user_treats_existing_account_as_registered(tmp_path: Path) -> None:
    """M_USER_IN_USE means the account already exists and registration is idempotent."""
    response = httpx.Response(400, json={"errcode": "M_USER_IN_USE", "error": "User ID already taken."})

    with patch(
        "mindroom.matrix.appservice.httpx.AsyncClient",
        _recording_client([], response),
    ):
        user_id = await register_appservice_user(
            "https://matrix.example.com",
            username="mindroom_agent",
            expected_user_id="@mindroom_agent:example.com",
            token=APPSERVICE_TOKEN,
            runtime_paths=_runtime_paths(tmp_path),
        )

    assert user_id == "@mindroom_agent:example.com"


@pytest.mark.asyncio
async def test_appservice_credentials_come_from_one_login_request(tmp_path: Path) -> None:
    """Login returns the per-user device credentials from a single spec login request."""
    captured: list[tuple[str, dict[str, str], dict[str, object]]] = []
    response = httpx.Response(
        200,
        json={
            "user_id": "@mindroom_agent:example.com",
            "access_token": "device-token",
            "device_id": "DEVICE",
        },
    )

    with patch(
        "mindroom.matrix.appservice.httpx.AsyncClient",
        _recording_client(captured, response),
    ):
        credentials = await login_appservice_credentials(
            "https://matrix.example.com",
            user_id="@mindroom_agent:example.com",
            token=APPSERVICE_TOKEN,
            runtime_paths=_runtime_paths(tmp_path),
        )

    assert credentials == (
        "@mindroom_agent:example.com",
        "DEVICE",
        "device-token",
    )
    assert [payload for _url, _headers, payload in captured] == [
        {
            "type": "m.login.application_service",
            "identifier": {"type": "m.id.user", "user": "@mindroom_agent:example.com"},
            "initial_device_display_name": "MindRoom",
        },
    ]


@pytest.mark.asyncio
async def test_register_appservice_user_rejects_invalid_success_json(tmp_path: Path) -> None:
    """A malformed successful registration response should become an actionable startup error."""
    response = httpx.Response(200, content=b"not-json")

    with (
        patch(
            "mindroom.matrix.appservice.httpx.AsyncClient",
            _recording_client([], response),
        ),
        pytest.raises(PermanentMatrixStartupError, match="registration returned invalid JSON"),
    ):
        await register_appservice_user(
            "https://matrix.example.com",
            username="mindroom_agent",
            expected_user_id="@mindroom_agent:example.com",
            token=APPSERVICE_TOKEN,
            runtime_paths=_runtime_paths(tmp_path),
        )


@pytest.mark.asyncio
async def test_login_appservice_credentials_rejects_invalid_success_json(tmp_path: Path) -> None:
    """A malformed successful login response should become an actionable startup error."""
    response = httpx.Response(200, content=b"not-json")

    with (
        patch(
            "mindroom.matrix.appservice.httpx.AsyncClient",
            _recording_client([], response),
        ),
        pytest.raises(PermanentMatrixStartupError, match="login returned invalid JSON"),
    ):
        await login_appservice_credentials(
            "https://matrix.example.com",
            user_id="@mindroom_agent:example.com",
            token=APPSERVICE_TOKEN,
            runtime_paths=_runtime_paths(tmp_path),
        )


@pytest.mark.asyncio
async def test_appservice_error_never_includes_token(tmp_path: Path) -> None:
    """Authentication failures must not leak the appservice bearer token."""
    response = httpx.Response(401, json={"errcode": "M_UNKNOWN_TOKEN", "error": "Unknown token"})

    with (
        patch(
            "mindroom.matrix.appservice.httpx.AsyncClient",
            _recording_client([], response),
        ),
        pytest.raises(PermanentMatrixStartupError) as excinfo,
    ):
        await login_appservice_credentials(
            "https://matrix.example.com",
            user_id="@mindroom_agent:example.com",
            token=APPSERVICE_TOKEN,
            runtime_paths=_runtime_paths(tmp_path),
        )

    assert "M_UNKNOWN_TOKEN" in str(excinfo.value)
    assert APPSERVICE_TOKEN not in str(excinfo.value)


@pytest.mark.asyncio
async def test_appservice_rate_limit_remains_retryable(tmp_path: Path) -> None:
    """Rate limiting should not bypass the orchestrator's startup retry loop."""
    response = httpx.Response(429, json={"errcode": "M_LIMIT_EXCEEDED", "error": "Slow down"})

    with (
        patch(
            "mindroom.matrix.appservice.httpx.AsyncClient",
            _recording_client([], response),
        ),
        pytest.raises(ValueError, match="M_LIMIT_EXCEEDED") as excinfo,
    ):
        await login_appservice_credentials(
            "https://matrix.example.com",
            user_id="@mindroom_agent:example.com",
            token=APPSERVICE_TOKEN,
            runtime_paths=_runtime_paths(tmp_path),
        )

    assert not isinstance(excinfo.value, PermanentMatrixStartupError)
