"""Tests for config auto-reload and room membership updates."""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from mindroom.agent_config import ROUTER_AGENT_NAME
from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.matrix import AgentMatrixUser
from mindroom.models import AgentConfig, Config, TeamConfig


@pytest.fixture
def initial_config():
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
    )


@pytest.fixture
def updated_config():
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
    )


@pytest.fixture
def mock_agent_users():
    """Create mock agent users."""
    return {
        ROUTER_AGENT_NAME: AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id=f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
            display_name="RouterAgent",
            password="test_password",
        ),
        "agent1": AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password="test_password",
        ),
        "agent2": AgentMatrixUser(
            agent_name="agent2",
            user_id="@mindroom_agent2:localhost",
            display_name="Agent 2",
            password="test_password",
        ),
        "agent3": AgentMatrixUser(
            agent_name="agent3",
            user_id="@mindroom_agent3:localhost",
            display_name="Agent 3",
            password="test_password",
        ),
        "team1": AgentMatrixUser(
            agent_name="team1",
            user_id="@mindroom_team1:localhost",
            display_name="Team 1",
            password="test_password",
        ),
    }


@pytest.mark.asyncio
async def test_agent_joins_new_rooms_on_config_reload(initial_config, updated_config, mock_agent_users, monkeypatch):
    """Test that agents join new rooms when their configuration is updated."""
    # Track room operations
    joined_rooms = {}
    left_rooms = {}

    async def mock_join_room(client, room_id):
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    async def mock_leave_room(client, room_id):
        user_id = client.user_id
        if user_id not in left_rooms:
            left_rooms[user_id] = []
        left_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.bot.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(client, room_id):
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases):
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(client):
        user_id = client.user_id
        if "agent1" in user_id:
            return ["room1", "room2"]  # agent1 is currently in room1 and room2
        elif "agent2" in user_id:
            return ["room1"]  # agent2 is currently in room1
        elif "team1" in user_id:
            return ["room3"]  # team1 is currently in room3
        elif ROUTER_AGENT_NAME in user_id:
            return ["room1", "room2", "room3"]  # router is in all initial rooms
        return []

    monkeypatch.setattr("mindroom.bot.get_joined_rooms", mock_get_joined_rooms)

    # Create agent1 bot with initial config
    agent1_bot = AgentBot(
        agent_user=mock_agent_users["agent1"],
        storage_path=Path("/tmp/test"),
        rooms=["room1", "room2"],  # Initial rooms
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_agent1:localhost"
    agent1_bot.client = mock_client

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
async def test_router_updates_rooms_on_config_reload(initial_config, updated_config, mock_agent_users, monkeypatch):
    """Test that the router updates its room list when agents/teams change their rooms."""
    # Track room operations
    joined_rooms = []
    left_rooms = []

    async def mock_join_room(client, room_id):
        joined_rooms.append(room_id)
        return True

    async def mock_leave_room(client, room_id):
        left_rooms.append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.bot.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(client, room_id):
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases):
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(client):
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
    router_bot = AgentBot(
        agent_user=mock_agent_users[ROUTER_AGENT_NAME],
        storage_path=Path("/tmp/test"),
        rooms=list(updated_router_rooms),
    )
    mock_client = AsyncMock()
    mock_client.user_id = f"@mindroom_{ROUTER_AGENT_NAME}:localhost"
    router_bot.client = mock_client

    # Apply room updates
    await router_bot.join_configured_rooms()
    await router_bot.leave_unconfigured_rooms()

    # Verify router joined new rooms
    for new_room in ["room4", "room5", "room6"]:
        assert new_room in joined_rooms

    # Router should not leave any rooms (all initial rooms still have agents)
    assert len(left_rooms) == 0


