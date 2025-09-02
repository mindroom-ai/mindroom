"""Tests for config auto-reload and room membership updates."""

from __future__ import annotations

import asyncio
from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock

import pytest

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.config import AgentConfig, Config, ModelConfig, RouterConfig, TeamConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_PASSWORD


def setup_test_bot(bot: AgentBot, mock_client: AsyncMock) -> None:
    """Helper to setup a test bot with required attributes."""
    bot.client = mock_client


@pytest.fixture
def initial_config() -> Config:
    """Initial configuration with some agents and rooms."""
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
                rooms=["room1"],
            ),
        },
        teams={
            "team1": TeamConfig(
                display_name="Team 1",
                role="Test team",
                agents=["agent1", "agent2"],
                rooms=["room3"],
            ),
        },
        models={
            "default": ModelConfig(
                provider="ollama",
                id="llama3.2",
                host="http://localhost:11434",
            ),
        },
    )


@pytest.fixture
def updated_config() -> Config:
    """Updated configuration with changed room assignments."""
    return Config(
        agents={
            "agent1": AgentConfig(
                display_name="Agent 1",
                role="Test agent",
                rooms=["room1", "room4"],  # Changed: removed room2, added room4
            ),
            "agent2": AgentConfig(
                display_name="Agent 2",
                role="Another test agent",
                rooms=["room2", "room3"],  # Changed: removed room1, added room2 and room3
            ),
            "agent3": AgentConfig(  # New agent
                display_name="Agent 3",
                role="New agent",
                rooms=["room5"],
            ),
        },
        teams={
            "team1": TeamConfig(
                display_name="Team 1",
                role="Test team",
                agents=["agent1", "agent2", "agent3"],  # Added agent3
                rooms=["room3", "room6"],  # Added room6
            ),
        },
        models={
            "default": ModelConfig(
                provider="ollama",
                id="llama3.2",
                host="http://localhost:11434",
            ),
        },
    )


@pytest.fixture
def mock_agent_users() -> dict[str, AgentMatrixUser]:
    """Create mock agent users."""
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
        "agent3": AgentMatrixUser(
            agent_name="agent3",
            user_id="@mindroom_agent3:localhost",
            display_name="Agent 3",
            password=TEST_PASSWORD,
        ),
        "team1": AgentMatrixUser(
            agent_name="team1",
            user_id="@mindroom_team1:localhost",
            display_name="Team 1",
            password=TEST_PASSWORD,
        ),
    }


