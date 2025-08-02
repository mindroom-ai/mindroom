"""Tests for the periodic cleanup functionality."""

import asyncio
import contextlib
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from mindroom.bot import MultiAgentOrchestrator
from mindroom.thread_invites import thread_invite_manager


@pytest_asyncio.fixture
async def cleanup_managers():
    """Clear invite managers before each test."""
    # Clear thread invites
    async with thread_invite_manager._lock:
        thread_invite_manager._invites.clear()
        thread_invite_manager._agent_threads.clear()

    yield

    # Clean up after test
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
async def test_cleanup_multiple_thread_invites(cleanup_managers, tmp_path):
    """Test cleanup handles multiple thread invites correctly."""
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
    orchestrator.running = True

    # Add multiple thread invites
    room_id = "!test_room:example.com"

    # Add invites that will expire at different times
    invites = [
        ("$thread1", "calculator", 10),  # expires in 10 hours
        ("$thread2", "research", 0.5),  # expires in 30 minutes
        ("$thread3", "general", None),  # no expiration
    ]

    for thread_id, agent_name, duration in invites:
        await thread_invite_manager.add_invite(thread_id, room_id, agent_name, "@user:example.com", duration)

    # Manually expire the 30-minute invite
    async with thread_invite_manager._lock:
        for thread_invites in thread_invite_manager._invites.values():
            for invite in thread_invites:
                if invite.thread_id == "$thread2":
                    invite.expires_at = datetime.now() - timedelta(minutes=1)

    # Run cleanup
    removed_count = await thread_invite_manager.cleanup_expired()

    # Should have removed only the expired invite
    assert removed_count == 1
    assert await thread_invite_manager.is_agent_invited_to_thread("$thread1", "calculator")
    assert not await thread_invite_manager.is_agent_invited_to_thread("$thread2", "research")
    assert await thread_invite_manager.is_agent_invited_to_thread("$thread3", "general")
