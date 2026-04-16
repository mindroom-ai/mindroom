"""Tests for scheduling functionality that actually exercise the real code."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom import scheduling
from mindroom.constants import SCHEDULED_TASK_EVENT_TYPE, resolve_runtime_paths
from mindroom.router_helpers import LiveRouterRuntime
from mindroom.scheduling import (
    CronSchedule,
    ScheduledTaskRecord,
    ScheduledWorkflow,
    SchedulingRuntime,
    _AgentValidationResult,
    _persist_scheduled_task_state,
    _run_cron_task,
    _run_once_task,
    cancel_all_scheduled_tasks,
    cancel_scheduled_task,
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

_SCHEDULED_TASK_EVENT_TYPE = SCHEDULED_TASK_EVENT_TYPE


def _runtime_paths() -> object:
    return resolve_runtime_paths(config_path=Path("config.yaml"), process_env={})


def _event_cache() -> AsyncMock:
    return make_event_cache_mock()


def _conversation_cache(
    thread_history: list[object] | None = None,
    *,
    latest_thread_event_id: str | None = None,
) -> AsyncMock:
    access = AsyncMock()
    access.get_thread_history = AsyncMock(return_value=list(thread_history or []))
    access.get_latest_thread_event_id_if_needed = AsyncMock(return_value=latest_thread_event_id)
    access.notify_outbound_message = Mock()
    return access


def _allow_scheduled_task_writes(
    client: AsyncMock,
    *,
    user_id: str,
    room_id: str = "!test:server",
    event_level: int = 0,
    users: dict[str, int] | None = None,
) -> None:
    client.user_id = user_id
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    user_id: {"display_name": user_id.removeprefix("@").split(":", 1)[0]},
                },
            },
            room_id=room_id,
        ),
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "users": users or {user_id: max(event_level, 100)},
                "users_default": 0,
                "state_default": 50,
                "events": {_SCHEDULED_TASK_EVENT_TYPE: event_level},
            },
            event_type="m.room.power_levels",
            state_key="",
            room_id=room_id,
        ),
    )


def _scheduling_runtime(
    *,
    client: AsyncMock | None = None,
    config: object | None = None,
    room: object | None = None,
    conversation_cache: AsyncMock | None = None,
    event_cache: AsyncMock | None = None,
    router_client: AsyncMock | None = None,
    router_runtime: LiveRouterRuntime | None = None,
) -> SchedulingRuntime:
    current_client = client or AsyncMock()
    current_conversation_cache = conversation_cache or _conversation_cache()
    current_event_cache = event_cache or _event_cache()
    if router_runtime is None and router_client is not None:
        router_runtime = LiveRouterRuntime(
            client=router_client,
            conversation_cache=_conversation_cache(),
            event_cache=_event_cache(),
        )
    return SchedulingRuntime(
        client=current_client,
        config=config or MagicMock(),
        runtime_paths=_runtime_paths(),
        room=room or MagicMock(),
        conversation_cache=current_conversation_cache,
        event_cache=current_event_cache,
        router_client=router_client,
        router_runtime=router_runtime,
    )


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


def _task_state_response(
    task_id: str,
    workflow: ScheduledWorkflow,
    *,
    sender: str,
    status: str = "pending",
    room_id: str = "!test:server",
) -> nio.RoomGetStateResponse | nio.RoomGetStateError:
    return nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": task_id,
                "content": {
                    "task_id": task_id,
                    "workflow": workflow.model_dump_json(),
                    "status": status,
                    "created_at": datetime.now(UTC).isoformat(),
                },
                "event_id": f"$state_{task_id}",
                "sender": sender,
                "origin_server_ts": 1234567890,
            },
        ],
        room_id=room_id,
    )


def _restore_config(*allowed_senders: str) -> MagicMock:
    config = MagicMock()
    config.get_ids.return_value = {
        f"sender_{idx}": MagicMock(full_id=sender) for idx, sender in enumerate(allowed_senders, start=1)
    }
    config.get_domain.return_value = "server"
    return config


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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with patch("mindroom.scheduling._start_scheduled_task") as mock_start:
        restored = await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=_restore_config("@system:server"),
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
        )

    assert restored == 1
    mock_start.assert_not_called()
    assert len(scheduling._deferred_overdue_tasks) == 1
    assert scheduling._deferred_overdue_tasks[0].task_id == "task_overdue"


@pytest.mark.asyncio
async def test_drain_deferred_overdue_tasks_starts_queued_tasks_after_sync() -> None:
    """Queued overdue tasks should start in order once sync is ready."""
    client = AsyncMock()
    config = _restore_config("@system:server")
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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with patch("mindroom.scheduling._start_scheduled_task") as mock_start_during_restore:
        await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
        )

    mock_start_during_restore.assert_not_called()

    with (
        patch("mindroom.scheduling._start_scheduled_task", side_effect=[True, True]) as mock_start,
        patch("mindroom.scheduling.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        drained = await drain_deferred_overdue_tasks(
            client,
            config,
            _runtime_paths(),
            _event_cache(),
            conversation_cache,
        )

    assert drained == 2
    assert [call.args[1] for call in mock_start.call_args_list] == ["task_overdue_1", "task_overdue_2"]
    mock_sleep.assert_awaited_once_with(scheduling._DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS)
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_drain_deferred_overdue_tasks_continues_after_one_start_failure() -> None:
    """One deferred task failure should not strand later queued tasks."""
    client = AsyncMock()
    config = _restore_config("@system:server")
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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with patch("mindroom.scheduling._start_scheduled_task") as mock_start_during_restore:
        await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
        )

    mock_start_during_restore.assert_not_called()

    with (
        patch(
            "mindroom.scheduling._start_scheduled_task",
            side_effect=[RuntimeError("boom"), True],
        ) as mock_start,
        patch("mindroom.scheduling.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
    ):
        drained = await drain_deferred_overdue_tasks(
            client,
            config,
            _runtime_paths(),
            _event_cache(),
            conversation_cache,
        )

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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with patch("mindroom.scheduling._start_scheduled_task", return_value=True) as mock_start:
        restored = await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=_restore_config("@system:server"),
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with patch("mindroom.scheduling._start_scheduled_task", return_value=True) as mock_start:
        restored = await restore_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=_restore_config("@system:server"),
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
        )

    assert restored == 1
    mock_start.assert_called_once()
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_uses_live_non_router_writer_when_router_cannot_write() -> None:
    """Restore should start runners on a live writer client instead of assuming the router can write."""
    router_client = AsyncMock()
    router_client.user_id = "@mindroom_router:server"
    router_client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_router:server": {"display_name": "mindroom_router"},
                    "@mindroom_general:server": {"display_name": "mindroom_general"},
                },
            },
            room_id="!test:server",
        ),
    )
    router_client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "users": {
                    "@mindroom_router:server": 0,
                    "@mindroom_general:server": 50,
                },
                "users_default": 0,
                "state_default": 50,
                "events": {_SCHEDULED_TASK_EVENT_TYPE: 50},
            },
            event_type="m.room.power_levels",
            state_key="",
            room_id="!test:server",
        ),
    )
    future_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=15),
        message="Future reminder",
        description="Future reminder",
        thread_id="$thread123",
        room_id="!test:server",
    )
    router_client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": _SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task_future",
                    "content": {
                        "workflow": future_workflow.model_dump_json(),
                        "status": "pending",
                    },
                    "event_id": "$state_task_future",
                    "sender": "@mindroom_general:server",
                    "origin_server_ts": 1234567890,
                },
            ],
            room_id="!test:server",
        ),
    )

    general_client = AsyncMock()
    _allow_scheduled_task_writes(general_client, user_id="@mindroom_general:server")

    with patch("mindroom.scheduling._start_scheduled_task", return_value=True) as mock_start:
        restored = await restore_scheduled_tasks(
            client=router_client,
            room_id="!test:server",
            config=_restore_config("@mindroom_router:server", "@mindroom_general:server"),
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            conversation_cache=_conversation_cache(),
            additional_writer_clients=(general_client,),
        )

    assert restored == 1
    assert mock_start.call_args.args[0] is general_client


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_handles_room_state_transport_failure() -> None:
    """Restore should not abort router setup when Matrix room-state reads raise."""
    client = AsyncMock()
    client.room_get_state = AsyncMock(side_effect=RuntimeError("boom transport"))

    restored = await restore_scheduled_tasks(
        client=client,
        room_id="!test:server",
        config=_restore_config("@system:server"),
        runtime_paths=_runtime_paths(),
        event_cache=_event_cache(),
        conversation_cache=_conversation_cache(),
    )

    assert restored == 0


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_skips_untrusted_sender() -> None:
    """Restore should ignore scheduled-task state written by non-bot users."""
    client = AsyncMock()
    config = MagicMock()
    config.get_ids.return_value = {}
    config.get_domain.return_value = "server"
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
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
                    "workflow": workflow.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task_future",
                "sender": "@attacker:server",
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
            config=config,
            runtime_paths=_runtime_paths(),
            event_cache=_event_cache(),
            conversation_cache=_conversation_cache(),
        )

    assert restored == 0
    mock_start.assert_not_called()


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
async def test_list_scheduled_tasks_transport_exception() -> None:
    """Test list_scheduled_tasks when Matrix room-state reads raise."""
    client = AsyncMock()
    client.room_get_state = AsyncMock(side_effect=RuntimeError("boom transport"))

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
    config = _restore_config("@system:server")
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

    async def _fetch_task(*_args: object, **_kwargs: object) -> tuple[ScheduledTaskRecord, str]:
        nonlocal fetch_count
        fetch_count += 1
        record = pending_record if fetch_count == 1 else cancelled_record
        return (record, "@system:server")

    with (
        patch("mindroom.scheduling.get_scheduled_task_with_sender", side_effect=_fetch_task),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
        patch("mindroom.scheduling.asyncio.sleep", new=AsyncMock()),
    ):
        await _run_once_task(
            client,
            "task_once_cancelled",
            workflow,
            config,
            _runtime_paths(),
            _event_cache(),
            _conversation_cache(),
        )

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_task_stops_when_pending_state_sender_becomes_untrusted() -> None:
    """One-time runners should stop instead of executing untrusted state overwrites."""
    client = AsyncMock()
    config = _restore_config("@system:server")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Original message",
        description="Original description",
        room_id="!test:server",
        thread_id="$thread123",
    )

    with (
        patch(
            "mindroom.scheduling.get_scheduled_task_with_sender",
            new=AsyncMock(
                return_value=(_record("task_once_untrusted", workflow, status="pending"), "@intruder:server"),
            ),
        ),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
    ):
        await _run_once_task(
            client,
            "task_once_untrusted",
            workflow,
            config,
            _runtime_paths(),
            _event_cache(),
            _conversation_cache(),
        )

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_task_executes_latest_state_workflow() -> None:
    """One-time tasks should execute using the latest persisted workflow data."""
    client = AsyncMock()
    config = _restore_config("@system:server")
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

    async def _fetch_task(*_args: object, **_kwargs: object) -> tuple[ScheduledTaskRecord, str]:
        return (_record("task_once_updated", updated_workflow, status="pending"), "@system:server")

    with (
        patch("mindroom.scheduling.get_scheduled_task_with_sender", side_effect=_fetch_task),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
    ):
        await _run_once_task(
            client,
            "task_once_updated",
            initial_workflow,
            config,
            _runtime_paths(),
            _event_cache(),
            _conversation_cache(),
        )

    execute_mock.assert_awaited_once()
    executed_workflow = execute_mock.await_args.args[1]
    assert executed_workflow.message == "Updated message"
    assert executed_workflow.description == "Updated description"


@pytest.mark.asyncio
async def test_run_once_task_marks_completed_after_success() -> None:
    """One-time tasks should overwrite pending state with completed after firing."""
    client = AsyncMock()
    client.room_put_state = AsyncMock()
    config = _restore_config("@system:server")
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
            "mindroom.scheduling.get_scheduled_task_with_sender",
            new=AsyncMock(side_effect=[(pending_record, "@system:server"), (pending_record, "@system:server")]),
        ),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock(return_value=True)) as execute_mock,
    ):
        await _run_once_task(
            client,
            "task_once_completed",
            workflow,
            config,
            _runtime_paths(),
            _event_cache(),
            _conversation_cache(),
        )

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
    config = _restore_config("@system:server")
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
            "mindroom.scheduling.get_scheduled_task_with_sender",
            new=AsyncMock(side_effect=[(pending_record, "@system:server"), (pending_record, "@system:server")]),
        ),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock(return_value=False)) as execute_mock,
    ):
        await _run_once_task(
            client,
            "task_once_failed",
            workflow,
            config,
            _runtime_paths(),
            _event_cache(),
            _conversation_cache(),
        )

    execute_mock.assert_awaited_once()
    client.room_put_state.assert_awaited_once()
    put_kwargs = client.room_put_state.await_args.kwargs
    assert put_kwargs["state_key"] == "task_once_failed"
    assert put_kwargs["content"]["status"] == "failed"
    assert put_kwargs["content"]["workflow"] == workflow.model_dump_json()


@pytest.mark.asyncio
async def test_run_once_task_retries_state_read_failures() -> None:
    """One-time runners should retry transient state-read failures instead of failing permanently."""
    client = AsyncMock()
    config = _restore_config("@system:server")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Run once",
        description="One-time retry",
        room_id="!test:server",
        thread_id="$thread123",
    )
    pending_record = _record("task_once_retry", workflow, status="pending")
    state_error = scheduling.ScheduledTaskOperationError(
        "state_unavailable",
        "Unable to retrieve scheduled task state.",
        diagnostic_message="boom transport",
    )

    with (
        patch(
            "mindroom.scheduling.get_scheduled_task_with_sender",
            new=AsyncMock(
                side_effect=[state_error, (pending_record, "@system:server"), (pending_record, "@system:server")],
            ),
        ),
        patch("mindroom.scheduling.asyncio.sleep", new=AsyncMock()) as sleep_mock,
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock(return_value=True)) as execute_mock,
        patch("mindroom.scheduling._save_one_time_task_status", new=AsyncMock()) as save_status_mock,
    ):
        await _run_once_task(
            client,
            "task_once_retry",
            workflow,
            config,
            _runtime_paths(),
            _event_cache(),
            _conversation_cache(),
        )

    sleep_mock.assert_awaited_once_with(1.0)
    execute_mock.assert_awaited_once()
    save_status_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_cron_task_executes_latest_state_workflow() -> None:
    """Recurring tasks should execute using the latest persisted workflow data."""
    client = AsyncMock()
    config = _restore_config("@system:server")
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

    async def _fetch_task(*_args: object, **_kwargs: object) -> tuple[ScheduledTaskRecord, str]:
        return (_record("task_cron_updated", updated_workflow, status="pending"), "@system:server")

    with (
        patch("mindroom.scheduling.get_scheduled_task_with_sender", side_effect=_fetch_task),
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
            _conversation_cache(),
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
    config = _restore_config("@system:server")
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
            "mindroom.scheduling.get_scheduled_task_with_sender",
            new=AsyncMock(side_effect=[(pending_record, "@system:server"), (pending_record, "@system:server")]),
        ),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock(return_value=True)) as execute_mock,
        patch("mindroom.scheduling.croniter", return_value=_ImmediateCron()),
    ):
        await _run_cron_task(
            client,
            "task_cron_pending",
            workflow,
            {},
            config,
            _runtime_paths(),
            _conversation_cache(),
        )

    execute_mock.assert_awaited_once()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_cron_task_stops_when_cancelled_via_matrix_state() -> None:
    """Recurring tasks should stop without executing once state is cancelled."""
    client = AsyncMock()
    config = _restore_config("@system:server")
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Recurring message",
        description="Recurring description",
        room_id="!test:server",
        thread_id="$thread123",
    )

    async def _fetch_task(*_args: object, **_kwargs: object) -> tuple[ScheduledTaskRecord, str]:
        return (_record("task_cron_cancelled", workflow, status="cancelled"), "@system:server")

    with (
        patch("mindroom.scheduling.get_scheduled_task_with_sender", side_effect=_fetch_task),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
    ):
        await _run_cron_task(
            client,
            "task_cron_cancelled",
            workflow,
            {},
            config,
            _runtime_paths(),
            _conversation_cache(),
        )

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks() -> None:
    """Test cancel_all_scheduled_tasks functionality."""
    # Create mock client
    client = AsyncMock()
    _allow_scheduled_task_writes(client, user_id="@mindroom_general:localhost")

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
async def test_cancel_all_scheduled_tasks_uses_fresh_state_and_skips_tasks_that_are_no_longer_pending() -> None:
    """Bulk cancellation should use the fresh per-task state instead of overwriting a newer terminal update."""
    client = AsyncMock()
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": _SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task1",
                    "content": {"status": "pending"},
                    "event_id": "$state_task1",
                    "sender": "@system:server",
                    "origin_server_ts": 1,
                },
                {
                    "type": _SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task2",
                    "content": {"status": "pending"},
                    "event_id": "$state_task2",
                    "sender": "@system:server",
                    "origin_server_ts": 2,
                },
            ],
            room_id="!test:server",
        ),
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Updated message",
        description="Updated task",
        room_id="!test:server",
    )

    async def resolve_fresh_state(
        *_args: object,
        task_id: str,
        **_kwargs: object,
    ) -> scheduling.ScheduledTaskMutationContext:
        if task_id == "task1":
            return scheduling.ScheduledTaskMutationContext(
                task=_record(
                    "task1",
                    ScheduledWorkflow(
                        schedule_type="once",
                        execute_at=datetime.now(UTC) + timedelta(minutes=5),
                        message="Completed message",
                        description="Completed task",
                        room_id="!test:server",
                    ),
                    status="completed",
                ),
                task_sender_id="@mindroom_general:localhost",
                writer_client=client,
                re_resolve_writer=AsyncMock(return_value=client),
                task_content={
                    "task_id": "task1",
                    "workflow": updated_workflow.model_dump_json(),
                    "status": "completed",
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
        return scheduling.ScheduledTaskMutationContext(
            task=_record("task2", updated_workflow),
            task_sender_id="@mindroom_general:localhost",
            writer_client=client,
            re_resolve_writer=AsyncMock(return_value=client),
            task_content={
                "task_id": "task2",
                "workflow": updated_workflow.model_dump_json(),
                "status": "pending",
                "created_at": datetime.now(UTC).isoformat(),
            },
        )

    with (
        patch(
            "mindroom.scheduling.resolve_existing_scheduled_task_mutation",
            new=AsyncMock(side_effect=resolve_fresh_state),
        ),
        patch(
            "mindroom.scheduling._persist_cancelled_scheduled_task_state",
            new=AsyncMock(return_value=client),
        ) as persist_mock,
    ):
        result = await cancel_all_scheduled_tasks(
            client=client,
            room_id="!test:server",
            config=_restore_config("@mindroom_general:localhost"),
            runtime_paths=_runtime_paths(),
        )

    assert result == "✅ Cancelled 1 scheduled task(s)"
    persist_mock.assert_awaited_once()
    assert persist_mock.await_args.kwargs["task_id"] == "task2"
    assert persist_mock.await_args.kwargs["existing_content"]["workflow"] == updated_workflow.model_dump_json()


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_surfaces_first_write_failure() -> None:
    """Bulk cancellation should stop on the first concrete write failure."""
    client = AsyncMock()
    _allow_scheduled_task_writes(client, user_id="@mindroom_general:localhost")

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Test message",
        description="Test task",
        thread_id="$thread123",
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": "com.mindroom.scheduled.task",
                    "state_key": "task1",
                    "content": {
                        "task_id": "task1",
                        "workflow": workflow.model_dump_json(),
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
                        "workflow": workflow.model_dump_json(),
                        "status": "pending",
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                    "event_id": "$state_task2",
                    "sender": "@system:server",
                    "origin_server_ts": 1234567891,
                },
            ],
            room_id="!test:server",
        ),
    )
    client.room_put_state = AsyncMock(
        return_value=nio.ErrorResponse(
            "You don't have permission to post that to the room.",
            "M_FORBIDDEN",
        ),
    )

    with (
        patch("mindroom.scheduling._cancel_running_task") as mock_cancel,
        pytest.raises(
            ValueError,
            match=(
                r"Failed to cancel scheduled tasks: MindRoom could not cancel this scheduled task "
                r"because Matrix rejected the room-state write\."
            ),
        ),
    ):
        await cancel_all_scheduled_tasks(client=client, room_id="!test:server")

    assert client.room_put_state.await_count == 2
    mock_cancel.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_cancels_in_memory_after_each_persisted_write() -> None:
    """Bulk cancellation should stop each runner only after its cancelled state is persisted."""
    client = AsyncMock()
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": "com.mindroom.scheduled.task",
                    "state_key": "task1",
                    "content": {"status": "pending"},
                    "event_id": "$state_task1",
                    "sender": "@system:server",
                    "origin_server_ts": 1234567890,
                },
                {
                    "type": "com.mindroom.scheduled.task",
                    "state_key": "task2",
                    "content": {"status": "pending"},
                    "event_id": "$state_task2",
                    "sender": "@system:server",
                    "origin_server_ts": 1234567891,
                },
            ],
            room_id="!test:server",
        ),
    )

    call_order: list[str] = []

    def record_cancel(task_id: str) -> None:
        call_order.append(f"cancel:{task_id}")

    async def record_resolve(*_args: object, **_kwargs: object) -> tuple[AsyncMock, AsyncMock]:
        call_order.append("resolve")
        return (client, AsyncMock(return_value=client))

    async def record_persist(*_args: object, **kwargs: object) -> AsyncMock:
        task_id = kwargs["task_id"]
        call_order.append(f"persist:{task_id}")
        return client

    with (
        patch("mindroom.scheduling._cancel_running_task", side_effect=record_cancel),
        patch(
            "mindroom.scheduling._resolve_scheduled_task_writer_with_retry",
            new=AsyncMock(side_effect=record_resolve),
        ),
        patch(
            "mindroom.scheduling._persist_cancelled_scheduled_task_state",
            new=AsyncMock(side_effect=record_persist),
        ),
    ):
        result = await cancel_all_scheduled_tasks(client=client, room_id="!test:server")

    assert result == "✅ Cancelled 2 scheduled task(s)"
    assert call_order == [
        "resolve",
        "persist:task1",
        "cancel:task1",
        "resolve",
        "persist:task2",
        "cancel:task2",
    ]


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_uses_router_writer_when_current_bot_cannot_write() -> None:
    """Bulk cancellation should fall back to the router when the current bot lacks state-write power."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_general:localhost": {"display_name": "mindroom_general"},
                },
            },
            room_id="!test:server",
        ),
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": _SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task1",
                    "content": {
                        "task_id": "task1",
                        "workflow": ScheduledWorkflow(
                            schedule_type="once",
                            execute_at=datetime.now(UTC) + timedelta(minutes=5),
                            message="Check deployment",
                            description="Deployment check",
                            room_id="!test:server",
                        ).model_dump_json(),
                        "status": "pending",
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                    "event_id": "$state_task1",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567890,
                },
            ],
            room_id="!test:server",
        ),
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "users": {
                    "@mindroom_general:localhost": 0,
                    "@mindroom_router:localhost": 50,
                },
                "users_default": 0,
                "state_default": 50,
                "events": {_SCHEDULED_TASK_EVENT_TYPE: 50},
            },
            event_type="m.room.power_levels",
            state_key="",
            room_id="!test:server",
        ),
    )
    client.room_put_state = AsyncMock()

    router_client = AsyncMock()
    _allow_scheduled_task_writes(
        router_client,
        user_id="@mindroom_router:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 50,
        },
    )
    router_client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$cancelled"}, room_id="!test:server"),
    )

    result = await cancel_all_scheduled_tasks(
        client=client,
        room_id="!test:server",
        router_client=router_client,
    )

    assert result == "✅ Cancelled 1 scheduled task(s)"
    client.room_put_state.assert_not_awaited()
    router_client.room_put_state.assert_awaited_once()


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
async def test_get_scheduled_tasks_for_room_handles_transport_exception() -> None:
    """Room schedule listing should surface room-state transport failures."""
    client = AsyncMock()
    client.room_get_state = AsyncMock(side_effect=RuntimeError("boom transport"))

    with pytest.raises(scheduling.ScheduledTaskOperationError, match="Unable to retrieve scheduled tasks\\."):
        await get_scheduled_tasks_for_room(client=client, room_id="!test:server", include_non_pending=True)


