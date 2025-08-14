"""Tests for CLI functionality."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import MultiAgentOrchestrator
from mindroom.matrix.client import register_user
from mindroom.matrix.state import MatrixState

from .conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD

if TYPE_CHECKING:
    from pathlib import Path


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

        with patch("mindroom.matrix.client.matrix_client", return_value=mock_context):
            user_id = await register_user("http://localhost:8008", "test_user", TEST_PASSWORD, "Test User")

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

        with patch("mindroom.matrix.client.matrix_client", return_value=mock_context):
            # Should return the user_id even when user exists
            user_id = await register_user("http://localhost:8008", "existing_user", "test_password", "Existing User")

            assert user_id == "@existing_user:localhost"

            # Verify registration was attempted
            mock_client.register.assert_called_once()

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
            user_id="@mindroom_user_test:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@mindroom_user_test:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with (
            patch("mindroom.matrix.client.matrix_client", return_value=mock_context),
            patch("mindroom.matrix.state.MATRIX_STATE_FILE", tmp_path / "matrix_state.yaml"),
            patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008"),
        ):
            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
            await orchestrator._ensure_user_account()

            # Check that user was created
            state = MatrixState.load()

            assert "agent_user" in state.accounts  # User is stored as agent_user
            assert state.accounts["agent_user"].username == "mindroom_user"
            assert state.accounts["agent_user"].password == "user_secure_password"  # noqa: S105

            # Verify registration was called
            mock_client.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_user_account_uses_existing_valid(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Test ensuring user account when valid credentials exist."""
        mock_context, mock_client = mock_matrix_client

        # Create existing config with user stored as agent_user
        config_file = tmp_path / "matrix_state.yaml"
        state = MatrixState()
        state.add_account("agent_user", "mindroom_user", "existing_password")

        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", config_file):
            state.save()

            # Mock that user already exists when trying to register
            mock_client.register.return_value = nio.ErrorResponse(
                message="User ID already taken",
                status_code="M_USER_IN_USE",
            )
            mock_client.login.return_value = nio.LoginResponse(
                user_id="@mindroom_user:localhost",
                device_id="TEST_DEVICE",
                access_token=TEST_ACCESS_TOKEN,
            )

            with (
                patch("mindroom.matrix.client.matrix_client", return_value=mock_context),
                patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008"),
            ):
                orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
                await orchestrator._ensure_user_account()

                # Should use existing account
                result_config = MatrixState.load()
                assert result_config.accounts["agent_user"].username == "mindroom_user"
                assert result_config.accounts["agent_user"].password == "existing_password"  # noqa: S105

                # Should have tried to register (which returns M_USER_IN_USE)
                mock_client.register.assert_called_once()
                # Login is not called by create_agent_user

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
        state.add_account("agent_user", "mindroom_user", "wrong_password")

        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", config_file):
            state.save()

            # Mock failed login
            mock_client.login.return_value = nio.LoginError(
                message="Invalid username or password",
                status_code="M_FORBIDDEN",
            )

            # Mock successful registration for new account
            mock_client.register.return_value = nio.RegisterResponse(
                user_id="@mindroom_user:localhost",
                device_id="TEST_DEVICE",
                access_token=TEST_ACCESS_TOKEN,
            )
            mock_client.set_displayname.return_value = AsyncMock()

            with (
                patch("mindroom.matrix.client.matrix_client", return_value=mock_context),
                patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008"),
            ):
                orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
                await orchestrator._ensure_user_account()

                # Should have kept the existing account credentials
                # (create_agent_user doesn't regenerate passwords on login failure)
                result_config = MatrixState.load()
                assert "agent_user" in result_config.accounts
                assert result_config.accounts["agent_user"].username == "mindroom_user"
                # Password stays the same - create_agent_user reuses existing credentials
                assert result_config.accounts["agent_user"].password == "wrong_password"  # noqa: S105

                # create_agent_user doesn't login, just registers
                # Should have registered new user
                mock_client.register.assert_called_once()
