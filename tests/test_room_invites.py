"""Tests for room-level agent invitations with activity tracking."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom.room_invites import AgentActivity, RoomInvite, RoomInviteManager


@pytest.fixture
def invite_manager():
    """Create a fresh RoomInviteManager instance."""
    return RoomInviteManager()


@pytest.mark.asyncio
async def test_add_room_invite(invite_manager):
    """Test adding a room invitation."""
    invite = await invite_manager.add_room_invite(
        room_id="!room123",
        agent_name="calculator",
        invited_by="@user:example.com",
        inactivity_timeout_hours=24,
    )

    assert invite.room_id == "!room123"
    assert invite.agent_name == "calculator"
    assert invite.invited_by == "@user:example.com"
    assert invite.inactivity_timeout_hours == 24
    assert invite.last_activity == invite.invited_at
    assert not invite.is_inactive()


@pytest.mark.asyncio
async def test_record_agent_activity(invite_manager):
    """Test recording agent activity."""
    # Add invitation
    await invite_manager.add_room_invite(
        room_id="!room123",
        agent_name="calculator",
        invited_by="@user:example.com",
    )

    # Wait a bit to ensure time difference
    await asyncio.sleep(0.01)

    # Record activity
    await invite_manager.record_agent_activity("!room123", "calculator")

    # Check that activity was updated
    invite = invite_manager._room_invites["!room123"]["calculator"]
    assert invite.last_activity > invite.invited_at


@pytest.mark.asyncio
async def test_is_agent_invited_to_room(invite_manager):
    """Test checking if an agent is invited to a room."""
    # Initially not invited
    assert not await invite_manager.is_agent_invited_to_room("!room123", "calculator")

    # Add invitation
    await invite_manager.add_room_invite(
        room_id="!room123",
        agent_name="calculator",
        invited_by="@user:example.com",
    )

    # Now should be invited
    assert await invite_manager.is_agent_invited_to_room("!room123", "calculator")
    assert not await invite_manager.is_agent_invited_to_room("!room123", "research")
    assert not await invite_manager.is_agent_invited_to_room("!other_room", "calculator")


@pytest.mark.asyncio
async def test_get_room_invites(invite_manager):
    """Test getting all agents invited to a room."""
    # Add multiple invitations
    await invite_manager.add_room_invite(
        room_id="!room123",
        agent_name="calculator",
        invited_by="@user:example.com",
    )
    await invite_manager.add_room_invite(
        room_id="!room123",
        agent_name="research",
        invited_by="@user:example.com",
    )
    await invite_manager.add_room_invite(
        room_id="!other_room",
        agent_name="code",
        invited_by="@user:example.com",
    )

    # Check room invites
    room_agents = await invite_manager.get_room_invites("!room123")
    assert set(room_agents) == {"calculator", "research"}

    other_agents = await invite_manager.get_room_invites("!other_room")
    assert other_agents == ["code"]

    no_agents = await invite_manager.get_room_invites("!nonexistent")
    assert no_agents == []


@pytest.mark.asyncio
async def test_remove_room_invite(invite_manager):
    """Test removing a room invitation."""
    # Add invitations
    await invite_manager.add_room_invite(
        room_id="!room123",
        agent_name="calculator",
        invited_by="@user:example.com",
    )
    await invite_manager.add_room_invite(
        room_id="!room123",
        agent_name="research",
        invited_by="@user:example.com",
    )

    # Remove one invitation
    removed = await invite_manager.remove_room_invite("!room123", "calculator")
    assert removed is True

    # Check remaining agents
    agents = await invite_manager.get_room_invites("!room123")
    assert agents == ["research"]

    # Try to remove non-existent invitation
    removed = await invite_manager.remove_room_invite("!room123", "calculator")
    assert removed is False


@pytest.mark.asyncio
async def test_inactive_invitations(invite_manager):
    """Test handling of inactive invitations."""
    # Create an inactive invitation manually
    invite = RoomInvite(
        room_id="!room123",
        agent_name="calculator",
        invited_by="@user:example.com",
        invited_at=datetime.now() - timedelta(hours=25),
        last_activity=datetime.now() - timedelta(hours=25),
        inactivity_timeout_hours=24,
    )

    assert invite.is_inactive()

    # Add to manager manually
    invite_manager._room_invites["!room123"] = {"calculator": invite}

    # Check that inactive invites are filtered out
    agents = await invite_manager.get_room_invites("!room123")
    assert agents == []  # Inactive invite is filtered

    # But the invite still exists in storage
    inactive = await invite_manager.get_inactive_invites()
    assert inactive == [("!room123", "calculator")]


@pytest.mark.asyncio
async def test_cleanup_inactive_invites_no_client(invite_manager):
    """Test cleanup of inactive invitations without Matrix client."""
    # Add active invitation
    await invite_manager.add_room_invite(
        room_id="!room1",
        agent_name="active_agent",
        invited_by="@user:example.com",
        inactivity_timeout_hours=48,
    )

    # Add inactive invitation manually
    inactive_invite = RoomInvite(
        room_id="!room2",
        agent_name="inactive_agent",
        invited_by="@user:example.com",
        invited_at=datetime.now() - timedelta(hours=25),
        last_activity=datetime.now() - timedelta(hours=25),
        inactivity_timeout_hours=24,
    )
    invite_manager._room_invites["!room2"] = {"inactive_agent": inactive_invite}

    # Run cleanup without client
    removed_count = await invite_manager.cleanup_inactive_invites()
    assert removed_count == 1

    # Check that only active invitation remains
    assert "!room1" in invite_manager._room_invites
    assert "!room2" not in invite_manager._room_invites


@pytest.mark.asyncio
async def test_cleanup_inactive_invites_with_client(invite_manager):
    """Test cleanup of inactive invitations with Matrix client."""
    # Create mock client
    mock_client = AsyncMock()
    mock_response = MagicMock(spec=nio.RoomKickResponse)
    mock_client.room_kick.return_value = mock_response

    # Add inactive invitation manually
    inactive_invite = RoomInvite(
        room_id="!room123",
        agent_name="calculator",
        invited_by="@user:example.com",
        invited_at=datetime.now() - timedelta(hours=25),
        last_activity=datetime.now() - timedelta(hours=25),
        inactivity_timeout_hours=24,
    )
    invite_manager._room_invites["!room123"] = {"calculator": inactive_invite}

    # Run cleanup with client
    removed_count = await invite_manager.cleanup_inactive_invites(mock_client)
    assert removed_count == 1

    # Verify kick was called
    mock_client.room_kick.assert_called_once_with(
        "!room123",
        "@mindroom_calculator:localhost",
        reason="Inactive for 24 hours - automatic removal",
    )

    # Check that invitation was removed
    assert "!room123" not in invite_manager._room_invites


@pytest.mark.asyncio
async def test_agent_activity_tracking():
    """Test AgentActivity class."""
    activity = AgentActivity("calculator")

    # Initially no activity
    assert activity.get_last_activity("!room123") is None

    # Record activity
    activity.record_activity("!room123")
    last_activity = activity.get_last_activity("!room123")
    assert last_activity is not None
    assert isinstance(last_activity, datetime)

    # Record activity in another room
    activity.record_activity("!room456")
    assert activity.get_last_activity("!room456") is not None
    assert len(activity.room_activities) == 2


@pytest.mark.asyncio
async def test_concurrent_operations(invite_manager):
    """Test thread-safe concurrent operations."""

    async def add_invites(room_id: str, count: int):
        for i in range(count):
            await invite_manager.add_room_invite(
                room_id=room_id,
                agent_name=f"agent_{i}",
                invited_by="@user:example.com",
            )

    async def record_activities(room_id: str, agent_prefix: str, count: int):
        for i in range(count):
            await invite_manager.record_agent_activity(room_id, f"{agent_prefix}_{i}")

    # Run concurrent operations
    await asyncio.gather(
        add_invites("!room1", 10),
        add_invites("!room2", 10),
        record_activities("!room1", "agent", 5),
    )

    # Verify all invitations were added
    room1_agents = await invite_manager.get_room_invites("!room1")
    assert len(room1_agents) == 10

    room2_agents = await invite_manager.get_room_invites("!room2")
    assert len(room2_agents) == 10