@pytest.mark.asyncio
async def test_get_scheduled_task_returns_none_for_m_not_found() -> None:
    """Missing task state should still resolve to None."""
    client = AsyncMock()
    client.room_get_state_event = AsyncMock(return_value=nio.ErrorResponse("Not found", "M_NOT_FOUND"))

    task = await scheduling.get_scheduled_task(client=client, room_id="!test:server", task_id="missing")

    assert task is None


@pytest.mark.asyncio
async def test_get_scheduled_task_raises_state_unavailable_for_non_not_found_error() -> None:
    """Non-not-found task-state errors should surface as state_unavailable."""
    client = AsyncMock()
    client.room_get_state_event = AsyncMock(return_value=nio.ErrorResponse("Forbidden", "M_FORBIDDEN"))

    with pytest.raises(scheduling.ScheduledTaskOperationError) as error_info:
        await scheduling.get_scheduled_task(client=client, room_id="!test:server", task_id="task123")

    assert error_info.value.reason == "state_unavailable"
    assert error_info.value.public_message == "Unable to retrieve scheduled task state."


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
    existing_task = ScheduledTaskRecord(
        task_id="task123",
        room_id="!test:server",
        status="pending",
        created_at=datetime.now(UTC),
        workflow=workflow,
    )
    mutation_context = scheduling.ScheduledTaskMutationContext(
        task=existing_task,
        task_sender_id="@mindroom_general:localhost",
        writer_client=client,
        re_resolve_writer=AsyncMock(return_value=client),
    )

    with (
        patch(
            "mindroom.scheduling.resolve_existing_scheduled_task_mutation",
            new=AsyncMock(return_value=mutation_context),
        ),
        patch(
            "mindroom.scheduling.schedule_task",
            new=AsyncMock(return_value=("task123", "✅ Scheduled")),
        ) as mock_schedule,
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
            thread_id="$fallback_thread",
        )

    assert "✅ Updated task `task123`." in result
    mock_schedule.assert_awaited_once()
    call_kwargs = mock_schedule.await_args.kwargs
    assert call_kwargs["runtime"].client is client
    assert call_kwargs["room_id"] == "!test:server"
    assert call_kwargs["thread_id"] == "$original_thread"
    assert call_kwargs["scheduled_by"] == "@user:server"
    assert call_kwargs["full_text"] == "tomorrow at 9am updated task"
    assert call_kwargs["runtime"].config is config
    assert call_kwargs["runtime"].room is room
    assert call_kwargs["new_thread"] is False
    assert call_kwargs["task_id"] == "task123"
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
    existing_task = ScheduledTaskRecord(
        task_id="task123",
        room_id="!test:server",
        status="pending",
        created_at=datetime.now(UTC),
        workflow=workflow,
    )
    mutation_context = scheduling.ScheduledTaskMutationContext(
        task=existing_task,
        task_sender_id="@mindroom_general:localhost",
        writer_client=client,
        re_resolve_writer=AsyncMock(return_value=client),
    )

    with (
        patch(
            "mindroom.scheduling.resolve_existing_scheduled_task_mutation",
            new=AsyncMock(return_value=mutation_context),
        ),
        patch(
            "mindroom.scheduling.schedule_task",
            new=AsyncMock(return_value=("task123", "✅ Scheduled")),
        ) as mock_schedule,
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
            thread_id="$fallback_thread",
        )

    assert "✅ Updated task `task123`." in result
    call_kwargs = mock_schedule.await_args.kwargs
    assert call_kwargs["thread_id"] is None
    assert call_kwargs["new_thread"] is True