@pytest.mark.asyncio
async def test_agent_joins_new_rooms_on_config_reload(  # noqa: C901
    initial_config: Config,  # noqa: ARG001
    updated_config: Config,  # noqa: ARG001
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that agents join new rooms when their configuration is updated."""
    # Track room operations
    joined_rooms: dict[str, list[str]] = {}
    left_rooms: dict[str, list[str]] = {}

    async def mock_join_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    async def mock_leave_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in left_rooms:
            left_rooms[user_id] = []
        left_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.bot.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(_client: AsyncMock, _room_id: str, _config: Config) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(client: AsyncMock) -> list[str]:
        user_id = client.user_id
        if "agent1" in user_id:
            return ["room1", "room2"]  # agent1 is currently in room1 and room2
        if "agent2" in user_id:
            return ["room1"]  # agent2 is currently in room1
        if "team1" in user_id:
            return ["room3"]  # team1 is currently in room3
        if ROUTER_AGENT_NAME in user_id:
            return ["room1", "room2", "room3"]  # router is in all initial rooms
        return []

    monkeypatch.setattr("mindroom.bot.get_joined_rooms", mock_get_joined_rooms)

    # Create agent1 bot with initial config
    config = Config(router=RouterConfig(model="default"))
    agent1_bot = AgentBot(
        agent_user=mock_agent_users["agent1"],
        storage_path=tmp_path,
        config=config,
        rooms=["room1", "room2"],  # Initial rooms
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_agent1:localhost"
    setup_test_bot(agent1_bot, mock_client)

    # Update to new config rooms
    agent1_bot.rooms = ["room1", "room4"]  # New rooms: removed room2, added room4

    # Apply room updates
    await agent1_bot.join_configured_rooms()
    await agent1_bot.leave_unconfigured_rooms()

    # Verify agent1 joined room4 (new room)
    assert "room4" in joined_rooms.get("@mindroom_agent1:localhost", [])
    # Verify agent1 left room2 (no longer configured)
    assert "room2" in left_rooms.get("@mindroom_agent1:localhost", [])


@pytest.mark.asyncio
async def test_router_updates_rooms_on_config_reload(
    initial_config: Config,
    updated_config: Config,
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that the router updates its room list when agents/teams change their rooms."""
    # Track room operations
    joined_rooms: list[str] = []
    left_rooms: list[str] = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> bool:
        joined_rooms.append(room_id)
        return True

    async def mock_leave_room(_client: AsyncMock, room_id: str) -> bool:
        left_rooms.append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.bot.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(_client: AsyncMock, _room_id: str, _config: Config) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(_client: AsyncMock) -> list[str]:
        # Router is currently in initial config rooms
        return ["room1", "room2", "room3"]

    monkeypatch.setattr("mindroom.bot.get_joined_rooms", mock_get_joined_rooms)

    # Get initial router rooms
    initial_router_rooms = initial_config.get_all_configured_rooms()
    assert initial_router_rooms == {"room1", "room2", "room3"}

    # Get updated router rooms
    updated_router_rooms = updated_config.get_all_configured_rooms()
    assert updated_router_rooms == {"room1", "room2", "room3", "room4", "room5", "room6"}

    # Create router bot with updated config
    config = Config(router=RouterConfig(model="default"))
    router_bot = AgentBot(
        agent_user=mock_agent_users[ROUTER_AGENT_NAME],
        storage_path=tmp_path,
        config=config,
        rooms=list(updated_router_rooms),
    )
    mock_client = AsyncMock()
    mock_client.user_id = f"@mindroom_{ROUTER_AGENT_NAME}:localhost"
    setup_test_bot(router_bot, mock_client)

    # Apply room updates
    await router_bot.join_configured_rooms()
    await router_bot.leave_unconfigured_rooms()

    # Verify router joined new rooms
    for new_room in ["room4", "room5", "room6"]:
        assert new_room in joined_rooms

    # Router should not leave any rooms (all initial rooms still have agents)
    assert len(left_rooms) == 0


@pytest.mark.asyncio
async def test_new_agent_joins_rooms_on_config_reload(
    initial_config: Config,  # noqa: ARG001
    updated_config: Config,  # noqa: ARG001
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that new agents are created and join their configured rooms."""
    # Track room operations
    joined_rooms: dict[str, list[str]] = {}

    async def mock_ensure_all_agent_users(_homeserver: str) -> dict[str, AgentMatrixUser]:
        # Return both existing and new agent users
        return mock_agent_users

    monkeypatch.setattr("mindroom.matrix.users.ensure_all_agent_users", mock_ensure_all_agent_users)

    async def mock_join_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(_client: AsyncMock, _room_id: str, _config: Config) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms
    async def mock_get_joined_rooms(_client: AsyncMock) -> list[str]:
        return []  # New agent has no rooms initially

    monkeypatch.setattr("mindroom.bot.get_joined_rooms", mock_get_joined_rooms)

    # Create agent3 bot (new agent in updated config)
    config = Config(router=RouterConfig(model="default"))
    agent3_bot = AgentBot(
        agent_user=mock_agent_users["agent3"],
        storage_path=tmp_path,
        config=config,
        rooms=["room5"],
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_agent3:localhost"
    setup_test_bot(agent3_bot, mock_client)

    # Apply room updates for new agent
    await agent3_bot.join_configured_rooms()

    # Verify agent3 joined its configured room
    assert "room5" in joined_rooms.get("@mindroom_agent3:localhost", [])


@pytest.mark.asyncio
async def test_team_room_changes_on_config_reload(
    initial_config: Config,  # noqa: ARG001
    updated_config: Config,  # noqa: ARG001
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that teams update their room memberships when configuration changes."""
    # Track room operations
    joined_rooms: dict[str, list[str]] = {}
    left_rooms: dict[str, list[str]] = {}

    async def mock_join_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    async def mock_leave_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in left_rooms:
            left_rooms[user_id] = []
        left_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.bot.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(_client: AsyncMock, _room_id: str, _config: Config) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(client: AsyncMock) -> list[str]:
        user_id = client.user_id
        if "team1" in user_id:
            return ["room3"]  # team1 is currently only in room3
        return []

    monkeypatch.setattr("mindroom.bot.get_joined_rooms", mock_get_joined_rooms)

    # Create team1 bot with updated config
    config = Config(router=RouterConfig(model="default"))
    team1_bot = AgentBot(
        agent_user=mock_agent_users["team1"],
        storage_path=tmp_path,
        config=config,
        rooms=["room3", "room6"],
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_team1:localhost"
    setup_test_bot(team1_bot, mock_client)

    # Apply room updates
    await team1_bot.join_configured_rooms()
    await team1_bot.leave_unconfigured_rooms()

    # Verify team1 joined room6 (new room)
    assert "room6" in joined_rooms.get("@mindroom_team1:localhost", [])
    # Team1 should not leave room3 (still configured)
    assert "room3" not in left_rooms.get("@mindroom_team1:localhost", [])


@pytest.mark.asyncio
@pytest.mark.requires_matrix  # This test requires a real Matrix server or extensive mocking
@pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
async def test_orchestrator_handles_config_reload(  # noqa: PLR0915
    initial_config: Config,
    updated_config: Config,
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that the orchestrator properly handles config reloads and updates all bots."""
    # Track config loads
    config_loads = [initial_config, updated_config]
    load_count = [0]

    def mock_load_config(_config_path: Path | None = None) -> Config:
        result = config_loads[min(load_count[0], len(config_loads) - 1)]
        load_count[0] += 1
        return result

    monkeypatch.setattr("mindroom.config.Config.from_yaml", mock_load_config)

    async def mock_ensure_all_agent_users(_homeserver: str) -> dict[str, AgentMatrixUser]:
        return mock_agent_users

    monkeypatch.setattr("mindroom.matrix.users.ensure_all_agent_users", mock_ensure_all_agent_users)

    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock topic generation to avoid calling AI
    async def mock_generate_room_topic_ai(room_key: str, room_name: str, config: Config) -> str:  # noqa: ARG001
        return f"Test topic for {room_name}"

    monkeypatch.setattr("mindroom.topic_generator.generate_room_topic_ai", mock_generate_room_topic_ai)

    # Create orchestrator
    # Mock start/sync at class level so newly created bots during update_config don't perform real login/sync
    # But we need to ensure client gets set when start() is called
    async def mock_start(self: AgentBot) -> None:
        """Mock start that sets a mock client."""
        self.client = AsyncMock()
        self.client.user_id = self.agent_user.user_id
        self.running = True

    monkeypatch.setattr("mindroom.bot.AgentBot.start", mock_start)
    monkeypatch.setattr("mindroom.bot.AgentBot.sync_forever", AsyncMock())
    monkeypatch.setattr("mindroom.bot.TeamBot.start", mock_start)
    monkeypatch.setattr("mindroom.bot.TeamBot.sync_forever", AsyncMock())

    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

    # Initialize with initial config
    await orchestrator.initialize()

    # Verify initial state
    assert "agent1" in orchestrator.agent_bots
    assert "agent2" in orchestrator.agent_bots
    assert "agent3" not in orchestrator.agent_bots  # Not in initial config
    assert "team1" in orchestrator.agent_bots
    assert ROUTER_AGENT_NAME in orchestrator.agent_bots

    # Check initial room assignments
    assert set(orchestrator.agent_bots["agent1"].rooms) == {"room1", "room2"}
    assert set(orchestrator.agent_bots["agent2"].rooms) == {"room1"}
    assert set(orchestrator.agent_bots["team1"].rooms) == {"room3"}
    assert set(orchestrator.agent_bots[ROUTER_AGENT_NAME].rooms) == {"room1", "room2", "room3"}

    # Create a mock start method that initializes client
    async def mock_start_with_thread_manager(self: AgentBot) -> None:
        """Mock start that initializes client."""
        if not hasattr(self, "client") or self.client is None:
            self.client = AsyncMock()
            self.client.user_id = self.agent_user.user_id

    # Patch AgentBot.start and TeamBot.start to use our mock
    monkeypatch.setattr("mindroom.bot.AgentBot.start", mock_start_with_thread_manager)
    monkeypatch.setattr("mindroom.bot.TeamBot.start", mock_start_with_thread_manager)

    # Mock bot operations for update
    for bot in orchestrator.agent_bots.values():
        monkeypatch.setattr(bot, "stop", AsyncMock())
        monkeypatch.setattr(bot, "start", mock_start_with_thread_manager)
        monkeypatch.setattr(bot, "ensure_user_account", AsyncMock())
        monkeypatch.setattr(bot, "sync_forever", AsyncMock(side_effect=asyncio.CancelledError()))

    # Update config
    updated = await orchestrator.update_config()
    assert updated  # Should return True since config changed

    # Verify updated state
    assert "agent1" in orchestrator.agent_bots
    assert "agent2" in orchestrator.agent_bots
    assert "agent3" in orchestrator.agent_bots  # New agent added
    assert "team1" in orchestrator.agent_bots
    assert ROUTER_AGENT_NAME in orchestrator.agent_bots

    # Check updated room assignments
    assert set(orchestrator.agent_bots["agent1"].rooms) == {"room1", "room4"}
    assert set(orchestrator.agent_bots["agent2"].rooms) == {"room2", "room3"}
    assert set(orchestrator.agent_bots["agent3"].rooms) == {"room5"}
    assert set(orchestrator.agent_bots["team1"].rooms) == {"room3", "room6"}
    assert set(orchestrator.agent_bots[ROUTER_AGENT_NAME].rooms) == {
        "room1",
        "room2",
        "room3",
        "room4",
        "room5",
        "room6",
    }


@pytest.mark.asyncio
async def test_room_membership_state_after_config_update(  # noqa: C901, PLR0915
    initial_config: Config,  # noqa: ARG001
    updated_config: Config,  # noqa: ARG001
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that room membership state is correct after config updates."""
    # Simulate room membership state
    room_memberships = {
        "room1": [
            "@mindroom_agent1:localhost",
            "@mindroom_agent2:localhost",
            f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
        ],
        "room2": ["@mindroom_agent1:localhost", f"@mindroom_{ROUTER_AGENT_NAME}:localhost"],
        "room3": ["@mindroom_team1:localhost", f"@mindroom_{ROUTER_AGENT_NAME}:localhost"],
    }

    def update_room_membership(user_id: str, room_id: str, action: str) -> None:
        """Update simulated room membership."""
        if action == "join":
            if room_id not in room_memberships:
                room_memberships[room_id] = []
            if user_id not in room_memberships[room_id]:
                room_memberships[room_id].append(user_id)
        elif action == "leave":
            if room_id in room_memberships and user_id in room_memberships[room_id]:
                room_memberships[room_id].remove(user_id)

    async def mock_join_room(client: AsyncMock, room_id: str) -> bool:
        update_room_membership(client.user_id, room_id, "join")
        return True

    async def mock_leave_room(client: AsyncMock, room_id: str) -> bool:
        update_room_membership(client.user_id, room_id, "leave")
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.bot.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(_client: AsyncMock, _room_id: str, _config: Config) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms based on room_memberships
    async def mock_get_joined_rooms(client: AsyncMock) -> list[str]:
        user_id = client.user_id
        rooms = []
        for room_id, members in room_memberships.items():
            if user_id in members:
                rooms.append(room_id)
        return rooms

    monkeypatch.setattr("mindroom.bot.get_joined_rooms", mock_get_joined_rooms)

    # Apply config updates for each bot
    bots_config = {
        "@mindroom_agent1:localhost": {"old": ["room1", "room2"], "new": ["room1", "room4"]},
        "@mindroom_agent2:localhost": {"old": ["room1"], "new": ["room2", "room3"]},
        "@mindroom_agent3:localhost": {"old": [], "new": ["room5"]},
        "@mindroom_team1:localhost": {"old": ["room3"], "new": ["room3", "room6"]},
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost": {
            "old": ["room1", "room2", "room3"],
            "new": ["room1", "room2", "room3", "room4", "room5", "room6"],
        },
    }

    # Simulate config update for each bot
    for user_id, bot_config in bots_config.items():
        mock_client = AsyncMock()
        mock_client.user_id = user_id

        # Determine which agent this is
        if "agent1" in user_id:
            agent_user = mock_agent_users["agent1"]
        elif "agent2" in user_id:
            agent_user = mock_agent_users["agent2"]
        elif "agent3" in user_id:
            agent_user = mock_agent_users["agent3"]
        elif "team1" in user_id:
            agent_user = mock_agent_users["team1"]
        else:
            agent_user = mock_agent_users[ROUTER_AGENT_NAME]

        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=bot_config["new"],
        )
        setup_test_bot(bot, mock_client)

        await bot.join_configured_rooms()
        await bot.leave_unconfigured_rooms()

    # Verify final room membership state
    assert set(room_memberships.get("room1", [])) == {
        "@mindroom_agent1:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room2", [])) == {
        "@mindroom_agent2:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room3", [])) == {
        "@mindroom_agent2:localhost",
        "@mindroom_team1:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room4", [])) == {
        "@mindroom_agent1:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room5", [])) == {
        "@mindroom_agent3:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room6", [])) == {
        "@mindroom_team1:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
