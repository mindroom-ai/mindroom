"""Tests for scheduling functionality that actually exercise the real code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import scheduling
from mindroom.constants import resolve_runtime_paths
from mindroom.scheduling import (
    _SCHEDULED_TASK_EVENT_TYPE,
    CronSchedule,
    ScheduledTaskRecord,
    ScheduledWorkflow,
    _run_cron_task,
    _run_once_task,
    cancel_all_scheduled_tasks,
    clear_deferred_overdue_tasks,
    drain_deferred_overdue_tasks,
    edit_scheduled_task,
    get_scheduled_tasks_for_room,
    list_scheduled_tasks,
    restore_scheduled_tasks,
    save_edited_scheduled_task,
    schedule_task,
)
from tests.conftest import make_event_cache_mock

if TYPE_CHECKING:
    from collections.abc import Generator


def _runtime_paths() -> object:
    return resolve_runtime_paths(config_path=Path("config.yaml"), process_env={})


def _event_cache() -> AsyncMock:
    return make_event_cache_mock()


def _conversation_cache(thread_history: list[object] | None = None) -> MagicMock:
    access = MagicMock()
    access.get_thread_history = AsyncMock(return_value=list(thread_history or []))
    return access


def _record(
    task_id: str,
    workflow: ScheduledWorkflow,
    *,
    status: str = "pending",
    room_id: str = "!test:server",
) -> ScheduledTaskRecord:
    return ScheduledTaskRecord(
        task_id=task_id,
        room_id=room_id,
        status=status,
        created_at=datetime.now(UTC),
        workflow=workflow,
    )


@pytest.fixture(autouse=True)
def _clear_deferred_overdue_queue() -> Generator[None, None, None]:
    clear_deferred_overdue_tasks()
    yield
    clear_deferred_overdue_tasks()


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_queues_overdue_one_time_tasks() -> None:
    """Overdue one-time tasks should wait for sync instead of firing during restore."""
    client = AsyncMock()
    overdue_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=5),
        message="Send the overdue reminder",
        description="Overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue",
                "content": {
                    "workflow": overdue_workflow.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task_overdue",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
        ],
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=state_response)

    with patch("mindroom.scheduling._start_scheduled_task") as mock_start:
        restored = await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=MagicMock(),
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
        )

    assert restored == 1
    mock_start.assert_not_called()
    assert len(scheduling._deferred_overdue_tasks) == 1
    assert scheduling._deferred_overdue_tasks[0].task_id == "task_overdue"


@pytest.mark.asyncio
async def test_drain_deferred_overdue_tasks_starts_queued_tasks_after_sync() -> None:
    """Queued overdue tasks should start in order once sync is ready."""
    client = AsyncMock()
    config = MagicMock()
    overdue_workflow_1 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=10),
        message="First overdue reminder",
        description="First overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
    )
    overdue_workflow_2 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=3),
        message="Second overdue reminder",
        description="Second overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue_1",
                "content": {
                    "workflow": overdue_workflow_1.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task_overdue_1",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue_2",
                "content": {
                    "workflow": overdue_workflow_2.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task_overdue_2",
                "sender": "@system:server",
                "origin_server_ts": 1234567891,
            },
        ],
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=state_response)

    with patch("mindroom.scheduling._start_scheduled_task") as mock_start_during_restore:
        await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
        )

    mock_start_during_restore.assert_not_called()

    with (
        patch("mindroom.scheduling._start_scheduled_task", side_effect=[True, True]) as mock_start,
        patch("mindroom.scheduling.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        drained = await drain_deferred_overdue_tasks(client, config, _runtime_paths(), _event_cache())

    assert drained == 2
    assert [call.args[1] for call in mock_start.call_args_list] == ["task_overdue_1", "task_overdue_2"]
    mock_sleep.assert_awaited_once_with(scheduling._DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS)
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_drain_deferred_overdue_tasks_continues_after_one_start_failure() -> None:
    """One deferred task failure should not strand later queued tasks."""
    client = AsyncMock()
    config = MagicMock()
    overdue_workflow_1 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=10),
        message="First overdue reminder",
        description="First overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
    )
    overdue_workflow_2 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=3),
        message="Second overdue reminder",
        description="Second overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue_1",
                "content": {
                    "workflow": overdue_workflow_1.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task_overdue_1",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue_2",
                "content": {
                    "workflow": overdue_workflow_2.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task_overdue_2",
                "sender": "@system:server",
                "origin_server_ts": 1234567891,
            },
        ],
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=state_response)

    with patch("mindroom.scheduling._start_scheduled_task") as mock_start_during_restore:
        await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
        )

    mock_start_during_restore.assert_not_called()

    with (
        patch(
            "mindroom.scheduling._start_scheduled_task",
            side_effect=[RuntimeError("boom"), True],
        ) as mock_start,
        patch("mindroom.scheduling.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        drained = await drain_deferred_overdue_tasks(client, config, _runtime_paths(), _event_cache())

    assert drained == 1
    assert [call.args[1] for call in mock_start.call_args_list] == ["task_overdue_1", "task_overdue_2"]
    mock_sleep.assert_awaited_once_with(scheduling._DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS)
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_keeps_cron_restoration_unchanged() -> None:
    """Recurring cron tasks should still be restored immediately."""
    client = AsyncMock()
    cron_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Run the daily report",
        description="Daily report",
        thread_id="$thread123",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_cron",
                "content": {
                    "workflow": cron_workflow.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task_cron",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
        ],
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=state_response)

    with patch("mindroom.scheduling._start_scheduled_task", return_value=True) as mock_start:
        restored = await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=MagicMock(),
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
        )

    assert restored == 1
    mock_start.assert_called_once()
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_does_not_queue_when_nothing_is_overdue() -> None:
    """Future one-time tasks should still start normally and leave no deferred queue."""
    client = AsyncMock()
    future_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=15),
        message="Future reminder",
        description="Future reminder",
        thread_id="$thread123",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_future",
                "content": {
                    "workflow": future_workflow.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task_future",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
        ],
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=state_response)

    with patch("mindroom.scheduling._start_scheduled_task", return_value=True) as mock_start:
        restored = await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=MagicMock(),
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
        )

    assert restored == 1
    mock_start.assert_called_once()
    assert len(scheduling._deferred_overdue_tasks) == 0


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

    workflow4 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=2),
        message="Room-level current-scope task",
        description="Room-level task",
        thread_id=None,
        room_id="!test:server",
    )

    workflow5 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=3),
        message="Future room-level thread root",
        description="New thread task",
        thread_id=None,
        room_id="!test:server",
        new_thread=True,
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
                    "workflow": workflow4.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task4",
                "sender": "@system:server",
                "origin_server_ts": 1234567893,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task5",
                "content": {
                    "workflow": workflow5.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task5",
                "sender": "@system:server",
                "origin_server_ts": 1234567894,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task6",
                "content": {
                    "status": "completed",  # This one is completed, should not appear
                },
                "event_id": "$state_task6",
                "sender": "@system:server",
                "origin_server_ts": 1234567895,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    # Test listing tasks for thread123
    result = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread123", config=None)

    current_section, _, new_thread_section = result.partition("**New Room-Level Thread Roots:**")

    # Should show thread123 tasks plus room-level current-scope tasks, but not new_thread tasks in the main section.
    assert "**Scheduled Tasks:**" in result
    assert "task1" in current_section
    assert "Test task 1" in current_section
    assert "Test message 1" in current_section
    assert "task3" in current_section
    assert "Test task 3" in current_section
    assert "Test message 3" in current_section
    assert "task4" in current_section
    assert "Room-level task" in current_section
    assert "task2" not in current_section  # Different thread
    assert "task5" not in current_section  # New-thread task is listed separately
    assert "task6" not in result  # Completed
    assert "task5" in new_thread_section
    assert "New thread task" in new_thread_section
    assert "1 task(s) scheduled in other threads" in result

    # Test listing tasks for thread456
    result2 = await list_scheduled_tasks(client=client, room_id="!test:server", thread_id="$thread456", config=None)
    current_section2, _, new_thread_section2 = result2.partition("**New Room-Level Thread Roots:**")

    assert "**Scheduled Tasks:**" in result2
    assert "task2" in current_section2
    assert "Test task 2" in current_section2
    assert "Test message 2" in current_section2
    assert "task4" in current_section2
    assert "task1" not in current_section2
    assert "task3" not in current_section2
    assert "task5" in new_thread_section2


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
async def test_run_once_task_stops_when_cancelled_via_matrix_state() -> None:
    """One-time tasks should stop without executing once state is cancelled."""
    client = AsyncMock()
    config = AsyncMock()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Original message",
        description="Original description",
        room_id="!test:server",
        thread_id="$thread123",
    )

    pending_record = _record("task_once_cancelled", workflow, status="pending")
    cancelled_record = _record("task_once_cancelled", workflow, status="cancelled")
    fetch_count = 0

    async def _fetch_task(*_args: object, **_kwargs: object) -> ScheduledTaskRecord:
        nonlocal fetch_count
        fetch_count += 1
        return pending_record if fetch_count == 1 else cancelled_record

    with (
        patch("mindroom.scheduling.get_scheduled_task", side_effect=_fetch_task),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
        patch("mindroom.scheduling.asyncio.sleep", new=AsyncMock()),
    ):
        await _run_once_task(client, "task_once_cancelled", workflow, config, _runtime_paths(), _event_cache())

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_task_executes_latest_state_workflow() -> None:
    """One-time tasks should execute using the latest persisted workflow data."""
    client = AsyncMock()
    config = AsyncMock()
    initial_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Old message",
        description="Old description",
        room_id="!test:server",
        thread_id="$thread123",
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Updated message",
        description="Updated description",
        room_id="!test:server",
        thread_id="$thread123",
    )

    async def _fetch_task(*_args: object, **_kwargs: object) -> ScheduledTaskRecord:
        return _record("task_once_updated", updated_workflow, status="pending")

    with (
        patch("mindroom.scheduling.get_scheduled_task", side_effect=_fetch_task),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
    ):
        await _run_once_task(client, "task_once_updated", initial_workflow, config, _runtime_paths(), _event_cache())

    execute_mock.assert_awaited_once()
    executed_workflow = execute_mock.await_args.args[1]
    assert executed_workflow.message == "Updated message"
    assert executed_workflow.description == "Updated description"


@pytest.mark.asyncio
async def test_run_once_task_marks_completed_after_success() -> None:
    """One-time tasks should overwrite pending state with completed after firing."""
    client = AsyncMock()
    client.room_put_state = AsyncMock()
    config = AsyncMock()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Run once",
        description="One-time success",
        room_id="!test:server",
        thread_id="$thread123",
    )
    pending_record = _record("task_once_completed", workflow, status="pending")

    with (
        patch(
            "mindroom.scheduling.get_scheduled_task",
            new=AsyncMock(side_effect=[pending_record, pending_record]),
        ),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock(return_value=True)) as execute_mock,
    ):
        await _run_once_task(client, "task_once_completed", workflow, config, _runtime_paths(), _event_cache())

    execute_mock.assert_awaited_once()
    client.room_put_state.assert_awaited_once()
    put_kwargs = client.room_put_state.await_args.kwargs
    assert put_kwargs["room_id"] == "!test:server"
    assert put_kwargs["event_type"] == _SCHEDULED_TASK_EVENT_TYPE
    assert put_kwargs["state_key"] == "task_once_completed"
    assert put_kwargs["content"]["status"] == "completed"
    assert put_kwargs["content"]["workflow"] == workflow.model_dump_json()
    assert put_kwargs["content"]["created_at"] == pending_record.created_at.isoformat()


@pytest.mark.asyncio
async def test_run_once_task_marks_failed_after_execution_failure() -> None:
    """One-time tasks should overwrite pending state with failed when firing fails."""
    client = AsyncMock()
    client.room_put_state = AsyncMock()
    config = AsyncMock()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Run once",
        description="One-time failure",
        room_id="!test:server",
        thread_id="$thread123",
    )
    pending_record = _record("task_once_failed", workflow, status="pending")

    with (
        patch(
            "mindroom.scheduling.get_scheduled_task",
            new=AsyncMock(side_effect=[pending_record, pending_record]),
        ),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock(return_value=False)) as execute_mock,
    ):
        await _run_once_task(client, "task_once_failed", workflow, config, _runtime_paths(), _event_cache())

    execute_mock.assert_awaited_once()
    client.room_put_state.assert_awaited_once()
    put_kwargs = client.room_put_state.await_args.kwargs
    assert put_kwargs["state_key"] == "task_once_failed"
    assert put_kwargs["content"]["status"] == "failed"
    assert put_kwargs["content"]["workflow"] == workflow.model_dump_json()


@pytest.mark.asyncio
async def test_run_cron_task_executes_latest_state_workflow() -> None:
    """Recurring tasks should execute using the latest persisted workflow data."""
    client = AsyncMock()
    config = AsyncMock()
    initial_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Old recurring message",
        description="Old recurring description",
        room_id="!test:server",
        thread_id="$thread123",
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Updated recurring message",
        description="Updated recurring description",
        room_id="!test:server",
        thread_id="$thread123",
    )

    class _ImmediateCron:
        def get_next(self, _type: object) -> datetime:
            return datetime.now(UTC) - timedelta(seconds=1)

    async def _fetch_task(*_args: object, **_kwargs: object) -> ScheduledTaskRecord:
        return _record("task_cron_updated", updated_workflow, status="pending")

    with (
        patch("mindroom.scheduling.get_scheduled_task", side_effect=_fetch_task),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
        patch("mindroom.scheduling.croniter", return_value=_ImmediateCron()),
    ):
        await _run_cron_task(
            client,
            "task_cron_updated",
            initial_workflow,
            {},
            config,
            _runtime_paths(),
            _event_cache(),
        )

    execute_mock.assert_awaited_once()
    executed_workflow = execute_mock.await_args.args[1]
    assert executed_workflow.message == "Updated recurring message"
    assert executed_workflow.description == "Updated recurring description"


@pytest.mark.asyncio
async def test_run_cron_task_keeps_pending_state_after_success() -> None:
    """Recurring tasks should keep their pending state after firing."""
    client = AsyncMock()
    client.room_put_state = AsyncMock()
    config = AsyncMock()
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Recurring message",
        description="Recurring description",
        room_id="!test:server",
        thread_id="$thread123",
    )
    pending_record = _record("task_cron_pending", workflow, status="pending")

    class _ImmediateCron:
        def get_next(self, _type: object) -> datetime:
            return datetime.now(UTC) - timedelta(seconds=1)

    with (
        patch(
            "mindroom.scheduling.get_scheduled_task",
            new=AsyncMock(side_effect=[pending_record, pending_record]),
        ),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock(return_value=True)) as execute_mock,
        patch("mindroom.scheduling.croniter", return_value=_ImmediateCron()),
    ):
        await _run_cron_task(client, "task_cron_pending", workflow, {}, config, _runtime_paths(), _event_cache())

    execute_mock.assert_awaited_once()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_cron_task_stops_when_cancelled_via_matrix_state() -> None:
    """Recurring tasks should stop without executing once state is cancelled."""
    client = AsyncMock()
    config = AsyncMock()
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Recurring message",
        description="Recurring description",
        room_id="!test:server",
        thread_id="$thread123",
    )

    async def _fetch_task(*_args: object, **_kwargs: object) -> ScheduledTaskRecord:
        return _record("task_cron_cancelled", workflow, status="cancelled")

    with (
        patch("mindroom.scheduling.get_scheduled_task", side_effect=_fetch_task),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
    ):
        await _run_cron_task(client, "task_cron_cancelled", workflow, {}, config, _runtime_paths(), _event_cache())

    execute_mock.assert_not_awaited()


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
    expected_workflows = {
        "task1": workflow1.model_dump_json(),
        "task2": workflow2.model_dump_json(),
    }
    for call in calls:
        state_key = call[1]["state_key"]
        assert call[1]["room_id"] == "!test:server"
        assert call[1]["event_type"] == "com.mindroom.scheduled.task"
        assert state_key in ["task1", "task2"]
        assert call[1]["content"]["status"] == "cancelled"
        assert call[1]["content"]["task_id"] == state_key
        assert call[1]["content"]["workflow"] == expected_workflows[state_key]
        assert "created_at" in call[1]["content"]


@pytest.mark.asyncio
async def test_get_scheduled_tasks_for_room_includes_cancelled_without_workflow() -> None:
    """Cancelled tasks without workflow payload are still returned for non-pending listings."""
    client = AsyncMock()
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "old_cancelled",
                "content": {
                    "status": "cancelled",
                },
                "event_id": "$state_cancelled",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    tasks = await get_scheduled_tasks_for_room(client=client, room_id="!test:server", include_non_pending=True)

    assert len(tasks) == 1
    assert tasks[0].task_id == "old_cancelled"
    assert tasks[0].status == "cancelled"
    assert tasks[0].workflow.description == "Cancelled task"


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


@pytest.mark.asyncio
async def test_edit_scheduled_task_reuses_existing_thread() -> None:
    """Editing should keep the task ID and original thread context."""
    client = AsyncMock()
    room = MagicMock()
    config = MagicMock()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Initial message",
        description="Initial task",
        thread_id="$original_thread",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateEventResponse(
        content={"status": "pending", "workflow": workflow.model_dump_json()},
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(return_value=state_response)

    with patch(
        "mindroom.scheduling.schedule_task",
        new=AsyncMock(return_value=("task123", "✅ Scheduled")),
    ) as mock_schedule:
        result = await edit_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            room=room,
            conversation_cache=_conversation_cache(),
            thread_id="$fallback_thread",
        )

    assert "✅ Updated task `task123`." in result
    mock_schedule.assert_awaited_once()
    call_kwargs = mock_schedule.await_args.kwargs
    assert call_kwargs["client"] is client
    assert call_kwargs["room_id"] == "!test:server"
    assert call_kwargs["thread_id"] == "$original_thread"
    assert call_kwargs["scheduled_by"] == "@user:server"
    assert call_kwargs["full_text"] == "tomorrow at 9am updated task"
    assert call_kwargs["config"] is config
    assert call_kwargs["room"] is room
    assert call_kwargs["new_thread"] is False
    assert call_kwargs["task_id"] == "task123"
    assert call_kwargs["restart_task"] is False
    assert call_kwargs["existing_task"].task_id == "task123"
    assert call_kwargs["existing_task"].workflow.thread_id == "$original_thread"


@pytest.mark.asyncio
async def test_edit_scheduled_task_preserves_new_thread_mode() -> None:
    """Editing a new-thread schedule should not repopulate thread_id from the editor context."""
    client = AsyncMock()
    room = MagicMock()
    config = MagicMock()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Initial message",
        description="Initial task",
        thread_id=None,
        room_id="!test:server",
        new_thread=True,
    )
    state_response = nio.RoomGetStateEventResponse(
        content={"status": "pending", "workflow": workflow.model_dump_json()},
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(return_value=state_response)

    with patch(
        "mindroom.scheduling.schedule_task",
        new=AsyncMock(return_value=("task123", "✅ Scheduled")),
    ) as mock_schedule:
        result = await edit_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            room=room,
            conversation_cache=_conversation_cache(),
            thread_id="$fallback_thread",
        )

    assert "✅ Updated task `task123`." in result
    call_kwargs = mock_schedule.await_args.kwargs
    assert call_kwargs["thread_id"] is None
    assert call_kwargs["new_thread"] is True


@pytest.mark.asyncio
async def test_edit_scheduled_task_rejects_non_pending() -> None:
    """Editing should fail for cancelled/completed tasks."""
    client = AsyncMock()
    room = MagicMock()
    state_response = nio.RoomGetStateEventResponse(
        content={"status": "cancelled"},
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(return_value=state_response)

    result = await edit_scheduled_task(
        client=client,
        room_id="!test:server",
        task_id="task123",
        full_text="tomorrow at 9am updated task",
        scheduled_by="@user:server",
        config=MagicMock(),
        runtime_paths=_runtime_paths(),
        event_cache=_event_cache(),
        room=room,
        conversation_cache=_conversation_cache(),
        thread_id="$thread123",
    )

    assert "cannot be edited" in result


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_preserves_created_at() -> None:
    """Editing should keep created_at metadata from the original task."""
    client = AsyncMock()
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    existing_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        message="original message",
        description="original description",
        thread_id="$thread1",
        room_id="!test:server",
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
        message="updated message",
        description="updated description",
        thread_id="$thread1",
        room_id="!test:server",
    )
    existing_task = ScheduledTaskRecord(
        task_id="task123",
        room_id="!test:server",
        status="pending",
        created_at=created_at,
        workflow=existing_workflow,
    )

    updated_task = await save_edited_scheduled_task(
        client=client,
        room_id="!test:server",
        task_id="task123",
        workflow=updated_workflow,
        config=MagicMock(),
        runtime_paths=_runtime_paths(),
        event_cache=_event_cache(),
        existing_task=existing_task,
        restart_task=False,
    )

    assert updated_task.created_at == created_at
    assert updated_task.workflow == updated_workflow
    client.room_put_state.assert_awaited_once()
    assert client.room_put_state.await_args.kwargs["content"]["created_at"] == created_at.isoformat()


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_allows_persist_only_without_event_cache() -> None:
    """State-only edits should not require a runtime event cache."""
    client = AsyncMock()
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    existing_task = ScheduledTaskRecord(
        task_id="task123",
        room_id="!test:server",
        status="pending",
        created_at=created_at,
        workflow=ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
            message="original message",
            description="original description",
            thread_id="$thread1",
            room_id="!test:server",
        ),
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
        message="updated message",
        description="updated description",
        thread_id="$thread1",
        room_id="!test:server",
    )

    updated_task = await save_edited_scheduled_task(
        client=client,
        room_id="!test:server",
        task_id="task123",
        workflow=updated_workflow,
        config=MagicMock(),
        runtime_paths=_runtime_paths(),
        event_cache=None,
        existing_task=existing_task,
        restart_task=False,
    )

    assert updated_task.created_at == created_at
    assert updated_task.workflow == updated_workflow
    client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_rejects_schedule_type_change() -> None:
    """Editing should reject switching between once and cron schedule types."""
    client = AsyncMock()
    existing_task = ScheduledTaskRecord(
        task_id="task123",
        room_id="!test:server",
        status="pending",
        created_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        workflow=ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
            message="original message",
            description="original description",
            thread_id="$thread1",
            room_id="!test:server",
        ),
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="updated message",
        description="updated description",
        thread_id="$thread1",
        room_id="!test:server",
    )

    with pytest.raises(ValueError, match="Changing schedule_type is not supported"):
        await save_edited_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task123",
            workflow=updated_workflow,
            config=MagicMock(),
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            existing_task=existing_task,
            restart_task=False,
        )

    client.room_put_state.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_task_returns_error_when_sender_blocked_from_all_agents() -> None:
    """Scheduling should return a user-facing error when no agents are visible to the sender."""
    client = AsyncMock()
    room = MagicMock()
    config = MagicMock()

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[],
        ),
        patch(
            "mindroom.scheduling._extract_mentioned_agents_from_text",
            return_value=[],
        ),
    ):
        task_id, message = await schedule_task(
            client=client,
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@blocked:server",
            full_text="remind me in 5 minutes to check logs",
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            room=room,
            conversation_cache=_conversation_cache(),
        )

    assert task_id is None
    assert "No agents" in message


@pytest.mark.asyncio
async def test_schedule_task_blocked_sender_new_thread_returns_error() -> None:
    """new_thread mode should also return a clean error when the sender has no visible agents."""
    client = AsyncMock()
    room = MagicMock()
    config = MagicMock()

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[],
        ),
        patch(
            "mindroom.scheduling._extract_mentioned_agents_from_text",
            return_value=[],
        ),
    ):
        task_id, message = await schedule_task(
            client=client,
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@blocked:server",
            full_text="remind me in 5 minutes",
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            room=room,
            conversation_cache=_conversation_cache(),
            new_thread=True,
        )

    assert task_id is None
    assert "No agents" in message
