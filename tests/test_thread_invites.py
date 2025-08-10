"""Tests for thread-specific agent invitations."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.thread_invites import AGENT_ACTIVITY_EVENT_TYPE, THREAD_INVITE_EVENT_TYPE, ThreadInviteManager


@pytest.fixture
def mock_client() -> AsyncMock:
    """Create a mock Matrix client."""
    return AsyncMock(spec=nio.AsyncClient)


@pytest.fixture
def invite_manager(mock_client: AsyncMock) -> ThreadInviteManager:
    """Create a fresh ThreadInviteManager instance."""
    return ThreadInviteManager(mock_client)


@pytest.mark.asyncio
async def test_add_invite(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
    """Test adding a thread invitation."""
    # Mock the room_put_state response (will be called twice - invite + activity)
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$event123", room_id="!room456")

    await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
    )

    # Verify both state events were created
    assert mock_client.room_put_state.call_count == 2

    # First call should be the invitation
    first_call = mock_client.room_put_state.call_args_list[0]
    assert first_call[1]["room_id"] == "!room456"
    assert first_call[1]["event_type"] == THREAD_INVITE_EVENT_TYPE
    assert first_call[1]["state_key"] == "$thread123:calculator"
    content = first_call[1]["content"]
    assert content["invited_by"] == "@user:example.com"
    assert "invited_at" in content
    # last_activity is now tracked separately
    assert "last_activity" not in content

    # Second call should be the activity update
    second_call = mock_client.room_put_state.call_args_list[1]
    assert second_call[1]["room_id"] == "!room456"
    assert second_call[1]["event_type"] == AGENT_ACTIVITY_EVENT_TYPE
    assert second_call[1]["state_key"] == "calculator"


@pytest.mark.asyncio
async def test_get_thread_agents(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
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
async def test_is_agent_invited_to_thread(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
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
async def test_get_agent_threads(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
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
async def test_remove_invite(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
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
async def test_cleanup_inactive_agents(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
    """Test cleanup of inactive agents using last_activity."""
    # Mock room_get_state to return some expired and non-expired invitations
    now = datetime.now(tz=UTC)
    old_time = (now - timedelta(hours=25)).isoformat()  # 25 hours ago - expired
    recent_time = (now - timedelta(hours=1)).isoformat()  # 1 hour ago - not expired

    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread1:expired_agent",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": old_time,
                },
            },
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread2:active_agent",
                "content": {
                    "invited_by": "@user:example.com",
                    "invited_at": old_time,
                },
            },
        ],
        room_id="!room456",
    )

    # Mock get_agent_activity for each agent
    recent_time = (now - timedelta(hours=1)).isoformat()
    mock_client.room_get_state_event.side_effect = [
        # expired_agent has no activity
        nio.RoomGetStateEventError(status_code="M_NOT_FOUND", message="Not found"),
        # active_agent has recent activity
        nio.RoomGetStateEventResponse(
            content={"last_activity": recent_time},
            event_type=AGENT_ACTIVITY_EVENT_TYPE,
            state_key="active_agent",
            room_id="!room456",
        ),
    ]

    # Mock room_kick response for expired agent
    mock_client.room_kick.return_value = nio.RoomKickResponse()

    # Mock room_put_state for removing invitation
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$remove1", room_id="!room456")

    # Run cleanup
    removed_count = await invite_manager.cleanup_inactive_agents("!room456", timeout_hours=24)

    # Should have removed 1 agent (expired_agent)
    assert removed_count == 1
    assert mock_client.room_kick.call_count == 1

    # Check that the correct agent was kicked
    kick_call = mock_client.room_kick.call_args
    assert kick_call[0][1] == "@mindroom_expired_agent:mindroom.space"  # Second positional arg is user_id


@pytest.mark.asyncio
async def test_get_invite_state(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
    """Test getting invitation state."""
    # Mock successful state retrieval
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "invited_by": "@user:example.com",
            "invited_at": "2024-01-01T10:00:00",
        },
        event_type=THREAD_INVITE_EVENT_TYPE,
        state_key="$thread123:calculator",
        room_id="!room456",
    )

    state = await invite_manager.get_invite_state("$thread123", "!room456", "calculator")
    assert state is not None
    assert state["invited_by"] == "@user:example.com"
    assert state["invited_at"] == "2024-01-01T10:00:00"

    # Mock not found
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventError(
        status_code="M_NOT_FOUND",
        message="Not found",
    )
    state = await invite_manager.get_invite_state("$thread123", "!room456", "unknown")
    assert state is None


@pytest.mark.asyncio
async def test_get_agent_activity(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
    """Test getting agent activity."""
    # Mock successful activity retrieval
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={"last_activity": "2024-01-01T12:00:00"},
        event_type=AGENT_ACTIVITY_EVENT_TYPE,
        state_key="calculator",
        room_id="!room456",
    )

    activity = await invite_manager.get_agent_activity("!room456", "calculator")
    assert activity == "2024-01-01T12:00:00"

    # Mock not found (no activity recorded)
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventError(
        status_code="M_NOT_FOUND",
        message="Not found",
    )
    activity = await invite_manager.get_agent_activity("!room456", "unknown")
    assert activity is None


@pytest.mark.asyncio
async def test_update_agent_activity(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
    """Test updating agent activity timestamp."""
    # Mock get_invite_state
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "invited_by": "@user:example.com",
            "invited_at": "2024-01-01T10:00:00",
            "last_activity": "2024-01-01T10:00:00",
        },
        event_type=THREAD_INVITE_EVENT_TYPE,
        state_key="$thread123:calculator",
        room_id="!room456",
    )

    # Mock room_put_state
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$update123", room_id="!room456")

    # Update activity (signature changed - no thread_id needed)
    await invite_manager.update_agent_activity("!room456", "calculator")

    # Verify state was updated
    assert mock_client.room_put_state.called
    call_args = mock_client.room_put_state.call_args
    assert call_args[1]["room_id"] == "!room456"
    assert call_args[1]["event_type"] == AGENT_ACTIVITY_EVENT_TYPE
    assert call_args[1]["state_key"] == "calculator"
    content = call_args[1]["content"]
    assert "last_activity" in content


@pytest.mark.asyncio
async def test_error_handling(invite_manager: ThreadInviteManager, mock_client: AsyncMock) -> None:
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
