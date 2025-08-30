"""Test that the router agent joins all configured rooms."""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock

import pytest

from mindroom.bot import MultiAgentOrchestrator, create_bot_for_entity
from mindroom.config import AgentConfig, Config, TeamConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_PASSWORD


@pytest.fixture
def config_with_rooms() -> Config:
    """Create a config with agents and teams that have rooms."""
    return Config(
        agents={
            "agent1": AgentConfig(
                display_name="Agent 1",
                role="Test agent",
                rooms=["room1", "room2"],
            ),
            "agent2": AgentConfig(
                display_name="Agent 2",
                role="Another test agent",
                rooms=["room3"],
            ),
        },
        teams={
            "team1": TeamConfig(
                display_name="Team 1",
                role="Test team",
                agents=["agent1", "agent2"],
                rooms=["room4"],
            ),
        },
    )


@pytest.mark.asyncio
async def test_router_gets_all_configured_rooms(
    config_with_rooms: Config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that the router agent is configured to join all rooms from agents and teams."""

    # Mock resolve_room_aliases to return the same aliases (no resolution)
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Create a temporary user for the router
    router_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id=f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
        display_name="RouterAgent",
        password=TEST_PASSWORD,
    )

    # Create the router bot
    router_bot = create_bot_for_entity(ROUTER_AGENT_NAME, router_user, config_with_rooms, tmp_path)

    # Check that the router has all rooms
    expected_rooms = {"room1", "room2", "room3", "room4"}
    assert router_bot is not None
    assert set(router_bot.rooms) == expected_rooms


@pytest.mark.asyncio
async def test_router_joins_rooms_on_start(
    config_with_rooms: Config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that the router actually joins all configured rooms when started."""
    # Track which rooms were joined
    joined_rooms: list[str] = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> bool:
        joined_rooms.append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(_client: AsyncMock, _room_id: str, _config: Config) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases to return the same aliases (no resolution)
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Create router user
    router_user = AgentMatrixUser(
        agent_name=ROUTER_AGENT_NAME,
        user_id=f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
        display_name="RouterAgent",
        password=TEST_PASSWORD,
    )

    # Create and configure the router bot
    router_bot = create_bot_for_entity(ROUTER_AGENT_NAME, router_user, config_with_rooms, tmp_path)

    # Mock the client
    mock_client = AsyncMock()
    assert router_bot is not None
    router_bot.client = mock_client

    # Test that the router joins all configured rooms
    await router_bot.join_configured_rooms()

    # Verify all rooms were joined
    expected_rooms = {"room1", "room2", "room3", "room4"}
    assert set(joined_rooms) == expected_rooms


@pytest.mark.asyncio
@pytest.mark.requires_matrix  # Requires real Matrix server for router room management
@pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
async def test_orchestrator_creates_router_with_all_rooms(
    config_with_rooms: Config,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that the orchestrator properly initializes the router with all rooms."""

    # Mock various async operations
    async def mock_ensure_all_agent_users(_homeserver: str) -> dict[str, AgentMatrixUser]:
        return {
            ROUTER_AGENT_NAME: AgentMatrixUser(
                agent_name=ROUTER_AGENT_NAME,
                user_id=f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
                display_name="RouterAgent",
                password=TEST_PASSWORD,
            ),
            "agent1": AgentMatrixUser(
                agent_name="agent1",
                user_id="@mindroom_agent1:localhost",
                display_name="Agent 1",
                password=TEST_PASSWORD,
            ),
            "agent2": AgentMatrixUser(
                agent_name="agent2",
                user_id="@mindroom_agent2:localhost",
                display_name="Agent 2",
                password=TEST_PASSWORD,
            ),
            "team1": AgentMatrixUser(
                agent_name="team1",
                user_id="@mindroom_team1:localhost",
                display_name="Team 1",
                password=TEST_PASSWORD,
            ),
        }

    monkeypatch.setattr("mindroom.matrix.users.ensure_all_agent_users", mock_ensure_all_agent_users)

    # Mock resolve_room_aliases to return the same aliases (no resolution needed for test)
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock load_config to return our test config
    def mock_load_config(_config_path: Path | None = None) -> Config:
        return config_with_rooms

    monkeypatch.setattr("mindroom.config.Config.from_yaml", mock_load_config)

    # Create orchestrator
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

    # Initialize (creates all bots)
    await orchestrator.initialize()

    # Check that router exists and has all rooms
    assert ROUTER_AGENT_NAME in orchestrator.agent_bots
    router_bot = orchestrator.agent_bots[ROUTER_AGENT_NAME]

    expected_rooms = {"room1", "room2", "room3", "room4"}
    assert set(router_bot.rooms) == expected_rooms


@pytest.mark.asyncio
@pytest.mark.requires_matrix  # Requires real Matrix server for router room updates
@pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
async def test_router_updates_rooms_on_config_change(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Test that the router updates its room list when config changes."""
    # Initial config with some rooms
    initial_config = Config(
        agents={
            "agent1": AgentConfig(
                display_name="Agent 1",
                role="Test agent",
                rooms=["room1"],
            ),
        },
    )

    # Updated config with more rooms
    updated_config = Config(
        agents={
            "agent1": AgentConfig(
                display_name="Agent 1",
                role="Test agent",
                rooms=["room1", "room2"],
            ),
            "agent2": AgentConfig(
                display_name="Agent 2",
                role="New agent",
                rooms=["room3"],
            ),
        },
    )

    # Mock various operations
    async def mock_ensure_all_agent_users(_homeserver: str) -> dict[str, AgentMatrixUser]:
        return {
            ROUTER_AGENT_NAME: AgentMatrixUser(
                agent_name=ROUTER_AGENT_NAME,
                user_id=f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
                display_name="RouterAgent",
                password=TEST_PASSWORD,
            ),
            "agent1": AgentMatrixUser(
                agent_name="agent1",
                user_id="@mindroom_agent1:localhost",
                display_name="Agent 1",
                password=TEST_PASSWORD,
            ),
        }

    monkeypatch.setattr("mindroom.matrix.users.ensure_all_agent_users", mock_ensure_all_agent_users)

    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock load_config to return different configs on different calls
    load_config_returns = [initial_config, updated_config]
    load_config_counter = [0]

    def mock_load_config(_config_path: Path | None = None) -> Config:
        result = load_config_returns[min(load_config_counter[0], len(load_config_returns) - 1)]
        load_config_counter[0] += 1
        return result

    monkeypatch.setattr("mindroom.config.Config.from_yaml", mock_load_config)

    # Create orchestrator with initial config
    # Mock start/sync_forever at class level so newly created bots in update_config don't perform real login/sync
    monkeypatch.setattr("mindroom.bot.AgentBot.start", AsyncMock())
    monkeypatch.setattr("mindroom.bot.AgentBot.sync_forever", AsyncMock())
    monkeypatch.setattr("mindroom.bot.AgentBot.join_configured_rooms", AsyncMock())
    monkeypatch.setattr("mindroom.bot.AgentBot.leave_unconfigured_rooms", AsyncMock())
    monkeypatch.setattr("mindroom.bot.TeamBot.start", AsyncMock())
    monkeypatch.setattr("mindroom.bot.TeamBot.sync_forever", AsyncMock())
    monkeypatch.setattr("mindroom.bot.TeamBot.join_configured_rooms", AsyncMock())
    monkeypatch.setattr("mindroom.bot.TeamBot.leave_unconfigured_rooms", AsyncMock())

    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

    await orchestrator.initialize()

    # Check initial router rooms
    router_bot = orchestrator.agent_bots[ROUTER_AGENT_NAME]
    assert set(router_bot.rooms) == {"room1"}

    # Mock bot operations using monkeypatch to avoid method assignment errors
    async def mock_stop() -> None:
        pass

    async def mock_start() -> None:
        pass

    async def mock_ensure_user_account() -> None:
        pass

    async def mock_sync_forever() -> None:
        raise asyncio.CancelledError

    for bot in orchestrator.agent_bots.values():
        monkeypatch.setattr(bot, "stop", mock_stop)
        monkeypatch.setattr(bot, "start", mock_start)
        monkeypatch.setattr(bot, "ensure_user_account", mock_ensure_user_account)
        monkeypatch.setattr(bot, "sync_forever", mock_sync_forever)

    # Update config
    updated = await orchestrator.update_config()
    assert updated  # Should return True since router needs restart

    # Router should be recreated with new rooms
    new_router_bot = orchestrator.agent_bots[ROUTER_AGENT_NAME]
    assert set(new_router_bot.rooms) == {"room1", "room2", "room3"}
