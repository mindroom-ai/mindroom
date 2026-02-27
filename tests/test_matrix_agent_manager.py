"""Tests for matrix agent manager functionality."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import yaml

from mindroom.config import Config
from mindroom.matrix.client import register_user
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import (
    INTERNAL_USER_AGENT_NAME,
    AgentMatrixUser,
    create_agent_user,
    ensure_all_agent_users,
    get_agent_credentials,
    login_agent_user,
    save_agent_credentials,
)

from .conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_INTERNAL_USERNAME = Config().mindroom_user.username


@pytest.fixture(autouse=True)
def _clear_matrix_registration_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep matrix registration tests deterministic unless explicitly overridden."""
    monkeypatch.delenv("MATRIX_REGISTRATION_TOKEN", raising=False)
    monkeypatch.delenv("MINDROOM_PROVISIONING_URL", raising=False)
    monkeypatch.delenv("MINDROOM_LOCAL_CLIENT_ID", raising=False)
    monkeypatch.delenv("MINDROOM_LOCAL_CLIENT_SECRET", raising=False)


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


@pytest.fixture
def mock_agent_config() -> dict:
    """Mock agent configuration."""
    return {
        "agents": {
            "calculator": {"display_name": "CalculatorAgent"},
            "general": {"display_name": "GeneralAgent"},
        },
    }


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
        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", temp_matrix_users_file):
            state = MatrixState.load()

        assert "bot" in state.accounts
        assert state.accounts["bot"].username == "mindroom_bot"
        assert "user" in state.accounts
        assert state.accounts["user"].username == DEFAULT_INTERNAL_USERNAME

    def test_load_matrix_users_no_file(self, tmp_path: Path) -> None:
        """Test loading matrix users when file doesn't exist."""
        missing_file = tmp_path / "missing_matrix_state.yaml"
        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", missing_file):
            state = MatrixState.load()
        assert state.accounts == {}
        assert state.rooms == {}

    def test_save_matrix_users(self, tmp_path: Path) -> None:
        """Test saving matrix users to file."""
        file_path = tmp_path / "test_users.yaml"

        with patch("mindroom.matrix.state.MATRIX_STATE_FILE", file_path):
            state = MatrixState()
            state.add_account("agent_test", "mindroom_test", "test_pass")
            state.save()

        # Verify the file was written correctly
        with file_path.open() as f:
            saved_data = yaml.safe_load(f)
        assert "accounts" in saved_data
        assert "agent_test" in saved_data["accounts"]
        assert saved_data["accounts"]["agent_test"]["username"] == "mindroom_test"

    @patch("mindroom.matrix.state.MatrixState.load")
    def test_get_agent_credentials(self, mock_load: MagicMock) -> None:
        """Test getting agent credentials."""
        mock_state = MatrixState()
        mock_state.add_account("agent_calculator", "mindroom_calculator", "calc_pass")
        mock_load.return_value = mock_state

        creds = get_agent_credentials("calculator")
        assert creds is not None
        assert creds["username"] == "mindroom_calculator"
        assert creds["password"] == "calc_pass"  # noqa: S105

        # Test non-existent agent
        creds = get_agent_credentials("nonexistent")
        assert creds is None

    @patch("mindroom.matrix.state.MatrixState.save")
    @patch("mindroom.matrix.state.MatrixState.load")
    def test_save_agent_credentials(self, mock_load: MagicMock, mock_save: MagicMock) -> None:
        """Test saving agent credentials."""
        mock_state = MatrixState()
        mock_state.add_account("bot", "bot", "pass")
        mock_load.return_value = mock_state

        save_agent_credentials("calculator", "mindroom_calculator", "calc_pass")

        # Verify the account was added
        assert "agent_calculator" in mock_state.accounts
        assert mock_state.accounts["agent_calculator"].username == "mindroom_calculator"
        assert mock_state.accounts["agent_calculator"].password == "calc_pass"  # noqa: S105
        mock_save.assert_called_once()


