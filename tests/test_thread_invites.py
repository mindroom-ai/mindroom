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
async def test_cleanup_inactive_agents(invite_manager, mock_client):
    """Test cleanup of inactive agents."""
    from datetime import datetime, timedelta

    # Mock room_get_state to return some expired and non-expired invitations
    now = datetime.now()
    old_time = (now - timedelta(hours=25)).isoformat()  # 25 hours ago - expired
    recent_time = (now - timedelta(hours=1)).isoformat()  # 1 hour ago - not expired

    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread1:expired_agent",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": old_time,  # Invited 25 hours ago
                },
            },
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread2:active_agent",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": recent_time,  # Recent invitation
                },
            },
        ],
        room_id="!room456",
    )

    # Mock room_kick response for expired agent
    mock_client.room_kick.return_value = nio.RoomKickResponse()

    # Mock room_put_state for removing invitation
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$remove1", room_id="!room456")

    # Mock room_messages to show expired_agent never sent messages
    mock_client.room_messages.return_value = nio.RoomMessagesResponse(
        chunk=[],  # No messages from agents
        start="",
        end="",
        room_id="!room456",
    )
    
    # Run cleanup
    removed_count = await invite_manager.cleanup_inactive_agents("!room456", timeout_hours=24)

    # Should have removed 1 agent (expired_agent)
    assert removed_count == 1
    assert mock_client.room_kick.call_count == 1

    # Check that the correct agent was kicked
    kick_call = mock_client.room_kick.call_args
    assert kick_call[0][1] == "@expired_agent:mindroom.space"  # Second positional arg is user_id


@pytest.mark.asyncio
async def test_cleanup_with_activity(invite_manager, mock_client):
    """Test cleanup correctly handles agents with recent activity."""
    from datetime import datetime, timedelta
    
    # Mock room_get_state to return invitations
    now = datetime.now()
    old_time = (now - timedelta(hours=30)).isoformat()  # 30 hours ago
    
    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread1:active_agent",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": old_time,  # Invited 30 hours ago
                },
            },
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread2:inactive_agent",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": old_time,  # Invited 30 hours ago
                },
            },
        ],
        room_id="!room456",
    )
    
    # Mock room_messages to show active_agent sent a message 1 hour ago
    from unittest.mock import Mock
    
    recent_timestamp = (now - timedelta(hours=1)).timestamp()
    old_timestamp = (now - timedelta(hours=26)).timestamp()
    
    # Create mock messages that look like nio.RoomMessageText
    active_msg = Mock(spec=nio.RoomMessageText)
    active_msg.sender = "@active_agent:mindroom.space"
    active_msg.server_timestamp = recent_timestamp * 1000  # Convert to milliseconds
    
    inactive_msg = Mock(spec=nio.RoomMessageText)
    inactive_msg.sender = "@inactive_agent:mindroom.space"
    inactive_msg.server_timestamp = old_timestamp * 1000  # Convert to milliseconds
    
    mock_client.room_messages.return_value = nio.RoomMessagesResponse(
        chunk=[active_msg, inactive_msg],
        start="",
        end="",
        room_id="!room456",
    )
    
    # Mock room_kick - should only kick inactive_agent
    mock_client.room_kick.return_value = nio.RoomKickResponse()
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$remove1", room_id="!room456")
    
    # Run cleanup
    removed_count = await invite_manager.cleanup_inactive_agents("!room456", timeout_hours=24)
    
    # Should have removed only 1 agent (inactive_agent)
    assert removed_count == 1
    assert mock_client.room_kick.call_count == 1
    
    # Check that only inactive_agent was kicked
    kick_call = mock_client.room_kick.call_args
    assert kick_call[0][1] == "@inactive_agent:mindroom.space"  # Second positional arg is user_id


@pytest.mark.asyncio
async def test_error_handling(invite_manager, mock_client):
    """Test error handling in various scenarios."""
    # Test add_invite failure - no longer raises since we removed error handling
    mock_client.room_put_state.return_value = nio.RoomPutStateError(status_code="M_FORBIDDEN", message="Forbidden")

    # Should not raise anymore - just returns
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
