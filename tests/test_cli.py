"""Tests for CLI functionality."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import constants as constants_mod
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.entity_resolution import mindroom_user_id
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, _register_user
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD

if TYPE_CHECKING:
    from pathlib import Path

DEFAULT_INTERNAL_USERNAME = MindRoomUserConfig().username
DEFAULT_INTERNAL_DISPLAY_NAME = MindRoomUserConfig().display_name


def _runtime_paths(tmp_path: Path) -> constants_mod.RuntimePaths:
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    return constants_mod.resolve_runtime_paths(config_path=config_path, storage_path=tmp_path)


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
    async def test_register_user_success(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Test successful user registration."""
        mock_context, mock_client = mock_matrix_client

        # Mock successful registration
        mock_client.register.return_value = nio.RegisterResponse(
            user_id="@test_user:localhost",
            device_id="TEST_DEVICE",
            access_token=TEST_ACCESS_TOKEN,
        )
        mock_client.set_displayname.return_value = AsyncMock()

        runtime_paths = _runtime_paths(tmp_path)
        with patch("mindroom.matrix.users.matrix_client", return_value=mock_context):
            user_id = await _register_user(
                "http://localhost:8008",
                "test_user",
                TEST_PASSWORD,
                "Test User",
                runtime_paths=runtime_paths,
            )

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
    async def test_register_user_already_exists(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
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

        runtime_paths = _runtime_paths(tmp_path)
        with patch("mindroom.matrix.users.matrix_client", return_value=mock_context):
            # Should return the user_id even when user exists
            user_id = await _register_user(
                "http://localhost:8008",
                "existing_user",
                "test_password",
                "Existing User",
                runtime_paths=runtime_paths,
            )

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
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
        ):
            runtime_paths = _runtime_paths(tmp_path)
            orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
            _config = Config(
                mindroom_user={"username": DEFAULT_INTERNAL_USERNAME, "display_name": DEFAULT_INTERNAL_DISPLAY_NAME},
            )
            await orchestrator._ensure_user_account(_config)

            # Check that user was created
            state = MatrixState.load(runtime_paths=runtime_paths)

            assert INTERNAL_USER_ACCOUNT_KEY in state.accounts
            assert state.accounts[INTERNAL_USER_ACCOUNT_KEY].username == f"{DEFAULT_INTERNAL_USERNAME}_test"
            generated_password = state.accounts[INTERNAL_USER_ACCOUNT_KEY].password
            assert generated_password
            assert generated_password != "user_secure_password"  # noqa: S105

            # Verify registration was called
            mock_client.register.assert_called_once()
            mock_client.set_displayname.assert_called_once_with(DEFAULT_INTERNAL_DISPLAY_NAME)

    @pytest.mark.asyncio
    async def test_ensure_user_account_logs_in_with_existing_credentials(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Existing stored credentials should be reused without re-registration."""
        mock_context, mock_client = mock_matrix_client

        # Create existing config with internal user account
        state = MatrixState()
        state.add_account(INTERNAL_USER_ACCOUNT_KEY, DEFAULT_INTERNAL_USERNAME, "existing_password")

        runtime_paths = _runtime_paths(tmp_path)
        state.save(runtime_paths=runtime_paths)

        with (
            patch("mindroom.matrix.users.matrix_client", return_value=mock_context),
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
        ):
            orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
            _config = Config(
                mindroom_user={
                    "username": DEFAULT_INTERNAL_USERNAME,
                    "display_name": DEFAULT_INTERNAL_DISPLAY_NAME,
                },
            )
            await orchestrator._ensure_user_account(_config)

            # Should use existing account
            result_config = MatrixState.load(runtime_paths=runtime_paths)
            assert result_config.accounts[INTERNAL_USER_ACCOUNT_KEY].username == DEFAULT_INTERNAL_USERNAME
            assert result_config.accounts[INTERNAL_USER_ACCOUNT_KEY].password == "existing_password"  # noqa: S105

            mock_client.register.assert_not_called()
            mock_client.login.assert_not_called()
            mock_client.set_displayname.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_user_account_recreates_account_when_stored_login_fails(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Stored credentials should be preserved until an explicit login happens."""
        mock_context, mock_client = mock_matrix_client

        # Create existing config with invalid credentials
        state = MatrixState()
        state.add_account(INTERNAL_USER_ACCOUNT_KEY, DEFAULT_INTERNAL_USERNAME, "wrong_password")

        runtime_paths = _runtime_paths(tmp_path)
        state.save(runtime_paths=runtime_paths)

        with (
            patch("mindroom.matrix.users.matrix_client", return_value=mock_context),
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
        ):
            orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
            _config = Config(
                mindroom_user={
                    "username": DEFAULT_INTERNAL_USERNAME,
                    "display_name": DEFAULT_INTERNAL_DISPLAY_NAME,
                },
            )
            await orchestrator._ensure_user_account(_config)

            # Should have kept the existing account credentials
            # (create_agent_user doesn't regenerate passwords on login failure)
            result_config = MatrixState.load(runtime_paths=runtime_paths)
            assert INTERNAL_USER_ACCOUNT_KEY in result_config.accounts
            assert result_config.accounts[INTERNAL_USER_ACCOUNT_KEY].username == DEFAULT_INTERNAL_USERNAME
            assert result_config.accounts[INTERNAL_USER_ACCOUNT_KEY].password == "wrong_password"  # noqa: S105

            mock_client.login.assert_not_called()
            mock_client.register.assert_not_called()
            mock_client.set_displayname.assert_not_called()

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
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
        ):
            runtime_paths = _runtime_paths(tmp_path)
            orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)
            await orchestrator._ensure_user_account(custom_config)

            state = MatrixState.load(runtime_paths=runtime_paths)
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
    async def test_ensure_user_account_uses_existing_persisted_identity(
        self,
        tmp_path: Path,
        mock_matrix_client: tuple[MagicMock, AsyncMock],
    ) -> None:
        """Internal user config username is only a proposal when no account exists yet."""
        mock_context, mock_client = mock_matrix_client
        state = MatrixState()
        state.add_account(
            INTERNAL_USER_ACCOUNT_KEY,
            "actual_mindroom_user",
            "existing_password",
            requested_username="alice",
            domain="matrix.example",
        )

        custom_config = Config(mindroom_user={"username": "alice", "display_name": "Alice Smith"})

        runtime_paths = _runtime_paths(tmp_path)
        state.save(runtime_paths=runtime_paths)
        with (
            patch("mindroom.matrix.users.matrix_client", return_value=mock_context),
            patch("mindroom.constants.runtime_matrix_homeserver", return_value="http://localhost:8008"),
        ):
            orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths)

            await orchestrator._ensure_user_account(custom_config)

        persisted_state = MatrixState.load(runtime_paths=runtime_paths)
        account = persisted_state.accounts[INTERNAL_USER_ACCOUNT_KEY]
        assert account.username == "actual_mindroom_user"
        assert account.domain == "matrix.example"
        assert mindroom_user_id(custom_config, runtime_paths) == "@actual_mindroom_user:matrix.example"
        mock_client.register.assert_not_called()


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


