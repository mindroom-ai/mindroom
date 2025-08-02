"""Tests for thread-specific agent invitations."""

from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.thread_invites import THREAD_INVITE_EVENT_TYPE, ThreadInviteManager


@pytest.fixture
def mock_client():
    """Create a mock Matrix client."""
    client = AsyncMock(spec=nio.AsyncClient)
    return client


@pytest.fixture
def invite_manager(mock_client):
    """Create a fresh ThreadInviteManager instance."""
    return ThreadInviteManager(mock_client)


@pytest.mark.asyncio
async def test_add_invite(invite_manager, mock_client):
    """Test adding a thread invitation."""
    # Mock the room_put_state response
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$event123", room_id="!room456")

    await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
    )

    # Verify the state event was created
    assert mock_client.room_put_state.called
    call_args = mock_client.room_put_state.call_args
    assert call_args[1]["room_id"] == "!room456"
    assert call_args[1]["event_type"] == THREAD_INVITE_EVENT_TYPE
    assert call_args[1]["state_key"] == "$thread123:calculator"
    content = call_args[1]["content"]
    assert content["invited_by"] == "@user:example.com"
    assert "invited_at" in content
    assert content["last_response"] is None


@pytest.mark.asyncio
async def test_get_thread_agents(invite_manager, mock_client):
    """Test getting agents invited to a thread."""
    # Mock room_get_state to return invitations
    mock_events = [
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$thread123:calculator",
            "content": {"invited_by": "@user:example.com"},
        },
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$thread123:research",
            "content": {"invited_by": "@user:example.com"},
        },
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$other_thread:code",
            "content": {"invited_by": "@user:example.com"},
        },
    ]
    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(events=mock_events, room_id="!room456")

    # Check thread agents
    agents = await invite_manager.get_thread_agents("$thread123", "!room456")
    assert set(agents) == {"calculator", "research"}

    other_agents = await invite_manager.get_thread_agents("$other_thread", "!room456")
    assert other_agents == ["code"]

    no_agents = await invite_manager.get_thread_agents("$nonexistent", "!room456")
    assert no_agents == []


@pytest.mark.asyncio
async def test_is_agent_invited_to_thread(invite_manager, mock_client):
    """Test checking if an agent is invited to a thread."""
    # Mock room_get_state_event for checking invitations
    mock_client.room_get_state_event.side_effect = [
        # First call: calculator is invited
        nio.RoomGetStateEventResponse(
            content={"invited_by": "@user:example.com"},
            event_type=THREAD_INVITE_EVENT_TYPE,
            state_key="$thread123:calculator",
            room_id="!room456",
        ),
        # Second call: research is not invited (404)
        nio.RoomGetStateEventError(status_code="M_NOT_FOUND", message="Not found"),
        # Third call: calculator in other thread (404)
        nio.RoomGetStateEventError(status_code="M_NOT_FOUND", message="Not found"),
    ]

    assert await invite_manager.is_agent_invited_to_thread("$thread123", "!room456", "calculator")
    assert not await invite_manager.is_agent_invited_to_thread("$thread123", "!room456", "research")
    assert not await invite_manager.is_agent_invited_to_thread("$other_thread", "!room456", "calculator")


@pytest.mark.asyncio
async def test_get_agent_threads(invite_manager, mock_client):
    """Test getting threads an agent is invited to."""
    # Mock room_get_state to return invitations for different agents
    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread1:calculator",
                "content": {"invited_by": "@user:example.com"},
            },
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread2:calculator",
                "content": {"invited_by": "@user:example.com"},
            },
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread3:research",
                "content": {"invited_by": "@user:example.com"},
            },
        ],
        room_id="!room456",
    )

    # Check threads per agent
    calc_threads = await invite_manager.get_agent_threads("!room456", "calculator")
    assert set(calc_threads) == {"$thread1", "$thread2"}

    research_threads = await invite_manager.get_agent_threads("!room456", "research")
    assert research_threads == ["$thread3"]

    no_threads = await invite_manager.get_agent_threads("!room456", "code")
    assert no_threads == []


