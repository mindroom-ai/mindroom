"""Tests for matrix agent manager functionality."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from typing import TYPE_CHECKING, Self
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from uuid import UUID

import aiohttp
import httpx
import nio
import pytest
import yaml
from nio.durable import DurableSyncConfig

from mindroom import constants as constants_mod
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.matrix import _owned_session, provisioning
from mindroom.matrix import users as matrix_users
from mindroom.matrix._owned_session import MatrixCredentials
from mindroom.matrix.appservice import ManagedAccountAuth
from mindroom.matrix.client_session import PermanentMatrixStartupError
from mindroom.matrix.state import MatrixAccount, MatrixState
from mindroom.matrix.users import (
    INTERNAL_USER_AGENT_NAME,
    AgentMatrixUser,
    _register_user,
    _save_agent_credentials,
    create_agent_user,
    load_agent_user,
    login_agent_http_client,
)
from mindroom.matrix_identifiers import agent_username_localpart
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD
from tests.matrix_state_helpers import save_matrix_state

if TYPE_CHECKING:
    from pathlib import Path

    from aioresponses import aioresponses

DEFAULT_INTERNAL_USERNAME = MindRoomUserConfig().username
APPSERVICE_TOKEN = "as-secret"  # noqa: S105


def _runtime_paths(tmp_path: Path, **env: str) -> constants_mod.RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    return constants_mod.resolve_runtime_paths(config_path=config_path, process_env={**os.environ, **env})


def _recording_httpx_async_client(
    captured_requests: list[tuple[str, dict[str, object]]],
    response: httpx.Response,
) -> type[object]:
    """Build a minimal AsyncClient replacement that records POST payloads."""

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
            json: dict[str, object],
            **_: object,
        ) -> httpx.Response:
            captured_requests.append((url, json))
            return response

    return _FakeAsyncClient


def _recording_httpx_sequence_client(
    captured_requests: list[tuple[str, str, dict[str, object] | None]],
    responses: list[httpx.Response],
) -> type[object]:
    """Build a minimal AsyncClient replacement that records GET and POST requests."""

    class _FakeAsyncClient:
        def __init__(self, *_: object, **__: object) -> None:
            pass

        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str, **_: object) -> httpx.Response:
            captured_requests.append(("GET", url, None))
            return responses.pop(0)

        async def post(
            self,
            url: str,
            json: dict[str, object],
            **_: object,
        ) -> httpx.Response:
            captured_requests.append(("POST", url, json))
            return responses.pop(0)

    return _FakeAsyncClient


@pytest.fixture(autouse=True)
def _clear_matrix_registration_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep matrix registration tests deterministic unless explicitly overridden."""
    monkeypatch.delenv("MATRIX_REGISTRATION_TOKEN", raising=False)
    monkeypatch.delenv("MATRIX_REGISTRATION_SHARED_SECRET", raising=False)
    monkeypatch.delenv("MATRIX_REGISTRATION_SHARED_SECRET_FILE", raising=False)
    monkeypatch.delenv("MINDROOM_PROVISIONING_URL", raising=False)
    monkeypatch.delenv("MINDROOM_LOCAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("MINDROOM_LOCAL_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("MATRIX_MANAGED_ACCOUNT_AUTH", raising=False)
    monkeypatch.delenv("MATRIX_APPSERVICE_TOKEN", raising=False)
    monkeypatch.delenv("MATRIX_APPSERVICE_TOKEN_FILE", raising=False)


@pytest.fixture
def temp_matrix_users_file(tmp_path: Path) -> Path:
    """Create a temporary matrix_state.yaml file."""
    file_path = tmp_path / "matrix_state.yaml"
    initial_data = {
        "accounts": {
            "bot": {"username": "mindroom_bot", "password": "bot_password_123"},
            "user": {"username": DEFAULT_INTERNAL_USERNAME, "password": "user_password_123"},
        },
        "rooms": {},
    }
    with file_path.open("w") as f:
        yaml.dump(initial_data, f)
    return file_path


class TestAgentMatrixUser:
    """Test AgentMatrixUser dataclass."""

    def test_agent_matrix_user_creation(self) -> None:
        """Test creating an AgentMatrixUser instance."""
        user = AgentMatrixUser(
            agent_name="calculator",
            user_id="@mindroom_calculator:localhost",
            display_name="CalculatorAgent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )
        assert user.agent_name == "calculator"
        assert user.user_id == "@mindroom_calculator:localhost"
        assert user.display_name == "CalculatorAgent"
        assert user.password == TEST_PASSWORD
        assert user.access_token == TEST_ACCESS_TOKEN


class TestMatrixUserManagement:
    """Test matrix user management functions."""

    def test_load_matrix_users(self, temp_matrix_users_file: Path) -> None:
        """Test loading matrix users from file."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=temp_matrix_users_file.parent / "config.yaml",
            storage_path=temp_matrix_users_file.parent,
        )
        runtime_paths.config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        state = MatrixState.load(runtime_paths=runtime_paths)

        assert "bot" in state.accounts
        assert state.accounts["bot"].username == "mindroom_bot"
        assert "user" in state.accounts
        assert state.accounts["user"].username == DEFAULT_INTERNAL_USERNAME

    def test_load_matrix_users_no_file(self, tmp_path: Path) -> None:
        """Test loading matrix users when file doesn't exist."""
        runtime_paths = constants_mod.resolve_runtime_paths(
            config_path=tmp_path / "config.yaml",
            storage_path=tmp_path / "missing-root",
        )
        runtime_paths.config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
        state = MatrixState.load(runtime_paths=runtime_paths)
        assert state.accounts == {}
        assert state.rooms == {}

    def test_load_agent_user_returns_the_stored_session(self, tmp_path: Path) -> None:
        """A stored account loads with its device and token, so callers reuse the same Matrix device."""
        runtime_paths = _runtime_paths(tmp_path)
        state = MatrixState()
        state.add_account(
            "agent_calculator",
            "actual_calculator",
            TEST_PASSWORD,
            domain="matrix.example",
            device_id="DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        save_matrix_state(state, runtime_paths)

        assert load_agent_user("calculator", runtime_paths) == AgentMatrixUser(
            agent_name="calculator",
            user_id="@actual_calculator:matrix.example",
            display_name="calculator",
            password=TEST_PASSWORD,
            device_id="DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        assert load_agent_user("missing", runtime_paths) is None

    def test_save_agent_credentials(self, tmp_path: Path) -> None:
        """Saving one agent's credentials adds its account and keeps the others."""
        runtime_paths = _runtime_paths(tmp_path)
        existing = MatrixState()
        existing.add_account("bot", "bot", "pass")
        save_matrix_state(existing, runtime_paths)

        _save_agent_credentials("calculator", "mindroom_calculator", "calc_pass", runtime_paths, domain="localhost")

        accounts = MatrixState.load(runtime_paths=runtime_paths).accounts
        assert set(accounts) == {"bot", "agent_calculator"}
        assert accounts["agent_calculator"].username == "mindroom_calculator"
        assert accounts["agent_calculator"].password == "calc_pass"  # noqa: S105


class TestMatrixRegistration:
    """Test Matrix user registration functions."""

    @pytest.mark.asyncio
    async def test_register_user_success(self, tmp_path: Path) -> None:
        """Test successful user registration."""
        mock_client = AsyncMock()
        # Mock successful registration
        mock_response = MagicMock(spec=nio.RegisterResponse)
        mock_response.user_id = "@test_user:localhost"
        mock_response.access_token = "test_token"  # noqa: S105
        mock_response.device_id = "test_device"
        mock_client.register.return_value = mock_response
        mock_client.set_displayname.return_value = AsyncMock()

        runtime_paths = _runtime_paths(tmp_path)
        with patch("mindroom.matrix.users.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await _register_user(
                "http://localhost:8008",
                "test_user",
                "test_pass",
                "Test User",
                runtime_paths=runtime_paths,
            )

            assert user_id == "@test_user:localhost"
            mock_client.register.assert_awaited_once_with(
                username="test_user",
                password="test_pass",  # noqa: S106
                device_name="mindroom_agent",
            )
            mock_client.set_displayname.assert_called_once_with("Test User")
            mock_matrix_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_user_already_exists(self, tmp_path: Path) -> None:
        """Test registration when user already exists."""
        mock_client = AsyncMock()
        # Mock user already exists error
        mock_response = MagicMock(spec=nio.ErrorResponse)
        mock_response.status_code = "M_USER_IN_USE"
        mock_client.register.return_value = mock_response
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@existing_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        runtime_paths = _runtime_paths(tmp_path)
        with patch("mindroom.matrix.users.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await _register_user(
                "http://localhost:8008",
                "existing_user",
                "test_pass",
                "Existing User",
                runtime_paths=runtime_paths,
            )

            assert user_id == "@existing_user:localhost"
            mock_matrix_client.assert_called_once()
            mock_client.login.assert_called_once_with("test_pass")
            mock_client.set_displayname.assert_called_once_with("Existing User")

    @pytest.mark.asyncio
    async def test_register_user_already_exists_login_failure(self, tmp_path: Path) -> None:
        """Test registration failure when user exists but provided password is invalid."""
        mock_client = AsyncMock()
        mock_response = MagicMock(spec=nio.ErrorResponse)
        mock_response.status_code = "M_USER_IN_USE"
        mock_client.register.return_value = mock_response
        mock_client.login.return_value = MagicMock(spec=nio.LoginError)

        runtime_paths = _runtime_paths(tmp_path)
        with patch("mindroom.matrix.users.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            with pytest.raises(ValueError, match="Matrix account collision"):
                await _register_user(
                    "http://localhost:8008",
                    "existing_user",
                    "wrong_pass",
                    "Existing User",
                    runtime_paths=runtime_paths,
                )

    @pytest.mark.asyncio
    async def test_register_user_failure(self, tmp_path: Path) -> None:
        """Test registration failure."""
        mock_client = AsyncMock()
        # Mock registration failure
        mock_response = MagicMock(spec=nio.ErrorResponse)
        mock_response.status_code = "M_FORBIDDEN"
        mock_response.message = "Forbidden"
        mock_client.register.return_value = mock_response

        runtime_paths = _runtime_paths(tmp_path)
        with patch("mindroom.matrix.users.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            with pytest.raises(PermanentMatrixStartupError, match="Failed to register user"):
                await _register_user(
                    "http://localhost:8008",
                    "test_user",
                    "test_pass",
                    "Test User",
                    runtime_paths=runtime_paths,
                )

            mock_matrix_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_user_uses_registration_token_when_configured(
        self,
        tmp_path: Path,
    ) -> None:
        """When MATRIX_REGISTRATION_TOKEN is set, register via token auth flow."""
        test_pass = "test_pass"  # noqa: S105
        registration_token = "token-123"  # noqa: S105
        runtime_paths = _runtime_paths(tmp_path, MATRIX_REGISTRATION_TOKEN=registration_token)

        mock_client = AsyncMock()
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@actual_test_user:matrix.example",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()
        captured_requests: list[tuple[str, dict[str, object]]] = []

        with (
            patch(
                "mindroom.matrix.users.httpx.AsyncClient",
                _recording_httpx_async_client(
                    captured_requests,
                    httpx.Response(200, json={"user_id": "@actual_test_user:matrix.example"}),
                ),
            ),
            patch("mindroom.matrix.users.matrix_client") as mock_matrix_client,
        ):
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await _register_user(
                "http://localhost:8008",
                "test_user",
                test_pass,
                "Test User",
                runtime_paths=runtime_paths,
            )

            assert user_id == "@actual_test_user:matrix.example"
            assert captured_requests == [
                (
                    "http://localhost:8008/_matrix/client/v3/register",
                    {
                        "username": "test_user",
                        "password": test_pass,
                        "device_name": "mindroom_agent",
                        "auth": {
                            "type": "m.login.registration_token",
                            "token": registration_token,
                        },
                    },
                ),
            ]
            mock_client.register.assert_not_called()
            mock_client.register_with_token.assert_not_called()
            mock_client.login.assert_called_once_with(test_pass)
            mock_client.set_displayname.assert_called_once_with("Test User")

    @pytest.mark.asyncio
    async def test_register_user_falls_back_to_matrix_nio_token_flow_on_uiaa_challenge(
        self,
        tmp_path: Path,
    ) -> None:
        """Spec-strict homeservers should fall back to matrix-nio's interactive token flow."""
        test_pass = "test_pass"  # noqa: S105
        registration_token = "token-123"  # noqa: S105
        runtime_paths = _runtime_paths(tmp_path, MATRIX_REGISTRATION_TOKEN=registration_token)

        mock_client = AsyncMock()
        mock_client.register_with_token.return_value = nio.RegisterResponse(
            user_id="@test_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()
        captured_requests: list[tuple[str, dict[str, object]]] = []

        with (
            patch(
                "mindroom.matrix.users.httpx.AsyncClient",
                _recording_httpx_async_client(
                    captured_requests,
                    httpx.Response(
                        401,
                        json={
                            "session": "sess-123",
                            "flows": [{"stages": ["m.login.registration_token"]}],
                        },
                    ),
                ),
            ),
            patch("mindroom.matrix.users.matrix_client") as mock_matrix_client,
        ):
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await _register_user(
                "http://localhost:8008",
                "test_user",
                test_pass,
                "Test User",
                runtime_paths=runtime_paths,
            )

            assert user_id == "@test_user:localhost"
            assert captured_requests == [
                (
                    "http://localhost:8008/_matrix/client/v3/register",
                    {
                        "username": "test_user",
                        "password": test_pass,
                        "device_name": "mindroom_agent",
                        "auth": {
                            "type": "m.login.registration_token",
                            "token": registration_token,
                        },
                    },
                ),
            ]
            mock_client.register_with_token.assert_called_once_with(
                username="test_user",
                password=test_pass,
                registration_token=registration_token,
                device_name="mindroom_agent",
            )
            mock_client.login.assert_not_called()
            mock_client.set_displayname.assert_called_once_with("Test User")

    @pytest.mark.asyncio
    async def test_register_user_rejects_invalid_direct_token_user_id(
        self,
        tmp_path: Path,
    ) -> None:
        """Direct token registration must persist only valid returned Matrix IDs."""
        test_pass = "test_pass"  # noqa: S105
        runtime_paths = _runtime_paths(tmp_path, MATRIX_REGISTRATION_TOKEN="token-123")  # noqa: S106

        with (
            patch(
                "mindroom.matrix.users.httpx.AsyncClient",
                _recording_httpx_async_client(
                    [],
                    httpx.Response(200, json={"user_id": "@test_user:"}),
                ),
            ),
            pytest.raises(PermanentMatrixStartupError, match="invalid user_id"),
        ):
            await _register_user(
                "http://localhost:8008",
                "test_user",
                test_pass,
                "Test User",
                runtime_paths=runtime_paths,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig"], ids=["plain", "byte-order-mark"])
    async def test_register_user_uses_synapse_shared_secret_file_when_configured(
        self,
        tmp_path: Path,
        encoding: str,
    ) -> None:
        """Hosted Synapse instances can create bot accounts with a shared secret file, saved with or without a BOM."""
        test_pass = "test_pass"  # noqa: S105
        shared_secret_path = tmp_path / "matrix-registration-secret"
        shared_secret_path.write_text("shared-secret-value\n", encoding=encoding)
        runtime_paths = _runtime_paths(
            tmp_path,
            MATRIX_REGISTRATION_SHARED_SECRET_FILE=str(shared_secret_path),
        )
        mock_client = AsyncMock()
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@test_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()
        captured_requests: list[tuple[str, str, dict[str, object] | None]] = []

        with (
            patch(
                "mindroom.matrix.users.httpx.AsyncClient",
                _recording_httpx_sequence_client(
                    captured_requests,
                    [
                        httpx.Response(200, json={"nonce": "nonce-123"}),
                        httpx.Response(200, json={"user_id": "@test_user:localhost"}),
                    ],
                ),
            ),
            patch("mindroom.matrix.users.matrix_client") as mock_matrix_client,
        ):
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await _register_user(
                "http://localhost:8008",
                "test_user",
                test_pass,
                "Test User",
                runtime_paths=runtime_paths,
            )

        assert user_id == "@test_user:localhost"
        assert captured_requests[0] == ("GET", "http://localhost:8008/_synapse/admin/v1/register", None)
        assert captured_requests[1][0:2] == ("POST", "http://localhost:8008/_synapse/admin/v1/register")
        payload = captured_requests[1][2]
        assert payload is not None
        expected_mac = hmac.new(
            b"shared-secret-value",
            b"\x00".join([b"nonce-123", b"test_user", test_pass.encode("utf-8"), b"notadmin"]),
            hashlib.sha1,
        ).hexdigest()
        assert payload == {
            "nonce": "nonce-123",
            "username": "test_user",
            "password": test_pass,
            "admin": False,
            "mac": expected_mac,
            "displayname": "Test User",
        }
        mock_client.register.assert_not_called()
        mock_client.register_with_token.assert_not_called()
        mock_client.login.assert_called_once_with(test_pass)
        mock_client.set_displayname.assert_called_once_with("Test User")

    @pytest.mark.asyncio
    async def test_register_user_falls_back_to_shared_secret_when_unpaired_hosted_install_has_one(
        self,
        tmp_path: Path,
    ) -> None:
        """A shared secret registers agents without pairing even when a provisioning URL is configured."""
        test_pass = "test_pass"  # noqa: S105
        runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_PROVISIONING_URL="https://provisioning.example",
            MATRIX_REGISTRATION_SHARED_SECRET="shared-secret-value",  # noqa: S106
            MATRIX_REGISTRATION_TOKEN="",
            MINDROOM_LOCAL_CLIENT_ID="",
            MINDROOM_LOCAL_CLIENT_SECRET="",
        )
        mock_client = AsyncMock()
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@test_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()
        captured_requests: list[tuple[str, str, dict[str, object] | None]] = []

        with (
            patch(
                "mindroom.matrix.users.httpx.AsyncClient",
                _recording_httpx_sequence_client(
                    captured_requests,
                    [
                        httpx.Response(200, json={"nonce": "nonce-123"}),
                        httpx.Response(200, json={"user_id": "@test_user:localhost"}),
                    ],
                ),
            ),
            patch("mindroom.matrix.users.matrix_client") as mock_matrix_client,
        ):
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await _register_user(
                "http://localhost:8008",
                "test_user",
                test_pass,
                "Test User",
                runtime_paths=runtime_paths,
            )

        assert user_id == "@test_user:localhost"
        assert [request[0:2] for request in captured_requests] == [
            ("GET", "http://localhost:8008/_synapse/admin/v1/register"),
            ("POST", "http://localhost:8008/_synapse/admin/v1/register"),
        ]

    def test_unpaired_hosted_install_without_another_registration_method_must_pair(self, tmp_path: Path) -> None:
        """Missing client credentials stay a startup error when pairing is the only way to register."""
        runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_PROVISIONING_URL="https://provisioning.example",
            MATRIX_REGISTRATION_SHARED_SECRET="",
            MATRIX_REGISTRATION_SHARED_SECRET_FILE="",
            MATRIX_REGISTRATION_TOKEN="",
            MINDROOM_LOCAL_CLIENT_ID="",
            MINDROOM_LOCAL_CLIENT_SECRET="",
        )

        with pytest.raises(PermanentMatrixStartupError, match="local client credentials are missing"):
            provisioning.required_local_provisioning_client_credentials_for_registration(
                provisioning_url="https://provisioning.example",
                registration_token=None,
                runtime_paths=runtime_paths,
            )

    @staticmethod
    def _one_time_password_client(
        login_result: nio.Response | Exception | None = None,
        change_result: nio.Response | Exception | None = None,
    ) -> AsyncMock:
        """Return a Matrix client double whose login and password change succeed unless overridden."""
        if change_result is None:
            change_result = nio.ChangePasswordResponse()
            change_result.transport_response = MagicMock(spec=aiohttp.ClientResponse, status=200)
        client = AsyncMock()
        # A one-item side_effect list returns a response or raises an exception.
        client.login.side_effect = [
            login_result
            or nio.LoginResponse(
                user_id="@test_user:localhost",
                device_id="ONE_TIME_DEVICE",
                access_token=TEST_ACCESS_TOKEN,
            ),
        ]
        client.change_password.side_effect = [change_result]
        return client

    @staticmethod
    async def _register_provisioned_account(
        tmp_path: Path,
        password: str = "test_pass",  # noqa: S107
    ) -> str:
        """Register through a provisioning service that reports a created account with a one-time password."""
        runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_PROVISIONING_URL="https://provisioning.example",
            MINDROOM_LOCAL_CLIENT_ID="client-123",
            MINDROOM_LOCAL_CLIENT_SECRET="secret-123",  # noqa: S106
        )
        with (
            patch(
                "mindroom.matrix.users.provisioning.register_user_via_provisioning_service",
                new_callable=AsyncMock,
                return_value=provisioning._ProvisioningRegisterResult(
                    status="created",
                    user_id="@test_user:localhost",
                    password="one-time-pass",  # noqa: S106
                ),
            ) as mock_register,
            patch(
                "mindroom.matrix.users.provisioning_env.registration_token_from_env",
                return_value=None,
            ),
        ):
            user_id = await _register_user(
                "http://localhost:8008",
                "test_user",
                password,
                "Test User",
                runtime_paths=runtime_paths,
            )
        mock_register.assert_called_once_with(
            provisioning_url="https://provisioning.example",
            client_id="client-123",
            client_secret="secret-123",  # noqa: S106
            homeserver="http://localhost:8008",
            username="test_user",
            display_name="Test User",
        )
        return user_id

    @pytest.mark.asyncio
    async def test_register_user_uses_provisioning_service_register_agent_when_configured(
        self,
        tmp_path: Path,
    ) -> None:
        """A provisioned account's one-time password is replaced with the install's own password."""
        http_client = self._one_time_password_client()

        with patch("mindroom.matrix.users.create_matrix_http_client", return_value=http_client) as mock_http_client:
            user_id = await self._register_provisioned_account(tmp_path, password="install-pass")  # noqa: S106

        assert user_id == "@test_user:localhost"
        mock_http_client.assert_called_once_with("http://localhost:8008", ANY, "@test_user:localhost")
        http_client.login.assert_awaited_once_with("one-time-pass")
        http_client.change_password.assert_awaited_once_with(
            {
                "type": "m.login.password",
                "identifier": {"type": "m.id.user", "user": "@test_user:localhost"},
                "password": "one-time-pass",
            },
            "install-pass",
        )
        http_client.logout.assert_awaited_once_with()
        http_client.close.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_register_user_keeps_replaced_password_when_logout_fails(self, tmp_path: Path) -> None:
        """Once the password is replaced, a failed logout of the one-time session does not fail registration."""
        http_client = self._one_time_password_client()
        http_client.logout.side_effect = aiohttp.ClientPayloadError("connection lost")

        with patch("mindroom.matrix.users.create_matrix_http_client", return_value=http_client):
            user_id = await self._register_provisioned_account(tmp_path)

        assert user_id == "@test_user:localhost"
        http_client.close.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_register_user_keeps_replaced_password_when_logout_hangs(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A logout that never answers is abandoned after the timeout instead of blocking registration."""
        monkeypatch.setattr(matrix_users, "_ONE_TIME_SESSION_LOGOUT_TIMEOUT_SECONDS", 0.01)
        http_client = self._one_time_password_client()
        http_client.logout.side_effect = asyncio.Event().wait

        with patch("mindroom.matrix.users.create_matrix_http_client", return_value=http_client):
            user_id = await asyncio.wait_for(self._register_provisioned_account(tmp_path), timeout=5)

        assert user_id == "@test_user:localhost"
        http_client.close.assert_awaited_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("login_result", "change_result"),
        [
            pytest.param(nio.LoginError("Invalid password"), None, id="login-error"),
            pytest.param(aiohttp.ClientPayloadError("connection lost"), None, id="login-raises"),
            pytest.param(None, nio.ChangePasswordError("Password change is disabled"), id="change-error"),
            pytest.param(None, aiohttp.ClientPayloadError("connection lost"), id="change-raises"),
        ],
    )
    async def test_register_user_fails_startup_when_one_time_password_is_not_replaced(
        self,
        tmp_path: Path,
        login_result: nio.Response | Exception | None,
        change_result: nio.Response | Exception | None,
    ) -> None:
        """A provisioned account whose one-time password cannot be replaced is a permanent startup error."""
        http_client = self._one_time_password_client(login_result, change_result)

        with (
            patch("mindroom.matrix.users.create_matrix_http_client", return_value=http_client),
            pytest.raises(PermanentMatrixStartupError, match=r"cannot be used again\. Run `mindroom connect --force`"),
        ):
            await self._register_provisioned_account(tmp_path)

        if login_result is not None:
            http_client.change_password.assert_not_awaited()
        http_client.logout.assert_not_awaited()
        http_client.close.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_register_user_rejects_password_change_answered_by_non_json_server_error(
        self,
        tmp_path: Path,
        aioresponse: aioresponses,
    ) -> None:
        """Nio reads a non-JSON 502 as an empty successful change body, so the HTTP status must decide."""
        aioresponse.post(
            re.compile(r"http://localhost:8008/_matrix/client/v3/login.*"),
            payload={"user_id": "@test_user:localhost", "device_id": "ONE_TIME_DEVICE", "access_token": "token"},
        )
        aioresponse.post(
            re.compile(r"http://localhost:8008/_matrix/client/v3/account/password.*"),
            status=502,
            body="<html>Bad Gateway</html>",
            content_type="text/html",
        )
        aioresponse.post(re.compile(r"http://localhost:8008/_matrix/client/v3/logout.*"), payload={})

        with pytest.raises(
            PermanentMatrixStartupError,
            match=r"replacing its one-time password failed \(HTTP 502\)\. Nobody knows",
        ):
            await self._register_provisioned_account(tmp_path)

        requested_paths = [url.path for _method, url in aioresponse.requests]
        assert requested_paths == ["/_matrix/client/v3/login", "/_matrix/client/v3/account/password"]

    @pytest.mark.asyncio
    async def test_register_user_provisioning_user_in_use_logs_in_and_syncs_display(
        self,
        tmp_path: Path,
    ) -> None:
        """When provisioning reports user exists, login locally and sync display name."""
        test_pass = "test_pass"  # noqa: S105
        runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_PROVISIONING_URL="https://provisioning.example",
            MINDROOM_LOCAL_CLIENT_ID="client-123",
            MINDROOM_LOCAL_CLIENT_SECRET="secret-123",  # noqa: S106
        )

        mock_client = AsyncMock()
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@test_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with (
            patch(
                "mindroom.matrix.users.provisioning.register_user_via_provisioning_service",
                new_callable=AsyncMock,
            ) as mock_register,
            patch("mindroom.matrix.users.matrix_client") as mock_matrix_client,
            patch(
                "mindroom.matrix.users.provisioning_env.registration_token_from_env",
                return_value=None,
            ),
        ):
            mock_register.return_value = MagicMock(
                status="user_in_use",
                user_id="@test_user:localhost",
            )
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await _register_user(
                "http://localhost:8008",
                "test_user",
                test_pass,
                "Test User",
                runtime_paths=runtime_paths,
            )

            assert user_id == "@test_user:localhost"
            mock_client.login.assert_called_once_with(test_pass)
            mock_client.set_displayname.assert_called_once_with("Test User")

    @pytest.mark.asyncio
    async def test_register_user_rejects_invalid_provisioning_user_id(
        self,
        tmp_path: Path,
    ) -> None:
        """Provisioning responses must return valid Matrix IDs before state persistence."""
        test_pass = "test_pass"  # noqa: S105
        runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_PROVISIONING_URL="https://provisioning.example",
            MINDROOM_LOCAL_CLIENT_ID="client-123",
            MINDROOM_LOCAL_CLIENT_SECRET="secret-123",  # noqa: S106
        )

        with (
            patch(
                "mindroom.matrix.provisioning.httpx.AsyncClient",
                _recording_httpx_async_client(
                    [],
                    httpx.Response(200, json={"status": "created", "user_id": "@test_user:"}),
                ),
            ),
            pytest.raises(PermanentMatrixStartupError, match="invalid user_id"),
        ):
            await _register_user(
                "http://localhost:8008",
                "test_user",
                test_pass,
                "Test User",
                runtime_paths=runtime_paths,
            )

    @pytest.mark.asyncio
    async def test_register_user_provisioning_user_in_use_uses_returned_user_id(
        self,
        tmp_path: Path,
    ) -> None:
        """When provisioning reports user_in_use, login with the returned actual user ID."""
        test_pass = "test_pass"  # noqa: S105
        runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_PROVISIONING_URL="https://provisioning.example",
            MINDROOM_LOCAL_CLIENT_ID="client-123",
            MINDROOM_LOCAL_CLIENT_SECRET="secret-123",  # noqa: S106
        )

        mock_client = AsyncMock()
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@test_user:internal-matrix",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with (
            patch(
                "mindroom.matrix.users.provisioning.register_user_via_provisioning_service",
                new_callable=AsyncMock,
            ) as mock_register,
            patch("mindroom.matrix.users.matrix_client") as mock_matrix_client,
            patch(
                "mindroom.matrix.users.provisioning_env.registration_token_from_env",
                return_value=None,
            ),
            patch(
                "mindroom.matrix.users.extract_server_name_from_homeserver",
                return_value="mindroom.chat",
            ),
        ):
            mock_register.return_value = MagicMock(
                status="user_in_use",
                user_id="@test_user:internal-matrix",
            )
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await _register_user(
                "https://internal-matrix:8448",
                "test_user",
                test_pass,
                "Test User",
                runtime_paths=runtime_paths,
            )

            assert user_id == "@test_user:internal-matrix"
            mock_matrix_client.assert_called_once_with(
                "https://internal-matrix:8448",
                user_id="@test_user:internal-matrix",
                runtime_paths=runtime_paths,
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("login_response", "error"),
        [
            pytest.param(
                nio.LoginResponse(
                    user_id="@test_user:mindroom.chat",
                    device_id="TEST_DEVICE",
                    access_token=TEST_ACCESS_TOKEN,
                ),
                "Matrix login returned",
                id="other-account",
            ),
            pytest.param(nio.LoginError("Invalid password", "M_FORBIDDEN"), "Matrix account collision", id="rejected"),
        ],
    )
    async def test_register_user_provisioning_user_in_use_rejects_login_identity_mismatch(
        self,
        tmp_path: Path,
        login_response: nio.LoginResponse | nio.LoginError,
        error: str,
    ) -> None:
        """Collision login must authenticate as the exact account returned by provisioning."""
        test_pass = "test_pass"  # noqa: S105
        runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_PROVISIONING_URL="https://provisioning.example",
            MINDROOM_LOCAL_CLIENT_ID="client-123",
            MINDROOM_LOCAL_CLIENT_SECRET="secret-123",  # noqa: S106
        )

        mock_client = AsyncMock()
        mock_client.login.return_value = login_response
        mock_client.set_displayname.return_value = AsyncMock()

        with (
            patch(
                "mindroom.matrix.users.provisioning.register_user_via_provisioning_service",
                new_callable=AsyncMock,
            ) as mock_register,
            patch("mindroom.matrix.users.matrix_client") as mock_matrix_client,
            patch(
                "mindroom.matrix.users.provisioning_env.registration_token_from_env",
                return_value=None,
            ),
        ):
            mock_register.return_value = MagicMock(
                status="user_in_use",
                user_id="@test_user:internal-matrix",
            )
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            with pytest.raises(PermanentMatrixStartupError, match=error):
                await _register_user(
                    "https://internal-matrix:8448",
                    "test_user",
                    test_pass,
                    "Test User",
                    runtime_paths=runtime_paths,
                )

    @pytest.mark.asyncio
    async def test_register_user_missing_provisioning_client_credentials_is_explicit(
        self,
        tmp_path: Path,
    ) -> None:
        """Provisioning URL without local client creds should fail with actionable guidance."""
        runtime_paths = _runtime_paths(tmp_path, MINDROOM_PROVISIONING_URL="https://provisioning.example")

        with pytest.raises(PermanentMatrixStartupError, match="mindroom connect"):
            await _register_user(
                "http://localhost:8008",
                "test_user",
                "test_pass",
                "Test User",
                runtime_paths=runtime_paths,
            )

    @pytest.mark.asyncio
    async def test_register_user_via_provisioning_verifies_tls_when_matrix_ssl_verify_is_false(
        self,
        tmp_path: Path,
    ) -> None:
        """MATRIX_SSL_VERIFY covers only the homeserver, so the register-agent call that returns a password verifies TLS."""
        runtime_paths = _runtime_paths(
            tmp_path,
            MATRIX_SSL_VERIFY="false",
            MINDROOM_PROVISIONING_URL="https://provisioning.example",
            MINDROOM_LOCAL_CLIENT_ID="client-123",
            MINDROOM_LOCAL_CLIENT_SECRET="secret-123",  # noqa: S106
        )
        client_kwargs: list[dict[str, object]] = []
        fake_client = _recording_httpx_async_client(
            [],
            httpx.Response(401, json={"detail": "Invalid local client credentials"}),
        )

        def _client(**kwargs: object) -> object:
            client_kwargs.append(kwargs)
            return fake_client(**kwargs)

        with (
            patch.object(provisioning.httpx, "AsyncClient", _client),
            patch("mindroom.matrix.users.provisioning_env.registration_token_from_env", return_value=None),
            pytest.raises(PermanentMatrixStartupError, match="invalid or revoked"),
        ):
            await _register_user(
                "http://localhost:8008",
                "test_user",
                "test_pass",
                "Test User",
                runtime_paths=runtime_paths,
            )

        # httpx verifies certificates unless told otherwise.
        assert [kwargs.get("verify", True) for kwargs in client_kwargs] == [True]

    @staticmethod
    async def _register_via_provisioning_with_response(response: httpx.Response) -> None:
        with patch.object(provisioning.httpx, "AsyncClient", _recording_httpx_async_client([], response)):
            await provisioning.register_user_via_provisioning_service(
                provisioning_url="https://provisioning.example",
                client_id="client-123",
                client_secret="secret-123",  # noqa: S106
                homeserver="http://localhost:8008",
                username="mindroom_test_user_otherns",
                display_name="Test User",
            )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "response",
        [
            pytest.param(
                httpx.Response(401, json={"detail": "Invalid local client credentials"}),
                id="invalid-credentials-401",
            ),
            pytest.param(
                httpx.Response(401, content=b"<html>authentication required</html>"),
                id="proxy-401-unrelated-body",
            ),
            pytest.param(
                httpx.Response(403, json={"detail": "Connection revoked"}),
                id="connection-revoked-403",
            ),
        ],
    )
    async def test_register_user_via_provisioning_service_client_auth_failure_is_permanent(
        self,
        response: httpx.Response,
    ) -> None:
        """Any 401, and a revoked-connection 403, should ask the user to re-pair."""
        with pytest.raises(PermanentMatrixStartupError, match="invalid or revoked"):
            await self._register_via_provisioning_with_response(response)

    @pytest.mark.asyncio
    async def test_register_user_via_provisioning_service_namespace_mismatch_surfaces_detail(
        self,
    ) -> None:
        """A namespace-enforcement 403 must surface the server detail, not blame the credentials."""
        detail = "Requested username is outside this local connection namespace"
        with pytest.raises(PermanentMatrixStartupError) as excinfo:
            await self._register_via_provisioning_with_response(
                httpx.Response(403, json={"detail": detail}),
            )

        message = str(excinfo.value)
        assert detail in message
        assert "mindroom_<entity>_<namespace>" in message
        assert "MINDROOM_NAMESPACE" in message
        assert "invalid or revoked" not in message

    @pytest.mark.asyncio
    async def test_register_user_via_provisioning_service_malformed_error_body_surfaces_text(
        self,
    ) -> None:
        """A 403 with a non-JSON body should surface the raw text instead of the re-pair advice."""
        with pytest.raises(PermanentMatrixStartupError, match="policy says no") as excinfo:
            await self._register_via_provisioning_with_response(
                httpx.Response(403, content=b"policy says no"),
            )

        assert "invalid or revoked" not in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_register_user_via_provisioning_service_validation_error_redacts_request_body(
        self,
    ) -> None:
        """FastAPI 422 bodies echo the request under 'input'; the password must never reach the error."""
        body = {
            "detail": [
                {
                    "type": "missing",
                    "loc": ["body", "display_name"],
                    "msg": "Field required",
                    "input": {
                        "homeserver": "http://localhost:8008",
                        "username": "mindroom_test_user_otherns",
                        "password": "test_pass",
                    },
                },
            ],
        }
        with pytest.raises(PermanentMatrixStartupError) as excinfo:
            await self._register_via_provisioning_with_response(httpx.Response(422, json=body))

        message = str(excinfo.value)
        assert "test_pass" not in message
        assert "body.display_name: Field required" in message

    @pytest.mark.asyncio
    async def test_register_user_via_provisioning_service_homeserver_mismatch_is_permanent(
        self,
    ) -> None:
        """A deterministic 400 (homeserver mismatch) must stop startup retries and surface the detail."""
        detail = "Invalid homeserver for this provisioning service. Expected https://a, got https://b."
        with pytest.raises(PermanentMatrixStartupError, match="Expected https://a"):
            await self._register_via_provisioning_with_response(
                httpx.Response(400, json={"detail": detail}),
            )

    @pytest.mark.asyncio
    async def test_register_user_via_provisioning_service_redirect_is_permanent(
        self,
    ) -> None:
        """A redirecting provisioning URL is a config error, not something to retry forever."""
        response = httpx.Response(308, headers={"location": "https://mindroom.chat/v1/local-mindroom"})
        with pytest.raises(PermanentMatrixStartupError, match="MINDROOM_PROVISIONING_URL"):
            await self._register_via_provisioning_with_response(response)

    @pytest.mark.asyncio
    async def test_register_user_via_provisioning_service_invalid_json_is_permanent(self) -> None:
        """Invalid provisioning responses should not trigger endless retries."""
        with pytest.raises(PermanentMatrixStartupError, match="invalid JSON"):
            await self._register_via_provisioning_with_response(
                httpx.Response(200, content=b"not json"),
            )

    @pytest.mark.asyncio
    async def test_register_user_via_provisioning_service_created_without_password_is_permanent(
        self,
    ) -> None:
        """A created account is unusable without its one-time password, so a response missing it is a permanent error."""
        with pytest.raises(PermanentMatrixStartupError, match="missing one-time password"):
            await self._register_via_provisioning_with_response(
                httpx.Response(200, json={"status": "created", "user_id": "@mindroom_test_user_otherns:localhost"}),
            )

    @pytest.mark.asyncio
    async def test_register_user_missing_token_error_is_explicit(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unknown register errors should become actionable when token flow is required."""
        monkeypatch.delenv("MATRIX_REGISTRATION_TOKEN", raising=False)

        mock_client = AsyncMock()
        mock_client.register.return_value = nio.ErrorResponse("unknown error")

        with (
            patch("mindroom.matrix.users.matrix_client") as mock_matrix_client,
            patch("mindroom.matrix.users._homeserver_requires_registration_token", new_callable=AsyncMock) as mock_req,
        ):
            mock_matrix_client.return_value.__aenter__.return_value = mock_client
            mock_req.return_value = True
            runtime_paths = _runtime_paths(tmp_path)

            with pytest.raises(PermanentMatrixStartupError, match="Set MATRIX_REGISTRATION_TOKEN"):
                await _register_user(
                    "http://localhost:8008",
                    "test_user",
                    "test_pass",
                    "Test User",
                    runtime_paths=runtime_paths,
                )


class TestAgentUserCreation:
    """Test agent user creation functions."""

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users._register_user")
    @patch("mindroom.matrix.users._save_agent_credentials")
    @patch("mindroom.matrix.users._get_agent_credentials")
    async def test_create_agent_user_new(
        self,
        mock_get_creds: MagicMock,
        mock_save_creds: MagicMock,
        mock_register: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Test creating a new agent user."""
        mock_get_creds.return_value = None  # No existing credentials
        mock_register.return_value = "@actual_calculator:matrix.example"

        runtime_paths = _runtime_paths(tmp_path)
        agent_user = await create_agent_user("http://localhost:8008", "calculator", "CalculatorAgent", runtime_paths)

        assert agent_user.agent_name == "calculator"
        assert agent_user.user_id == "@actual_calculator:matrix.example"
        assert agent_user.display_name == "CalculatorAgent"
        assert agent_user.password

        mock_save_creds.assert_called_once_with(
            "calculator",
            "actual_calculator",
            agent_user.password,
            runtime_paths,
            domain="matrix.example",
            requested_username="mindroom_calculator",
        )
        mock_register.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_agent_user_appservice_mode_stores_no_password(self, tmp_path: Path) -> None:
        """Application-service accounts must not generate or persist passwords."""
        runtime_paths = _runtime_paths(
            tmp_path,
            MATRIX_MANAGED_ACCOUNT_AUTH="appservice",
            MATRIX_APPSERVICE_TOKEN=APPSERVICE_TOKEN,
        )

        with patch("mindroom.matrix.users.appservice.register_appservice_user", new_callable=AsyncMock) as register:
            register.return_value = "@mindroom_calculator:localhost"
            agent_user = await create_agent_user(
                "http://localhost:8008",
                "calculator",
                "CalculatorAgent",
                runtime_paths,
            )

        assert agent_user.password is None
        assert MatrixState.load(runtime_paths=runtime_paths).accounts["agent_calculator"].password is None
        register.assert_awaited_once_with(
            "http://localhost:8008",
            username="mindroom_calculator",
            expected_user_id="@mindroom_calculator:localhost",
            token=APPSERVICE_TOKEN,
            runtime_paths=runtime_paths,
        )

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users.matrix_client")
    @patch("mindroom.matrix.users._save_agent_credentials")
    @patch("mindroom.matrix.users._get_agent_credentials")
    async def test_create_agent_user_existing_credentials_reuses_stored_credentials(
        self,
        mock_get_creds: MagicMock,
        mock_save_creds: MagicMock,
        mock_matrix_client: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Existing credentials should be reused without re-registration."""
        mock_get_creds.return_value = MatrixAccount(username="mindroom_calculator", password="existing_pass")  # noqa: S106

        runtime_paths = _runtime_paths(tmp_path)
        agent_user = await create_agent_user("http://localhost:8008", "calculator", "CalculatorAgent", runtime_paths)

        assert agent_user.password == "existing_pass"  # noqa: S105
        assert agent_user.device_id is None
        assert agent_user.access_token is None
        mock_save_creds.assert_not_called()  # Should not save again
        mock_matrix_client.assert_not_called()

    @pytest.mark.asyncio
    async def test_create_agent_user_rejects_stored_account_without_password(self, tmp_path: Path) -> None:
        """Password auth refuses a stored account that has no password instead of registering again."""
        runtime_paths = _runtime_paths(tmp_path)
        state = MatrixState()
        state.add_account("agent_calculator", "mindroom_calculator", None)
        save_matrix_state(state, runtime_paths)

        with pytest.raises(PermanentMatrixStartupError, match="credentials for calculator are incomplete"):
            await create_agent_user("http://localhost:8008", "calculator", "CalculatorAgent", runtime_paths)

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users._register_user")
    @patch("mindroom.matrix.users.matrix_client")
    @patch("mindroom.matrix.users._save_agent_credentials")
    @patch("mindroom.matrix.users._get_agent_credentials")
    async def test_create_agent_user_existing_credentials_preserves_session_fields(
        self,
        mock_get_creds: MagicMock,
        mock_save_creds: MagicMock,
        mock_matrix_client: MagicMock,
        mock_register: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Existing credentials should preserve stored session fields."""
        mock_get_creds.return_value = MatrixAccount(
            username="mindroom_calculator",
            password="stale_pass",  # noqa: S106
            device_id="stored_device",
            access_token="stored_token",  # noqa: S106
        )

        runtime_paths = _runtime_paths(tmp_path)
        agent_user = await create_agent_user("http://localhost:8008", "calculator", "CalculatorAgent", runtime_paths)

        assert agent_user.password == "stale_pass"  # noqa: S105
        assert agent_user.device_id == "stored_device"
        assert agent_user.access_token == "stored_token"  # noqa: S105
        mock_save_creds.assert_not_called()
        mock_matrix_client.assert_not_called()
        mock_register.assert_not_called()

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users._register_user")
    async def test_create_internal_user_rejects_config_username_change_from_persisted_state(
        self,
        mock_register: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """The internal user's account-creation username request is immutable after account creation."""
        runtime_paths = _runtime_paths(tmp_path)
        state = MatrixState.load(runtime_paths=runtime_paths)
        state.add_account(
            "agent_user",
            "old_internal",
            "existing_pass",
            requested_username="old_internal",
            domain="localhost",
        )
        save_matrix_state(state, runtime_paths)

        with pytest.raises(PermanentMatrixStartupError, match=r"mindroom_user\.username cannot be changed"):
            await create_agent_user(
                "http://localhost:8008",
                INTERNAL_USER_AGENT_NAME,
                "MindRoomUser",
                runtime_paths,
                username="new_internal",
            )

        mock_register.assert_not_called()

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users._register_user")
    async def test_create_internal_user_legacy_state_uses_actual_username_as_original_request(
        self,
        mock_register: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """An account predating requested_username reuses only its persisted actual username."""
        runtime_paths = _runtime_paths(tmp_path)
        state_file = constants_mod.matrix_state_file(runtime_paths=runtime_paths)
        state_file.parent.mkdir(parents=True)
        state_file.write_text(
            """accounts:
  agent_user:
    username: legacy_internal
    password: existing_pass
    domain: localhost
    device_id: DEVICE
    access_token: stored-token
rooms: {}
space_room_id: null
""",
            encoding="utf-8",
        )

        reused = await create_agent_user(
            "http://localhost:8008",
            INTERNAL_USER_AGENT_NAME,
            "MindRoomUser",
            runtime_paths,
            username="legacy_internal",
        )

        assert reused.user_id == "@legacy_internal:localhost"
        assert reused.password == "existing_pass"  # noqa: S105
        assert reused.device_id == "DEVICE"
        assert reused.access_token == "stored-token"  # noqa: S105
        with pytest.raises(PermanentMatrixStartupError, match=r"mindroom_user\.username cannot be changed"):
            await create_agent_user(
                "http://localhost:8008",
                INTERNAL_USER_AGENT_NAME,
                "MindRoomUser",
                runtime_paths,
                username="different_internal",
            )
        mock_register.assert_not_awaited()

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users._register_user")
    @patch("mindroom.matrix.users._get_agent_credentials")
    async def test_create_internal_user_allows_persisted_actual_username_drift(
        self,
        mock_get_creds: MagicMock,
        mock_register: AsyncMock,
        tmp_path: Path,
    ) -> None:
        """Hosted provisioning may return an actual username different from the account-creation request."""
        mock_get_creds.return_value = MatrixAccount(
            username="actual_internal",
            password="existing_pass",  # noqa: S106
            requested_username=DEFAULT_INTERNAL_USERNAME,
            domain="matrix.example",
        )

        runtime_paths = _runtime_paths(tmp_path)
        agent_user = await create_agent_user(
            "http://localhost:8008",
            INTERNAL_USER_AGENT_NAME,
            "MindRoomUser",
            runtime_paths,
            username=DEFAULT_INTERNAL_USERNAME,
        )

        assert agent_user.user_id == "@actual_internal:matrix.example"
        assert agent_user.password == "existing_pass"  # noqa: S105
        mock_register.assert_not_called()

    @pytest.mark.parametrize(
        ("colliding_entity_name", "agents", "teams"),
        [
            ("router", {}, {}),
            ("general", {"general": {"display_name": "GeneralAgent"}}, {}),
            (
                "helpers",
                {"general": {"display_name": "GeneralAgent"}},
                {
                    "helpers": {
                        "display_name": "HelpersTeam",
                        "role": "Coordinate helper agents",
                        "agents": ["general"],
                    },
                },
            ),
        ],
    )
    @pytest.mark.asyncio
    async def test_startup_rejects_internal_user_generated_entity_localpart_collision_before_writes(
        self,
        tmp_path: Path,
        colliding_entity_name: str,
        agents: dict[str, dict[str, object]],
        teams: dict[str, dict[str, object]],
    ) -> None:
        """Fresh startup must reject generated proposal collisions before persisting any account."""
        runtime_paths = _runtime_paths(
            tmp_path,
            MINDROOM_NAMESPACE="",
            MINDROOM_STORAGE_PATH=str(tmp_path / "mindroom_data"),
        )
        constants_mod.matrix_state_file(runtime_paths=runtime_paths).unlink(missing_ok=True)
        mindroom_username = agent_username_localpart(colliding_entity_name, runtime_paths=runtime_paths)
        config = Config.validate_with_runtime(
            {
                "models": {"default": {"provider": "openai", "id": "test-model"}},
                "agents": agents,
                "teams": teams,
                "mindroom_user": {
                    "username": mindroom_username,
                    "display_name": "MindRoomUser",
                },
            },
            runtime_paths,
        )
        orchestrator = _MultiAgentOrchestrator(runtime_paths, config=config)
        create = AsyncMock()

        with (
            patch("mindroom.orchestration.accounts.create_agent_user", new=create),
            pytest.raises(PermanentMatrixStartupError, match="localpart collision"),
        ):
            await orchestrator.initialize()

        create.assert_not_awaited()
        assert MatrixState.load(runtime_paths=runtime_paths).accounts == {}


class TestAgentLogin:
    """Test agent login functionality."""

    _PROFILE_NAME_URL = re.compile(r"http://localhost:8008/_matrix/client/v3/profile/.+/displayname$")

    @staticmethod
    def _internal_user(*, password: str | None = TEST_PASSWORD, **session: str) -> AgentMatrixUser:
        return AgentMatrixUser(
            agent_name=INTERNAL_USER_AGENT_NAME,
            user_id="@mindroom_user:localhost",
            display_name="MindRoomUser",
            password=password,
            **session,
        )

    @pytest.mark.asyncio
    async def test_http_login_reuses_the_saved_session_without_a_crypto_store(
        self,
        tmp_path: Path,
        aioresponse: aioresponses,
    ) -> None:
        """A saved session the server accepts is reused, on a client that never opens a crypto store."""
        aioresponse.get(self._PROFILE_NAME_URL, payload={"displayname": "MindRoomUser"})
        agent_user = self._internal_user(device_id="SAVED", access_token="saved-token")  # noqa: S106
        runtime_paths = _runtime_paths(tmp_path)
        restored = MatrixCredentials("@mindroom_user:localhost", "SAVED", "saved-token")
        with (
            patch("mindroom.matrix.users.restore_credentials", new=AsyncMock(return_value=restored)) as restore,
            patch("mindroom.matrix.users.login_password_credentials", new=AsyncMock()) as password_login,
        ):
            client = await login_agent_http_client("http://localhost:8008", agent_user, runtime_paths)

        try:
            assert (client.user_id, client.device_id, client.access_token) == (
                "@mindroom_user:localhost",
                "SAVED",
                "saved-token",
            )
            assert client.store_path is None
            assert client.config.encryption_enabled is False
        finally:
            await client.close()
        restore.assert_awaited_once_with(
            "http://localhost:8008",
            "@mindroom_user:localhost",
            "SAVED",
            "saved-token",
            runtime_paths,
        )
        password_login.assert_not_awaited()
        assert [method for method, _url in aioresponse.requests] == ["GET"]

    @pytest.mark.asyncio
    async def test_http_login_replaces_a_rejected_session_on_the_saved_device(
        self,
        tmp_path: Path,
        aioresponse: aioresponses,
    ) -> None:
        """A session the server rejects, such as one signed out from another client, falls back to password login."""
        aioresponse.get(self._PROFILE_NAME_URL, payload={"displayname": "MindRoomUser"})
        agent_user = self._internal_user(device_id="SAVED", access_token="revoked-token")  # noqa: S106
        runtime_paths = _runtime_paths(tmp_path)
        renewed = MatrixCredentials("@mindroom_user:localhost", "SAVED", "new-token")
        with (
            patch(
                "mindroom.matrix.users.restore_credentials",
                new=AsyncMock(side_effect=PermanentMatrixStartupError("M_UNKNOWN_TOKEN")),
            ),
            patch(
                "mindroom.matrix.users.login_password_credentials",
                new=AsyncMock(return_value=renewed),
            ) as password_login,
        ):
            client = await login_agent_http_client("http://localhost:8008", agent_user, runtime_paths)
        await client.close()

        password_login.assert_awaited_once_with(
            "http://localhost:8008",
            "@mindroom_user:localhost",
            TEST_PASSWORD,
            runtime_paths,
            device_id="SAVED",
        )
        assert client.access_token == "new-token"  # noqa: S105
        account = MatrixState.load(runtime_paths=runtime_paths).accounts["agent_user"]
        assert (account.username, account.domain, account.device_id, account.access_token) == (
            "mindroom_user",
            "localhost",
            "SAVED",
            "new-token",
        )

    @pytest.mark.asyncio
    async def test_http_login_in_appservice_mode_needs_no_password_and_sets_the_profile_name(
        self,
        tmp_path: Path,
        aioresponse: aioresponses,
    ) -> None:
        """Application-service registration sets no profile name, so the first login sets the configured one."""
        aioresponse.get(self._PROFILE_NAME_URL, payload={"displayname": "mindroom_user"})
        aioresponse.put(self._PROFILE_NAME_URL, payload={})
        agent_user = self._internal_user(password=None)
        runtime_paths = _runtime_paths(
            tmp_path,
            MATRIX_MANAGED_ACCOUNT_AUTH="appservice",
            MATRIX_APPSERVICE_TOKEN=APPSERVICE_TOKEN,
        )
        with patch(
            "mindroom.matrix.users.appservice.login_appservice_credentials",
            new=AsyncMock(return_value=("@mindroom_user:localhost", "NEW", "new-token")),
        ) as login_as:
            client = await login_agent_http_client("http://localhost:8008", agent_user, runtime_paths)
        await client.close()

        login_as.assert_awaited_once_with(
            "http://localhost:8008",
            user_id="@mindroom_user:localhost",
            token=APPSERVICE_TOKEN,
            runtime_paths=runtime_paths,
            device_id=None,
        )
        account = MatrixState.load(runtime_paths=runtime_paths).accounts["agent_user"]
        assert account.password is None
        assert (account.device_id, account.access_token) == ("NEW", "new-token")
        [profile_update] = [calls for (method, _url), calls in aioresponse.requests.items() if method == "PUT"]
        assert json.loads(profile_update[0].kwargs["data"]) == {"displayname": "MindRoomUser"}

    @pytest.mark.asyncio
    async def test_http_login_failure_persists_nothing(self, tmp_path: Path) -> None:
        """A failed password login raises and leaves the saved account untouched."""
        agent_user = self._internal_user()
        runtime_paths = _runtime_paths(tmp_path)
        with (
            patch(
                "mindroom.matrix.users.login_password_credentials",
                new=AsyncMock(side_effect=ValueError("Failed to login @mindroom_user:localhost")),
            ),
            pytest.raises(ValueError, match="Failed to login"),
        ):
            await login_agent_http_client("http://localhost:8008", agent_user, runtime_paths)

        assert MatrixState.load(runtime_paths=runtime_paths).accounts == {}

    @pytest.mark.asyncio
    async def test_login_agent_owned_session_gets_credentials_before_factory(
        self,
        tmp_path: Path,
    ) -> None:
        """Password HTTP closes before the sole owned-client factory begins."""
        agent_user = AgentMatrixUser(
            agent_name="calculator",
            user_id="@mindroom_calculator:localhost",
            display_name="CalculatorAgent",
            password=TEST_PASSWORD,
        )
        runtime_paths = _runtime_paths(tmp_path)
        generation = UUID("22222222-2222-4222-8222-222222222222")
        config = DurableSyncConfig(sync_timeout_ms=30_000)
        credentials = _owned_session.MatrixCredentials(
            agent_user.user_id,
            "AGENTDEVICE",
            "access-token",
        )
        owned_client = MagicMock(
            user_id=credentials.user_id,
            device_id=credentials.device_id,
            access_token=credentials.access_token,
            get_displayname=AsyncMock(return_value=nio.ProfileGetDisplayNameResponse("CalculatorAgent")),
            set_displayname=AsyncMock(),
        )
        opened = MagicMock(client=owned_client, session=AsyncMock())
        consumer_store = object()
        order: list[str] = []

        async def password_credentials(*_args: object, **_kwargs: object) -> object:
            order.append("credentials_closed")
            return credentials

        async def open_owned(*_args: object, persist_credentials: object, **_kwargs: object) -> object:
            assert order == ["credentials_closed"]
            order.append("owned_opened")
            persist_credentials(owned_client)
            return opened

        with (
            patch(
                "mindroom.matrix.users.appservice.resolve_managed_account_auth",
                return_value=ManagedAccountAuth(mode="password"),
            ),
            patch(
                "mindroom.matrix.users.login_password_credentials",
                side_effect=password_credentials,
            ) as get_credentials,
            patch(
                "mindroom.matrix.users.open_owned_matrix_session",
                side_effect=open_owned,
            ) as open_session,
            patch("mindroom.matrix.users._persist_authenticated_agent_session") as persist,
            patch(
                "mindroom.matrix.users.ensure_agent_cross_signing",
                new=AsyncMock(),
            ) as cross_signing,
        ):
            result = await matrix_users.login_agent_owned_session(
                "http://localhost:8008",
                agent_user,
                runtime_paths,
                consumer_store=consumer_store,
                new_consumer_generation=generation,
                config=config,
            )

        assert result is opened
        assert order == ["credentials_closed", "owned_opened"]
        get_credentials.assert_awaited_once_with(
            "http://localhost:8008",
            agent_user.user_id,
            TEST_PASSWORD,
            runtime_paths,
            device_id=None,
        )
        open_session.assert_awaited_once_with(
            "http://localhost:8008",
            credentials,
            runtime_paths,
            consumer_store=consumer_store,
            new_consumer_generation=generation,
            config=config,
            persist_credentials=ANY,
        )
        persist.assert_called_once()
        cross_signing.assert_awaited_once_with(owned_client, agent_user)
        owned_client.set_displayname.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restored_owned_session_updates_a_changed_display_name(self, tmp_path: Path) -> None:
        """A display name changed in the config reaches the Matrix profile, though the session is restored."""
        agent_user = AgentMatrixUser(
            agent_name="calculator",
            user_id="@mindroom_calculator:localhost",
            display_name="Calculator Two",
            password=TEST_PASSWORD,
            device_id="AGENTDEVICE",
            access_token="access-token",  # noqa: S106
        )
        credentials = _owned_session.MatrixCredentials(agent_user.user_id, "AGENTDEVICE", "access-token")
        owned_client = MagicMock(
            user_id=credentials.user_id,
            device_id=credentials.device_id,
            get_displayname=AsyncMock(return_value=nio.ProfileGetDisplayNameResponse("Calculator One")),
            set_displayname=AsyncMock(),
        )
        opened = MagicMock(client=owned_client, session=AsyncMock())

        with (
            patch(
                "mindroom.matrix.users.appservice.resolve_managed_account_auth",
                return_value=ManagedAccountAuth(mode="password"),
            ),
            patch("mindroom.matrix.users.olm_store_exists", return_value=True),
            patch("mindroom.matrix.users.restore_credentials", new=AsyncMock(return_value=credentials)),
            patch("mindroom.matrix.users.login_password_credentials", new=AsyncMock()) as password_login,
            patch("mindroom.matrix.users.open_owned_matrix_session", new=AsyncMock(return_value=opened)),
            patch("mindroom.matrix.users.ensure_agent_cross_signing", new=AsyncMock()),
        ):
            result = await matrix_users.login_agent_owned_session(
                "http://localhost:8008",
                agent_user,
                _runtime_paths(tmp_path),
                consumer_store=object(),
                new_consumer_generation=UUID("33333333-3333-4333-8333-333333333333"),
                config=DurableSyncConfig(sync_timeout_ms=30_000),
            )

        assert result is opened
        password_login.assert_not_awaited()
        owned_client.set_displayname.assert_awaited_once_with("Calculator Two")
