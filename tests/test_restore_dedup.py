"""Test scheduled task restoration and deduplication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import nio
import pytest

from mindroom import scheduling
from mindroom.constants import resolve_runtime_paths
from mindroom.scheduling import ScheduledWorkflow, restore_scheduled_tasks


@pytest.mark.asyncio
async def test_restore_skips_past_once_and_does_not_duplicate_cron() -> None:
    """Test that past once tasks are skipped and cron tasks are not duplicated."""
    client = AsyncMock()
    config = AsyncMock()

    past_once = ScheduledWorkflow(
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

    # Build state events: first two should be skipped; third malformed also skipped
    response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "id1",
                "content": {"workflow": past_once.model_dump_json(), "status": "pending"},
                "event_id": "$e1",
                "sender": "@s:server",
                "origin_server_ts": 1,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "id2",
                "content": {"workflow": cron.model_dump_json(), "status": "pending"},
                "event_id": "$e2",
                "sender": "@s:server",
                "origin_server_ts": 2,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "id3",
                "content": {"workflow": valid_cron.model_dump_json(), "status": "cancelled"},
                "event_id": "$e3",
                "sender": "@s:server",
                "origin_server_ts": 3,
            },
        ],
        room_id="!r:server",
    )
    client.room_get_state = AsyncMock(return_value=response)

    restored = await restore_scheduled_tasks(client, "!r:server", config, resolve_runtime_paths(process_env={}))
    # All should be skipped: 0 restored
    assert restored == 0


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

    restored = await restore_scheduled_tasks(client, "!r:server", config, resolve_runtime_paths(process_env={}))

    assert restored == 0
    create_task.assert_not_called()