@pytest.mark.asyncio
async def test_persist_scheduled_task_state_raises_on_room_put_state_error() -> None:
    """Scheduled-task persistence should fail loudly when Matrix rejects the state write."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(
        return_value=nio.ErrorResponse(
            "You don't have permission to post that to the room.",
            "M_FORBIDDEN",
        ),
    )
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )

    with pytest.raises(scheduling.ScheduledTaskOperationError) as error_info:
        await _persist_scheduled_task_state(
            client=client,
            room_id="!test:server",
            task_id="task123",
            workflow=workflow,
        )

    error = error_info.value
    assert error.reason == "permission_denied"
    assert error.public_message == (
        "MindRoom could not save this scheduled task because Matrix rejected the room-state write. "
        f"Ensure a joined MindRoom bot can send `{_SCHEDULED_TASK_EVENT_TYPE}` state events and retry."
    )
    assert "M_FORBIDDEN: You don't have permission to post that to the room." in error.diagnostic_message


@pytest.mark.asyncio
async def test_schedule_task_surfaces_room_put_state_error_to_user() -> None:
    """Scheduling should return the Matrix state-write failure instead of claiming success."""
    client = AsyncMock()
    _allow_scheduled_task_writes(client, user_id="@mindroom_general:localhost")
    client.room_put_state = AsyncMock(
        return_value=nio.ErrorResponse(
            "You don't have permission to post that to the room.",
            "M_FORBIDDEN",
        ),
    )
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
    ):
        runtime = _scheduling_runtime(client=client)
        task_id, message = await schedule_task(
            runtime=runtime,
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )

    assert task_id is None
    assert "Failed to schedule" in message
    assert "MindRoom could not save this scheduled task because Matrix rejected the room-state write." in message
    assert _SCHEDULED_TASK_EVENT_TYPE in message


@pytest.mark.asyncio
async def test_schedule_task_uses_router_writer_when_current_bot_cannot_write() -> None:
    """Scheduling should fall back to the router and run the real one-time task there."""
    client = AsyncMock()
    _allow_scheduled_task_writes(
        client,
        user_id="@mindroom_general:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 50,
        },
    )
    client.room_put_state = AsyncMock()

    router_client = AsyncMock()
    router_conversation_cache = _conversation_cache()
    router_event_cache = _event_cache()
    router_client.user_id = "@mindroom_router:localhost"
    scheduled_task_state: dict[str, dict[str, object]] = {}
    router_client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_general:localhost": {"display_name": "mindroom_general"},
                    "@mindroom_router:localhost": {"display_name": "mindroom_router"},
                },
            },
            room_id="!test:server",
        ),
    )

    power_levels_response = nio.RoomGetStateEventResponse(
        content={
            "users": {
                "@mindroom_general:localhost": 0,
                "@mindroom_router:localhost": 50,
            },
            "users_default": 0,
            "state_default": 50,
            "events": {_SCHEDULED_TASK_EVENT_TYPE: 50},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!test:server",
    )

    async def router_room_get_state_event(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == "m.room.power_levels":
            return power_levels_response
        if event_type == _SCHEDULED_TASK_EVENT_TYPE and state_key in scheduled_task_state:
            return nio.RoomGetStateEventResponse(
                content=scheduled_task_state[state_key],
                event_type=event_type,
                state_key=state_key,
                room_id=room_id,
            )
        return nio.RoomGetStateEventError.from_dict(
            {"errcode": "M_NOT_FOUND", "error": "Missing state"},
            room_id=room_id,
        )

    async def router_room_put_state(
        room_id: str,
        event_type: str,
        content: dict[str, object],
        state_key: str,
    ) -> nio.RoomEventIdResponse | nio.ErrorResponse:
        assert event_type == _SCHEDULED_TASK_EVENT_TYPE
        scheduled_task_state[state_key] = dict(content)
        return nio.RoomPutStateResponse.from_dict({"event_id": f"${state_key}"}, room_id=room_id)

    async def router_room_get_state(room_id: str) -> nio.RoomGetStateResponse | nio.RoomGetStateError:
        events = [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": state_key,
                "content": content,
                "event_id": f"$state_{state_key}",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
            }
            for state_key, content in scheduled_task_state.items()
        ]
        return nio.RoomGetStateResponse.from_dict(events, room_id=room_id)

    router_client.room_get_state_event = AsyncMock(side_effect=router_room_get_state_event)
    router_client.room_get_state = AsyncMock(side_effect=router_room_get_state)
    router_client.room_put_state = AsyncMock(side_effect=router_room_put_state)

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(milliseconds=50),
        message="Check deployment",
        description="Deployment check",
    )
    config = MagicMock(timezone="UTC")
    config.get_domain.return_value = "localhost"
    config.get_ids.return_value = {
        "router": MagicMock(full_id="@mindroom_router:localhost"),
        "general": MagicMock(full_id="@mindroom_general:localhost"),
    }
    delivered = MagicMock(event_id="$sent", content_sent={"body": "sent"})

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
        patch("mindroom.scheduling.format_message_with_mentions", return_value={"body": "Check deployment"}),
        patch("mindroom.scheduling.send_message_result", new=AsyncMock(return_value=delivered)) as mock_send,
    ):
        task_id, message = await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                router_client=router_client,
                router_runtime=LiveRouterRuntime(
                    client=router_client,
                    conversation_cache=router_conversation_cache,
                    event_cache=router_event_cache,
                ),
                config=config,
            ),
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )
        assert task_id is not None
        running_task = scheduling._running_tasks[task_id]
        await asyncio.wait_for(running_task, timeout=1.0)

    assert "✅ Scheduled for" in message
    client.room_put_state.assert_not_awaited()
    assert router_client.room_put_state.await_count >= 2
    assert scheduled_task_state[task_id]["status"] == "completed"
    assert task_id not in scheduling._running_tasks
    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[0] is router_client
    assert router_conversation_cache.notify_outbound_message.call_count == 1


@pytest.mark.asyncio
async def test_schedule_task_prefers_router_writer_when_router_runtime_is_live() -> None:
    """New schedules should default to the live router writer even when the current bot can also write."""
    client = AsyncMock()
    _allow_scheduled_task_writes(client, user_id="@mindroom_general:localhost")

    router_client = AsyncMock()
    _allow_scheduled_task_writes(router_client, user_id="@mindroom_router:localhost")

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
        patch("mindroom.scheduling._save_pending_scheduled_task", new=AsyncMock()) as save_mock,
    ):
        task_id, message = await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                router_client=router_client,
                router_runtime=LiveRouterRuntime(
                    client=router_client,
                    conversation_cache=_conversation_cache(),
                    event_cache=_event_cache(),
                ),
                config=MagicMock(timezone="UTC"),
            ),
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )

    assert task_id is not None
    assert "✅ Scheduled for" in message
    assert save_mock.await_args.kwargs["writer_client"] is router_client


@pytest.mark.asyncio
async def test_schedule_task_surfaces_runtime_unavailable_when_router_runtime_is_missing() -> None:
    """Scheduling should fail clearly when the router writer has no live runtime bundle."""
    client = AsyncMock()
    _allow_scheduled_task_writes(
        client,
        user_id="@mindroom_general:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 50,
        },
    )
    client.room_put_state = AsyncMock()

    router_client = AsyncMock()
    _allow_scheduled_task_writes(
        router_client,
        user_id="@mindroom_router:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 50,
        },
    )
    router_client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$saved"}, room_id="!test:server"),
    )

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
    ):
        runtime = SchedulingRuntime(
            client=client,
            config=MagicMock(timezone="UTC"),
            runtime_paths=_runtime_paths(),
            room=MagicMock(),
            conversation_cache=_conversation_cache(),
            event_cache=_event_cache(),
            router_client=router_client,
            router_runtime=None,
        )
        task_id, message = await schedule_task(
            runtime=runtime,
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )

    assert task_id is None
    assert "selected bot runtime" in message
    client.room_put_state.assert_not_awaited()
    router_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_task_surfaces_raw_membership_exception_as_structured_failure() -> None:
    """Scheduling should convert joined-members transport failures into a user-facing error."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members = AsyncMock(side_effect=RuntimeError("boom transport"))

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
    ):
        task_id, message = await schedule_task(
            runtime=_scheduling_runtime(client=client),
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )

    assert task_id is None
    assert "Cannot persist scheduled tasks in this room" in message
    assert "MindRoom could not verify room membership for scheduled tasks" in message


