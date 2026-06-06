"""Tests for scheduling functionality that actually exercise the real code."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom import scheduling
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import resolve_runtime_paths
from mindroom.scheduling import (
    _SCHEDULED_TASK_EVENT_TYPE,
    CronSchedule,
    ScheduledTaskRecord,
    ScheduledWorkflow,
    SchedulingRuntime,
    _run_cron_task,
    _run_once_task,
    build_edited_scheduled_workflow,
    build_scheduled_task_read_model,
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
    scheduled_task_read_sort_key,
)
from tests.conftest import bind_runtime_paths, make_event_cache_mock
from tests.identity_helpers import entity_ids, persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Generator


def _runtime_paths() -> object:
    return resolve_runtime_paths(config_path=Path("config.yaml"), process_env={})


def _test_runtime_paths(tmp_path: Path) -> object:
    return resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )


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


def _room_put_state_success(room_id: str = "!test:server") -> nio.RoomPutStateResponse:
    return nio.RoomPutStateResponse.from_dict({"event_id": "$state_saved"}, room_id=room_id)


def _matrix_room(
    room_id: str,
    *,
    members: tuple[str, ...] = (),
    members_synced: bool = True,
) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id=room_id, own_user_id="@mindroom_router:server")
    for member_id in members:
        room.add_member(member_id, None, None)
    room.members_synced = members_synced
    return room


def _scheduling_runtime(
    *,
    client: AsyncMock | None = None,
    config: object | None = None,
    runtime_paths: object | None = None,
    room: object | None = None,
    conversation_cache: AsyncMock | None = None,
    event_cache: AsyncMock | None = None,
) -> SchedulingRuntime:
    return SchedulingRuntime(
        client=client or AsyncMock(),
        config=config or MagicMock(),
        runtime_paths=runtime_paths or _runtime_paths(),
        room=room or MagicMock(),
        conversation_cache=conversation_cache or _conversation_cache(),
        event_cache=event_cache or _event_cache(),
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


async def _signed_task_content(
    runtime_paths: object,
    task_id: str,
    workflow: ScheduledWorkflow,
    *,
    status: str = "pending",
    created_at: datetime | None = None,
) -> dict[str, object]:
    client = AsyncMock()
    client.room_put_state.return_value = _room_put_state_success(workflow.room_id or "!test:server")
    await scheduling._persist_scheduled_task_state(
        client=client,
        room_id=workflow.room_id or "!test:server",
        task_id=task_id,
        workflow=workflow,
        runtime_paths=runtime_paths,
        status=status,
        created_at=created_at or datetime.now(UTC),
    )
    return client.room_put_state.await_args.kwargs["content"]


async def _signed_state_event(
    runtime_paths: object,
    state_key: str,
    workflow: ScheduledWorkflow,
    *,
    status: str = "pending",
    idx: int = 1,
) -> dict[str, object]:
    return {
        "type": _SCHEDULED_TASK_EVENT_TYPE,
        "state_key": state_key,
        "content": await _signed_task_content(runtime_paths, state_key, workflow, status=status),
        "event_id": f"$state_{state_key}",
        "sender": "@system:server",
        "origin_server_ts": 1234567890 + idx,
    }


def _authorized_schedule_config(runtime_paths: object) -> Config:
    config = bind_runtime_paths(Config(authorization={"default_room_access": True}), runtime_paths)
    persist_entity_accounts(config, runtime_paths, usernames={"router": "mindroom_router"})
    return config


def test_scheduled_task_read_model_derives_display_fields_and_sort_order() -> None:
    """Schedule read models should preserve API-visible derived fields."""
    current_time = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    once_record = _record(
        "once123",
        ScheduledWorkflow(
            schedule_type="once",
            execute_at=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
            message="one-time message",
            description="One-time task",
            new_thread=True,
        ),
        status="cancelled",
    )
    cron_record = _record(
        "cron123",
        ScheduledWorkflow(
            schedule_type="cron",
            cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
            message="cron message",
            description="Cron task",
            created_by="@user:server",
            thread_id="$thread1",
        ),
    )

    once_model = build_scheduled_task_read_model(once_record, current_time=current_time)
    cron_model = build_scheduled_task_read_model(cron_record, current_time=current_time)

    assert once_model.task_id == "once123"
    assert once_model.status == "cancelled"
    assert once_model.next_run_at == datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    assert once_model.cron_expression is None
    assert once_model.new_thread is True
    assert cron_model.cron_expression == "0 9 * * *"
    assert cron_model.cron_description == "At 09:00"
    assert cron_model.next_run_at == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    assert cron_model.created_by == "@user:server"
    assert cron_model.thread_id == "$thread1"
    assert sorted([once_model, cron_model], key=scheduled_task_read_sort_key) == [cron_model, once_model]


def test_build_edited_scheduled_workflow_preserves_metadata_and_strips_text() -> None:
    """Patch-style edits should preserve ownership/thread metadata while normalizing text."""
    existing = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="original message",
        description="Original description",
        created_by="@user:server",
        thread_id="$thread1",
        room_id="!old:server",
        new_thread=True,
    )

    updated = build_edited_scheduled_workflow(
        existing,
        room_id="!new:server",
        schedule_type="cron",
        cron_expression="30 8 * * 1-5",
        message="  updated message  ",
        description="   ",
    )

    assert updated == ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="30", hour="8", day="*", month="*", weekday="1-5"),
        execute_at=None,
        message="updated message",
        description="updated message",
        created_by="@user:server",
        thread_id="$thread1",
        room_id="!new:server",
        new_thread=True,
    )


def test_build_edited_scheduled_workflow_rejects_invalid_field_combinations() -> None:
    """Patch-style edits should keep existing API validation messages."""
    existing_once = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2026, 1, 2, 9, 30, tzinfo=UTC),
        message="original message",
        description="Original description",
    )
    existing_cron = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="original message",
        description="Original description",
    )

    with pytest.raises(ValueError, match="Changing schedule_type is not supported"):
        build_edited_scheduled_workflow(existing_once, room_id="!room:server", schedule_type="cron")

    with pytest.raises(ValueError, match="cron_expression is only valid for cron schedules"):
        build_edited_scheduled_workflow(existing_once, room_id="!room:server", cron_expression="0 9 * * *")

    with pytest.raises(ValueError, match="execute_at is only valid for one-time schedules"):
        build_edited_scheduled_workflow(
            existing_cron,
            room_id="!room:server",
            execute_at=datetime(2026, 1, 3, 9, 30, tzinfo=UTC),
        )

    with pytest.raises(ValueError, match="message cannot be empty"):
        build_edited_scheduled_workflow(existing_once, room_id="!room:server", message="   ")


def test_build_edited_scheduled_workflow_rejects_too_frequent_cron() -> None:
    """Cron edits should enforce a minimum cadence."""
    existing_cron = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="original message",
        description="Original description",
    )

    with pytest.raises(ValueError, match="at least 5 minutes") as exc_info:
        build_edited_scheduled_workflow(existing_cron, room_id="!room:server", cron_expression="* * * * *")
    assert str(exc_info.value) == "Cron cadence must be at least 5 minutes"


def test_signature_key_creation_tolerates_unsupported_fsync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Signature key generation should not crash on filesystems that reject fsync."""
    runtime_paths = _test_runtime_paths(tmp_path)

    def _raise_unsupported_fsync(_fd: int) -> None:
        raise OSError

    monkeypatch.setattr(scheduling.os, "fsync", _raise_unsupported_fsync)

    key = scheduling._load_or_create_scheduled_task_signature_key(runtime_paths)

    assert len(key) == 32
    assert (runtime_paths.storage_root / "scheduling" / "scheduled_task_state.key").exists()


