"""Tests for scheduling functionality that actually exercise the real code."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.scheduling import list_scheduled_tasks


@pytest.mark.asyncio
async def test_list_scheduled_tasks_real_implementation() -> None:
    """Test list_scheduled_tasks with real implementation, only mocking Matrix API."""
    # Create mock client
    client = AsyncMock()

    # Create a proper RoomGetStateResponse with scheduled tasks
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "task_id": "task1",
                    "room_id": "!test:server",
                    "thread_id": "$thread123",
                    "agent_user_id": "@bot:server",
                    "scheduled_by": "@user:server",
                    "scheduled_at": datetime.now(UTC).isoformat(),
                    "execute_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "message": "Test message 1",
                    "status": "pending",
                },
                "event_id": "$state_task1",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task2",
                "content": {
                    "task_id": "task2",
                    "room_id": "!test:server",
                    "thread_id": "$thread456",  # Different thread
                    "agent_user_id": "@bot:server",
                    "scheduled_by": "@user:server",
                    "scheduled_at": datetime.now(UTC).isoformat(),
                    "execute_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                    "message": "Test message 2",
                    "status": "pending",
                },
                "event_id": "$state_task2",
                "sender": "@system:server",
                "origin_server_ts": 1234567891,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task3",
                "content": {
                    "task_id": "task3",
                    "room_id": "!test:server",
                    "thread_id": "$thread123",  # Same thread as task1
                    "agent_user_id": "@bot:server",
                    "scheduled_by": "@user:server",
                    "scheduled_at": datetime.now(UTC).isoformat(),
                    "execute_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    "message": "Test message 3",
                    "status": "pending",
                },
                "event_id": "$state_task3",
                "sender": "@system:server",
                "origin_server_ts": 1234567892,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task4",
                "content": {
                    "task_id": "task4",
                    "room_id": "!test:server",
                    "thread_id": "$thread123",
                    "status": "completed",  # This one is completed, should not appear
                },
                "event_id": "$state_task4",
                "sender": "@system:server",
                "origin_server_ts": 1234567893,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    # Test listing tasks for thread123
    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123")

    # Should show 2 tasks from thread123, not task2 (different thread) or task4 (completed)
    assert "**Scheduled Tasks:**" in result
    assert "task1" in result
    assert "Test message 1" in result
    assert "task3" in result
    assert "Test message 3" in result
    assert "task2" not in result  # Different thread
    assert "task4" not in result  # Completed

    # Test listing tasks for thread456
    result2 = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread456")

    # Should only show task2
    assert "**Scheduled Tasks:**" in result2
    assert "task2" in result2
    assert "Test message 2" in result2
    assert "task1" not in result2
    assert "task3" not in result2


@pytest.mark.asyncio
async def test_list_scheduled_tasks_no_tasks() -> None:
    """Test list_scheduled_tasks when there are no tasks."""
    client = AsyncMock()

    # Empty response
    mock_response = nio.RoomGetStateResponse.from_dict([], room_id="!test:server")
    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123")

    assert result == "No scheduled tasks found."


@pytest.mark.asyncio
async def test_list_scheduled_tasks_tasks_in_other_threads() -> None:
    """Test list_scheduled_tasks when all tasks are in other threads."""
    client = AsyncMock()

    # Tasks only in other threads
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "task_id": "task1",
                    "thread_id": "$thread456",  # Different thread
                    "execute_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "message": "Test message",
                    "status": "pending",
                },
                "event_id": "$state_task1",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        thread_id="$thread123",  # Looking for thread123, but task is in thread456
    )

    assert "No scheduled tasks in this thread" in result
    assert "1 task(s) scheduled in other threads" in result


@pytest.mark.asyncio
async def test_list_scheduled_tasks_error_response() -> None:
    """Test list_scheduled_tasks when Matrix returns an error."""
    client = AsyncMock()

    # Return an error response
    error_response = nio.RoomGetStateError.from_dict({"error": "Not authorized"}, room_id="!test:server")
    client.room_get_state = AsyncMock(return_value=error_response)

    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123")

    assert result == "Unable to retrieve scheduled tasks."


@pytest.mark.asyncio
async def test_list_scheduled_tasks_invalid_task_data() -> None:
    """Test list_scheduled_tasks handles invalid task data gracefully."""
    client = AsyncMock()

    # Mix of valid and invalid tasks
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    # Missing execute_at - should be skipped
                    "task_id": "task1",
                    "thread_id": "$thread123",
                    "message": "Test message",
                    "status": "pending",
                },
                "event_id": "$state_task1",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task2",
                "content": {
                    "task_id": "task2",
                    "thread_id": "$thread123",
                    "execute_at": "invalid-date",  # Invalid date format
                    "message": "Test message",
                    "status": "pending",
                },
                "event_id": "$state_task2",
                "sender": "@system:server",
                "origin_server_ts": 1234567891,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task3",
                "content": {
                    "task_id": "task3",
                    "thread_id": "$thread123",
                    "execute_at": (datetime.now(UTC) + timedelta(minutes=5)).isoformat(),
                    "message": "Valid task",
                    "status": "pending",
                },
                "event_id": "$state_task3",
                "sender": "@system:server",
                "origin_server_ts": 1234567892,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123")

    # Should only show the valid task
    assert "**Scheduled Tasks:**" in result
    assert "task3" in result
    assert "Valid task" in result
    assert "task1" not in result  # Missing execute_at
    assert "task2" not in result  # Invalid date format
