"""Test scheduled task restoration and deduplication."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock

import nio
import pytest

from mindroom import scheduling
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.scheduling import _MISSED_TASK_MAX_AGE_SECONDS, ScheduledWorkflow, restore_scheduled_tasks
from tests.conftest import bind_runtime_paths, make_event_cache_mock
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from pathlib import Path


def _conversation_cache() -> AsyncMock:
    access = AsyncMock()
    access.get_latest_thread_event_id_if_needed.return_value = None
    access.notify_outbound_message = Mock()
    return access


def _runtime_paths(tmp_path: Path) -> object:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


def _config(runtime_paths: object) -> Config:
    config = bind_runtime_paths(Config(authorization={"default_room_access": True}), runtime_paths)
    persist_entity_accounts(config, runtime_paths)
    return config


async def _make_state_event(
    runtime_paths: object,
    state_key: str,
    workflow: ScheduledWorkflow,
    status: str = "pending",
    idx: int = 1,
) -> dict:
    """Build a Matrix state event dict for a scheduled task."""
    client = AsyncMock()
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": f"$state_{state_key}"},
        room_id=workflow.room_id or "!r:server",
    )
    await scheduling._persist_scheduled_task_state(
        client=client,
        room_id=workflow.room_id or "!r:server",
        task_id=state_key,
        workflow=workflow,
        runtime_paths=runtime_paths,
        status=status,
        created_at=datetime.now(UTC),
    )
    return {
        "type": "com.mindroom.scheduled.task",
        "state_key": state_key,
        "content": client.room_put_state.await_args.kwargs["content"],
        "event_id": f"$e{idx}",
        "sender": "@s:server",
        "origin_server_ts": idx,
    }


@pytest.mark.asyncio
async def test_restore_executes_recent_missed_once_and_skips_invalid_cron(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Past once-tasks within the grace period should be restored; invalid cron skipped."""
    client = AsyncMock()
    runtime_paths = _runtime_paths(tmp_path)
    config = _config(runtime_paths)

    recent_past_once = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=10),
        message="Past",
        description="Past",
        room_id="!r:server",
        thread_id="$t",
        created_by="@user:server",
    )
    cron = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=None,  # invalid; should be skipped
        message="Cron",
        description="Cron",
        room_id="!r:server",
        thread_id="$t",
        created_by="@user:server",
    )

    valid_cron = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=None,
        message="Cron2",
        description="Cron2",
        room_id="!r:server",
        thread_id="$t",
        created_by="@user:server",
    )

    response = nio.RoomGetStateResponse.from_dict(
        [
            await _make_state_event(runtime_paths, "id1", recent_past_once, idx=1),
            await _make_state_event(runtime_paths, "id2", cron, idx=2),
            await _make_state_event(runtime_paths, "id3", valid_cron, status="cancelled", idx=3),
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
        runtime_paths,
        make_event_cache_mock(),
        _conversation_cache(),
    )
    # recent past once-task is restored; invalid cron and cancelled cron are skipped
    assert restored == 1


@pytest.mark.asyncio
async def test_restore_marks_ancient_missed_task_as_failed(tmp_path: Path) -> None:
    """One-time task older than the grace period should be marked as failed."""
    client = AsyncMock()
    runtime_paths = _runtime_paths(tmp_path)
    config = _config(runtime_paths)

    ancient_once = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=_MISSED_TASK_MAX_AGE_SECONDS + 3600),
        message="Ancient",
        description="Ancient task",
        room_id="!r:server",
        thread_id="$t",
        created_by="@user:server",
    )

    response = nio.RoomGetStateResponse.from_dict(
        [await _make_state_event(runtime_paths, "id-ancient", ancient_once)],
        room_id="!r:server",
    )
    client.room_get_state = AsyncMock(return_value=response)

    restored = await restore_scheduled_tasks(
        client,
        "!r:server",
        config,
        runtime_paths,
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
async def test_restore_future_task_still_works(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Future one-time tasks should be restored normally."""
    client = AsyncMock()
    runtime_paths = _runtime_paths(tmp_path)
    config = _config(runtime_paths)

    future_once = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=2),
        message="Future",
        description="Future task",
        room_id="!r:server",
        thread_id="$t",
        created_by="@user:server",
    )

    response = nio.RoomGetStateResponse.from_dict(
        [await _make_state_event(runtime_paths, "id-future", future_once)],
        room_id="!r:server",
    )
    client.room_get_state = AsyncMock(return_value=response)

    start_mock = MagicMock(return_value=True)
    monkeypatch.setattr(scheduling, "_start_scheduled_task", start_mock)

    restored = await restore_scheduled_tasks(
        client,
        "!r:server",
        config,
        runtime_paths,
        make_event_cache_mock(),
        _conversation_cache(),
    )
    assert restored == 1
    start_mock.assert_called_once()


@pytest.mark.asyncio
async def test_restore_skips_tasks_that_are_already_running(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Restoration should not create duplicate asyncio tasks for the same task id."""
    client = AsyncMock()
    runtime_paths = _runtime_paths(tmp_path)
    config = _config(runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Future",
        description="Future",
        room_id="!r:server",
        thread_id="$t",
        created_by="@user:server",
    )

    response = nio.RoomGetStateResponse.from_dict(
        [
            await _make_state_event(runtime_paths, "id1", workflow),
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
        runtime_paths,
        make_event_cache_mock(),
        _conversation_cache(),
    )

    assert restored == 0
    create_task.assert_not_called()