def test_signature_key_creation_fails_on_invalid_existing_key(tmp_path: Path) -> None:
    """Existing corrupt signing keys should not be overwritten by fresh keys."""
    runtime_paths = _test_runtime_paths(tmp_path)
    key_path = runtime_paths.storage_root / "scheduling" / "scheduled_task_state.key"
    key_path.parent.mkdir(parents=True)
    key_path.write_text("not-hex", encoding="utf-8")

    with pytest.raises(RuntimeError, match="Invalid scheduled task signature key"):
        scheduling._load_or_create_scheduled_task_signature_key(runtime_paths)

    assert key_path.read_text(encoding="utf-8") == "not-hex"


@pytest.mark.asyncio
async def test_persist_scheduled_task_state_fails_closed_on_matrix_error(tmp_path: Path) -> None:
    """Matrix state writes must fail closed when the homeserver rejects them."""
    client = AsyncMock()
    client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="State write",
        description="State write",
        room_id="!test:server",
    )

    with pytest.raises(RuntimeError, match="Failed to persist scheduled task `task_write_error`"):
        await scheduling._persist_scheduled_task_state(
            client=client,
            room_id="!test:server",
            task_id="task_write_error",
            workflow=workflow,
            runtime_paths=_test_runtime_paths(tmp_path),
        )


