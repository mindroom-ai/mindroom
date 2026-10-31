"""Test scheduled task restoration and deduplication."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.scheduling.runtime import _MISSED_TASK_MAX_AGE_SECONDS, _TaskRunner
from mindroom.scheduling.tasks import ScheduledWorkflow
from tests.conftest import make_matrix_client_mock
from tests.scheduling_helpers import (
    schedule_runtime_paths,
    scheduled_task_runtime,
    scheduling_bot,
    serve_room_schedule_state,
)

if TYPE_CHECKING:
    from pathlib import Path

_SCHEDULE_WRITER_ID = "@s:server"


def _make_state_event(state_key: str, workflow: ScheduledWorkflow, status: str = "pending", idx: int = 1) -> dict:
    """Build a Matrix state event dict for a scheduled task."""
    return {
        "type": "com.mindroom.scheduled.task",
        "state_key": state_key,
        "content": {"workflow": workflow.model_dump_json(), "status": status},
        "event_id": f"$e{idx}",
        "sender": _SCHEDULE_WRITER_ID,
        "origin_server_ts": idx,
    }


def _once(execute_at: datetime, description: str = "Task") -> ScheduledWorkflow:
    return ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=execute_at,
        message=description,
        description=description,
        room_id="!r:server",
        thread_id="$t",
    )


def _router_client(*events: dict) -> AsyncMock:
    """Return the router's client, the account that wrote every task in the room."""
    client = make_matrix_client_mock(user_id=_SCHEDULE_WRITER_ID)
    serve_room_schedule_state(client, "!r:server", list(events), sender=_SCHEDULE_WRITER_ID)
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$failed"},
        room_id="!r:server",
    )
    return client


@pytest.mark.asyncio
async def test_restore_executes_recent_missed_once_and_skips_invalid_cron(tmp_path: Path) -> None:
    """Past once-tasks within the grace period should be restored; invalid cron skipped."""
    cron = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="cron",
        cron_schedule=None,  # invalid; should be skipped
        message="Cron",
        description="Cron",
        room_id="!r:server",
        thread_id="$t",
    )
    client = _router_client(
        _make_state_event("id1", _once(datetime.now(UTC) - timedelta(minutes=10)), idx=1),
        _make_state_event("id2", cron, idx=2),
        _make_state_event("id3", cron, status="cancelled", idx=3),
    )
    bot = scheduling_bot(client)
    runtime = scheduled_task_runtime(schedule_runtime_paths(tmp_path, _SCHEDULE_WRITER_ID), bot, config=Config())

    with patch.object(runtime, "_start_runner", return_value=True) as start:
        restored = await runtime.restore_room(bot, "!r:server")

    # recent past once-task is restored; invalid cron and cancelled cron are skipped
    assert restored == 1
    start.assert_called_once()
    assert start.call_args.args[:2] == ("router", "id1")


@pytest.mark.asyncio
async def test_restore_marks_ancient_missed_task_as_failed(tmp_path: Path) -> None:
    """One-time task older than the grace period should be marked as failed."""
    ancient_once = _once(datetime.now(UTC) - timedelta(seconds=_MISSED_TASK_MAX_AGE_SECONDS + 3600), "Ancient task")
    client = _router_client(_make_state_event("id-ancient", ancient_once))
    bot = scheduling_bot(client)
    runtime = scheduled_task_runtime(schedule_runtime_paths(tmp_path, _SCHEDULE_WRITER_ID), bot, config=Config())

    restored = await runtime.restore_room(bot, "!r:server")

    assert restored == 0
    assert not runtime._runners
    client.room_put_state.assert_awaited_once()
    call_kwargs = client.room_put_state.call_args
    assert call_kwargs.kwargs["content"]["task_id"] == "id-ancient"
    assert call_kwargs.kwargs["content"]["status"] == "failed"
    assert call_kwargs.kwargs["state_key"] == "id-ancient"


@pytest.mark.asyncio
async def test_restore_marks_ancient_missed_task_failed_through_a_bot_that_may_write_state(tmp_path: Path) -> None:
    """A refused failure write goes through another ready bot whose room power allows it."""
    ancient_once = _once(datetime.now(UTC) - timedelta(seconds=_MISSED_TASK_MAX_AGE_SECONDS + 3600), "Ancient task")
    client = _router_client(_make_state_event("id-ancient", ancient_once))
    client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    writer = make_matrix_client_mock(user_id="@mindroom_general:server")
    room = nio.MatrixRoom("!r:server", writer.user_id)
    room.power_levels.users[writer.user_id] = 100
    writer.rooms = {"!r:server": room}
    writer.room_put_state.return_value = nio.RoomPutStateResponse.from_dict({"event_id": "$w"}, room_id="!r:server")
    bot = scheduling_bot(client)
    runtime = scheduled_task_runtime(
        schedule_runtime_paths(tmp_path, _SCHEDULE_WRITER_ID),
        bot,
        scheduling_bot(writer, agent_name="general"),
        config=Config(),
    )

    restored = await runtime.restore_room(bot, "!r:server")

    assert restored == 0
    client.room_put_state.assert_awaited_once()
    writer.room_put_state.assert_awaited_once()
    content = writer.room_put_state.call_args.kwargs["content"]
    assert content["task_id"] == "id-ancient"
    assert content["status"] == "failed"
    assert content["workflow"] == ancient_once.model_dump_json()


@pytest.mark.asyncio
async def test_restore_future_task_still_works(tmp_path: Path) -> None:
    """Future one-time tasks should be restored normally."""
    client = _router_client(_make_state_event("id-future", _once(datetime.now(UTC) + timedelta(hours=2))))
    bot = scheduling_bot(client)
    runtime = scheduled_task_runtime(schedule_runtime_paths(tmp_path, _SCHEDULE_WRITER_ID), bot, config=Config())

    with patch.object(runtime, "_start_runner", return_value=True) as start:
        restored = await runtime.restore_room(bot, "!r:server")

    assert restored == 1
    start.assert_called_once()


@pytest.mark.asyncio
async def test_restore_on_every_join_starts_each_task_once(tmp_path: Path) -> None:
    """Joining a room again, by any path, never starts a second runner for a task that already runs."""
    client = _router_client(_make_state_event("id1", _once(datetime.now(UTC) + timedelta(minutes=10))))
    bot = scheduling_bot(client)
    runtime = scheduled_task_runtime(schedule_runtime_paths(tmp_path, _SCHEDULE_WRITER_ID), bot, config=Config())
    started = asyncio.Event()

    async def wait_forever(_self: _TaskRunner, _workflow: ScheduledWorkflow) -> None:
        started.set()
        await asyncio.Event().wait()

    with patch.object(_TaskRunner, "run_once", wait_forever):
        first = await runtime.restore_room(bot, "!r:server")
        await started.wait()
        second = await runtime.restore_room(bot, "!r:server")
        runners = dict(runtime._runners)
        await runtime.stop()

    assert (first, second) == (1, 0)
    assert list(runners) == ["id1"]