@pytest.mark.asyncio
async def test_schedule_task_surfaces_raw_power_level_read_exception_as_structured_failure() -> None:
    """Scheduling should convert power-level read transport failures into a user-facing error."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_general:localhost": {"display_name": "mindroom_general"},
                },
            },
            room_id="!test:server",
        ),
    )
    client.room_get_state_event = AsyncMock(side_effect=RuntimeError("boom transport"))

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
    ):
        task_id, message = await schedule_task(
            runtime=_scheduling_runtime(client=client),
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )

    assert task_id is None
    assert "Cannot persist scheduled tasks in this room" in message
    assert "MindRoom could not read the room power levels needed for scheduled tasks" in message


@pytest.mark.asyncio
async def test_schedule_task_fails_loudly_when_router_has_not_joined_yet() -> None:
    """Scheduling should explain the router-join race instead of pretending to succeed."""
    client = AsyncMock()
    _allow_scheduled_task_writes(
        client,
        user_id="@mindroom_general:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 50,
        },
    )

    router_client = AsyncMock()
    router_client.user_id = "@mindroom_router:localhost"
    router_client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_general:localhost": {"display_name": "mindroom_general"},
                },
            },
            room_id="!test:server",
        ),
    )
    router_client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "users": {
                    "@mindroom_general:localhost": 0,
                    "@mindroom_router:localhost": 50,
                },
                "users_default": 0,
                "state_default": 50,
                "events": {_SCHEDULED_TASK_EVENT_TYPE: 50},
            },
            event_type="m.room.power_levels",
            state_key="",
            room_id="!test:server",
        ),
    )

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
    ):
        task_id, message = await schedule_task(
            runtime=_scheduling_runtime(client=client, router_client=router_client),
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )

    assert task_id is None
    assert "Cannot persist scheduled tasks in this room" in message
    assert "wait for it to join and retry" in message
    assert "!test:server" not in message
    assert "@mindroom_router:localhost" not in message


@pytest.mark.asyncio
@pytest.mark.parametrize("errcode", ["M_FORBIDDEN", "M_NOT_FOUND"])
async def test_schedule_task_treats_router_membership_errors_as_not_joined(errcode: str) -> None:
    """Router membership probes that imply absence should surface the not-joined remediation."""
    client = AsyncMock()
    _allow_scheduled_task_writes(
        client,
        user_id="@mindroom_general:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 50,
        },
    )

    router_client = AsyncMock()
    router_client.user_id = "@mindroom_router:localhost"
    router_client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersError.from_dict(
            {"errcode": errcode, "error": "You are not joined to this room"},
            room_id="!test:server",
        ),
    )

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
    ):
        task_id, message = await schedule_task(
            runtime=_scheduling_runtime(client=client, router_client=router_client),
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )

    assert task_id is None
    assert "Cannot persist scheduled tasks in this room" in message
    assert "wait for it to join and retry" in message


@pytest.mark.asyncio
async def test_resolve_scheduled_task_writer_reports_not_logged_in() -> None:
    """Writer resolution should report a not-ready bot without looping forever."""
    client = AsyncMock()
    client.user_id = None

    with pytest.raises(scheduling.ScheduledTaskOperationError) as error_info:
        await scheduling.resolve_scheduled_task_writer(client, "!test:server")

    error = error_info.value
    assert error.reason == "writer_unavailable"
    assert "MindRoom is not ready to manage scheduled tasks yet" in error.public_message


@pytest.mark.asyncio
async def test_resolve_scheduled_task_writer_reports_invalid_power_levels() -> None:
    """Writer resolution should surface invalid power-level state as a state-check failure."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_general:localhost": {"display_name": "mindroom_general"},
                },
            },
            room_id="!test:server",
        ),
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content="not-a-dict",
            event_type="m.room.power_levels",
            state_key="",
            room_id="!test:server",
        ),
    )

    with pytest.raises(scheduling.ScheduledTaskOperationError) as error_info:
        await scheduling.resolve_scheduled_task_writer(client, "!test:server")

    error = error_info.value
    assert error.reason == "writer_state_unavailable"
    assert "invalid room power-level state" in error.public_message


