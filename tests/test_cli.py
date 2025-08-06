"""Tests for CLI functionality."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.cli import _ensure_user_account
from mindroom.matrix import MatrixState


@pytest.fixture
def mock_matrix_client():
    """Create a mock matrix client context manager."""
    mock_client = AsyncMock()
    mock_context = MagicMock()
    mock_context.__aenter__.return_value = mock_client
    mock_context.__aexit__.return_value = None
    return mock_context, mock_client


class TestUserAccountManagement:
    """Test user account creation and management."""

    @pytest.mark.asyncio
    async def test_register_user_success(self, mock_matrix_client) -> None:
        """Test successful user registration."""
        from mindroom.matrix.client import register_user

        mock_context, mock_client = mock_matrix_client

        # Mock successful registration
        mock_client.register.return_value = nio.RegisterResponse(
            user_id="@test_user:localhost", device_id="TEST_DEVICE", access_token="test_token"
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with patch("mindroom.matrix.client.matrix_client", return_value=mock_context):
            user_id = await register_user("http://localhost:8008", "test_user", "test_password", "Test User")

            assert user_id == "@test_user:localhost"

            # Verify registration was called
            mock_client.register.assert_called_once_with(
                username="test_user", password="test_password", device_name="mindroom_agent"
            )
            # Verify display name was set
            mock_client.set_displayname.assert_called_once_with("Test User")

    @pytest.mark.asyncio
    async def test_register_user_already_exists(self, mock_matrix_client) -> None:
        """Test registration when user already exists."""
        from mindroom.matrix.client import register_user

        mock_context, mock_client = mock_matrix_client

        # Mock user already exists error
        mock_client.register.return_value = nio.responses.RegisterErrorResponse(
            message="User ID already taken.", status_code="M_USER_IN_USE"
        )

        with patch("mindroom.matrix.client.matrix_client", return_value=mock_context):
            # Should return the user_id even when user exists
            user_id = await register_user("http://localhost:8008", "existing_user", "test_password", "Existing User")

            assert user_id == "@existing_user:localhost"

            # Verify registration was attempted
            mock_client.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_user_account_creates_new(self, tmp_path: Path, mock_matrix_client) -> None:
        """Test ensuring user account when none exists."""
        mock_context, mock_client = mock_matrix_client

        # Setup mocks for successful registration
        mock_client.register.return_value = nio.RegisterResponse(
            user_id="@mindroom_user_test:localhost", device_id="TEST_DEVICE", access_token="test_token"
        )
        mock_client.login.return_value = nio.LoginResponse(
            user_id="@mindroom_user_test:localhost", device_id="TEST_DEVICE", access_token="test_token"
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with (
            patch("mindroom.cli.matrix_client", return_value=mock_context),
            patch("mindroom.matrix.client.matrix_client", return_value=mock_context),
            patch("mindroom.matrix.state.MATRIX_STATE_FILE", tmp_path / "matrix_state.yaml"),
        ):
            state = await _ensure_user_account()

            # Check that user was created
            assert "user" in state.accounts
            assert state.accounts["user"].username == "mindroom_user"
            assert state.accounts["user"].password.startswith("mindroom_password_")

            # Verify registration was called
            mock_client.register.assert_called_once()

    @pytest.mark.asyncio
    async def test_ensure_user_account_uses_existing_valid(self, tmp_path: Path, mock_matrix_client) -> None:
        """Test ensuring user account when valid credentials exist."""
        mock_context, mock_client = mock_matrix_client

        # Create existing config
        config_file = tmp_path / "matrix_state.yaml"
        state = MatrixState()
        state.add_account("user", "existing_user", "existing_password")

        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", config_file):
            state.save()

            # Mock successful login with existing credentials
            mock_client.login.return_value = nio.LoginResponse(
                user_id="@existing_user:localhost", device_id="TEST_DEVICE", access_token="test_token"
            )

            with patch("mindroom.cli.matrix_client", return_value=mock_context):
                result_config = await _ensure_user_account()

                # Should use existing account
                assert result_config.accounts["user"].username == "existing_user"
                assert result_config.accounts["user"].password == "existing_password"

                # Should have tried to login
                mock_client.login.assert_called_once_with(password="existing_password")
                # Should not register new user
                mock_client.register.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_user_account_invalid_credentials(self, tmp_path: Path, mock_matrix_client) -> None:
        """Test ensuring user account when stored credentials are invalid."""
        mock_context, mock_client = mock_matrix_client

        # Create existing config with invalid credentials
        config_file = tmp_path / "matrix_state.yaml"
        state = MatrixState()
        state.add_account("user", "invalid_user", "wrong_password")

        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", config_file):
            state.save()

            # Mock failed login
            mock_client.login.return_value = nio.LoginError(
                message="Invalid username or password", status_code="M_FORBIDDEN"
            )

            # Mock successful registration for new account
            mock_client.register.return_value = nio.RegisterResponse(
                user_id="@mindroom_user_new:localhost", device_id="TEST_DEVICE", access_token="test_token"
            )
            mock_client.set_displayname.return_value = AsyncMock()

            with (
                patch("mindroom.cli.matrix_client", return_value=mock_context),
                patch("mindroom.matrix.client.matrix_client", return_value=mock_context),
            ):
                result_config = await _ensure_user_account()

                # Should have created new account
                assert "user" in result_config.accounts
                assert result_config.accounts["user"].username != "invalid_user"
                assert result_config.accounts["user"].username == "mindroom_user"

                # Should have tried old credentials first
                assert mock_client.login.call_count >= 1
                # Should have registered new user
                mock_client.register.assert_called_once()
