"""Tests for automatic room invite functionality."""

from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.models import AgentConfig, Config, TeamConfig
from mindroom.room_cleanup import invite_all_missing_bots


@pytest.fixture
def mock_config():
    """Create a mock config with agents and teams."""
    config = Config(
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
                rooms=["room2"],
            ),
        },
    )
    return config


@pytest.mark.asyncio
async def test_invite_missing_bots(mock_config, monkeypatch):
    """Test that missing bots are invited to their configured rooms."""
    # Mock the config loading
    monkeypatch.setattr("mindroom.room_cleanup.load_config", lambda: mock_config)

    # Mock resolve_room_aliases to map room aliases to room IDs
    def mock_resolve_room_aliases(room_aliases):
        mapping = {
            "room1": "!room1:localhost",
            "room2": "!room2:localhost",
        }
        return [mapping.get(alias) for alias in room_aliases]

    monkeypatch.setattr("mindroom.matrix.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock extract_server_name_from_homeserver
    monkeypatch.setattr("mindroom.matrix.extract_server_name_from_homeserver", lambda x: "localhost")

    # Create mock client
    mock_client = AsyncMock()
    mock_client.homeserver = "http://localhost:8008"

    # Mock joined rooms response
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost", "!room2:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Mock room members responses
    # Room 1: has agent1, missing agent2 and router
    room1_members = MagicMock()
    room1_members.__class__ = nio.JoinedMembersResponse
    room1_members.members = {
        "@mindroom_agent1:localhost": {},
    }

    # Room 2: has nothing, missing agent1, team1, and router
    room2_members = MagicMock()
    room2_members.__class__ = nio.JoinedMembersResponse
    room2_members.members = {}

    # Set up joined_members to return different responses based on room_id
    async def mock_joined_members(room_id):
        if room_id == "!room1:localhost":
            return room1_members
        elif room_id == "!room2:localhost":
            return room2_members
        return MagicMock()

    mock_client.joined_members = mock_joined_members

    # Mock successful invite responses
    invite_response = MagicMock()
    invite_response.__class__ = nio.RoomInviteResponse
    mock_client.room_invite.return_value = invite_response

    # Call the function
    await invite_all_missing_bots(mock_client)

    # Check that invites were sent
    assert (
        mock_client.room_invite.call_count == 5
    )  # router to room1, agent2 to room1, router to room2, agent1 to room2, team1 to room2

    # Check the specific invites
    invite_calls = mock_client.room_invite.call_args_list
    invited_pairs = [(call[0][0], call[0][1]) for call in invite_calls]

    # Should have invited the following:
    # Room 1: router, agent2
    # Room 2: router, agent1, team1
    expected_invites = [
        ("!room1:localhost", "@mindroom_router:localhost"),
        ("!room1:localhost", "@mindroom_agent2:localhost"),
        ("!room2:localhost", "@mindroom_router:localhost"),
        ("!room2:localhost", "@mindroom_agent1:localhost"),
        ("!room2:localhost", "@mindroom_team1:localhost"),
    ]

    for expected in expected_invites:
        assert expected in invited_pairs, f"Expected invite {expected} not found"


@pytest.mark.asyncio
async def test_no_missing_bots(mock_config, monkeypatch):
    """Test that no invites are sent when all bots are already in their rooms."""
    # Mock the config loading
    monkeypatch.setattr("mindroom.room_cleanup.load_config", lambda: mock_config)

    # Mock resolve_room_aliases to map room aliases to room IDs
    def mock_resolve_room_aliases(room_aliases):
        mapping = {
            "room1": "!room1:localhost",
            "room2": "!room2:localhost",
        }
        return [mapping.get(alias) for alias in room_aliases]

    monkeypatch.setattr("mindroom.matrix.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock extract_server_name_from_homeserver
    monkeypatch.setattr("mindroom.matrix.extract_server_name_from_homeserver", lambda x: "localhost")

    # Create mock client
    mock_client = AsyncMock()
    mock_client.homeserver = "http://localhost:8008"

    # Mock joined rooms response
    joined_rooms_response = MagicMock()
    joined_rooms_response.__class__ = nio.JoinedRoomsResponse
    joined_rooms_response.rooms = ["!room1:localhost"]
    mock_client.joined_rooms.return_value = joined_rooms_response

    # Mock room members response - all configured bots are present
    room_members = MagicMock()
    room_members.__class__ = nio.JoinedMembersResponse
    room_members.members = {
        "@mindroom_router:localhost": {},
        "@mindroom_agent1:localhost": {},
        "@mindroom_agent2:localhost": {},
    }
    mock_client.joined_members.return_value = room_members

    # Call the function
    result = await invite_all_missing_bots(mock_client)

    # Check that no invites were sent
    assert mock_client.room_invite.call_count == 0
    assert result == {}
