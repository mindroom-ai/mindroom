"""Tests for CLI functionality."""

from __future__ import annotations

import asyncio
import re
import socket
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import nio
import pytest
from typer.testing import CliRunner

from mindroom import constants as constants_mod
from mindroom.cli import main as main_module
from mindroom.cli.config import activate_cli_runtime
from mindroom.cli.main import app
from mindroom.cli.pairing_probes import serve_pairing_probes
from mindroom.config.main import Config
from mindroom.config.matrix import MindRoomUserConfig
from mindroom.entity_resolution import mindroom_user_id
from mindroom.matrix.state import MatrixState
from mindroom.matrix.users import INTERNAL_USER_ACCOUNT_KEY, _register_user
from mindroom.orchestrator import _MultiAgentOrchestrator
from tests.conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

runner = CliRunner()
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


def test_mindroom_user_username_rejects_persisted_router_collision(tmp_path: Path) -> None:
    """Internal user localpart must not collide with the prepared router account localpart."""
    runtime_paths = _runtime_paths(tmp_path)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account("agent_router", "mindroom_router", TEST_PASSWORD, domain="localhost")
    state.save(runtime_paths=runtime_paths)

    with pytest.raises(ValueError, match="conflicts with router 'router'"):
        Config.model_validate(
            {"mindroom_user": {"username": "mindroom_router", "display_name": "Alice"}},
            context={"runtime_paths": runtime_paths},
        )


def test_mindroom_user_username_allows_unprepared_agent_proposal_name(tmp_path: Path) -> None:
    """Generated account proposals are not reserved runtime identities before provisioning."""
    runtime_paths = _runtime_paths(tmp_path)
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
            models={"default": {"provider": "openai", "id": "gpt-5.6-luna"}},
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
        "models": {"default": {"provider": "openai", "id": "gpt-5.6-luna"}},
    }
    config_data[section][entity_name] = {
        "display_name": entity_name.title(),
        "role": "Reserved entity",
    }
    if section == "teams":
        config_data[section][entity_name]["agents"] = ["assistant"]

    with pytest.raises(ValueError, match=f"reserved internal entity names: {entity_name}"):
        Config(**config_data)


def test_run_pairs_when_hosted_without_credentials(tmp_path: Path) -> None:
    """Run command initiates pairing when MINDROOM_PROVISIONING_URL is set without credentials."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")

    pair_called = []

    def fake_pair_local_install(runtime_paths: constants_mod.RuntimePaths, **_kwargs: object) -> None:  # noqa: ARG001
        pair_called.append(True)
        # Write credentials to .env to simulate successful pairing
        env_content = env_path.read_text(encoding="utf-8")
        env_content += "\nMINDROOM_LOCAL_CLIENT_ID=test_id\nMINDROOM_LOCAL_CLIENT_SECRET=test_secret\n"
        env_path.write_text(env_content, encoding="utf-8")

    seen_credentials = []

    async def fake_run(*, config_path: Path | None, storage_path: Path | None, **_kwargs: object) -> None:
        runtime_paths = activate_cli_runtime(config_path, storage_path=storage_path)
        seen_credentials.append(
            (
                runtime_paths.env_value("MINDROOM_LOCAL_CLIENT_ID"),
                runtime_paths.env_value("MINDROOM_LOCAL_CLIENT_SECRET"),
            ),
        )

    with (
        patch("mindroom.cli.connect.pair_local_install", side_effect=fake_pair_local_install),
        patch("mindroom.cli.main._run", side_effect=fake_run),
    ):
        result = runner.invoke(app, ["run", "--no-api", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert pair_called, "pair_local_install should have been called"
    assert seen_credentials == [("test_id", "test_secret")]


def _unused_local_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_run_answers_health_probes_while_waiting_for_pairing(tmp_path: Path) -> None:
    """An unpaired run binds the API address first: liveness passes, readiness waits for pairing, then startup proceeds."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")
    port = _unused_local_port()
    probes: dict[str, httpx.Response] = {}
    started_ports: list[int] = []

    def fake_pair_local_install(_runtime_paths: object, **_kwargs: object) -> None:
        for name in ("health", "ready"):
            probes[name] = httpx.get(f"http://127.0.0.1:{port}/api/{name}", trust_env=False)
        with env_path.open("a", encoding="utf-8") as env_file:
            env_file.write("MINDROOM_LOCAL_CLIENT_ID=test_id\nMINDROOM_LOCAL_CLIENT_SECRET=test_secret\n")

    async def fake_run(*, api_host: str, api_port: int, **_kwargs: object) -> None:
        # The real API server binds the same address, with SO_REUSEADDR like Uvicorn, once pairing has finished.
        with socket.socket() as api_socket:
            api_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            api_socket.bind((api_host, api_port))
        started_ports.append(api_port)

    with (
        patch("mindroom.cli.connect.pair_local_install", side_effect=fake_pair_local_install),
        patch("mindroom.cli.main._run", side_effect=fake_run),
    ):
        result = runner.invoke(
            app,
            ["run", "--config", str(config_path), "--api-host", "127.0.0.1", "--api-port", str(port)],
        )

    assert result.exit_code == 0, result.output
    assert probes["health"].status_code == 200
    assert probes["health"].json() == {"status": "healthy"}
    assert probes["ready"].status_code == 503
    assert probes["ready"].json() == {"status": "starting", "detail": "Waiting for local pairing approval"}
    assert started_ports == [port]


