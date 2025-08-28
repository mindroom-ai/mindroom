"""Tests for scheduling functionality that actually exercise the real code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import nio
import pytest

from mindroom.scheduling import ScheduledWorkflow, cancel_all_scheduled_tasks, list_scheduled_tasks


@pytest.mark.asyncio
async def test_list_scheduled_tasks_real_implementation() -> None:
    """Test list_scheduled_tasks with real implementation, only mocking Matrix API."""
    # Create mock client
    client = AsyncMock()

    # Create workflows
    workflow1 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Test message 1",
        description="Test task 1",
        thread_id="$thread123",
        room_id="!test:server",
    )

    workflow2 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Test message 2",
        description="Test task 2",
        thread_id="$thread456",
        room_id="!test:server",
    )

    workflow3 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        message="Test message 3",
        description="Test task 3",
        thread_id="$thread123",
        room_id="!test:server",
    )

    # Create a proper RoomGetStateResponse with scheduled tasks
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "workflow": workflow1.model_dump_json(),
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
                    "workflow": workflow2.model_dump_json(),
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
                    "workflow": workflow3.model_dump_json(),
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
    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123", config=None)

    # Should show 2 tasks from thread123, not task2 (different thread) or task4 (completed)
    assert "**Scheduled Tasks:**" in result
    assert "task1" in result
    assert "Test task 1" in result
    assert "Test message 1" in result
    assert "task3" in result
    assert "Test task 3" in result
    assert "Test message 3" in result
    assert "task2" not in result  # Different thread
    assert "task4" not in result  # Completed

    # Test listing tasks for thread456
    result2 = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread456", config=None)

    # Should only show task2
    assert "**Scheduled Tasks:**" in result2
    assert "task2" in result2
    assert "Test task 2" in result2
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

    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123", config=None)

    assert result == "No scheduled tasks found."


@pytest.mark.asyncio
async def test_list_scheduled_tasks_tasks_in_other_threads() -> None:
    """Test list_scheduled_tasks when all tasks are in other threads."""
    client = AsyncMock()

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Test message",
        description="Test task",
        thread_id="$thread456",  # Different thread
        room_id="!test:server",
    )

    # Tasks only in other threads
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "workflow": workflow.model_dump_json(),
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
        config=None,
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

    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123", config=None)

    assert result == "Unable to retrieve scheduled tasks."


@pytest.mark.asyncio
async def test_list_scheduled_tasks_invalid_task_data() -> None:
    """Test list_scheduled_tasks handles invalid task data gracefully."""
    client = AsyncMock()

    valid_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Valid task",
        description="Valid task description",
        thread_id="$thread123",
        room_id="!test:server",
    )

    # Mix of valid and invalid tasks
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    # Missing workflow - should be skipped
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
                    "workflow": "invalid-json",  # Invalid JSON
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
                    "workflow": valid_workflow.model_dump_json(),
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

    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123", config=None)

    # Should only show the valid task
    assert "**Scheduled Tasks:**" in result
    assert "task3" in result
    assert "Valid task" in result
    assert "task1" not in result  # Missing execute_at
    assert "task2" not in result  # Invalid date format


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks() -> None:
    """Test cancel_all_scheduled_tasks functionality."""
    # Create mock client
    client = AsyncMock()

    # Create workflows for testing
    workflow1 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Test message 1",
        description="Test task 1",
        thread_id="$thread123",
        room_id="!test:server",
    )

    workflow2 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Test message 2",
        description="Test task 2",
        thread_id="$thread456",
        room_id="!test:server",
    )

    # Create a proper RoomGetStateResponse with scheduled tasks
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "task_id": "task1",
                    "workflow": workflow1.model_dump_json(),
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
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
                    "workflow": workflow2.model_dump_json(),
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
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
                    "workflow": workflow1.model_dump_json(),
                    "status": "cancelled",  # Already cancelled
                    "created_at": datetime.now(UTC).isoformat(),
                },
                "event_id": "$state_task3",
                "sender": "@system:server",
                "origin_server_ts": 1234567892,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$event123"}, room_id="!test:server"),
    )

    result = await cancel_all_scheduled_tasks(client=client, room_id="!test:server")

    # Should cancel 2 pending tasks (task3 is already cancelled)
    assert "✅ Cancelled 2 scheduled task(s)" in result

    # Verify room_put_state was called twice (once for each pending task)
    assert client.room_put_state.call_count == 2

    # Verify the calls were made with correct parameters
    calls = client.room_put_state.call_args_list
    for call in calls:
        assert call[1]["room_id"] == "!test:server"
        assert call[1]["event_type"] == "com.mindroom.scheduled.task"
        assert call[1]["content"] == {"status": "cancelled"}
        assert call[1]["state_key"] in ["task1", "task2"]


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_no_tasks() -> None:
    """Test cancel_all_scheduled_tasks when no tasks exist."""
    # Create mock client
    client = AsyncMock()

    # Create empty response
    mock_response = nio.RoomGetStateResponse.from_dict(
        [],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await cancel_all_scheduled_tasks(client=client, room_id="!test:server")

    # Should indicate no tasks to cancel
    assert result == "No scheduled tasks to cancel."

    # Verify room_put_state was never called
    client.room_put_state.assert_not_called()
