"""Tests for the periodic cleanup functionality."""

import asyncio
import contextlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
import pytest_asyncio

from mindroom.bot import MultiAgentOrchestrator
from mindroom.room_invites import room_invite_manager
from mindroom.thread_invites import thread_invite_manager


@pytest_asyncio.fixture
async def cleanup_managers():
    """Clear invite managers before each test."""
    # Clear room invites
    async with room_invite_manager._lock:
        room_invite_manager._room_invites.clear()

    # Clear thread invites
    async with thread_invite_manager._lock:
        thread_invite_manager._invites.clear()
        thread_invite_manager._agent_threads.clear()

    yield

    # Clean up after test
    async with room_invite_manager._lock:
        room_invite_manager._room_invites.clear()

    async with thread_invite_manager._lock:
        thread_invite_manager._invites.clear()
        thread_invite_manager._agent_threads.clear()


@pytest.mark.asyncio
async def test_periodic_cleanup_runs_every_minute(cleanup_managers, tmp_path):
    """Test that periodic cleanup runs at the correct interval."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.running = True

    # Mock the cleanup methods to track calls
    thread_cleanup_mock = AsyncMock(return_value=0)
    AsyncMock(return_value=0)

    # Track sleep calls and iterations
    iteration_count = 0

    async def mock_sleep(duration):
        nonlocal iteration_count
        assert duration == 60  # Should sleep for 60 seconds
        iteration_count += 1
        if iteration_count >= 3:
            orchestrator.running = False
        return  # Don't actually sleep

    with (
        patch.object(thread_invite_manager, "cleanup_expired", thread_cleanup_mock),
        patch("mindroom.bot.asyncio.sleep", AsyncMock(side_effect=mock_sleep)),
    ):
        # Run cleanup task
        await orchestrator._periodic_cleanup()

        # Thread cleanup should have been called 3 times (once per iteration)
        assert thread_cleanup_mock.call_count == 3
        assert iteration_count == 3


@pytest.mark.asyncio
async def test_cleanup_checks_thread_activity_before_kicking(cleanup_managers, tmp_path):
    """Test that cleanup checks for active thread invites before kicking from room."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.running = True

    # Create a mock general bot with client
    mock_client = AsyncMock(spec=nio.AsyncClient)
    mock_bot = MagicMock()
    mock_bot.client = mock_client
    orchestrator.agent_bots = {"general": mock_bot}

    # Add an inactive room invite
    room_id = "!test_room:example.com"
    agent_name = "calculator"

    # Create invite that's inactive (25 hours old)
    old_time = datetime.now() - timedelta(hours=25)
    await room_invite_manager.add_invite(room_id, agent_name, "@user:example.com")

    # Manually set the last activity to old time
    async with room_invite_manager._lock:
        room_invite_manager._room_invites[room_id][agent_name].last_activity = old_time

    # Add an active thread invite in the same room
    thread_id = "$thread123"
    await thread_invite_manager.add_invite(thread_id, room_id, agent_name, "@user:example.com")

    # Mock room_kick to track if it's called
    mock_client.room_kick = AsyncMock(return_value=nio.RoomKickResponse())

    # Run one iteration of cleanup
    with patch("mindroom.bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = [None, asyncio.CancelledError()]  # Run once then cancel

        cleanup_task = asyncio.create_task(orchestrator._periodic_cleanup())
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    # Agent should NOT have been kicked because they have active thread invite
    mock_client.room_kick.assert_not_called()

    # Room invite should have updated activity
    async with room_invite_manager._lock:
        invite = room_invite_manager._room_invites[room_id][agent_name]
        # Activity should be updated to recent time
        assert (datetime.now() - invite.last_activity).total_seconds() < 60


@pytest.mark.asyncio
async def test_cleanup_kicks_truly_inactive_agents(cleanup_managers, tmp_path):
    """Test that cleanup kicks agents with no thread activity."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.running = True

    # Create a mock general bot with client
    mock_client = AsyncMock(spec=nio.AsyncClient)
    mock_bot = MagicMock()
    mock_bot.client = mock_client
    orchestrator.agent_bots = {"general": mock_bot}

    # Add an inactive room invite
    room_id = "!test_room:example.com"
    agent_name = "research"

    # Create invite that's inactive (25 hours old)
    old_time = datetime.now() - timedelta(hours=25)
    await room_invite_manager.add_invite(room_id, agent_name, "@user:example.com")

    # Manually set the last activity to old time
    async with room_invite_manager._lock:
        room_invite_manager._room_invites[room_id][agent_name].last_activity = old_time

    # No thread invites for this agent

    # Mock room_kick
    mock_client.room_kick = AsyncMock(return_value=nio.RoomKickResponse())

    # Run one iteration of cleanup
    with patch("mindroom.bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = [None, asyncio.CancelledError()]  # Run once then cancel

        cleanup_task = asyncio.create_task(orchestrator._periodic_cleanup())
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    # Agent SHOULD have been kicked
    mock_client.room_kick.assert_called_once_with(
        room_id, f"@mindroom_{agent_name}:localhost", reason="Inactive for 24 hours - automatic removal"
    )

    # Room invite should be removed
    assert not await room_invite_manager.is_agent_invited_to_room(room_id, agent_name)


@pytest.mark.asyncio
async def test_cleanup_handles_multiple_agents_and_rooms(cleanup_managers, tmp_path):
    """Test cleanup handles multiple agents across multiple rooms correctly."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.running = True

    # Create a mock general bot with client
    mock_client = AsyncMock(spec=nio.AsyncClient)
    mock_bot = MagicMock()
    mock_bot.client = mock_client
    orchestrator.agent_bots = {"general": mock_bot}

    # Set up test data
    room1 = "!room1:example.com"
    room2 = "!room2:example.com"
    agent1 = "calculator"
    agent2 = "research"
    agent3 = "shell"

    old_time = datetime.now() - timedelta(hours=25)

    # Room 1: agent1 (inactive, has thread), agent2 (inactive, no thread)
    await room_invite_manager.add_invite(room1, agent1, "@user:example.com")
    await room_invite_manager.add_invite(room1, agent2, "@user:example.com")

    # Room 2: agent3 (inactive, no thread)
    await room_invite_manager.add_invite(room2, agent3, "@user:example.com")

    # Make all invites old
    async with room_invite_manager._lock:
        room_invite_manager._room_invites[room1][agent1].last_activity = old_time
        room_invite_manager._room_invites[room1][agent2].last_activity = old_time
        room_invite_manager._room_invites[room2][agent3].last_activity = old_time

    # Add thread invite for agent1 in room1
    await thread_invite_manager.add_invite("$thread1", room1, agent1, "@user:example.com")

    # Mock room_kick
    kick_calls = []

    async def mock_kick(room_id, user_id, reason):
        kick_calls.append((room_id, user_id))
        return nio.RoomKickResponse()

    mock_client.room_kick = mock_kick

    # Run one iteration of cleanup
    with patch("mindroom.bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = [None, asyncio.CancelledError()]

        cleanup_task = asyncio.create_task(orchestrator._periodic_cleanup())
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    # Should have kicked agent2 from room1 and agent3 from room2
    assert len(kick_calls) == 2
    assert (room1, f"@mindroom_{agent2}:localhost") in kick_calls
    assert (room2, f"@mindroom_{agent3}:localhost") in kick_calls

    # agent1 should still be invited to room1
    assert await room_invite_manager.is_agent_invited_to_room(room1, agent1)

    # agent2 and agent3 should be removed
    assert not await room_invite_manager.is_agent_invited_to_room(room1, agent2)
    assert not await room_invite_manager.is_agent_invited_to_room(room2, agent3)


@pytest.mark.asyncio
async def test_cleanup_continues_on_error(cleanup_managers, tmp_path):
    """Test that cleanup continues running even if an error occurs."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.running = True

    # Track cleanup calls
    cleanup_count = 0
    iteration_count = 0

    async def mock_cleanup():
        nonlocal cleanup_count
        cleanup_count += 1
        if cleanup_count == 1:
            # First call raises an error
            raise Exception("Test error")
        return 0

    # Mock sleep to track iterations
    async def mock_sleep(duration):
        nonlocal iteration_count
        iteration_count += 1
        if iteration_count >= 3:
            orchestrator.running = False
        return

    with (
        patch.object(thread_invite_manager, "cleanup_expired", mock_cleanup),
        patch("mindroom.bot.asyncio.sleep", AsyncMock(side_effect=mock_sleep)),
    ):
        # Run cleanup task
        await orchestrator._periodic_cleanup()

        # Should have continued after error
        assert cleanup_count >= 3


@pytest.mark.asyncio
async def test_cleanup_handles_expired_thread_invites(cleanup_managers, tmp_path):
    """Test that expired thread invites are cleaned up."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.running = True

    # Add thread invites with different expiration times
    room_id = "!test_room:example.com"

    # Active invite (expires in future)
    await thread_invite_manager.add_invite("$thread1", room_id, "calculator", "@user:example.com", duration_hours=2)

    # Expired invite
    thread_id2 = "$thread2"
    await thread_invite_manager.add_invite(thread_id2, room_id, "research", "@user:example.com", duration_hours=1)

    # Manually expire the second invite
    async with thread_invite_manager._lock:
        for invites in thread_invite_manager._invites.values():
            for invite in invites:
                if invite.thread_id == thread_id2:
                    invite.expires_at = datetime.now() - timedelta(hours=1)

    # Run one iteration of cleanup
    with patch("mindroom.bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = [None, asyncio.CancelledError()]

        cleanup_task = asyncio.create_task(orchestrator._periodic_cleanup())
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    # Check that only the active invite remains
    assert await thread_invite_manager.is_agent_invited_to_thread("$thread1", "calculator")
    assert not await thread_invite_manager.is_agent_invited_to_thread("$thread2", "research")


@pytest.mark.asyncio
async def test_cleanup_logs_activity_updates(cleanup_managers, tmp_path, caplog):
    """Test that cleanup logs when it updates room activity due to thread participation."""
    import logging

    # Set the caplog level on the specific logger
    caplog.set_level(logging.DEBUG, logger="mindroom.bot")

    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.running = True

    # Create inactive room invite with active thread invite
    room_id = "!test_room:example.com"
    agent_name = "calculator"

    old_time = datetime.now() - timedelta(hours=25)
    await room_invite_manager.add_invite(room_id, agent_name, "@user:example.com")

    async with room_invite_manager._lock:
        room_invite_manager._room_invites[room_id][agent_name].last_activity = old_time

    await thread_invite_manager.add_invite("$thread1", room_id, agent_name, "@user:example.com")

    # Create a mock general bot (needed for room cleanup)
    mock_client = AsyncMock(spec=nio.AsyncClient)
    mock_bot = MagicMock()
    mock_bot.client = mock_client
    orchestrator.agent_bots = {"general": mock_bot}

    # Run one iteration
    with patch("mindroom.bot.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        mock_sleep.side_effect = [None, asyncio.CancelledError()]

        cleanup_task = asyncio.create_task(orchestrator._periodic_cleanup())
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task

    # The activity should have been updated
    async with room_invite_manager._lock:
        invite = room_invite_manager._room_invites[room_id][agent_name]
        # Activity should be updated to recent time
        assert (datetime.now() - invite.last_activity).total_seconds() < 10, "Room activity was not updated"