def test_mindroom_user_username_rejects_router_collision(tmp_path: Path) -> None:
    """Internal user localpart must not collide with the router account localpart."""
    runtime_paths = _runtime_paths(tmp_path)
    with pytest.raises(ValueError, match="conflicts with router 'router'"):
        Config.model_validate(
            {"mindroom_user": {"username": "mindroom_router", "display_name": "Alice"}},
            context={"runtime_paths": runtime_paths},
        )


def test_mindroom_user_username_rejects_agent_collision(tmp_path: Path) -> None:
    """Internal user localpart must not collide with configured agent localparts."""
    runtime_paths = _runtime_paths(tmp_path)
    with pytest.raises(ValueError, match="conflicts with agent 'assistant'"):
        Config.model_validate(
            {
                "agents": {
                    "assistant": {
                        "display_name": "Assistant",
                        "role": "Test assistant",
                        "rooms": ["test_room"],
                    },
                },
                "mindroom_user": {"username": "mindroom_assistant", "display_name": "Alice"},
            },
            context={"runtime_paths": runtime_paths},
        )


def test_mindroom_user_username_rejects_persisted_agent_username_collision(tmp_path: Path) -> None:
    """Internal user localpart must not collide with prepared agent account localparts."""
    runtime_paths = _runtime_paths(tmp_path)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account("agent_assistant", "actual_assistant", TEST_PASSWORD, domain="localhost")
    state.save(runtime_paths=runtime_paths)

    with pytest.raises(ValueError, match="conflicts with agent 'assistant'"):
        Config.model_validate(
            {
                "agents": {
                    "assistant": {
                        "display_name": "Assistant",
                        "role": "Test assistant",
                        "rooms": ["test_room"],
                    },
                },
                "mindroom_user": {"username": "actual_assistant", "display_name": "Alice"},
            },
            context={"runtime_paths": runtime_paths},
        )


def test_mindroom_user_username_allows_prepared_agent_proposal_name(tmp_path: Path) -> None:
    """Prepared agent accounts reserve their actual localpart, not the original proposal."""
    runtime_paths = _runtime_paths(tmp_path)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account("agent_assistant", "actual_assistant", TEST_PASSWORD, domain="localhost")
    state.save(runtime_paths=runtime_paths)

    config = Config.model_validate(
        {
            "agents": {
                "assistant": {
                    "display_name": "Assistant",
                    "role": "Test assistant",
                    "rooms": ["test_room"],
                },
            },
            "mindroom_user": {"username": "mindroom_assistant", "display_name": "Alice"},
        },
        context={"runtime_paths": runtime_paths},
    )

    assert config.mindroom_user is not None
    assert config.mindroom_user.username == "mindroom_assistant"


def test_mindroom_user_none_validates_and_returns_none_id() -> None:
    """Config with mindroom_user omitted should validate and return None user ID."""
    config = Config()
    assert config.mindroom_user is None
    runtime_paths = constants_mod.resolve_runtime_paths(process_env={"MINDROOM_NAMESPACE": ""})
    assert mindroom_user_id(config, runtime_paths) is None


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


@pytest.mark.parametrize("section", ["agents", "teams"])
@pytest.mark.parametrize("entity_name", [constants_mod.ROUTER_AGENT_NAME, "user"])
def test_agent_and_team_names_reject_internal_entity_name(section: str, entity_name: str) -> None:
    """Built-in managed entity account keys are not configurable responder aliases."""
    config_data = {
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Test assistant",
            },
        },
        "teams": {},
        "models": {"default": {"provider": "openai", "id": "gpt-4o-mini"}},
    }
    config_data[section][entity_name] = {
        "display_name": entity_name.title(),
        "role": "Reserved entity",
    }
    if section == "teams":
        config_data[section][entity_name]["agents"] = ["assistant"]

    with pytest.raises(ValueError, match=f"reserved internal entity names: {entity_name}"):
        Config(**config_data)