@pytest.mark.asyncio
async def test_new_agent_joins_rooms_on_config_reload(initial_config, updated_config, mock_agent_users, monkeypatch):
    """Test that new agents are created and join their configured rooms."""
    # Track room operations
    joined_rooms = {}

    async def mock_ensure_all_agent_users(homeserver):
        # Return both existing and new agent users
        return mock_agent_users

    monkeypatch.setattr("mindroom.matrix.users.ensure_all_agent_users", mock_ensure_all_agent_users)

    async def mock_join_room(client, room_id):
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(client, room_id):
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases):
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms
    async def mock_get_joined_rooms(client):
        return []  # New agent has no rooms initially

    monkeypatch.setattr("mindroom.bot.get_joined_rooms", mock_get_joined_rooms)

    # Create agent3 bot (new agent in updated config)
    agent3_bot = AgentBot(
        agent_user=mock_agent_users["agent3"],
        storage_path=Path("/tmp/test"),
        rooms=["room5"],  # From updated config
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_agent3:localhost"
    agent3_bot.client = mock_client

    # Apply room updates for new agent
    await agent3_bot.join_configured_rooms()

    # Verify agent3 joined its configured room
    assert "room5" in joined_rooms.get("@mindroom_agent3:localhost", [])


@pytest.mark.asyncio
async def test_team_room_changes_on_config_reload(initial_config, updated_config, mock_agent_users, monkeypatch):
    """Test that teams update their room memberships when configuration changes."""
    # Track room operations
    joined_rooms = {}
    left_rooms = {}

    async def mock_join_room(client, room_id):
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    async def mock_leave_room(client, room_id):
        user_id = client.user_id
        if user_id not in left_rooms:
            left_rooms[user_id] = []
        left_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.bot.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(client, room_id):
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases):
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(client):
        user_id = client.user_id
        if "team1" in user_id:
            return ["room3"]  # team1 is currently only in room3
        return []

    monkeypatch.setattr("mindroom.bot.get_joined_rooms", mock_get_joined_rooms)

    # Create team1 bot with updated config
    team1_bot = AgentBot(
        agent_user=mock_agent_users["team1"],
        storage_path=Path("/tmp/test"),
        rooms=["room3", "room6"],  # Updated rooms: added room6
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_team1:localhost"
    team1_bot.client = mock_client

    # Apply room updates
    await team1_bot.join_configured_rooms()
    await team1_bot.leave_unconfigured_rooms()

    # Verify team1 joined room6 (new room)
    assert "room6" in joined_rooms.get("@mindroom_team1:localhost", [])
    # Team1 should not leave room3 (still configured)
    assert "room3" not in left_rooms.get("@mindroom_team1:localhost", [])


@pytest.mark.asyncio
async def test_orchestrator_handles_config_reload(initial_config, updated_config, mock_agent_users, monkeypatch):
    """Test that the orchestrator properly handles config reloads and updates all bots."""
    # Track config loads
    config_loads = [initial_config, updated_config]
    load_count = [0]

    def mock_load_config(config_path=None):
        result = config_loads[min(load_count[0], len(config_loads) - 1)]
        load_count[0] += 1
        return result

    monkeypatch.setattr("mindroom.bot.load_config", mock_load_config)

    async def mock_ensure_all_agent_users(homeserver):
        return mock_agent_users

    monkeypatch.setattr("mindroom.matrix.users.ensure_all_agent_users", mock_ensure_all_agent_users)

    def mock_resolve_room_aliases(aliases):
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Create orchestrator
    orchestrator = MultiAgentOrchestrator(storage_path=Path("/tmp/test"))

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

    # Mock bot operations for update
    for bot in orchestrator.agent_bots.values():
        bot.stop = AsyncMock()
        bot.start = AsyncMock()
        bot.ensure_user_account = AsyncMock()
        bot.sync_forever = AsyncMock(side_effect=asyncio.CancelledError())

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
async def test_room_membership_state_after_config_update(initial_config, updated_config, mock_agent_users, monkeypatch):
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

    def update_room_membership(user_id, room_id, action):
        """Update simulated room membership."""
        if action == "join":
            if room_id not in room_memberships:
                room_memberships[room_id] = []
            if user_id not in room_memberships[room_id]:
                room_memberships[room_id].append(user_id)
        elif action == "leave":
            if room_id in room_memberships and user_id in room_memberships[room_id]:
                room_memberships[room_id].remove(user_id)

    async def mock_join_room(client, room_id):
        update_room_membership(client.user_id, room_id, "join")
        return True

    async def mock_leave_room(client, room_id):
        update_room_membership(client.user_id, room_id, "leave")
        return True

    monkeypatch.setattr("mindroom.bot.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.bot.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(client, room_id):
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases):
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms based on room_memberships
    async def mock_get_joined_rooms(client):
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
    for user_id, config in bots_config.items():
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

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=Path("/tmp/test"),
            rooms=config["new"],
        )
        bot.client = mock_client

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