@pytest.mark.asyncio
async def test_remove_invite(invite_manager, mock_client):
    """Test removing an invitation."""
    # Mock is_agent_invited_to_thread check (done internally by remove_invite)
    mock_client.room_get_state_event.side_effect = [
        # First call: check if exists (yes)
        nio.RoomGetStateEventResponse(
            content={"invited_by": "@user:example.com"},
            event_type=THREAD_INVITE_EVENT_TYPE,
            state_key="$thread123:calculator",
            room_id="!room456",
        ),
        # Second call: check if exists (no)
        nio.RoomGetStateEventError(status_code="M_NOT_FOUND", message="Not found"),
    ]

    # Mock room_put_state for removal
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$remove123", room_id="!room456")

    # Remove existing invitation
    removed = await invite_manager.remove_invite("$thread123", "!room456", "calculator")
    assert removed is True

    # Verify empty content was sent to remove the state
    call_args = mock_client.room_put_state.call_args
    assert call_args[1]["content"] == {}
    assert call_args[1]["state_key"] == "$thread123:calculator"

    # Try to remove non-existent invitation
    removed = await invite_manager.remove_invite("$thread123", "!room456", "calculator")
    assert removed is False


@pytest.mark.asyncio
async def test_update_last_response(invite_manager, mock_client):
    """Test updating last response time."""
    # Mock getting the current state
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "invited_by": "@user:example.com",
            "invited_at": "2024-01-01T10:00:00",
            "last_response": None,
        },
        event_type=THREAD_INVITE_EVENT_TYPE,
        state_key="$thread123:calculator",
        room_id="!room456",
    )

    # Mock updating the state
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$update123", room_id="!room456")

    await invite_manager.update_last_response(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
    )

    # Verify the state was updated
    assert mock_client.room_put_state.called
    call_args = mock_client.room_put_state.call_args
    content = call_args[1]["content"]
    assert "last_response" in content
    assert content["last_response"] is not None  # Should be current timestamp


@pytest.mark.asyncio
async def test_cleanup_inactive_agents(invite_manager, mock_client):
    """Test cleanup of inactive agents."""
    from datetime import datetime, timedelta

    # Mock room_get_state to return some active and inactive invitations
    now = datetime.now()
    old_time = (now - timedelta(hours=25)).isoformat()  # 25 hours ago
    recent_time = (now - timedelta(hours=1)).isoformat()  # 1 hour ago

    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread1:inactive_agent",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": old_time,
                    "last_response": old_time,  # Last response 25 hours ago
                },
            },
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread2:active_agent",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": old_time,
                    "last_response": recent_time,  # Recent response
                },
            },
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread3:never_responded",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": old_time,
                    "last_response": None,  # Never responded, invited 25 hours ago
                },
            },
        ],
        room_id="!room456",
    )

    # Mock room_kick responses
    mock_client.room_kick.side_effect = [
        nio.RoomKickResponse(),  # Success for inactive_agent
        nio.RoomKickResponse(),  # Success for never_responded
    ]

    # Mock room_put_state for removing invitations
    mock_client.room_put_state.side_effect = [
        nio.RoomPutStateResponse(event_id="$remove1", room_id="!room456"),
        nio.RoomPutStateResponse(event_id="$remove2", room_id="!room456"),
    ]

    # Run cleanup
    removed_count = await invite_manager.cleanup_inactive_agents("!room456", inactivity_hours=24)

    # Should have removed 2 agents (inactive_agent and never_responded)
    assert removed_count == 2
    assert mock_client.room_kick.call_count == 2

    # Check that the correct agents were kicked
    kick_calls = mock_client.room_kick.call_args_list
    kicked_users = [call[1]["user_id"] for call in kick_calls]
    assert "@inactive_agent:mindroom.space" in kicked_users
    assert "@never_responded:mindroom.space" in kicked_users


@pytest.mark.asyncio
async def test_error_handling(invite_manager, mock_client):
    """Test error handling in various scenarios."""
    # Test add_invite failure
    mock_client.room_put_state.return_value = nio.RoomPutStateError(status_code="M_FORBIDDEN", message="Forbidden")

    with pytest.raises(RuntimeError, match="Failed to add thread invitation"):
        await invite_manager.add_invite(
            thread_id="$thread123",
            room_id="!room456",
            agent_name="calculator",
            invited_by="@user:example.com",
        )

    # Test get_thread_agents with room state error
    mock_client.room_get_state.return_value = nio.RoomGetStateError(status_code="M_FORBIDDEN", message="Forbidden")

    agents = await invite_manager.get_thread_agents("$thread123", "!room456")
    assert agents == []

    # Test remove_invite failure
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"invited_by": "@user:example.com"},
        event_type=THREAD_INVITE_EVENT_TYPE,
        state_key="$thread123:calculator",
        room_id="!room456",
    )
    mock_client.room_put_state.return_value = nio.RoomPutStateError(status_code="M_FORBIDDEN", message="Forbidden")

    removed = await invite_manager.remove_invite("$thread123", "!room456", "calculator")
    assert removed is False