@pytest.fixture(autouse=True)
def _clear_deferred_overdue_queue() -> Generator[None, None, None]:
    clear_deferred_overdue_tasks()
    yield
    clear_deferred_overdue_tasks()


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_queues_overdue_one_time_tasks(tmp_path: Path) -> None:
    """Overdue one-time tasks should wait for sync instead of firing during restore."""
    client = AsyncMock()
    runtime_paths = _test_runtime_paths(tmp_path)
    overdue_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=5),
        message="Send the overdue reminder",
        description="Overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    content = await _signed_task_content(runtime_paths, "task_overdue", overdue_workflow)
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue",
                "content": content,
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
            config=_authorized_schedule_config(runtime_paths),
            runtime_paths=runtime_paths,
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
        )

    assert restored == 1
    mock_start.assert_not_called()
    assert len(scheduling._deferred_overdue_tasks) == 1
    assert scheduling._deferred_overdue_tasks[0].task_id == "task_overdue"


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_skips_unsigned_pending_state(tmp_path: Path) -> None:
    """Restore must fail closed for unsigned Matrix state."""
    client = AsyncMock()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Unsigned reminder",
        description="Unsigned reminder",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "unsigned_task",
                "content": {
                    "workflow": workflow.model_dump_json(),
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
                },
                "event_id": "$state_unsigned_task",
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
            runtime_paths=_test_runtime_paths(tmp_path),
            event_cache=_event_cache(),
            conversation_cache=_conversation_cache(),
        )

    assert restored == 0
    mock_start.assert_not_called()


@pytest.mark.asyncio
async def test_drain_deferred_overdue_tasks_starts_queued_tasks_after_sync(tmp_path: Path) -> None:
    """Queued overdue tasks should start in order once sync is ready."""
    client = AsyncMock()
    runtime_paths = _test_runtime_paths(tmp_path)
    config = _authorized_schedule_config(runtime_paths)
    overdue_workflow_1 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=10),
        message="First overdue reminder",
        description="First overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    overdue_workflow_2 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=3),
        message="Second overdue reminder",
        description="Second overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    content_1 = await _signed_task_content(runtime_paths, "task_overdue_1", overdue_workflow_1)
    content_2 = await _signed_task_content(runtime_paths, "task_overdue_2", overdue_workflow_2)
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue_1",
                "content": content_1,
                "event_id": "$state_task_overdue_1",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue_2",
                "content": content_2,
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
            runtime_paths=runtime_paths,
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
            runtime_paths,
            _event_cache(),
            conversation_cache,
        )

    assert drained == 2
    assert [call.args[1] for call in mock_start.call_args_list] == ["task_overdue_1", "task_overdue_2"]
    mock_sleep.assert_awaited_once_with(scheduling._DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS)
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_drain_deferred_overdue_tasks_continues_after_one_start_failure(tmp_path: Path) -> None:
    """One deferred task failure should not strand later queued tasks."""
    client = AsyncMock()
    runtime_paths = _test_runtime_paths(tmp_path)
    config = _authorized_schedule_config(runtime_paths)
    overdue_workflow_1 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=10),
        message="First overdue reminder",
        description="First overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    overdue_workflow_2 = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(minutes=3),
        message="Second overdue reminder",
        description="Second overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    content_1 = await _signed_task_content(runtime_paths, "task_overdue_1", overdue_workflow_1)
    content_2 = await _signed_task_content(runtime_paths, "task_overdue_2", overdue_workflow_2)
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue_1",
                "content": content_1,
                "event_id": "$state_task_overdue_1",
                "sender": "@system:server",
                "origin_server_ts": 1234567890,
            },
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_overdue_2",
                "content": content_2,
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
            runtime_paths=runtime_paths,
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
            runtime_paths,
            _event_cache(),
            conversation_cache,
        )

    assert drained == 1
    assert [call.args[1] for call in mock_start.call_args_list] == ["task_overdue_1", "task_overdue_2"]
    mock_sleep.assert_awaited_once_with(scheduling._DEFERRED_OVERDUE_TASK_START_DELAY_SECONDS)
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_keeps_cron_restoration_unchanged(tmp_path: Path) -> None:
    """Recurring cron tasks should still be restored immediately."""
    client = AsyncMock()
    runtime_paths = _test_runtime_paths(tmp_path)
    cron_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Run the daily report",
        description="Daily report",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    content = await _signed_task_content(runtime_paths, "task_cron", cron_workflow)
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_cron",
                "content": content,
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
            config=_authorized_schedule_config(runtime_paths),
            runtime_paths=runtime_paths,
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
        )

    assert restored == 1
    mock_start.assert_called_once()
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_does_not_queue_when_nothing_is_overdue(tmp_path: Path) -> None:
    """Future one-time tasks should still start normally and leave no deferred queue."""
    client = AsyncMock()
    runtime_paths = _test_runtime_paths(tmp_path)
    future_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=15),
        message="Future reminder",
        description="Future reminder",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    content = await _signed_task_content(runtime_paths, "task_future", future_workflow)
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_future",
                "content": content,
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
            config=_authorized_schedule_config(runtime_paths),
            runtime_paths=runtime_paths,
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
        )

    assert restored == 1
    mock_start.assert_called_once()
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_restore_scheduled_tasks_uses_canonical_state_parser_for_mixed_records(tmp_path: Path) -> None:
    """Restore should skip non-pending and malformed records while restoring valid pending records."""
    client = AsyncMock()
    runtime_paths = _test_runtime_paths(tmp_path)
    cron_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Run the daily report",
        description="Daily report",
        thread_id="$thread123",
        room_id="!test:server",
        created_by="@user:server",
    )
    content = await _signed_task_content(runtime_paths, "task_cron", cron_workflow)
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "malformed_content",
                "content": "not a dict",
                "event_id": "$state_malformed_content",
                "sender": "@system:server",
                "origin_server_ts": 1234567888,
            },
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "old_cancelled",
                "content": {
                    "status": "cancelled",
                },
                "event_id": "$state_cancelled",
                "sender": "@system:server",
                "origin_server_ts": 1234567889,
            },
            {
                "type": _SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_cron",
                "content": content,
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
            config=_authorized_schedule_config(runtime_paths),
            runtime_paths=runtime_paths,
            event_cache=_event_cache(),
            conversation_cache=conversation_cache,
        )

    assert restored == 1
    mock_start.assert_called_once()
    assert mock_start.call_args.args[1] == "task_cron"
    assert len(scheduling._deferred_overdue_tasks) == 0


