"""Schedules stop when their creator no longer belongs to the target room."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom import scheduling
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.recurring_schedule import RecurringOccurrence, _RecurringCheckpoint
from mindroom.scheduling_executor import ScheduledWorkflowOutcome
from tests.conftest import make_conversation_reader_mock, make_matrix_client_mock

if TYPE_CHECKING:
    from pathlib import Path


def _owner_schedule(
    memberships: list[dict[str, Any] | nio.RoomGetStateEventError | Exception],
    *,
    schedule_type: Literal["once", "cron"] = "once",
    created_by: str | None = "@alice:server",
) -> tuple[AsyncMock, scheduling.ScheduledWorkflow, dict[str, Any]]:
    workflow = scheduling.ScheduledWorkflow(
        schedule_type=schedule_type,
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        cron_schedule=scheduling.CronSchedule(),
        message="Check the queue",
        description="Queue check",
        room_id="!test:server",
        created_by=created_by,
    )
    state: dict[str, Any] = {
        "task_id": "owner_task",
        "workflow": workflow.model_dump_json(),
        "status": "pending",
        "created_at": "2026-09-01T00:00:00+00:00",
    }

    async def read_state(room_id: str, event_type: str, state_key: str = "") -> object:
        assert room_id == "!test:server"
        if event_type == "m.room.encryption":
            return nio.RoomGetStateEventError("not encrypted", "M_NOT_FOUND")
        if event_type == "m.room.member":
            assert state_key == created_by
            membership = memberships.pop(0) if len(memberships) > 1 else memberships[0]
            if isinstance(membership, Exception):
                raise membership
            if isinstance(membership, nio.RoomGetStateEventError):
                return membership
            content = membership
        else:
            assert event_type == "com.mindroom.scheduled.task"
            assert state_key == "owner_task"
            content = state.copy()
        return nio.RoomGetStateEventResponse(content, event_type, state_key, room_id)

    async def write_state(room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> object:
        assert (room_id, event_type, state_key) == ("!test:server", "com.mindroom.scheduled.task", "owner_task")
        state.clear()
        state.update(content)
        return nio.RoomPutStateResponse("$state", room_id)

    client = make_matrix_client_mock(user_id="@router:server")
    client.homeserver = "https://matrix.example"
    client.user_id = "@router:server"
    client.device_id = "TEST"
    client.rooms = {}
    client.room_send.return_value = nio.RoomSendResponse("$message", "!test:server")
    client.room_get_state_event.side_effect = read_state
    client.room_put_state.side_effect = write_state
    return client, workflow, state


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["leave", "ban", "invite", "knock"])
async def test_departed_owner_cancels_persisted_schedule(membership: str) -> None:
    """Leaving, removal, or deactivation must retire the creator's pending task."""
    client, workflow, state = _owner_schedule([{"membership": membership}])

    task = await scheduling._reconcile_runnable_task_retrying(client, "!test:server", "owner_task")

    assert task is None
    assert state["status"] == "cancelled"
    assert state["workflow"] == workflow.model_dump_json()
    assert state["created_at"] == "2026-09-01T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("created_by", ["@alice:server", "@bob:server", None])
async def test_joined_or_unowned_schedule_stays_pending(created_by: str | None) -> None:
    """Other owners and existing schedules without ownership must remain usable."""
    client, _, state = _owner_schedule([{"membership": "join"}], created_by=created_by)

    task = await scheduling._reconcile_runnable_task_retrying(client, "!test:server", "owner_task")

    assert task is not None
    assert state["status"] == "pending"
    client.room_put_state.assert_not_awaited()
    if created_by is None:
        assert client.room_get_state_event.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