@pytest.mark.asyncio
async def test_resolve_existing_scheduled_task_mutation_keeps_reader_in_routerless_room() -> None:
    """Routerless mutations should fall back to the current reader when the original writer is gone."""
    reader_client = AsyncMock()
    _allow_scheduled_task_writes(reader_client, user_id="@mindroom_reader:server")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )
    reader_client.room_get_state = AsyncMock(
        return_value=_task_state_response(
            "task123",
            workflow,
            sender="@mindroom_writer:server",
        ),
    )

    with patch("mindroom.scheduling.login_agent_user", side_effect=AssertionError("sender login should not run")):
        mutation_context = await scheduling.resolve_existing_scheduled_task_mutation(
            reader_client=reader_client,
            room_id="!test:server",
            task_id="task123",
            config=_restore_config("@mindroom_writer:server"),
            runtime_paths=_runtime_paths(),
            router_client=None,
        )

    assert mutation_context.writer_client is reader_client
    await mutation_context.close()


@pytest.mark.asyncio
async def test_resolve_existing_scheduled_task_mutation_prefers_live_router_before_sender_login() -> None:
    """Mutation resolution should use the live router before attempting sender login."""
    reader_client = AsyncMock()
    _allow_scheduled_task_writes(
        reader_client,
        user_id="@mindroom_reader:server",
        event_level=50,
        users={
            "@mindroom_reader:server": 0,
            "@mindroom_router:server": 50,
        },
    )
    router_client = AsyncMock()
    _allow_scheduled_task_writes(
        router_client,
        user_id="@mindroom_router:server",
        event_level=50,
        users={
            "@mindroom_reader:server": 0,
            "@mindroom_router:server": 50,
        },
    )
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )
    reader_client.room_get_state = AsyncMock(
        return_value=_task_state_response(
            "task123",
            workflow,
            sender="@mindroom_writer:server",
        ),
    )

    with patch("mindroom.scheduling.login_agent_user", side_effect=AssertionError("sender login should not run")):
        mutation_context = await scheduling.resolve_existing_scheduled_task_mutation(
            reader_client=reader_client,
            room_id="!test:server",
            task_id="task123",
            config=_restore_config("@mindroom_writer:server"),
            runtime_paths=_runtime_paths(),
            router_client=router_client,
        )

    assert mutation_context.writer_client is router_client
    await mutation_context.close()