@pytest.mark.asyncio
async def test_list_scheduled_tasks_real_implementation() -> None:
    """Test list_scheduled_tasks with real implementation, only mocking Matrix API."""
    # Create mock client
    client = AsyncMock()
    runtime_paths = _runtime_paths()

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
            await _signed_state_event(runtime_paths, "task1", workflow1, idx=1),
            await _signed_state_event(runtime_paths, "task2", workflow2, idx=2),
            await _signed_state_event(runtime_paths, "task3", workflow3, idx=3),
            await _signed_state_event(runtime_paths, "task4", workflow4, idx=4),
            await _signed_state_event(runtime_paths, "task5", workflow5, idx=5),
            await _signed_state_event(runtime_paths, "task6", workflow1, status="completed", idx=6),
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    # Test listing tasks for thread123
    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread123",
        config=None,
    )

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
    result2 = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread456",
        config=None,
    )
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
    runtime_paths = _runtime_paths()

    # Empty response
    mock_response = nio.RoomGetStateResponse.from_dict([], room_id="!test:server")
    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread123",
        config=None,
    )

    assert result == "No scheduled tasks found."


@pytest.mark.asyncio
async def test_list_scheduled_tasks_tasks_in_other_threads() -> None:
    """Test list_scheduled_tasks when all tasks are in other threads."""
    client = AsyncMock()
    runtime_paths = _runtime_paths()

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
            await _signed_state_event(runtime_paths, "task1", workflow),
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread123",  # Looking for thread123, but task is in thread456
        config=None,
    )

    assert "No scheduled tasks in this thread" in result
    assert "1 task(s) scheduled in other threads" in result


@pytest.mark.asyncio
async def test_list_scheduled_tasks_error_response() -> None:
    """Test list_scheduled_tasks when Matrix returns an error."""
    client = AsyncMock()
    runtime_paths = _runtime_paths()

    # Return an error response
    error_response = nio.RoomGetStateError.from_dict({"error": "Not authorized"}, room_id="!test:server")
    client.room_get_state = AsyncMock(return_value=error_response)

    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread123",
        config=None,
    )

    assert result == "Unable to retrieve scheduled tasks."