@pytest.mark.parametrize("use_admin", [False, True])
async def test_runner_cancels_absent_owner_before_waiting_or_firing(
    schedule_type: Literal["once", "cron"],
    use_admin: bool,
    tmp_path: Path,
) -> None:
    """Both runner paths must check ownership on startup, including restored tasks."""
    client, workflow, state = _owner_schedule([{"membership": "leave"}], schedule_type=schedule_type)
    admin = None
    if use_admin:
        write_state = client.room_put_state.side_effect

        async def admin_write(room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> bool:
            await write_state(room_id, event_type, state_key, content)
            return True

        admin = AsyncMock()
        admin.put_room_state.side_effect = admin_write
        client.room_put_state.side_effect = None
        client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    with (
        patch("mindroom.scheduling.asyncio.sleep", side_effect=AssertionError("Departed owner's task kept waiting")),
        patch(
            "mindroom.scheduling_executor.execute_scheduled_workflow",
            return_value=ScheduledWorkflowOutcome(status="delivered"),
        ) as execute,
    ):
        if schedule_type == "once":
            await scheduling._run_once_task(
                client,
                "owner_task",
                workflow,
                Config(),
                runtime_paths,
                make_conversation_reader_mock(),
                admin,
            )
        else:
            await scheduling._run_cron_task(
                client,
                "owner_task",
                workflow,
                {},
                Config(),
                runtime_paths,
                make_conversation_reader_mock(),
                admin,
            )

    assert state["status"] == "cancelled"
    execute.assert_not_awaited()
    client.room_send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unavailable",
    [
        nio.RoomGetStateEventError("rate limited", "M_LIMIT_EXCEEDED"),
        nio.RoomGetStateEventError("forbidden", "M_FORBIDDEN"),
        nio.RoomGetStateEventError("missing", "M_NOT_FOUND"),
        OSError("connection lost"),
        {},
        {"membership": "unexpected"},
    ],
)
async def test_unknown_membership_waits_for_authoritative_state(
    unavailable: dict[str, Any] | nio.RoomGetStateEventError | Exception,
) -> None:
    """Lookup failures must neither run the task nor cancel a potentially valid owner."""
    client, _, state = _owner_schedule([unavailable, {"membership": "join"}])

    async def wait_for_retry(_delay: float) -> None:
        assert state["status"] == "pending"
        client.room_put_state.assert_not_awaited()

    with patch("mindroom.scheduling.asyncio.sleep", side_effect=wait_for_retry) as sleep:
        task = await scheduling._reconcile_runnable_task_retrying(client, "!test:server", "owner_task")

    assert task is not None
    sleep.assert_awaited_once()
    assert state["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("editor", "message"),
    [("@alice:server", "Edited request"), ("@bob:server", "Edited request"), ("@alice:server", "Check the queue")],
)
async def test_edit_during_membership_lookup_survives_stale_departure(
    editor: str,
    message: str,
    tmp_path: Path,
) -> None:
    """A completed edit must invalidate departure evidence for the old workflow."""
    client, workflow, state = _owner_schedule([{"membership": "leave"}])
    existing = await scheduling.get_scheduled_task(client, "!test:server", "owner_task")
    assert existing is not None
    lookup_started = asyncio.Event()
    resume_lookup = asyncio.Event()
    read_state = client.room_get_state_event.side_effect

    async def blocked_lookup(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == "m.room.member":
            if not lookup_started.is_set():
                lookup_started.set()
                await resume_lookup.wait()
                membership = "leave"
            else:
                assert state_key == editor
                membership = "join"
            return nio.RoomGetStateEventResponse({"membership": membership}, event_type, state_key, room_id)
        return await read_state(room_id, event_type, state_key)

    client.room_get_state_event.side_effect = blocked_lookup
    updated = workflow.model_copy(update={"message": message, "created_by": editor})
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    with patch(
        "mindroom.scheduling_executor.execute_scheduled_workflow",
        return_value=ScheduledWorkflowOutcome(status="delivered"),
    ) as execute:
        async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
            tasks.create_task(
                scheduling._run_once_task(
                    client,
                    "owner_task",
                    workflow,
                    Config(),
                    runtime_paths,
                    make_conversation_reader_mock(),
                ),
            )
            await lookup_started.wait()
            await scheduling.save_edited_scheduled_task(client, "!test:server", "owner_task", updated, existing)
            resume_lookup.set()

    execute.assert_awaited_once()
    assert execute.await_args.args[1] == updated
    assert state["status"] == "completed"
    assert state["workflow"] == updated.model_dump_json()


@pytest.mark.asyncio
async def test_edit_cannot_resurrect_schedule_while_cancellation_is_persisting() -> None:
    """Runtime cancellation and a separate API client must serialize their writes."""
    client, workflow, state = _owner_schedule([{"membership": "leave"}])
    existing = await scheduling.get_scheduled_task(client, "!test:server", "owner_task")
    assert existing is not None
    write_started = asyncio.Event()
    resume_write = asyncio.Event()
    edit_started = asyncio.Event()
    write_state = client.room_put_state.side_effect

    async def blocked_write(room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> object:
        if content["status"] == "cancelled":
            write_started.set()
            await resume_write.wait()
        return await write_state(room_id, event_type, state_key, content)

    client.room_put_state.side_effect = blocked_write
    api_client = make_matrix_client_mock(user_id="@router:server")
    api_client.homeserver = client.homeserver
    api_client.room_get_state_event.side_effect = client.room_get_state_event.side_effect
    api_client.room_put_state.side_effect = write_state

    async def edit() -> None:
        edit_started.set()
        with pytest.raises(ValueError, match="cannot be edited"):
            await scheduling.save_edited_scheduled_task(
                api_client,
                "!test:server",
                "owner_task",
                workflow.model_copy(update={"message": "Late edit"}),
                existing,
            )

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        tasks.create_task(scheduling._reconcile_runnable_task_retrying(client, "!test:server", "owner_task"))
        await write_started.wait()
        tasks.create_task(edit())
        await edit_started.wait()
        resume_write.set()

    assert state["status"] == "cancelled"
    api_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_during_cancellation_retry_invalidates_old_departure() -> None:
    """Retry sleep must allow a rejoined creator to replace the departed workflow."""
    client, workflow, state = _owner_schedule([{"membership": "leave"}, {"membership": "join"}])
    existing = await scheduling.get_scheduled_task(client, "!test:server", "owner_task")
    assert existing is not None
    updated = workflow.model_copy(update={"message": "New request after rejoining"})
    write_state = client.room_put_state.side_effect
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("unavailable", "M_UNKNOWN")

    async def edit_on_retry(_delay: float) -> None:
        client.room_put_state.side_effect = write_state
        await scheduling.save_edited_scheduled_task(client, "!test:server", "owner_task", updated, existing)

    with patch("mindroom.scheduling.asyncio.sleep", side_effect=edit_on_retry):
        runnable = await scheduling._reconcile_runnable_task_retrying(client, "!test:server", "owner_task")

    assert runnable is not None
    assert runnable.workflow == updated
    assert state["status"] == "pending"


@pytest.mark.asyncio
async def test_stale_edit_cannot_overwrite_a_newer_workflow() -> None:
    """Slow parsing must not overwrite an intervening committed edit."""
    client, workflow, state = _owner_schedule([{"membership": "join"}])
    existing = await scheduling.get_scheduled_task(client, "!test:server", "owner_task")
    assert existing is not None
    updated = workflow.model_copy(update={"message": "First edit"})
    await scheduling.save_edited_scheduled_task(client, "!test:server", "owner_task", updated, existing)

    with pytest.raises(ValueError, match="changed"):
        await scheduling.save_edited_scheduled_task(client, "!test:server", "owner_task", workflow, existing)

    assert state["workflow"] == updated.model_dump_json()
    assert state["status"] == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
@pytest.mark.parametrize("due", [False, True])
async def test_departure_is_checked_while_waiting_and_before_firing(
    schedule_type: Literal["once", "cron"],
    due: bool,
    tmp_path: Path,
) -> None:
    """A creator leaving after startup must stop timers and imminent deliveries alike."""
    client, workflow, state = _owner_schedule(
        [{"membership": "join"}, {"membership": "leave"}],
        schedule_type=schedule_type,
    )
    execute_at = datetime.now(UTC) + timedelta(hours=-1 if due else 1)
    workflow.execute_at = execute_at
    state["workflow"] = workflow.model_dump_json()
    occurrence = RecurringOccurrence(tmp_path / "checkpoint.json", _RecurringCheckpoint("workflow", execute_at))
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    with (
        patch("mindroom.scheduling.plan_recurring_occurrence", return_value=occurrence),
        patch("mindroom.scheduling.asyncio.sleep", side_effect=[None, AssertionError("Task kept waiting")]),
        patch(
            "mindroom.scheduling_executor.execute_scheduled_workflow",
            return_value=ScheduledWorkflowOutcome(status="delivered"),
        ) as execute,
    ):
        assert scheduling._start_scheduled_task(
            client,
            "owner_task",
            workflow,
            Config(),
            runtime_paths,
            make_conversation_reader_mock(),
        )
        task = scheduling._running_tasks["owner_task"]
        await task

    assert state["status"] == "cancelled"
    assert "owner_task" not in scheduling._running_tasks
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancellation_write_failure_retries_without_reviving_departed_owner() -> None:
    """A failed cancellation write must stay retry-owned even if the user rejoins."""
    client, _, state = _owner_schedule([{"membership": "leave"}, {"membership": "join"}])
    write_state = client.room_put_state.side_effect
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("unavailable", "M_UNKNOWN")

    async def recover_state_write(_delay: float) -> None:
        assert state["status"] == "pending"
        client.room_put_state.side_effect = write_state

    with patch("mindroom.scheduling.asyncio.sleep", side_effect=recover_state_write) as sleep:
        task = await scheduling._reconcile_runnable_task_retrying(client, "!test:server", "owner_task")

    assert task is None
    assert state["status"] == "cancelled"
    sleep.assert_awaited_once()
