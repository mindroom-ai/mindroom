"""Tests for CLI functionality."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, _register_user
from mindroom.orchestrator import MultiAgentOrchestrator
from tests.conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_INTERNAL_USERNAME = Config().mindroom_user.username
DEFAULT_INTERNAL_DISPLAY_NAME = Config().mindroom_user.display_name


@pytest.fixture(autouse=True)
def _clear_matrix_registration_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep register-user tests deterministic unless explicitly overridden."""
    monkeypatch.delenv("MATRIX_REGISTRATION_TOKEN", raising=False)
    monkeypatch.delenv("MINDROOM_PROVISIONING_URL", raising=False)
    monkeypatch.delenv("MINDROOM_LOCAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("MINDROOM_LOCAL_CLIENT_SECRET", raising=False)


@pytest.fixture
def mock_matrix_client() -> tuple[MagicMock, AsyncMock]:
    """Create a mock matrix client context manager."""
    mock_client = AsyncMock()
    mock_context = MagicMock()
    mock_context.__aenter__.return_value = mock_client
    mock_context.__aexit__.return_value = None
    return mock_context, mock_client


class TestUserAccountManagement:
    """Test user account creation and management."""

    @pytest.mark.asyncio
    async def test_register_user_success(self, mock_matrix_client: tuple[MagicMock, AsyncMock]) -> None:
        """Test successful user registration."""
        mock_context, mock_client = mock_matrix_client

        # Mock successful registration
        mock_client.register.return_value = nio.RegisterResponse(
            user_id="@test_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with patch("mindroom.matrix.users.matrix_client", return_value=mock_context):
            user_id = await _register_user("http://localhost:8008", "test_user", TEST_PASSWORD, "Test User")

            assert user_id == "@test_user:localhost"

            # Verify registration was called
            mock_client.register.assert_called_once_with(
                username="test_user",
                password=TEST_PASSWORD,
                device_name="mindroom_agent",
            )
            # Verify display name was set
            mock_client.set_displayname.assert_called_once_with("Test User")

    @pytest.mark.asyncio
    async def test_register_user_already_exists(self, mock_matrix_client: tuple[MagicMock, AsyncMock]) -> None:
        """Test registration when user already exists."""
        mock_context, mock_client = mock_matrix_client

        # Mock user already exists error
        mock_client.register.return_value = nio.responses.RegisterErrorResponse(
            message="User ID already taken.",
            status_code="M_USER_IN_USE",
        )
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@existing_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with patch("mindroom.matrix.users.matrix_client", return_value=mock_context):
            # Should return the user_id even when user exists
            user_id = await _register_user("http://localhost:8008", "existing_user", "test_password", "Existing User")

            assert user_id == "@existing_user:localhost"

            # Verify registration was attempted
            mock_client.register.assert_called_once()
            mock_client.login.assert_called_once_with("test_password")
            mock_client.set_displayname.assert_called_once_with("Existing User")

    @pytest.mark.asyncio
    async def test_ensure_user_account_creates_new(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Test ensuring user account when none exists."""
        mock_context, mock_client = mock_matrix_client

        # Setup mocks for successful registration
        mock_client.register.return_value = nio.RegisterResponse(
            user_id=f"@{DEFAULT_INTERNAL_USERNAME}_test:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.login.return_value = nio.LoginResponse(
            user_id=f"@{DEFAULT_INTERNAL_USERNAME}_test:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with (
            patch("mindroom.matrix.users.matrix_client", return_value=mock_context),
            patch("mindroom.matrix.state.MATRIX_STATE_FILE", tmp_path / "matrix_state.yaml"),
            patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008"),
        ):
            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
            await orchestrator._ensure_user_account(Config())

            # Check that user was created
            state = MatrixState.load()

            assert INTERNAL_USER_ACCOUNT_KEY in state.accounts
            assert state.accounts[INTERNAL_USER_ACCOUNT_KEY].username == DEFAULT_INTERNAL_USERNAME
            generated_password = state.accounts[INTERNAL_USER_ACCOUNT_KEY].password
            assert generated_password
            assert generated_password != "user_secure_password"  # noqa: S105

            # Verify registration was called
            mock_client.register.assert_called_once()
            mock_client.set_displayname.assert_called_once_with(DEFAULT_INTERNAL_DISPLAY_NAME)

    @pytest.mark.asyncio
    async def test_ensure_user_account_uses_existing_valid(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Test ensuring user account when valid credentials exist."""
        mock_context, mock_client = mock_matrix_client

        # Create existing config with internal user account
        config_file = tmp_path / "matrix_state.yaml"
        state = MatrixState()
        state.add_account(INTERNAL_USER_ACCOUNT_KEY, DEFAULT_INTERNAL_USERNAME, "existing_password")

        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", config_file):
            state.save()

            # Mock that user already exists when trying to register
            mock_client.register.return_value = nio.ErrorResponse(
                message="User ID already taken",
                status_code="M_USER_IN_USE",
            )
            mock_client.login.return_value = nio.LoginResponse(
                user_id=f"@{DEFAULT_INTERNAL_USERNAME}:localhost",
                device_id="TEST_DEVICE",
                access_token=TEST_ACCESS_TOKEN,
            )

            with (
                patch("mindroom.matrix.users.matrix_client", return_value=mock_context),
                patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008"),
            ):
                orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
                await orchestrator._ensure_user_account(Config())

                # Should use existing account
                result_config = MatrixState.load()
                assert result_config.accounts[INTERNAL_USER_ACCOUNT_KEY].username == DEFAULT_INTERNAL_USERNAME
                assert result_config.accounts[INTERNAL_USER_ACCOUNT_KEY].password == "existing_password"  # noqa: S105

                # Should have tried to register (which returns M_USER_IN_USE)
                mock_client.register.assert_called_once()
                mock_client.login.assert_called_once_with("existing_password")
                mock_client.set_displayname.assert_called_once_with(DEFAULT_INTERNAL_DISPLAY_NAME)

    @pytest.mark.asyncio
    async def test_ensure_user_account_invalid_credentials(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Test ensuring user account when stored credentials are invalid."""
        mock_context, mock_client = mock_matrix_client

        # Create existing config with invalid credentials
        config_file = tmp_path / "matrix_state.yaml"
        state = MatrixState()
        state.add_account(INTERNAL_USER_ACCOUNT_KEY, DEFAULT_INTERNAL_USERNAME, "wrong_password")

        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", config_file):
            state.save()

            # Mock failed login
            mock_client.login.return_value = nio.LoginError(
                message="Invalid username or password",
                status_code="M_FORBIDDEN",
            )

            # Mock successful registration for new account
            mock_client.register.return_value = nio.RegisterResponse(
                user_id=f"@{DEFAULT_INTERNAL_USERNAME}:localhost",
                device_id="TEST_DEVICE",
                access_token=TEST_ACCESS_TOKEN,
            )
            mock_client.set_displayname.return_value = AsyncMock()

            with (
                patch("mindroom.matrix.users.matrix_client", return_value=mock_context),
                patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008"),
            ):
                orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
                await orchestrator._ensure_user_account(Config())

                # Should have kept the existing account credentials
                # (create_agent_user doesn't regenerate passwords on login failure)
                result_config = MatrixState.load()
                assert INTERNAL_USER_ACCOUNT_KEY in result_config.accounts
                assert result_config.accounts[INTERNAL_USER_ACCOUNT_KEY].username == DEFAULT_INTERNAL_USERNAME
                # Password stays the same - create_agent_user reuses existing credentials
                assert result_config.accounts[INTERNAL_USER_ACCOUNT_KEY].password == "wrong_password"  # noqa: S105

                # Should have registered new user
                mock_client.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_user_account_uses_configured_identity(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Test ensuring user account uses configured username and display name."""
        mock_context, mock_client = mock_matrix_client
        custom_config = Config(mindroom_user={"username": "alice", "display_name": "Alice Smith"})

        mock_client.register.return_value = nio.RegisterResponse(
            user_id="@alice:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with (
            patch("mindroom.matrix.users.matrix_client", return_value=mock_context),
            patch("mindroom.matrix.state.MATRIX_STATE_FILE", tmp_path / "matrix_state.yaml"),
            patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008"),
        ):
            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
            await orchestrator._ensure_user_account(custom_config)

            state = MatrixState.load()
            assert state.accounts[INTERNAL_USER_ACCOUNT_KEY].username == "alice"
            generated_password = state.accounts[INTERNAL_USER_ACCOUNT_KEY].password
            assert generated_password
            assert generated_password != "user_secure_password"  # noqa: S105
            mock_client.register.assert_called_once()
            register_call_kwargs = mock_client.register.call_args.kwargs
            assert register_call_kwargs["username"] == "alice"
            assert register_call_kwargs["password"] == generated_password
            assert register_call_kwargs["device_name"] == "mindroom_agent"
            mock_client.set_displayname.assert_called_once_with("Alice Smith")

    @pytest.mark.asyncio
    async def test_ensure_user_account_rejects_changing_existing_username(
        self,
        tmp_path: Path,
    ) -> None:
        """Internal username cannot be changed after initial account bootstrap."""
        config_file = tmp_path / "matrix_state.yaml"
        state = MatrixState()
        state.add_account(INTERNAL_USER_ACCOUNT_KEY, DEFAULT_INTERNAL_USERNAME, "existing_password")

        custom_config = Config(mindroom_user={"username": "alice", "display_name": "Alice Smith"})

        with (
            patch("mindroom.matrix.state.MATRIX_STATE_FILE", config_file),
            patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008"),
        ):
            state.save()
            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

            with pytest.raises(ValueError, match="cannot be changed"):
                await orchestrator._ensure_user_account(custom_config)


def test_mindroom_user_username_normalizes_single_leading_at() -> None:
    """Config should accept a single leading @ and normalize it to localpart form."""
    config = Config(mindroom_user={"username": "@alice", "display_name": "Alice"})
    assert config.mindroom_user.username == "alice"


def test_mindroom_user_username_rejects_multiple_at() -> None:
    """Config should reject malformed usernames with multiple @ characters."""
    with pytest.raises(ValueError, match="at most one leading @"):
        Config(mindroom_user={"username": "@@alice", "display_name": "Alice"})


def test_mindroom_user_username_rejects_invalid_characters() -> None:
    """Config should reject localparts containing disallowed characters."""
    with pytest.raises(ValueError, match="contains invalid characters"):
        Config(mindroom_user={"username": "alice smith", "display_name": "Alice"})


def test_mindroom_user_username_rejects_router_collision() -> None:
    """Internal user localpart must not collide with the router account localpart."""
    with pytest.raises(ValueError, match="conflicts with router 'router'"):
        Config(mindroom_user={"username": "mindroom_router", "display_name": "Alice"})


def test_mindroom_user_username_rejects_agent_collision() -> None:
    """Internal user localpart must not collide with configured agent localparts."""
    with pytest.raises(ValueError, match="conflicts with agent 'assistant'"):
        Config(
            agents={
                "assistant": {
                    "display_name": "Assistant",
                    "role": "Test assistant",
                    "rooms": ["test_room"],
                },
            },
            mindroom_user={"username": "mindroom_assistant", "display_name": "Alice"},
        )


def test_agent_and_team_names_must_not_overlap() -> None:
    """Agent keys and team keys must be distinct to avoid identity collisions."""
    with pytest.raises(ValueError, match="Agent and team names must be distinct"):
        Config(
            agents={
                "assistant": {
                    "display_name": "Assistant",
                    "role": "Test assistant",
                    "rooms": ["test_room"],
                },
            },
            teams={
                "assistant": {
                    "display_name": "Assistant Team",
                    "role": "Team role",
                    "agents": ["assistant"],
                    "model": "default",
                },
            },
            models={"default": {"provider": "openai", "id": "gpt-4o-mini"}},
        )