@pytest.mark.asyncio
async def test_list_scheduled_tasks_invalid_task_data() -> None:
    """Test list_scheduled_tasks handles invalid task data gracefully."""
    client = AsyncMock()
    runtime_paths = _runtime_paths()

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
                "content": await _signed_task_content(runtime_paths, "task3", valid_workflow),
                "event_id": "$state_task3",
                "sender": "@system:server",
                "origin_server_ts": 1234567892,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread123",
        config=None,
    )

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
    client.room_put_state = AsyncMock(return_value=_room_put_state_success())
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
    client.room_put_state = AsyncMock(return_value=_room_put_state_success())
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
        created_by="@user:server",
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Updated recurring message",
        description="Updated recurring description",
        room_id="!test:server",
        thread_id="$thread123",
        created_by="@user:server",
    )

    class _ImmediateCron:
        def get_next(self, _type: object) -> datetime:
            return datetime.now(UTC) - timedelta(seconds=1)

    async def _fetch_task(*_args: object, **_kwargs: object) -> ScheduledTaskRecord:
        return _record("task_cron_updated", updated_workflow, status="pending")

    with (
        patch("mindroom.scheduling.get_scheduled_task", side_effect=_fetch_task),
        patch("mindroom.scheduling._scheduled_workflow_authorization_error", return_value=None),
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
    config = AsyncMock()
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Recurring message",
        description="Recurring description",
        room_id="!test:server",
        thread_id="$thread123",
        created_by="@user:server",
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
        patch("mindroom.scheduling._scheduled_workflow_authorization_error", return_value=None),
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
async def test_run_cron_task_marks_failed_when_creator_authorization_is_revoked(tmp_path: Path) -> None:
    """Recurring tasks should stop once the stored creator loses schedule permissions."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(return_value=_room_put_state_success())
    config = AsyncMock()
    runtime_paths = _test_runtime_paths(tmp_path)
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Recurring message",
        description="Recurring description",
        room_id="!test:server",
        thread_id="$thread123",
        created_by="@revoked:server",
    )
    pending_record = _record("task_cron_revoked", workflow, status="pending")

    class _ImmediateCron:
        def get_next(self, _type: object) -> datetime:
            return datetime.now(UTC) - timedelta(seconds=1)

    with (
        patch(
            "mindroom.scheduling.get_scheduled_task",
            new=AsyncMock(side_effect=[pending_record, pending_record]),
        ),
        patch("mindroom.scheduling._scheduled_workflow_authorization_error", return_value="revoked"),
        patch("mindroom.scheduling._execute_scheduled_workflow", new=AsyncMock()) as execute_mock,
        patch("mindroom.scheduling.croniter", return_value=_ImmediateCron()),
    ):
        await _run_cron_task(
            client,
            "task_cron_revoked",
            workflow,
            {},
            config,
            runtime_paths,
            _conversation_cache(),
        )

    execute_mock.assert_not_awaited()
    client.room_put_state.assert_awaited_once()
    put_kwargs = client.room_put_state.await_args.kwargs
    assert put_kwargs["state_key"] == "task_cron_revoked"
    assert put_kwargs["content"]["status"] == "failed"


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
async def test_cancel_all_scheduled_tasks() -> None:
    """Test cancel_all_scheduled_tasks functionality."""
    # Create mock client
    client = AsyncMock()
    runtime_paths = _runtime_paths()

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
            await _signed_state_event(runtime_paths, "task1", workflow1, idx=1),
            await _signed_state_event(runtime_paths, "task2", workflow2, idx=2),
            await _signed_state_event(runtime_paths, "task3", workflow1, status="cancelled", idx=3),
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$event123"}, room_id="!test:server"),
    )

    result = await cancel_all_scheduled_tasks(client=client, room_id="!test:server", runtime_paths=runtime_paths)

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
        assert call[1]["content"]["signature_version"] == "hmac-sha256-v1"
        assert "signature" in call[1]["content"]


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_reports_when_every_cancel_write_fails() -> None:
    """Bulk cancellation should not report no tasks when every state write failed."""
    client = AsyncMock()
    runtime_paths = _runtime_paths()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Pending",
        description="Pending",
        room_id="!test:server",
    )
    mock_response = nio.RoomGetStateResponse.from_dict(
        [await _signed_state_event(runtime_paths, "task1", workflow, idx=1)],
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=mock_response)
    client.room_put_state = AsyncMock(return_value=nio.RoomPutStateError("forbidden", "M_FORBIDDEN"))

    result = await cancel_all_scheduled_tasks(client=client, room_id="!test:server", runtime_paths=runtime_paths)

    assert result == "❌ Failed to cancel 1 scheduled task(s)"


@pytest.mark.asyncio
async def test_cancel_scheduled_task_rejects_unsigned_state() -> None:
    """Cancelling by id should not trust unsigned Matrix state."""
    client = AsyncMock()
    runtime_paths = _runtime_paths()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Unsigned task",
        description="Unsigned task",
        thread_id="$thread123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "task_id": "task_unsigned",
                "workflow": workflow.model_dump_json(),
                "status": "pending",
                "created_at": datetime.now(UTC).isoformat(),
            },
            event_type=_SCHEDULED_TASK_EVENT_TYPE,
            state_key="task_unsigned",
            room_id="!test:server",
        ),
    )

    result = await cancel_scheduled_task(
        client=client,
        room_id="!test:server",
        task_id="task_unsigned",
        runtime_paths=runtime_paths,
    )

    assert result == "❌ Task `task_unsigned` not found."
    client.room_put_state.assert_not_called()


@pytest.mark.asyncio
async def test_get_scheduled_tasks_for_room_skips_cancelled_without_workflow() -> None:
    """Cancelled tasks must carry the same workflow payload as active tasks."""
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

    assert tasks == []


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

    result = await cancel_all_scheduled_tasks(client=client, room_id="!test:server", runtime_paths=_runtime_paths())

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
    runtime_paths = _runtime_paths()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Initial message",
        description="Initial task",
        thread_id="$original_thread",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateEventResponse(
        content=await _signed_task_content(runtime_paths, "task123", workflow),
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
            runtime=_scheduling_runtime(client=client, config=config, runtime_paths=runtime_paths, room=room),
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
    runtime_paths = _runtime_paths()
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
        content=await _signed_task_content(runtime_paths, "task123", workflow),
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
            runtime=_scheduling_runtime(client=client, config=config, runtime_paths=runtime_paths, room=room),
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
async def test_edit_scheduled_task_rejects_non_pending() -> None:
    """Editing should fail for cancelled/completed tasks."""
    client = AsyncMock()
    room = MagicMock()
    runtime_paths = _runtime_paths()
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        message="original task",
        description="original task",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateEventResponse(
        content=await _signed_task_content(runtime_paths, "task123", workflow, status="cancelled"),
        event_type=_SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(return_value=state_response)

    result = await edit_scheduled_task(
        runtime=_scheduling_runtime(client=client, runtime_paths=runtime_paths, room=room),
        room_id="!test:server",
        task_id="task123",
        full_text="tomorrow at 9am updated task",
        scheduled_by="@user:server",
        thread_id="$thread123",
    )

    assert "cannot be edited" in result


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_preserves_created_at() -> None:
    """Editing should keep created_at metadata from the original task."""
    client = AsyncMock()
    client.room_put_state.return_value = _room_put_state_success()
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
    client.room_put_state.return_value = _room_put_state_success()
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
            "mindroom.scheduling.responder_candidate_entities_for_room",
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
async def test_schedule_task_rejects_when_room_task_cap_reached() -> None:
    """New schedules should stop before parsing when room pending-task cap is reached."""
    client = AsyncMock()
    room = MagicMock()
    existing_tasks = [
        _record(
            f"task{i}",
            ScheduledWorkflow(
                schedule_type="once",
                execute_at=datetime.now(UTC) + timedelta(minutes=5),
                message=f"Existing {i}",
                description=f"Existing {i}",
                room_id="!test:server",
                created_by="@user:server",
            ),
        )
        for i in range(100)
    ]

    get_tasks_mock = AsyncMock(return_value=existing_tasks)
    with (
        patch(
            "mindroom.scheduling.responder_candidate_entities_for_room",
            return_value=[MagicMock()],
        ),
        patch("mindroom.scheduling.get_scheduled_tasks_for_room", new=get_tasks_mock),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock()) as parse_mock,
        patch("mindroom.scheduling._save_pending_scheduled_task", new=AsyncMock()) as save_mock,
    ):
        runtime = _scheduling_runtime(client=client, room=room)
        task_id, message = await schedule_task(
            runtime=runtime,
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@user:server",
            full_text="in 5 minutes check logs",
        )

    assert task_id is None
    assert "Maximum scheduled tasks" in message
    get_tasks_mock.assert_awaited_once_with(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime.runtime_paths,
        require_trusted=True,
    )
    parse_mock.assert_not_awaited()
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_task_blocked_sender_new_thread_returns_error() -> None:
    """new_thread mode should also return a clean error when the sender has no visible agents."""
    client = AsyncMock()
    room = MagicMock()
    config = MagicMock()

    with (
        patch(
            "mindroom.scheduling.responder_candidate_entities_for_room",
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


@pytest.mark.asyncio
async def test_schedule_task_uses_configured_room_boundary_without_membership_refresh(tmp_path: Path) -> None:
    """Configured schedule rooms should use the static responder boundary without membership refresh."""
    client = AsyncMock()
    room = _matrix_room("!test:server", members_synced=False)
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "assistant": AgentConfig(
                    display_name="Assistant",
                    role="Test assistant",
                    rooms=["!test:server"],
                ),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "mindroom_router_oldns", "assistant": "mindroom_assistant_oldns"},
    )
    room.add_member(f"@mindroom_router_oldns:{config.get_domain(runtime_paths)}", "Router", None)
    runtime = _scheduling_runtime(client=client, config=config, runtime_paths=runtime_paths, room=room)
    parse_result = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="check logs",
        description="check logs",
        room_id="!test:server",
        thread_id=None,
    )
    client.joined_members.return_value = nio.JoinedMembersResponse.from_dict(
        {
            "joined": {
                f"@mindroom_router_oldns:{config.get_domain(runtime_paths)}": {"display_name": "Router"},
                f"@mindroom_assistant_oldns:{config.get_domain(runtime_paths)}": {"display_name": "Assistant"},
            },
        },
        room_id="!test:server",
    )

    with (
        patch("mindroom.scheduling._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=parse_result)),
        patch("mindroom.scheduling._validate_agent_mentions") as mock_validate,
        patch("mindroom.scheduling._save_pending_scheduled_task", new=AsyncMock(return_value=None)),
        patch("mindroom.scheduling.uuid.uuid4", return_value="task12345"),
    ):
        mock_validate.return_value = scheduling._AgentValidationResult(True, [], [])
        task_id, message = await schedule_task(
            runtime=runtime,
            room_id="!test:server",
            thread_id=None,
            scheduled_by=f"@alice:{config.get_domain(runtime_paths)}",
            full_text="in 5 minutes check logs",
        )

    assert task_id == "task1234"
    assert "Scheduled" in message
    client.joined_members.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_task_rejects_mentions_outside_existing_thread_scope(tmp_path: Path) -> None:
    """Existing-thread schedules should validate parsed mentions against thread-scoped responders."""
    client = AsyncMock()
    room = _matrix_room("!test:server")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "assistant": AgentConfig(display_name="Assistant", role="Test assistant"),
                "writer": AgentConfig(display_name="Writer", role="Test writer"),
            },
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(
        config,
        runtime_paths,
        usernames={"assistant": "actual_assistant", "writer": "actual_writer"},
    )
    thread_message = MagicMock()
    thread_message.sender = ids["assistant"].full_id
    runtime = _scheduling_runtime(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        room=room,
        conversation_cache=_conversation_cache(thread_history=[thread_message]),
    )
    parse_result = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@writer check logs",
        description="check logs",
        room_id="!test:server",
        thread_id="$thread",
    )

    with (
        patch(
            "mindroom.scheduling.responder_candidate_entities_for_room",
            new=AsyncMock(return_value=[ids["assistant"], ids["writer"]]),
        ),
        patch("mindroom.scheduling._parse_workflow_schedule", new=AsyncMock(return_value=parse_result)),
        patch("mindroom.scheduling._save_pending_scheduled_task", new=AsyncMock()) as save_task,
    ):
        task_id, message = await schedule_task(
            runtime=runtime,
            room_id="!test:server",
            thread_id="$thread",
            scheduled_by="@alice:localhost",
            full_text="in 5 minutes ask writer to check logs",
        )

    assert task_id is None
    assert "@writer is not available in this thread" in message
    save_task.assert_not_awaited()