class TestMatrixRegistration:
    """Test Matrix user registration functions."""

    @pytest.mark.asyncio
    async def test_register_user_success(self) -> None:
        """Test successful user registration."""
        mock_client = AsyncMock()
        # Mock successful registration
        mock_response = MagicMock(spec=nio.RegisterResponse)
        mock_response.user_id = "@test_user:localhost"
        mock_response.access_token = "test_token"  # noqa: S105
        mock_response.device_id = "test_device"
        mock_client.register.return_value = mock_response
        mock_login_response = MagicMock(spec=nio.LoginResponse)
        mock_client.login.return_value = mock_login_response
        mock_client.set_displayname.return_value = AsyncMock()

        with patch("mindroom.matrix.client.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await register_user("http://localhost:8008", "test_user", "test_pass", "Test User")

            assert user_id == "@test_user:localhost"
            mock_client.register.assert_called_once()
            mock_client.set_displayname.assert_called_once_with("Test User")
            mock_matrix_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_user_already_exists(self) -> None:
        """Test registration when user already exists."""
        mock_client = AsyncMock()
        # Mock user already exists error
        mock_response = MagicMock(spec=nio.ErrorResponse)
        mock_response.status_code = "M_USER_IN_USE"
        mock_client.register.return_value = mock_response
        mock_client.login.return_value = MagicMock(spec=nio.LoginResponse)
        mock_client.set_displayname.return_value = AsyncMock()

        with patch("mindroom.matrix.client.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await register_user("http://localhost:8008", "existing_user", "test_pass", "Existing User")

            assert user_id == "@existing_user:localhost"
            mock_matrix_client.assert_called_once()
            mock_client.login.assert_called_once_with("test_pass")
            mock_client.set_displayname.assert_called_once_with("Existing User")

    @pytest.mark.asyncio
    async def test_register_user_already_exists_login_failure(self) -> None:
        """Test registration failure when user exists but provided password is invalid."""
        mock_client = AsyncMock()
        mock_response = MagicMock(spec=nio.ErrorResponse)
        mock_response.status_code = "M_USER_IN_USE"
        mock_client.register.return_value = mock_response
        mock_client.login.return_value = MagicMock(spec=nio.LoginError)

        with patch("mindroom.matrix.client.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            with pytest.raises(ValueError, match="Login failed for existing user"):
                await register_user("http://localhost:8008", "existing_user", "wrong_pass", "Existing User")

    @pytest.mark.asyncio
    async def test_register_user_failure(self) -> None:
        """Test registration failure."""
        mock_client = AsyncMock()
        # Mock registration failure
        mock_response = MagicMock()
        mock_response.status_code = "M_FORBIDDEN"
        mock_client.register.return_value = mock_response

        with patch("mindroom.matrix.client.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            with pytest.raises(ValueError, match="Failed to register user"):
                await register_user("http://localhost:8008", "test_user", "test_pass", "Test User")

            mock_matrix_client.assert_called_once()

    @pytest.mark.asyncio
    async def test_register_user_uses_registration_token_when_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When MATRIX_REGISTRATION_TOKEN is set, register via token auth flow."""
        monkeypatch.setenv("MATRIX_REGISTRATION_TOKEN", "token-123")

        mock_client = AsyncMock()
        mock_client._send.return_value = nio.RegisterResponse(
            user_id="@test_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with patch("mindroom.matrix.client.matrix_client") as mock_matrix_client:
            mock_matrix_client.return_value.__aenter__.return_value = mock_client

            user_id = await register_user("http://localhost:8008", "test_user", "test_pass", "Test User")

            assert user_id == "@test_user:localhost"
            mock_client._send.assert_called_once()
            mock_client.register.assert_not_called()
            mock_client.set_displayname.assert_called_once_with("Test User")

    @pytest.mark.asyncio
    async def test_register_user_uses_provisioning_service_token_when_configured(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When provisioning client creds are set, fetch token and register via token auth."""
        monkeypatch.setenv("MINDROOM_PROVISIONING_URL", "https://provisioning.example")
        monkeypatch.setenv("MINDROOM_LOCAL_CLIENT_ID", "client-123")
        client_secret = "secret-123"  # noqa: S105
        monkeypatch.setenv("MINDROOM_LOCAL_CLIENT_SECRET", client_secret)

        mock_client = AsyncMock()
        mock_client._send.return_value = nio.RegisterResponse(
            user_id="@test_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        with (
            patch("mindroom.matrix.client.matrix_client") as mock_matrix_client,
            patch(
                "mindroom.matrix.client._fetch_registration_token_from_provisioning",
                new_callable=AsyncMock,
            ) as mock_fetch,
        ):
            mock_matrix_client.return_value.__aenter__.return_value = mock_client
            mock_fetch.return_value = "issued-token"

            user_id = await register_user("http://localhost:8008", "test_user", "test_pass", "Test User")

            assert user_id == "@test_user:localhost"
            mock_fetch.assert_called_once_with(
                provisioning_url="https://provisioning.example",
                client_id="client-123",
                client_secret=client_secret,
            )
            mock_client._send.assert_called_once()
            mock_client.register.assert_not_called()
            mock_client.set_displayname.assert_called_once_with("Test User")

    @pytest.mark.asyncio
    async def test_register_user_missing_provisioning_client_credentials_is_explicit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Provisioning URL without local client creds should fail with actionable guidance."""
        monkeypatch.setenv("MINDROOM_PROVISIONING_URL", "https://provisioning.example")
        monkeypatch.delenv("MINDROOM_LOCAL_CLIENT_ID", raising=False)
        monkeypatch.delenv("MINDROOM_LOCAL_CLIENT_SECRET", raising=False)

        with pytest.raises(ValueError, match="mindroom connect --pair-code"):
            await register_user("http://localhost:8008", "test_user", "test_pass", "Test User")

    @pytest.mark.asyncio
    async def test_register_user_missing_token_error_is_explicit(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Unknown register errors should become actionable when token flow is required."""
        monkeypatch.delenv("MATRIX_REGISTRATION_TOKEN", raising=False)

        mock_client = AsyncMock()
        mock_client.register.return_value = nio.ErrorResponse("unknown error")

        with (
            patch("mindroom.matrix.client.matrix_client") as mock_matrix_client,
            patch("mindroom.matrix.client._homeserver_requires_registration_token", new_callable=AsyncMock) as mock_req,
        ):
            mock_matrix_client.return_value.__aenter__.return_value = mock_client
            mock_req.return_value = True

            with pytest.raises(ValueError, match="Set MATRIX_REGISTRATION_TOKEN"):
                await register_user("http://localhost:8008", "test_user", "test_pass", "Test User")


class TestAgentUserCreation:
    """Test agent user creation functions."""

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users.register_user")
    @patch("mindroom.matrix.users.save_agent_credentials")
    @patch("mindroom.matrix.users.get_agent_credentials")
    async def test_create_agent_user_new(
        self,
        mock_get_creds: MagicMock,
        mock_save_creds: MagicMock,
        mock_register: AsyncMock,
    ) -> None:
        """Test creating a new agent user."""
        mock_get_creds.return_value = None  # No existing credentials
        mock_register.return_value = "@mindroom_calculator:localhost"

        agent_user = await create_agent_user("http://localhost:8008", "calculator", "CalculatorAgent")

        assert agent_user.agent_name == "calculator"
        assert agent_user.user_id == "@mindroom_calculator:localhost"
        assert agent_user.display_name == "CalculatorAgent"
        assert agent_user.password

        mock_save_creds.assert_called_once()
        mock_register.assert_called_once()

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users.register_user")
    @patch("mindroom.matrix.users.save_agent_credentials")
    @patch("mindroom.matrix.users.get_agent_credentials")
    async def test_create_agent_user_existing(
        self,
        mock_get_creds: MagicMock,
        mock_save_creds: MagicMock,
        mock_register: AsyncMock,
    ) -> None:
        """Test creating agent user with existing credentials."""
        mock_get_creds.return_value = {
            "username": "mindroom_calculator",
            "password": "existing_pass",
        }
        mock_register.return_value = "@mindroom_calculator:localhost"

        agent_user = await create_agent_user("http://localhost:8008", "calculator", "CalculatorAgent")

        assert agent_user.password == "existing_pass"  # noqa: S105
        mock_save_creds.assert_not_called()  # Should not save again
        mock_register.assert_called_once()  # Still tries to register/verify

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users.register_user")
    @patch("mindroom.matrix.users.get_agent_credentials")
    async def test_create_internal_user_rejects_username_change(
        self,
        mock_get_creds: MagicMock,
        mock_register: AsyncMock,
    ) -> None:
        """Internal username cannot change once credentials already exist."""
        mock_get_creds.return_value = {
            "username": DEFAULT_INTERNAL_USERNAME,
            "password": "existing_pass",
        }

        with pytest.raises(ValueError, match="cannot be changed"):
            await create_agent_user(
                "http://localhost:8008",
                INTERNAL_USER_AGENT_NAME,
                "MindRoomUser",
                username="alice_internal",
            )

        mock_register.assert_not_called()


class TestAgentLogin:
    """Test agent login functionality."""

    @pytest.mark.asyncio
    async def test_login_agent_user_success(self) -> None:
        """Test successful agent login."""
        agent_user = AgentMatrixUser(
            agent_name="calculator",
            user_id="@mindroom_calculator:localhost",
            display_name="CalculatorAgent",
            password=TEST_PASSWORD,
        )

        with patch("mindroom.matrix.users.login") as mock_login:
            mock_client = AsyncMock()
            mock_client.access_token = "new_token"  # noqa: S105
            mock_login.return_value = mock_client

            client = await login_agent_user("http://localhost:8008", agent_user)

            assert client == mock_client
            assert agent_user.access_token == "new_token"  # noqa: S105
            mock_login.assert_called_once_with("http://localhost:8008", agent_user.user_id, agent_user.password)

    @pytest.mark.asyncio
    async def test_login_agent_user_failure(self) -> None:
        """Test failed agent login."""
        agent_user = AgentMatrixUser(
            agent_name="calculator",
            user_id="@mindroom_calculator:localhost",
            display_name="CalculatorAgent",
            password=TEST_PASSWORD,
        )

        with patch("mindroom.matrix.users.login") as mock_login:
            # Mock failed login
            mock_login.side_effect = ValueError("Failed to login @mindroom_calculator:localhost: Login error")

            with pytest.raises(ValueError, match="Failed to login"):
                await login_agent_user("http://localhost:8008", agent_user)


class TestEnsureAllAgentUsers:
    """Test ensuring all agents have user accounts."""

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users.create_agent_user")
    async def test_ensure_all_agent_users(
        self,
        mock_create_user: AsyncMock,
    ) -> None:
        """Test ensuring all configured agents have users."""
        # Load real configuration
        config = Config.from_yaml()

        # Mock user creation - router is created first, then configured agents
        mock_users = []
        # Router user
        mock_users.append(AgentMatrixUser("router", "@mindroom_router:localhost", "RouterAgent", "router_pass"))
        # Create mock users for all configured agents
        for agent_name in config.agents:
            user_id = f"@mindroom_{agent_name}:localhost"
            display_name = config.agents[agent_name].display_name or f"{agent_name.title()}Agent"
            mock_users.append(AgentMatrixUser(agent_name, user_id, display_name, f"pass_{agent_name}"))
        # Create mock users for all configured teams
        for team_name in config.teams:
            user_id = f"@mindroom_{team_name}:localhost"
            display_name = config.teams[team_name].display_name or f"{team_name.title()}Team"
            mock_users.append(AgentMatrixUser(team_name, user_id, display_name, f"pass_{team_name}"))

        mock_create_user.side_effect = mock_users

        agent_users = await ensure_all_agent_users("http://localhost:8008", config)

        # Should have router + all configured agents + all configured teams
        expected_count = 1 + len(config.agents) + len(config.teams)
        assert len(agent_users) == expected_count
        assert "router" in agent_users
        assert mock_create_user.call_count == expected_count

    @pytest.mark.asyncio
    @patch("mindroom.matrix.users.create_agent_user")
    async def test_ensure_all_agent_users_with_error(
        self,
        mock_create_user: AsyncMock,
    ) -> None:
        """Test handling errors when creating agent users."""
        # Load real configuration
        config = Config.from_yaml()

        # Mock user creation with a failure - create list of results with one error
        mock_results = []
        mock_results.append(AgentMatrixUser("router", "@mindroom_router:localhost", "RouterAgent", "router_pass"))

        # Add successful agents first
        for i, agent_name in enumerate(config.agents):
            if i == 0:  # First agent succeeds
                user_id = f"@mindroom_{agent_name}:localhost"
                display_name = config.agents[agent_name].display_name or f"{agent_name.title()}Agent"
                mock_results.append(AgentMatrixUser(agent_name, user_id, display_name, f"pass_{agent_name}"))
            elif i == 1:  # Second agent raises an exception
                # Create a mock that will raise an exception when awaited
                mock_error = AsyncMock(side_effect=Exception("Failed to create user"))
                mock_create_user.side_effect = [*mock_results, mock_error]
                break

        agent_users = await ensure_all_agent_users("http://localhost:8008", config)

        # Should still return the successful ones (router + first agent)
        assert len(agent_users) >= 2  # At least router + one agent
        assert "router" in agent_users
