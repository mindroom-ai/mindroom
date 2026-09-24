"""Scheduled tasks only run from room state written by MindRoom's own bot accounts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
from structlog.testing import capture_logs

from mindroom import scheduling
from mindroom.config.main import Config
from mindroom.scheduling_executor import ScheduledWorkflowOutcome
from tests.conftest import make_conversation_reader_mock, make_matrix_client_mock
from tests.scheduling_helpers import (
    SCHEDULE_WRITER_ID,
    persist_schedule_writer,
    schedule_runtime_paths,
    scheduled_task_state_event,
    serve_task_state_events,
)

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path

    from mindroom.constants import RuntimePaths

ROOM_ID = "!test:server"
TASK_ID = "task1"
HUMAN_ID = "@mallory:server"
RETIRED_AGENT_ID = "@mindroom_retired:server"
TASK_STATE_PATH = "/_matrix/client/v3/rooms/%21test%3Aserver/state/com.mindroom.scheduled.task/task1?format=event"


@pytest.fixture(autouse=True)
def _reset_scheduler_state() -> Generator[None, None, None]:
    scheduling.clear_deferred_overdue_tasks()
    scheduling._warn_ignored_task_state.cache_clear()
    yield
    scheduling.clear_deferred_overdue_tasks()


def _workflow(created_by: str | None) -> scheduling.ScheduledWorkflow:
    return scheduling.ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Send the weekly report",
        description="Weekly report",
        room_id=ROOM_ID,
        created_by=created_by,
    )


def _pending_content(workflow: scheduling.ScheduledWorkflow) -> dict[str, Any]:
    return {
        "task_id": TASK_ID,
        "workflow": workflow.model_dump_json(),
        "status": "pending",
        "created_at": "2026-09-01T00:00:00+00:00",
    }


def _room_with_task(content: dict[str, Any], *, sender: str) -> AsyncMock:
    """Return a client for a room holding one task state event from ``sender`` where every member is joined."""
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    client.homeserver = "https://matrix.example"

    async def read_state_event(room_id: str, event_type: str, state_key: str = "") -> nio.RoomGetStateEventResponse:
        if event_type == "m.room.member":
            return nio.RoomGetStateEventResponse({"membership": "join"}, event_type, state_key, room_id)
        assert (room_id, event_type, state_key) == (ROOM_ID, "com.mindroom.scheduled.task", TASK_ID)
        return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)

    client.room_get_state_event.side_effect = read_state_event
    client.room_get_state.return_value = nio.RoomGetStateResponse.from_dict(
        [scheduled_task_state_event(TASK_ID, content, room_id=ROOM_ID, sender=sender)],
        room_id=ROOM_ID,
    )
    client.room_put_state.return_value = nio.RoomPutStateResponse("$written", ROOM_ID)
    serve_task_state_events(client, sender=sender)
    return client


async def _run_once(
    client: AsyncMock,
    workflow: scheduling.ScheduledWorkflow,
    runtime_paths: RuntimePaths,
) -> AsyncMock:
    with patch(
        "mindroom.scheduling_executor.execute_scheduled_workflow",
        new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
    ) as execute:
        await scheduling._run_once_task(
            client,
            TASK_ID,
            workflow,
            Config(),
            runtime_paths,
            make_conversation_reader_mock(),
        )
    return execute


@pytest.mark.asyncio
async def test_human_written_task_naming_another_requester_never_fires(tmp_path: Path) -> None:
    """A room admin's task state must not run as the creator it names, and must not be rewritten."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    workflow = _workflow(created_by="@victim:server")
    client = _room_with_task(_pending_content(workflow), sender=HUMAN_ID)

    execute = await _run_once(client, workflow, runtime_paths)

    execute.assert_not_awaited()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity_name", "writer_id"),
    [("router", SCHEDULE_WRITER_ID), ("assistant", "@mindroom_assistant:server")],
)
async def test_task_written_by_managed_bot_account_fires(tmp_path: Path, entity_name: str, writer_id: str) -> None:
    """Router- and agent-written task state runs as its recorded creator."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    persist_schedule_writer(runtime_paths, writer_id, entity_name=entity_name)
    workflow = _workflow(created_by="@alice:server")
    client = _room_with_task(_pending_content(workflow), sender=writer_id)

    execute = await _run_once(client, workflow, runtime_paths)

    execute.assert_awaited_once()
    assert execute.await_args.args[1].created_by == "@alice:server"
    assert client.room_put_state.await_args.kwargs["content"]["status"] == "completed"


@pytest.mark.asyncio
async def test_creatorless_task_written_by_router_runs(tmp_path: Path) -> None:
    """Legacy task state without a creator still runs when MindRoom wrote it."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    workflow = _workflow(created_by=None)
    client = _room_with_task(_pending_content(workflow), sender=SCHEDULE_WRITER_ID)

    execute = await _run_once(client, workflow, runtime_paths)

    execute.assert_awaited_once()
    assert execute.await_args.args[1].created_by is None


