"""Tests for thread-specific agent invitations."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.thread_invites import THREAD_INVITE_EVENT_TYPE, ThreadInvite, ThreadInviteManager


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

    invite = await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
        duration_hours=2,
    )

    assert invite.thread_id == "$thread123"
    assert invite.room_id == "!room456"
    assert invite.agent_name == "calculator"
    assert invite.invited_by == "@user:example.com"
    assert invite.expires_at is not None
    assert invite.expires_at > datetime.now()


@pytest.mark.asyncio
async def test_add_invite_no_expiry(invite_manager, mock_client):
    """Test adding a thread invitation with no expiry."""
    # Mock the room_put_state response
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$event123", room_id="!room456")

    invite = await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
        duration_hours=None,
    )

    assert invite.expires_at is None
    assert not invite.is_expired()


@pytest.mark.asyncio
async def test_get_thread_agents(invite_manager, mock_client):
    """Test getting agents invited to a thread."""
    # Mock room_put_state for adding invites
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$event123", room_id="!room456")

    # Add multiple invitations
    await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
    )
    await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="research",
        invited_by="@user:example.com",
    )
    await invite_manager.add_invite(
        thread_id="$other_thread",
        room_id="!room456",
        agent_name="code",
        invited_by="@user:example.com",
    )

    # Mock room_get_state to return our invitations
    mock_events = [
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$thread123:calculator",
            "content": ThreadInvite(
                thread_id="$thread123",
                room_id="!room456",
                agent_name="calculator",
                invited_by="@user:example.com",
                invited_at=datetime.now(),
            ).to_dict(),
        },
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$thread123:research",
            "content": ThreadInvite(
                thread_id="$thread123",
                room_id="!room456",
                agent_name="research",
                invited_by="@user:example.com",
                invited_at=datetime.now(),
            ).to_dict(),
        },
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$other_thread:code",
            "content": ThreadInvite(
                thread_id="$other_thread",
                room_id="!room456",
                agent_name="code",
                invited_by="@user:example.com",
                invited_at=datetime.now(),
            ).to_dict(),
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
    # Mock room_put_state for adding invite
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$event123", room_id="!room456")

    await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
    )

    # Mock room_get_state_event for checking invitations
    mock_client.room_get_state_event.side_effect = [
        # First call: calculator is invited
        nio.RoomGetStateEventResponse(
            content=ThreadInvite(
                thread_id="$thread123",
                room_id="!room456",
                agent_name="calculator",
                invited_by="@user:example.com",
                invited_at=datetime.now(),
            ).to_dict(),
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
    # Mock room_put_state for adding invites
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$event123", room_id="!room456")

    # Add invitations across different threads and rooms
    await invite_manager.add_invite(
        thread_id="$thread1",
        room_id="!room1",
        agent_name="calculator",
        invited_by="@user:example.com",
    )
    await invite_manager.add_invite(
        thread_id="$thread2",
        room_id="!room1",
        agent_name="calculator",
        invited_by="@user:example.com",
    )
    await invite_manager.add_invite(
        thread_id="$thread3",
        room_id="!room2",
        agent_name="calculator",
        invited_by="@user:example.com",
    )

    # Mock room_get_state to return invitations for room1
    room1_events = [
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$thread1:calculator",
            "content": ThreadInvite(
                thread_id="$thread1",
                room_id="!room1",
                agent_name="calculator",
                invited_by="@user:example.com",
                invited_at=datetime.now(),
            ).to_dict(),
        },
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$thread2:calculator",
            "content": ThreadInvite(
                thread_id="$thread2",
                room_id="!room1",
                agent_name="calculator",
                invited_by="@user:example.com",
                invited_at=datetime.now(),
            ).to_dict(),
        },
    ]

    # Mock room_get_state to return invitations for room2
    room2_events = [
        {
            "type": THREAD_INVITE_EVENT_TYPE,
            "state_key": "$thread3:calculator",
            "content": ThreadInvite(
                thread_id="$thread3",
                room_id="!room2",
                agent_name="calculator",
                invited_by="@user:example.com",
                invited_at=datetime.now(),
            ).to_dict(),
        },
    ]

    mock_client.room_get_state.side_effect = [
        nio.RoomGetStateResponse(events=room1_events, room_id="!room1"),
        nio.RoomGetStateResponse(events=room2_events, room_id="!room2"),
        nio.RoomGetStateResponse(events=[], room_id="!room1"),  # No threads for research
    ]

    # Check threads per room
    room1_threads = await invite_manager.get_agent_threads("!room1", "calculator")
    assert set(room1_threads) == {"$thread1", "$thread2"}

    room2_threads = await invite_manager.get_agent_threads("!room2", "calculator")
    assert room2_threads == ["$thread3"]

    no_threads = await invite_manager.get_agent_threads("!room1", "research")
    assert no_threads == []


@pytest.mark.asyncio
async def test_remove_invite(invite_manager, mock_client):
    """Test removing an invitation."""
    # Mock room_put_state for adding invites
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$event123", room_id="!room456")

    # Add invitations
    await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
    )
    await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="research",
        invited_by="@user:example.com",
    )

    # Mock is_agent_invited_to_thread check (done internally by remove_invite)
    mock_client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content=ThreadInvite(
            thread_id="$thread123",
            room_id="!room456",
            agent_name="calculator",
            invited_by="@user:example.com",
            invited_at=datetime.now(),
        ).to_dict(),
        event_type=THREAD_INVITE_EVENT_TYPE,
        state_key="$thread123:calculator",
        room_id="!room456",
    )

    # Remove one invitation
    removed = await invite_manager.remove_invite("$thread123", "!room456", "calculator")
    assert removed is True

    # Mock room_get_state to return only research invitation
    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread123:research",
                "content": ThreadInvite(
                    thread_id="$thread123",
                    room_id="!room456",
                    agent_name="research",
                    invited_by="@user:example.com",
                    invited_at=datetime.now(),
                ).to_dict(),
            },
        ],
        room_id="!room456",
    )

    # Check remaining agents
    agents = await invite_manager.get_thread_agents("$thread123", "!room456")
    assert agents == ["research"]

    # Try to remove non-existent invitation (mock not found)
    mock_client.room_get_state_event.side_effect = [
        nio.RoomGetStateEventError(status_code="M_NOT_FOUND", message="Not found")
    ]
    removed = await invite_manager.remove_invite("$thread123", "!room456", "calculator")
    assert removed is False


@pytest.mark.asyncio
async def test_expired_invitations(invite_manager, mock_client):
    """Test handling of expired invitations."""
    # Create an already expired invitation
    expired_invite = ThreadInvite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
        invited_at=datetime.now() - timedelta(hours=2),
        expires_at=datetime.now() - timedelta(hours=1),
    )

    assert expired_invite.is_expired()

    # Mock room_get_state to return expired invitation
    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread123:calculator",
                "content": expired_invite.to_dict(),
            },
        ],
        room_id="!room456",
    )

    # Check that expired invites are filtered out
    agents = await invite_manager.get_thread_agents("$thread123", "!room456")
    assert agents == []

    threads = await invite_manager.get_agent_threads("!room456", "calculator")
    assert threads == []


@pytest.mark.asyncio
async def test_cleanup_expired_invites(invite_manager, mock_client):
    """Test cleanup of expired invitations."""
    # Create active and expired invitations
    active_invite = ThreadInvite(
        thread_id="$thread1",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
        invited_at=datetime.now(),
        expires_at=datetime.now() + timedelta(hours=2),
    )

    expired_invite = ThreadInvite(
        thread_id="$thread2",
        room_id="!room456",
        agent_name="research",
        invited_by="@user:example.com",
        invited_at=datetime.now() - timedelta(hours=2),
        expires_at=datetime.now() - timedelta(hours=1),
    )

    # Mock room_get_state to return both invitations
    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(
        events=[
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread1:calculator",
                "content": active_invite.to_dict(),
            },
            {
                "type": THREAD_INVITE_EVENT_TYPE,
                "state_key": "$thread2:research",
                "content": expired_invite.to_dict(),
            },
        ],
        room_id="!room456",
    )

    # Mock room_put_state for cleanup (removing expired invitation)
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$cleanup123", room_id="!room456")

    # Run cleanup
    removed_count = await invite_manager.cleanup_expired("!room456")
    assert removed_count == 1

    # Verify room_put_state was called to remove the expired invitation
    # Should have been called once to remove the expired invite
    calls = [
        call
        for call in mock_client.room_put_state.call_args_list
        if call[1].get("state_key") == "$thread2:research" and call[1].get("content") == {}
    ]
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_concurrent_access(invite_manager, mock_client):
    """Test thread-safe concurrent access to invite manager."""
    # Mock room_put_state for adding invites
    mock_client.room_put_state.return_value = nio.RoomPutStateResponse(event_id="$event123", room_id="!room456")

    async def add_invites(agent_name: str, count: int):
        for i in range(count):
            await invite_manager.add_invite(
                thread_id=f"$thread_{agent_name}_{i}",
                room_id="!room456",
                agent_name=agent_name,
                invited_by="@user:example.com",
            )

    # Add invitations concurrently
    await asyncio.gather(
        add_invites("calculator", 10),
        add_invites("research", 10),
        add_invites("code", 10),
    )

    # Create mock events for all invitations
    all_events = []
    for agent_name in ["calculator", "research", "code"]:
        for i in range(10):
            all_events.append(
                {
                    "type": THREAD_INVITE_EVENT_TYPE,
                    "state_key": f"$thread_{agent_name}_{i}:{agent_name}",
                    "content": ThreadInvite(
                        thread_id=f"$thread_{agent_name}_{i}",
                        room_id="!room456",
                        agent_name=agent_name,
                        invited_by="@user:example.com",
                        invited_at=datetime.now(),
                    ).to_dict(),
                }
            )

    # Mock room_get_state to return all events
    mock_client.room_get_state.return_value = nio.RoomGetStateResponse(events=all_events, room_id="!room456")

    # Verify all invitations were added
    calc_threads = await invite_manager.get_agent_threads("!room456", "calculator")
    assert len(calc_threads) == 10

    research_threads = await invite_manager.get_agent_threads("!room456", "research")
    assert len(research_threads) == 10

    code_threads = await invite_manager.get_agent_threads("!room456", "code")
    assert len(code_threads) == 10