@pytest.mark.asyncio
async def test_resolve_existing_scheduled_task_mutation_does_not_login_deconfigured_historical_writer() -> None:
    """Removed agents should no longer regain write authority from persisted Matrix credentials."""
    reader_client = AsyncMock()
    reader_client.user_id = None
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )
    reader_client.room_get_state = AsyncMock(
        return_value=_task_state_response(
            "task123",
            workflow,
            sender="@mindroom_archived_writer:server",
        ),
    )

    with (
        patch("mindroom.scheduling.create_agent_user", side_effect=AssertionError("historical login should not run")),
        patch("mindroom.scheduling.login_agent_user", side_effect=AssertionError("historical login should not run")),
        pytest.raises(scheduling.ScheduledTaskOperationError) as error_info,
    ):
        await scheduling.resolve_existing_scheduled_task_mutation(
            reader_client=reader_client,
            room_id="!test:server",
            task_id="task123",
            config=_restore_config(),
            runtime_paths=_runtime_paths(),
            router_client=None,
        )

    assert error_info.value.reason == "writer_unavailable"


@pytest.mark.asyncio
async def test_resolve_existing_scheduled_task_mutation_closes_temp_client_on_resolution_failure() -> None:
    """Temporary sender clients should be closed when writer re-resolution still fails."""
    reader_client = AsyncMock()
    reader_client.user_id = None
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )
    reader_client.room_get_state = AsyncMock(
        return_value=_task_state_response(
            "task123",
            workflow,
            sender="@mindroom_writer:server",
        ),
    )

    sender_client = AsyncMock()
    sender_client.user_id = "@mindroom_writer:server"
    sender_client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersError.from_dict(
            {"errcode": "M_FORBIDDEN", "error": "You are not joined to this room"},
            room_id="!test:server",
        ),
    )
    sender_client.close = AsyncMock()

    with (
        patch("mindroom.scheduling.create_agent_user", return_value=MagicMock(agent_name="sender_1")),
        patch("mindroom.scheduling.login_agent_user", return_value=sender_client),
        pytest.raises(scheduling.ScheduledTaskOperationError),
    ):
        await scheduling.resolve_existing_scheduled_task_mutation(
            reader_client=reader_client,
            room_id="!test:server",
            task_id="task123",
            config=_restore_config("@mindroom_writer:server"),
            runtime_paths=_runtime_paths(),
            router_client=None,
        )

    sender_client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_existing_scheduled_task_mutation_re_resolves_to_router_after_initial_selection() -> None:
    """Mutation re-resolution should be able to move writes to the router after an initial non-router pick."""
    reader_client = AsyncMock()
    _allow_scheduled_task_writes(reader_client, user_id="@mindroom_reader:server")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )
    reader_client.room_get_state = AsyncMock(
        return_value=_task_state_response(
            "task123",
            workflow,
            sender="@mindroom_reader:server",
        ),
    )

    router_client = AsyncMock()
    router_client.user_id = "@mindroom_router:server"
    router_client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersError.from_dict(
            {"errcode": "M_NOT_FOUND", "error": "You are not joined to this room"},
            room_id="!test:server",
        ),
    )

    mutation_context = await scheduling.resolve_existing_scheduled_task_mutation(
        reader_client=reader_client,
        room_id="!test:server",
        task_id="task123",
        config=_restore_config("@mindroom_reader:server"),
        runtime_paths=_runtime_paths(),
        router_client=router_client,
    )

    assert mutation_context.writer_client is reader_client

    _allow_scheduled_task_writes(router_client, user_id="@mindroom_router:server")
    retry_writer = await mutation_context.re_resolve_writer()
    assert retry_writer is router_client
    await mutation_context.close()


@pytest.mark.asyncio
async def test_resolve_existing_scheduled_task_mutation_rejects_cross_thread_non_owner() -> None:
    """Non-owners outside the task thread should not be able to mutate a task."""
    reader_client = AsyncMock()
    _allow_scheduled_task_writes(reader_client, user_id="@mindroom_reader:server")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
        thread_id="$task-thread",
        created_by="@owner:server",
    )
    reader_client.room_get_state = AsyncMock(
        return_value=_task_state_response(
            "task123",
            workflow,
            sender="@mindroom_reader:server",
        ),
    )

    with pytest.raises(scheduling.ScheduledTaskOperationError) as error_info:
        await scheduling.resolve_existing_scheduled_task_mutation(
            reader_client=reader_client,
            room_id="!test:server",
            task_id="task123",
            config=_restore_config("@mindroom_reader:server"),
            runtime_paths=_runtime_paths(),
            router_client=None,
            requester_id="@other-user:server",
            requester_thread_id="$different-thread",
        )

    assert error_info.value.reason == "permission_denied"


@pytest.mark.asyncio
async def test_resolve_existing_scheduled_task_mutation_allows_same_thread_non_owner() -> None:
    """Same-thread requesters should still be allowed to mutate a task they did not create."""
    reader_client = AsyncMock()
    _allow_scheduled_task_writes(reader_client, user_id="@mindroom_reader:server")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
        thread_id="$shared-thread",
        created_by="@owner:server",
    )
    reader_client.room_get_state = AsyncMock(
        return_value=_task_state_response(
            "task123",
            workflow,
            sender="@mindroom_reader:server",
        ),
    )

    mutation_context = await scheduling.resolve_existing_scheduled_task_mutation(
        reader_client=reader_client,
        room_id="!test:server",
        task_id="task123",
        config=_restore_config("@mindroom_reader:server"),
        runtime_paths=_runtime_paths(),
        router_client=None,
        requester_id="@thread-participant:server",
        requester_thread_id="$shared-thread",
    )

    assert mutation_context.writer_client is reader_client
    await mutation_context.close()


@pytest.mark.asyncio
async def test_get_pending_task_record_with_retry_stops_after_max_retries() -> None:
    """Task-state retries should stop after a bounded number of failures."""
    error = scheduling.ScheduledTaskOperationError("state_unavailable", "Unable to retrieve scheduled task state.")

    with (
        patch(
            "mindroom.scheduling._get_pending_task_record",
            new=AsyncMock(side_effect=error),
        ) as mock_get_pending,
        patch("mindroom.scheduling.asyncio.sleep", new=AsyncMock()) as mock_sleep,
        pytest.raises(scheduling.ScheduledTaskOperationError, match="Unable to retrieve scheduled task state"),
    ):
        await scheduling._get_pending_task_record_with_retry(
            client=AsyncMock(),
            room_id="!test:server",
            task_id="task123",
        )

    assert mock_get_pending.await_count == scheduling._MAX_PENDING_TASK_READ_RETRIES
    assert mock_sleep.await_count == scheduling._MAX_PENDING_TASK_READ_RETRIES - 1


def test_scheduled_task_restore_sender_ids_include_persisted_agent_accounts() -> None:
    """Restore should trust persisted `agent_*` Matrix accounts in addition to configured bots."""
    state = scheduling.MatrixState(
        accounts={
            "agent_archived_writer": {
                "username": "mindroom_archived_writer",
                "password": "secret",
            },
        },
    )

    with patch("mindroom.scheduling.MatrixState.load", return_value=state):
        sender_ids = scheduling._scheduled_task_restore_sender_ids(
            _restore_config("@configured:server"),
            _runtime_paths(),
        )

    assert "@configured:server" in sender_ids
    assert "@mindroom_archived_writer:server" in sender_ids


@pytest.mark.asyncio
async def test_schedule_task_reports_router_power_level_remediation() -> None:
    """Scheduling should recommend a power-level fix when the router is joined but underpowered."""
    client = AsyncMock()
    _allow_scheduled_task_writes(
        client,
        user_id="@mindroom_general:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 0,
        },
    )

    router_client = AsyncMock()
    _allow_scheduled_task_writes(
        router_client,
        user_id="@mindroom_router:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 0,
        },
    )

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
    ):
        task_id, message = await schedule_task(
            runtime=_scheduling_runtime(client=client, router_client=router_client),
            room_id="!test:server",
            thread_id="$thread123",
            scheduled_by="@user:server",
            full_text="in 5 minutes check deployment",
        )

    assert task_id is None
    assert "Cannot persist scheduled tasks in this room" in message
    assert "grant a joined MindRoom bot enough power" in message
    assert "@mindroom_router:localhost" not in message
    assert "!test:server" not in message