@pytest.mark.asyncio
async def test_creatorless_task_written_by_human_is_ignored_without_cancellation(tmp_path: Path) -> None:
    """Human-written creatorless state is skipped on restore and in a running poll, never canceled."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    workflow = _workflow(created_by=None)
    client = _room_with_task(_pending_content(workflow), sender=HUMAN_ID)

    with capture_logs() as logs:
        restored = await scheduling.restore_scheduled_tasks(
            client,
            ROOM_ID,
            Config(),
            runtime_paths,
            make_conversation_reader_mock(),
        )
    execute = await _run_once(client, workflow, runtime_paths)

    assert restored == 0
    assert not scheduling.has_deferred_overdue_tasks()
    execute.assert_not_awaited()
    client.room_put_state.assert_not_awaited()
    assert [entry for entry in logs if entry["event"] == "scheduled_task_state_ignored_unmanaged_author"] == [
        {
            "event": "scheduled_task_state_ignored_unmanaged_author",
            "log_level": "warning",
            "room_id": ROOM_ID,
            "task_id": TASK_ID,
            "event_id": f"$state_{TASK_ID}",
            "sender": HUMAN_ID,
        },
    ]


@pytest.mark.asyncio
async def test_human_written_task_is_hidden_from_list_and_cancel(tmp_path: Path) -> None:
    """MindRoom does not present or rewrite task state that its own accounts did not write."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    client = _room_with_task(_pending_content(_workflow(created_by="@victim:server")), sender=HUMAN_ID)

    listed = await scheduling.list_scheduled_tasks(client, ROOM_ID, runtime_paths)
    cancelled = await scheduling.cancel_scheduled_task(client, ROOM_ID, TASK_ID, runtime_paths)
    cancelled_all = await scheduling.cancel_all_scheduled_tasks(client, ROOM_ID, runtime_paths)

    assert listed == "No scheduled tasks found."
    assert cancelled == f"❌ Task `{TASK_ID}` not found."
    assert cancelled_all == "No scheduled tasks to cancel."
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_task_written_by_removed_agent_stays_listable_and_cancellable(tmp_path: Path) -> None:
    """An agent removed from the configuration keeps its persisted account, so its tasks stay manageable."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    persist_schedule_writer(runtime_paths, RETIRED_AGENT_ID, entity_name="retired")
    client = _room_with_task(_pending_content(_workflow(created_by="@alice:server")), sender=RETIRED_AGENT_ID)

    listed = await scheduling.list_scheduled_tasks(client, ROOM_ID, runtime_paths, config=Config())
    cancelled = await scheduling.cancel_scheduled_task(client, ROOM_ID, TASK_ID, runtime_paths)

    assert f"`{TASK_ID}`" in listed
    assert cancelled == f"✅ Cancelled task `{TASK_ID}`"
    client.room_put_state.assert_awaited_once()
    assert client.room_put_state.await_args.kwargs["content"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_task_polls_read_one_state_event_and_never_full_room_state(tmp_path: Path) -> None:
    """Each poll costs one full-event task read, as before the author check, not a room-state fetch."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    client = _room_with_task(_pending_content(_workflow(created_by="@alice:server")), sender=SCHEDULE_WRITER_ID)

    for _ in range(3):
        task = await scheduling._reconcile_runnable_task_retrying(
            client,
            ROOM_ID,
            TASK_ID,
            config=Config(),
            runtime_paths=runtime_paths,
        )
        assert task is not None

    client.room_get_state.assert_not_awaited()
    assert [call.args[2] for call in client._send.await_args_list] == [TASK_STATE_PATH] * 3


@pytest.mark.asyncio
async def test_ignored_state_is_logged_once_per_state_event(tmp_path: Path) -> None:
    """Periodic room-state scans must not repeat the warning for the same ignored event."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    content = _pending_content(_workflow(created_by="@victim:server"))
    client = _room_with_task(content, sender=HUMAN_ID)
    rewritten = scheduled_task_state_event(TASK_ID, content, room_id=ROOM_ID, sender=HUMAN_ID)
    rewritten["event_id"] = "$rewritten"

    with capture_logs() as logs:
        for _ in range(3):
            assert await scheduling.get_pending_schedule_thread_ids_for_room(client, ROOM_ID, runtime_paths) == set()
        client.room_get_state.return_value = nio.RoomGetStateResponse.from_dict([rewritten], room_id=ROOM_ID)
        assert await scheduling.get_pending_schedule_thread_ids_for_room(client, ROOM_ID, runtime_paths) == set()

    warned_event_ids = [
        entry["event_id"] for entry in logs if entry["event"] == "scheduled_task_state_ignored_unmanaged_author"
    ]
    assert warned_event_ids == [f"$state_{TASK_ID}", "$rewritten"]


@pytest.mark.asyncio
async def test_content_only_task_state_response_is_a_read_error(tmp_path: Path) -> None:
    """A homeserver that ignores format=event must not have its content treated as a whole event."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    content = _pending_content(_workflow(created_by="@alice:server"))
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    client._send.return_value = nio.RoomGetStateEventResponse(
        content,
        "com.mindroom.scheduled.task",
        TASK_ID,
        ROOM_ID,
    )

    with pytest.raises(RuntimeError, match="was not returned as a full state event"):
        await scheduling.get_scheduled_task(client, ROOM_ID, TASK_ID, runtime_paths)
