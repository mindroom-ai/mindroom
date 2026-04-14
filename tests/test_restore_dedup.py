"""Test scheduled task restoration and deduplication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import scheduling
from mindroom.constants import resolve_runtime_paths
from mindroom.scheduling import _MISSED_TASK_MAX_AGE_SECONDS, ScheduledWorkflow, restore_scheduled_tasks
from tests.conftest import make_event_cache_mock


def _conversation_cache() -> AsyncMock:
    access = AsyncMock()
    access.get_latest_thread_event_id_if_needed.return_value = None
    access.record_outbound_message = AsyncMock()
    return access


def _make_state_event(state_key: str, workflow: ScheduledWorkflow, status: str = "pending", idx: int = 1) -> dict:
    """Build a Matrix state event dict for a scheduled task."""
    return {
        "type": "com.mindroom.scheduled.task",
        "state_key": state_key,
        "content": {"workflow": workflow.model_dump_json(), "status": status},
        "event_id": f"$e{idx}",
        "sender": "@s:server",
        "origin_server_ts": idx,
    }


@pytest.mark.asyncio
async def test_restore_executes_recent_missed_once_and_skips_invalid_cron(monkeypatch: pytest.MonkeyPatch) -> None:
    """Past once-tasks within the grace period should be restored; invalid cron skipped."""
    client = AsyncMock()
    config = AsyncMock()

    recent_past_once = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=10),
        message="Past",
        description="Past",
        room_id="!r:server",
        thread_id="$t",
    )
    cron = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=None,  # invalid; should be skipped
        message="Cron",
        description="Cron",
        room_id="!r:server",
        thread_id="$t",
    )

    valid_cron = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=None,
        message="Cron2",
        description="Cron2",
        room_id="!r:server",
        thread_id="$t",
    )

    response = nio.RoomGetStateResponse.from_dict(
        [
            _make_state_event("id1", recent_past_once, idx=1),
            _make_state_event("id2", cron, idx=2),
            _make_state_event("id3", valid_cron, status="cancelled", idx=3),
        ],
        room_id="!r:server",
    )
    client.room_get_state = AsyncMock(return_value=response)

    # Stub _start_scheduled_task so no real asyncio task is created
    monkeypatch.setattr(scheduling, "_start_scheduled_task", MagicMock(return_value=True))

    restored = await restore_scheduled_tasks(
        client,
        "!r:server",
        config,
        resolve_runtime_paths(process_env={}),
        make_event_cache_mock(),
        _conversation_cache(),
    )
    # recent past once-task is restored; invalid cron and cancelled cron are skipped
    assert restored == 1


@pytest.mark.asyncio
async def test_restore_marks_ancient_missed_task_as_failed() -> None:
    """One-time task older than the grace period should be marked as failed."""
    client = AsyncMock()
    config = AsyncMock()

    ancient_once = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=_MISSED_TASK_MAX_AGE_SECONDS + 3600),
        message="Ancient",
        description="Ancient task",
        room_id="!r:server",
        thread_id="$t",
    )

    response = nio.RoomGetStateResponse.from_dict(
        [_make_state_event("id-ancient", ancient_once)],
        room_id="!r:server",
    )
    client.room_get_state = AsyncMock(return_value=response)

    restored = await restore_scheduled_tasks(
        client,
        "!r:server",
        config,
        resolve_runtime_paths(process_env={}),
        make_event_cache_mock(),
        _conversation_cache(),
    )
    assert restored == 0

    # Verify the task was marked as failed via room_put_state
    client.room_put_state.assert_called_once()
    call_kwargs = client.room_put_state.call_args
    assert call_kwargs.kwargs["content"]["status"] == "failed"
    assert call_kwargs.kwargs["state_key"] == "id-ancient"


@pytest.mark.asyncio
async def test_restore_future_task_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    """Future one-time tasks should be restored normally."""
    client = AsyncMock()
    config = AsyncMock()

    future_once = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=2),
        message="Future",
        description="Future task",
        room_id="!r:server",
        thread_id="$t",
    )

    response = nio.RoomGetStateResponse.from_dict(
        [_make_state_event("id-future", future_once)],
        room_id="!r:server",
    )
    client.room_get_state = AsyncMock(return_value=response)

    start_mock = MagicMock(return_value=True)
    monkeypatch.setattr(scheduling, "_start_scheduled_task", start_mock)

    restored = await restore_scheduled_tasks(
        client,
        "!r:server",
        config,
        resolve_runtime_paths(process_env={}),
        make_event_cache_mock(),
        _conversation_cache(),
    )
    assert restored == 1
    start_mock.assert_called_once()


@pytest.mark.asyncio
async def test_restore_skips_tasks_that_are_already_running(monkeypatch: pytest.MonkeyPatch) -> None:
    """Restoration should not create duplicate asyncio tasks for the same task id."""
    client = AsyncMock()
    config = AsyncMock()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Future",
        description="Future",
        room_id="!r:server",
        thread_id="$t",
    )

    response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "id1",
                "content": {"workflow": workflow.model_dump_json(), "status": "pending"},
                "event_id": "$e1",
                "sender": "@s:server",
                "origin_server_ts": 1,
            },
        ],
        room_id="!r:server",
    )
    client.room_get_state = AsyncMock(return_value=response)

    existing_task = MagicMock()
    existing_task.done.return_value = False
    monkeypatch.setattr(scheduling, "_running_tasks", {"id1": existing_task})
    create_task = MagicMock()
    monkeypatch.setattr(scheduling.asyncio, "create_task", create_task)

    restored = await restore_scheduled_tasks(
        client,
        "!r:server",
        config,
        resolve_runtime_paths(process_env={}),
        make_event_cache_mock(),
        _conversation_cache(),
    )

    assert restored == 0
    create_task.assert_not_called()