@pytest.mark.asyncio
async def test_cancel_scheduled_task_uses_router_writer_when_current_bot_cannot_write() -> None:
    """Cancellation should fall back to the router when the current bot lacks state-write power."""
    client = AsyncMock()
    power_levels_response = nio.RoomGetStateEventResponse(
        content={
            "users": {
                "@mindroom_general:localhost": 0,
                "@mindroom_router:localhost": 50,
            },
            "users_default": 0,
            "state_default": 50,
            "events": {_SCHEDULED_TASK_EVENT_TYPE: 50},
        },
        event_type="m.room.power_levels",
        state_key="",
        room_id="!test:server",
    )
    client.user_id = "@mindroom_general:localhost"
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_general:localhost": {"display_name": "mindroom_general"},
                },
            },
            room_id="!test:server",
        ),
    )
    client.room_get_state_event = AsyncMock(
        side_effect=[
            nio.RoomGetStateEventResponse(
                content={
                    "task_id": "task123",
                    "workflow": ScheduledWorkflow(
                        schedule_type="once",
                        execute_at=datetime.now(UTC) + timedelta(minutes=5),
                        message="Check deployment",
                        description="Deployment check",
                        room_id="!test:server",
                    ).model_dump_json(),
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
                },
                event_type=_SCHEDULED_TASK_EVENT_TYPE,
                state_key="task123",
                room_id="!test:server",
            ),
            power_levels_response,
        ],
    )
    client.room_put_state = AsyncMock()

    router_client = AsyncMock()
    _allow_scheduled_task_writes(
        router_client,
        user_id="@mindroom_router:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 50,
        },
    )
    router_client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$cancelled"}, room_id="!test:server"),
    )

    result = await cancel_scheduled_task(
        client=client,
        room_id="!test:server",
        task_id="task123",
        router_client=router_client,
    )

    assert result == "✅ Cancelled task `task123`"
    client.room_put_state.assert_not_awaited()
    router_client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancel_scheduled_task_cancels_in_memory_after_persisting_state() -> None:
    """Cancellation should stop the in-memory runner only after state persistence succeeds."""
    client = AsyncMock()
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={"status": "pending"},
            event_type=_SCHEDULED_TASK_EVENT_TYPE,
            state_key="task123",
            room_id="!test:server",
        ),
    )

    call_order: list[str] = []

    def record_cancel(task_id: str) -> None:
        assert task_id == "task123"
        call_order.append("cancel")

    async def record_resolve(*_args: object, **_kwargs: object) -> tuple[AsyncMock, AsyncMock]:
        call_order.append("resolve")
        return (client, AsyncMock(return_value=client))

    async def record_persist(*_args: object, **_kwargs: object) -> AsyncMock:
        call_order.append("persist")
        return client

    with (
        patch("mindroom.scheduling._cancel_running_task", side_effect=record_cancel),
        patch(
            "mindroom.scheduling._resolve_scheduled_task_writer_with_retry",
            new=AsyncMock(side_effect=record_resolve),
        ),
        patch(
            "mindroom.scheduling._persist_cancelled_scheduled_task_state",
            new=AsyncMock(side_effect=record_persist),
        ),
    ):
        result = await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task123",
        )

    assert result == "✅ Cancelled task `task123`"
    assert call_order == ["resolve", "persist", "cancel"]


@pytest.mark.asyncio
async def test_cancel_scheduled_task_rejects_terminal_task_state() -> None:
    """Single-task cancellation should reject tasks that are no longer pending."""
    client = AsyncMock()
    completed_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "task_id": "task123",
                "workflow": completed_workflow.model_dump_json(),
                "status": "completed",
                "created_at": datetime.now(UTC).isoformat(),
            },
            event_type=_SCHEDULED_TASK_EVENT_TYPE,
            state_key="task123",
            room_id="!test:server",
        ),
    )

    with pytest.raises(scheduling.ScheduledTaskOperationError) as error_info:
        await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task123",
        )

    assert error_info.value.reason == "invalid_state"
    assert error_info.value.public_message == "Task `task123` cannot be cancelled because it is `completed`."


@pytest.mark.asyncio
async def test_cancel_scheduled_task_keeps_runner_alive_when_persist_fails() -> None:
    """Cancellation should not stop the in-memory runner when the state write fails."""
    client = AsyncMock()
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={"status": "pending"},
            event_type=_SCHEDULED_TASK_EVENT_TYPE,
            state_key="task123",
            room_id="!test:server",
        ),
    )

    with (
        patch("mindroom.scheduling._cancel_running_task") as mock_cancel,
        patch(
            "mindroom.scheduling._resolve_scheduled_task_writer_with_retry",
            new=AsyncMock(return_value=(client, AsyncMock(return_value=client))),
        ),
        patch(
            "mindroom.scheduling._persist_cancelled_scheduled_task_state",
            new=AsyncMock(
                side_effect=scheduling.ScheduledTaskOperationError(
                    "cancel_failed",
                    "MindRoom could not cancel this scheduled task because Matrix rejected the room-state write.",
                ),
            ),
        ),
        pytest.raises(scheduling.ScheduledTaskOperationError),
    ):
        await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task123",
        )

    mock_cancel.assert_not_called()


@pytest.mark.asyncio
async def test_save_pending_scheduled_task_skips_runner_start_after_interleaved_cancel() -> None:
    """Scheduling should not start a runner when cancellation wins before startup finishes."""
    client = AsyncMock()
    _allow_scheduled_task_writes(client, user_id="@mindroom_general:localhost")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )

    pending_persisted = asyncio.Event()
    allow_schedule_to_continue = asyncio.Event()
    task_state = {"status": "pending"}
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={"status": "pending"},
            event_type=_SCHEDULED_TASK_EVENT_TYPE,
            state_key="task123",
            room_id="!test:server",
        ),
    )

    async def persist_pending(*_args: object, **_kwargs: object) -> AsyncMock:
        task_state["status"] = "pending"
        pending_persisted.set()
        await allow_schedule_to_continue.wait()
        return client

    async def persist_cancelled(*_args: object, **_kwargs: object) -> AsyncMock:
        task_state["status"] = "cancelled"
        return client

    async def read_pending(*_args: object, **_kwargs: object) -> ScheduledTaskRecord | None:
        if task_state["status"] != "pending":
            return None
        return _record("task123", workflow, status="pending")

    with (
        patch("mindroom.scheduling._persist_scheduled_task_state", new=AsyncMock(side_effect=persist_pending)),
        patch(
            "mindroom.scheduling._persist_cancelled_scheduled_task_state",
            new=AsyncMock(side_effect=persist_cancelled),
        ),
        patch("mindroom.scheduling._get_pending_task_record", new=AsyncMock(side_effect=read_pending)),
        patch("mindroom.scheduling._start_scheduled_task") as start_mock,
    ):
        schedule_future = asyncio.create_task(
            scheduling._save_pending_scheduled_task(
                writer_client=client,
                room_id="!test:server",
                task_id="task123",
                workflow=workflow,
                runtime=_scheduling_runtime(client=client),
            ),
        )
        await pending_persisted.wait()
        cancel_result = await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task123",
            writer_client=client,
            re_resolve_writer=AsyncMock(return_value=client),
        )
        allow_schedule_to_continue.set()
        await schedule_future

    assert cancel_result == "✅ Cancelled task `task123`"
    start_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_can_beat_runner_start_during_interleaved_schedule() -> None:
    """Bulk cancellation should prevent a concurrent pending schedule from starting its runner."""
    client = AsyncMock()
    _allow_scheduled_task_writes(client, user_id="@mindroom_general:localhost")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        room_id="!test:server",
    )

    pending_persisted = asyncio.Event()
    allow_schedule_to_continue = asyncio.Event()
    task_state = {"status": "pending"}
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": _SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task123",
                    "content": {"status": "pending"},
                    "event_id": "$state_task123",
                    "sender": "@system:server",
                    "origin_server_ts": 1234567890,
                },
            ],
            room_id="!test:server",
        ),
    )

    async def persist_pending(*_args: object, **_kwargs: object) -> AsyncMock:
        task_state["status"] = "pending"
        pending_persisted.set()
        await allow_schedule_to_continue.wait()
        return client

    async def persist_cancelled(*_args: object, **_kwargs: object) -> AsyncMock:
        task_state["status"] = "cancelled"
        return client

    async def read_pending(*_args: object, **_kwargs: object) -> ScheduledTaskRecord | None:
        if task_state["status"] != "pending":
            return None
        return _record("task123", workflow, status="pending")

    with (
        patch("mindroom.scheduling._persist_scheduled_task_state", new=AsyncMock(side_effect=persist_pending)),
        patch(
            "mindroom.scheduling._resolve_scheduled_task_writer_with_retry",
            new=AsyncMock(return_value=(client, AsyncMock(return_value=client))),
        ),
        patch(
            "mindroom.scheduling._persist_cancelled_scheduled_task_state",
            new=AsyncMock(side_effect=persist_cancelled),
        ),
        patch("mindroom.scheduling._get_pending_task_record", new=AsyncMock(side_effect=read_pending)),
        patch("mindroom.scheduling._start_scheduled_task") as start_mock,
    ):
        schedule_future = asyncio.create_task(
            scheduling._save_pending_scheduled_task(
                writer_client=client,
                room_id="!test:server",
                task_id="task123",
                workflow=workflow,
                runtime=_scheduling_runtime(client=client),
            ),
        )
        await pending_persisted.wait()
        cancel_result = await cancel_all_scheduled_tasks(client=client, room_id="!test:server")
        allow_schedule_to_continue.set()
        await schedule_future

    assert cancel_result == "✅ Cancelled 1 scheduled task(s)"
    start_mock.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_normalizes_initial_room_state_exception() -> None:
    """Bulk cancellation should convert room-state transport failures into structured errors."""
    client = AsyncMock()
    client.room_get_state = AsyncMock(side_effect=RuntimeError("boom transport"))

    with pytest.raises(
        scheduling.ScheduledTaskOperationError,
        match=r"Unable to retrieve scheduled tasks\.",
    ):
        await cancel_all_scheduled_tasks(client=client, room_id="!test:server")