def _require_ipv6_loopback() -> None:
    try:
        with socket.socket(socket.AF_INET6) as probe:
            probe.bind(("::1", 0))
    except OSError:
        pytest.skip("IPv6 loopback is unavailable")


def _probe_status(host: str, port: int) -> int:
    return httpx.get(f"http://{host}:{port}/api/health", trust_env=False).status_code


def test_pairing_probes_answer_on_every_localhost_address() -> None:
    """Like the real API server, `localhost` probes answer on both loopbacks, and a taken IPv4 loopback fails early."""
    _require_ipv6_loopback()
    families = {info[0] for info in socket.getaddrinfo("localhost", None, type=socket.SOCK_STREAM)}
    if families != {socket.AF_INET, socket.AF_INET6}:
        pytest.skip("localhost does not resolve to both loopbacks")
    port = _unused_local_port()

    with serve_pairing_probes("localhost", port):
        assert _probe_status("127.0.0.1", port) == 200
        assert _probe_status("[::1]", port) == 200

    # Taking the second resolved loopback makes the failure come after the first listener is already bound.
    resolved = list(
        dict.fromkeys(
            (info[0], info[4])
            for info in socket.getaddrinfo("localhost", port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
        ),
    )
    (first_family, first_address), (second_family, second_address) = resolved[:2]
    # Probe connections leave TIME_WAIT entries, which SO_REUSEADDR skips just as the real API server does.
    with socket.socket(second_family) as occupied:
        occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        occupied.bind(second_address)
        occupied.listen()
        with (
            pytest.raises(OSError, match=re.escape(f"localhost:{port} (Address already in use)")),
            serve_pairing_probes("localhost", port),
        ):
            pass
    # The first listener is released again after the failure.
    with socket.socket(first_family) as released:
        released.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        released.bind(first_address)
        released.listen()


def test_pairing_probes_on_ipv6_wildcard_leave_ipv4_alone() -> None:
    """Like the real API server, `::` binds IPv6 only, so an IPv4 listener on the same port does not block pairing."""
    _require_ipv6_loopback()
    port = _unused_local_port()

    with serve_pairing_probes("::", port):
        assert _probe_status("[::1]", port) == 200
        with pytest.raises(httpx.ConnectError):
            _probe_status("127.0.0.1", port)

    with socket.socket() as ipv4_listener:
        ipv4_listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        ipv4_listener.bind(("0.0.0.0", port))  # noqa: S104
        ipv4_listener.listen()
        with serve_pairing_probes("::", port):
            assert _probe_status("[::1]", port) == 200


def test_run_fails_before_pairing_when_the_api_address_is_taken(tmp_path: Path) -> None:
    """A run that cannot bind its API address stops before asking anyone to approve pairing."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    (tmp_path / ".env").write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")

    with (
        socket.socket() as occupied,
        patch("mindroom.cli.connect.pair_local_install") as mock_pair,
        patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run,
    ):
        occupied.bind(("127.0.0.1", 0))
        occupied.listen()
        port = occupied.getsockname()[1]
        result = runner.invoke(
            app,
            ["run", "--config", str(config_path), "--api-host", "127.0.0.1", "--api-port", str(port)],
        )

    assert result.exit_code == 1
    assert "Cannot listen on the API address" in result.output
    assert f"127.0.0.1:{port}" in result.output
    mock_pair.assert_not_called()
    mock_run.assert_not_called()


def test_run_stops_waiting_when_another_process_pairs(tmp_path: Path) -> None:
    """A waiting run notices credentials written by `mindroom connect` or the macOS app."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")
    observed: list[bool] = []

    def fake_pair_local_install(_runtime_paths: object, *, stop_waiting: Callable[[], bool], **_kwargs: object) -> None:
        observed.append(stop_waiting())
        # A half-written or undecodable .env mid-pairing keeps the run waiting instead of escaping as a traceback.
        env_path.write_bytes(b"MINDROOM_PROVISIONING_URL=https://mindroom.chat\nMINDROOM_LOCAL_CLIENT_ID=\xff\n")
        observed.append(stop_waiting())
        env_path.write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\nMINDROOM_LOCAL_CLIENT_ID=other_id\n")
        observed.append(stop_waiting())
        with env_path.open("a", encoding="utf-8") as env_file:
            env_file.write("MINDROOM_LOCAL_CLIENT_SECRET=other_secret\n")
        observed.append(stop_waiting())

    with (
        patch("mindroom.cli.connect.pair_local_install", side_effect=fake_pair_local_install),
        patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run,
    ):
        result = runner.invoke(app, ["run", "--no-api", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert observed == [False, False, False, True]
    mock_run.assert_called_once()


def test_run_exits_with_printed_credentials_when_env_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Credentials that cannot be saved are printed once and the run stops instead of starting without them."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    (tmp_path / ".env").write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")
    responses = [
        httpx.Response(
            200,
            json={
                "pair_code": "ABCD-EFGH",
                "device_secret": "device-secret",
                "approve_url": "https://chat.example/connect?code=ABCD-EFGH",
                "expires_at": "2026-09-26T12:10:00Z",
                "poll_interval_seconds": 3,
            },
        ),
        httpx.Response(
            200,
            json={"status": "connected", "client_id": "client-123", "client_secret": "secret-123", "namespace": ""},
        ),
    ]
    monkeypatch.setattr("mindroom.cli.connect._httpx_post", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr("mindroom.cli.connect.time.sleep", lambda _seconds: None)

    def read_only(*_args: object, **_kwargs: object) -> Path:
        raise OSError(30, "Read-only file system")

    monkeypatch.setattr("mindroom.cli.connect.upsert_env_values", read_only)

    with patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run:
        result = runner.invoke(app, ["run", "--no-api", "--config", str(config_path)])

    assert result.exit_code == 1
    assert "export MINDROOM_LOCAL_CLIENT_SECRET=secret-123" in result.output
    assert "Could not save credentials" in result.output
    mock_run.assert_not_called()


@pytest.mark.parametrize("interactive", [False, True])
def test_run_confirms_the_approving_account_only_in_a_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interactive: bool,
) -> None:
    """A terminal run asks whether the approver is the user; services only print the approving account."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    (tmp_path / ".env").write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")
    confirmers: list[object] = []
    monkeypatch.setattr("mindroom.cli.main._stdin_is_interactive", lambda: interactive)

    def fake_pair_local_install(_runtime_paths: object, *, confirm_approver: object, **_kwargs: object) -> None:
        confirmers.append(confirm_approver)

    with (
        patch("mindroom.cli.connect.pair_local_install", side_effect=fake_pair_local_install),
        patch("mindroom.cli.main._run", new_callable=AsyncMock),
    ):
        result = runner.invoke(app, ["run", "--no-api", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert (confirmers[0] is not None) is interactive


def test_run_stops_when_the_approving_account_is_declined(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Declining the approver in a terminal discards the credentials and does not start MindRoom."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")
    responses = [
        httpx.Response(
            200,
            json={
                "pair_code": "ABCD-EFGH",
                "device_secret": "device-secret",
                "approve_url": "https://chat.example/connect?code=ABCD-EFGH",
                "expires_at": "2026-09-26T12:10:00Z",
                "poll_interval_seconds": 3,
            },
        ),
        httpx.Response(
            200,
            json={
                "status": "connected",
                "client_id": "client-123",
                "client_secret": "secret-123",
                "namespace": "",
                "owner_user_id": "@mallory:mindroom.chat",
            },
        ),
    ]
    monkeypatch.setattr("mindroom.cli.main._stdin_is_interactive", lambda: True)
    monkeypatch.setattr("mindroom.cli.connect._httpx_post", lambda *_args, **_kwargs: responses.pop(0))
    monkeypatch.setattr("mindroom.cli.connect.time.sleep", lambda _seconds: None)

    with patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run:
        result = runner.invoke(app, ["run", "--no-api", "--config", str(config_path)], input="n\n")

    assert result.exit_code == 1
    assert "Approved by @mallory:mindroom.chat." in result.output
    assert "Credentials discarded" in result.output
    assert env_path.read_text(encoding="utf-8") == "MINDROOM_PROVISIONING_URL=https://mindroom.chat\n"
    mock_run.assert_not_called()


def test_run_warns_about_missing_model_keys_before_pairing(tmp_path: Path) -> None:
    """Missing provider keys are reported before the run waits for a human to approve pairing, and only once."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    (tmp_path / ".env").write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")
    events: list[str] = []

    with (
        patch("mindroom.cli.main.check_env_keys", side_effect=lambda *_a, **_kw: events.append("keys")),
        patch("mindroom.cli.connect.pair_local_install", side_effect=lambda *_a, **_kw: events.append("pair")),
        patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run,
    ):
        result = runner.invoke(app, ["run", "--no-api", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    assert events == ["keys", "pair"]
    mock_run.assert_called_once()


def test_run_warns_about_missing_model_keys_once_without_pairing(tmp_path: Path) -> None:
    """Runs that need no pairing still check provider keys exactly once."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")

    with (
        patch("mindroom.cli.main.check_env_keys") as mock_check,
        patch("mindroom.cli.connect.pair_local_install") as mock_pair,
        patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run,
    ):
        result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 0, result.output
    mock_check.assert_called_once()
    mock_pair.assert_not_called()
    mock_run.assert_called_once()


def test_run_body_does_not_repeat_the_missing_key_warning(tmp_path: Path) -> None:
    """The async run body leaves the provider key check to the command."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")

    with (
        patch("mindroom.cli.main.check_env_keys") as mock_check,
        patch("mindroom.orchestrator.main", new_callable=AsyncMock),
    ):
        asyncio.run(
            main_module._run(
                log_level="INFO",
                config_path=config_path,
                storage_path=tmp_path / "data",
                api=False,
                api_port=8765,
                api_host="127.0.0.1",
            ),
        )

    mock_check.assert_not_called()


def test_run_reports_incomplete_pairing_credentials_without_traceback(tmp_path: Path) -> None:
    """Half-configured local credentials print a friendly error instead of a traceback."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    (tmp_path / ".env").write_text(
        "MINDROOM_PROVISIONING_URL=https://mindroom.chat\nMINDROOM_LOCAL_CLIENT_ID=test_id\n",
        encoding="utf-8",
    )

    with (
        patch("mindroom.cli.connect.pair_local_install") as mock_pair,
        patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run,
    ):
        result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "Error:" in result.output
    assert "Provisioning credentials are incomplete" in result.output
    mock_pair.assert_not_called()
    mock_run.assert_not_called()


def test_run_rejects_broken_config_before_pairing(tmp_path: Path) -> None:
    """A broken config.yaml fails before the user is asked to approve pairing."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: [\n", encoding="utf-8")
    (tmp_path / ".env").write_text("MINDROOM_PROVISIONING_URL=https://mindroom.chat\n", encoding="utf-8")

    with (
        patch("mindroom.cli.connect.pair_local_install") as mock_pair,
        patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run,
    ):
        result = runner.invoke(app, ["run", "--config", str(config_path)])

    assert result.exit_code == 1
    mock_pair.assert_not_called()
    mock_run.assert_not_called()


def test_run_skips_pairing_with_registration_token(tmp_path: Path) -> None:
    """Run command skips pairing when MATRIX_REGISTRATION_TOKEN is set."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text("agents: {}\nmodels: {}\nrouter:\n  model: default\n", encoding="utf-8")
    env_path = tmp_path / ".env"
    env_path.write_text(
        "MINDROOM_PROVISIONING_URL=https://mindroom.chat\nMATRIX_REGISTRATION_TOKEN=test_token\n",
        encoding="utf-8",
    )

    pair_called = []

    def fake_pair_local_install(runtime_paths: constants_mod.RuntimePaths, **_kwargs: object) -> None:  # noqa: ARG001
        pair_called.append(True)

    with (
        patch("mindroom.cli.connect.pair_local_install", side_effect=fake_pair_local_install),
        patch("mindroom.cli.main._run", new_callable=AsyncMock) as mock_run,
    ):
        result = runner.invoke(app, ["run", "--config", str(config_path)])

        assert not pair_called, "pair_local_install should not have been called when MATRIX_REGISTRATION_TOKEN is set"
        mock_run.assert_called_once()
        assert result.exit_code == 0
