"""Tests for thread-specific agent invitations."""

import asyncio
from datetime import datetime, timedelta

import pytest

from mindroom.thread_invites import ThreadInvite, ThreadInviteManager


@pytest.fixture
def invite_manager():
    """Create a fresh ThreadInviteManager instance."""
    return ThreadInviteManager()


@pytest.mark.asyncio
async def test_add_invite(invite_manager):
    """Test adding a thread invitation."""
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
async def test_add_invite_no_expiry(invite_manager):
    """Test adding a thread invitation with no expiry."""
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
async def test_get_thread_agents(invite_manager):
    """Test getting agents invited to a thread."""
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

    # Check thread agents
    agents = await invite_manager.get_thread_agents("$thread123")
    assert set(agents) == {"calculator", "research"}

    other_agents = await invite_manager.get_thread_agents("$other_thread")
    assert other_agents == ["code"]

    no_agents = await invite_manager.get_thread_agents("$nonexistent")
    assert no_agents == []


@pytest.mark.asyncio
async def test_is_agent_invited_to_thread(invite_manager):
    """Test checking if an agent is invited to a thread."""
    await invite_manager.add_invite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
    )

    assert await invite_manager.is_agent_invited_to_thread("$thread123", "calculator")
    assert not await invite_manager.is_agent_invited_to_thread("$thread123", "research")
    assert not await invite_manager.is_agent_invited_to_thread("$other_thread", "calculator")


@pytest.mark.asyncio
async def test_get_agent_threads(invite_manager):
    """Test getting threads an agent is invited to."""
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

    # Check threads per room
    room1_threads = await invite_manager.get_agent_threads("!room1", "calculator")
    assert set(room1_threads) == {"$thread1", "$thread2"}

    room2_threads = await invite_manager.get_agent_threads("!room2", "calculator")
    assert room2_threads == ["$thread3"]

    no_threads = await invite_manager.get_agent_threads("!room1", "research")
    assert no_threads == []


@pytest.mark.asyncio
async def test_remove_invite(invite_manager):
    """Test removing an invitation."""
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

    # Remove one invitation
    removed = await invite_manager.remove_invite("$thread123", "calculator")
    assert removed is True

    # Check remaining agents
    agents = await invite_manager.get_thread_agents("$thread123")
    assert agents == ["research"]

    # Try to remove non-existent invitation
    removed = await invite_manager.remove_invite("$thread123", "calculator")
    assert removed is False


@pytest.mark.asyncio
async def test_expired_invitations(invite_manager):
    """Test handling of expired invitations."""
    # Create an already expired invitation
    invite = ThreadInvite(
        thread_id="$thread123",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
        invited_at=datetime.now() - timedelta(hours=2),
        expires_at=datetime.now() - timedelta(hours=1),
    )

    assert invite.is_expired()

    # Add expired invitation manually (for testing)
    invite_manager._invites["$thread123"] = [invite]
    invite_manager._agent_threads[("!room456", "calculator")] = {"$thread123"}

    # Check that expired invites are filtered out
    agents = await invite_manager.get_thread_agents("$thread123")
    assert agents == []

    threads = await invite_manager.get_agent_threads("!room456", "calculator")
    assert threads == []


@pytest.mark.asyncio
async def test_cleanup_expired_invites(invite_manager):
    """Test cleanup of expired invitations."""
    # Add a mix of expired and active invitations
    # Active invitation
    await invite_manager.add_invite(
        thread_id="$thread1",
        room_id="!room456",
        agent_name="calculator",
        invited_by="@user:example.com",
        duration_hours=2,
    )

    # Add expired invitations manually
    expired_invite = ThreadInvite(
        thread_id="$thread2",
        room_id="!room456",
        agent_name="research",
        invited_by="@user:example.com",
        invited_at=datetime.now() - timedelta(hours=2),
        expires_at=datetime.now() - timedelta(hours=1),
    )
    invite_manager._invites["$thread2"] = [expired_invite]
    invite_manager._agent_threads[("!room456", "research")] = {"$thread2"}

    # Run cleanup
    removed_count = await invite_manager.cleanup_expired_invites()
    assert removed_count == 1

    # Check that only active invitations remain
    assert "$thread1" in invite_manager._invites
    assert "$thread2" not in invite_manager._invites
    assert ("!room456", "calculator") in invite_manager._agent_threads
    assert ("!room456", "research") not in invite_manager._agent_threads


@pytest.mark.asyncio
async def test_concurrent_access(invite_manager):
    """Test thread-safe concurrent access to invite manager."""

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

    # Verify all invitations were added
    calc_threads = await invite_manager.get_agent_threads("!room456", "calculator")
    assert len(calc_threads) == 10

    research_threads = await invite_manager.get_agent_threads("!room456", "research")
    assert len(research_threads) == 10

    code_threads = await invite_manager.get_agent_threads("!room456", "code")
    assert len(code_threads) == 10