@pytest.mark.asyncio
async def test_cancel_scheduled_task_surfaces_raw_write_exception_as_structured_error() -> None:
    """Cancellation should normalize Matrix transport failures during state writes."""
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_general:localhost": {"display_name": "mindroom_general"},
                },
            },
            room_id="!test:server",
        ),
    )
    client.room_get_state_event = AsyncMock(
        side_effect=[
            nio.RoomGetStateEventResponse(
                content={"status": "pending"},
                event_type=_SCHEDULED_TASK_EVENT_TYPE,
                state_key="task123",
                room_id="!test:server",
            ),
            nio.RoomGetStateEventResponse(
                content={
                    "users": {"@mindroom_general:localhost": 100},
                    "users_default": 0,
                    "state_default": 50,
                    "events": {_SCHEDULED_TASK_EVENT_TYPE: 0},
                },
                event_type="m.room.power_levels",
                state_key="",
                room_id="!test:server",
            ),
        ],
    )
    client.room_put_state = AsyncMock(side_effect=RuntimeError("boom transport"))

    with pytest.raises(scheduling.ScheduledTaskOperationError) as error_info:
        await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="task123",
        )

    error = error_info.value
    assert error.public_message == (
        "MindRoom could not cancel this scheduled task because Matrix rejected the room-state write. "
        f"Ensure a joined MindRoom bot can send `{_SCHEDULED_TASK_EVENT_TYPE}` state events and retry."
    )
    assert "RuntimeError: boom transport" in error.diagnostic_message


@pytest.mark.asyncio
async def test_edit_scheduled_task_uses_router_writer_when_current_bot_cannot_write() -> None:
    """Editing should fall back to the router when the current bot lacks state-write power."""
    client = AsyncMock()
    existing_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Check deployment",
        description="Deployment check",
        thread_id="$thread123",
        room_id="!test:server",
    )
    client.user_id = "@mindroom_general:localhost"
    client.joined_members = AsyncMock(
        return_value=nio.JoinedMembersResponse.from_dict(
            {
                "joined": {
                    "@mindroom_general:localhost": {"display_name": "mindroom_general"},
                },
            },
            room_id="!test:server",
        ),
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": _SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task123",
                    "content": {
                        "task_id": "task123",
                        "workflow": existing_workflow.model_dump_json(),
                        "status": "pending",
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                    "event_id": "$state_task",
                    "sender": "@mindroom_general:localhost",
                    "origin_server_ts": 1234567890,
                },
            ],
            room_id="!test:server",
        ),
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "users": {
                    "@mindroom_general:localhost": 0,
                    "@mindroom_router:localhost": 50,
                },
                "users_default": 0,
                "state_default": 50,
                "events": {_SCHEDULED_TASK_EVENT_TYPE: 50},
            },
            event_type="m.room.power_levels",
            state_key="",
            room_id="!test:server",
        ),
    )
    client.room_put_state = AsyncMock()

    router_client = AsyncMock()
    _allow_scheduled_task_writes(
        router_client,
        user_id="@mindroom_router:localhost",
        event_level=50,
        users={
            "@mindroom_general:localhost": 0,
            "@mindroom_router:localhost": 50,
        },
    )
    router_client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$updated"}, room_id="!test:server"),
    )

    updated_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Updated deployment check",
        description="Updated deployment task",
    )

    with (
        patch(
            "mindroom.scheduling.get_available_agents_for_sender",
            return_value=[scheduling.MatrixID(username="general", domain="localhost")],
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=updated_workflow)),
        patch(
            "mindroom.scheduling._validate_agent_mentions",
            new=AsyncMock(return_value=_AgentValidationResult(True, [], [])),
        ),
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(
                client=client,
                router_client=router_client,
                config=MagicMock(timezone="UTC"),
            ),
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
            thread_id="$thread123",
        )

    assert "✅ Updated task `task123`." in result
    client.room_put_state.assert_not_awaited()
    router_client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_edit_scheduled_task_rejects_non_pending() -> None:
    """Editing should fail for cancelled/completed tasks."""
    client = AsyncMock()
    room = MagicMock()
    mutation_context = scheduling.ScheduledTaskMutationContext(
        task=ScheduledTaskRecord(
            task_id="task123",
            room_id="!test:server",
            status="cancelled",
            created_at=datetime.now(UTC),
            workflow=ScheduledWorkflow(
                schedule_type="once",
                execute_at=datetime.now(UTC) + timedelta(minutes=5),
                message="Initial message",
                description="Initial task",
                thread_id="$thread123",
                room_id="!test:server",
            ),
        ),
        task_sender_id="@mindroom_general:localhost",
        writer_client=client,
        re_resolve_writer=AsyncMock(return_value=client),
    )
    with patch(
        "mindroom.scheduling.resolve_existing_scheduled_task_mutation",
        new=AsyncMock(return_value=mutation_context),
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(client=client, room=room),
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
            thread_id="$thread123",
        )

    assert "cannot be edited" in result


@pytest.mark.asyncio
async def test_edit_scheduled_task_handles_raw_state_read_exception() -> None:
    """Editing should surface scheduled-task state read transport failures cleanly."""
    client = AsyncMock()
    with patch(
        "mindroom.scheduling.resolve_existing_scheduled_task_mutation",
        new=AsyncMock(
            side_effect=scheduling.ScheduledTaskOperationError(
                "state_unavailable",
                "Unable to retrieve scheduled task state.",
            ),
        ),
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(client=client),
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
            thread_id="$thread123",
        )

    assert result == "❌ Unable to retrieve scheduled task state."


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_preserves_created_at() -> None:
    """Editing should keep created_at metadata from the original task."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse(event_id="$evt", room_id="!test:server"),
    )
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
        existing_task=existing_task,
    )

    assert updated_task.created_at == created_at
    assert updated_task.workflow == updated_workflow
    client.room_put_state.assert_awaited_once()
    assert client.room_put_state.await_args.kwargs["content"]["created_at"] == created_at.isoformat()


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_is_state_only() -> None:
    """State-only edits should not require runtime-only scheduling collaborators."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse(event_id="$evt", room_id="!test:server"),
    )
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
        existing_task=existing_task,
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
            existing_task=existing_task,
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
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@blocked:server",
            full_text="remind me in 5 minutes to check logs",
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
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@blocked:server",
            full_text="remind me in 5 minutes",
            new_thread=True,
        )

    assert task_id is None
    assert "No agents" in message
