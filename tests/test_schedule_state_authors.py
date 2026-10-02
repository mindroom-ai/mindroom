"""Scheduled tasks only run from room state written by MindRoom's own bot accounts."""

from __future__ import annotations

import asyncio
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
    room_create_state_response,
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
CREATE_STATE_PATH = "/_matrix/client/v3/rooms/%21test%3Aserver/state/m.room.create"


@pytest.fixture(autouse=True)
def _reset_scheduler_state() -> Generator[None, None, None]:
    scheduling.clear_deferred_overdue_tasks()
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
    """Each poll reads one task state event, checks the homeserver on the create event, and fetches the task event by ID.

    No poll reads full room state.
    """
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
    assert [call.args[2] for call in client._send.await_args_list] == [
        TASK_STATE_PATH,
        f"{CREATE_STATE_PATH}?format=event",
        CREATE_STATE_PATH,
    ] * 3
    assert [call.args for call in client.room_get_event.await_args_list] == [(ROOM_ID, f"$state_{TASK_ID}")] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "detail"),
    [
        (_pending_content(_workflow(created_by="@alice:server")), "no HTTP status"),
        ({"errcode": "M_FORBIDDEN", "error": "You are not in this room"}, "M_FORBIDDEN"),
    ],
)
async def test_non_event_task_state_response_is_a_read_error(
    tmp_path: Path,
    body: dict[str, Any],
    detail: str,
) -> None:
    """Content from a server that ignores format=event, or an error body, is never treated as a whole event."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    client._send.return_value = nio.RoomGetStateEventResponse(body, "com.mindroom.scheduled.task", TASK_ID, ROOM_ID)

    with pytest.raises(RuntimeError, match=rf"was not returned as a full state event \({detail}\)"):
        await scheduling.get_scheduled_task(client, ROOM_ID, TASK_ID, runtime_paths)


def _forged_envelope_client(
    content: dict[str, Any],
    fetched: nio.RoomGetEventResponse | nio.RoomGetEventError,
    room_state: list[dict[str, Any]] | None = None,
) -> AsyncMock:
    """Return a client whose task state read returned content shaped like a router event.

    The homeserver honours format=event for the room's create event, so only the fetched event can refute the shape.
    The bare state read returns that envelope's inner content, as after the writer rewrites the state between reads.
    Full room-state reads return ``room_state``.
    """
    forged = scheduled_task_state_event(TASK_ID, content, room_id=ROOM_ID, sender=SCHEDULE_WRITER_ID)
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    client.room_get_state.return_value = nio.RoomGetStateResponse.from_dict(room_state or [], room_id=ROOM_ID)

    async def send(_response_class: type, _method: str, path: str, **_kwargs: object) -> nio.RoomGetStateEventResponse:
        if path.startswith(CREATE_STATE_PATH):
            return room_create_state_response(ROOM_ID, full_event=path.endswith("?format=event"))
        return nio.RoomGetStateEventResponse(forged, "com.mindroom.scheduled.task", TASK_ID, ROOM_ID)

    client._send.side_effect = send
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content,
        "com.mindroom.scheduled.task",
        TASK_ID,
        ROOM_ID,
    )
    client.room_get_event.side_effect = None
    client.room_get_event.return_value = fetched
    return client


@pytest.mark.asyncio
async def test_task_content_shaped_like_a_bot_event_takes_its_sender_from_the_fetched_event(tmp_path: Path) -> None:
    """A sender written inside state content is never trusted, even when that content matches the bare state."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    content = _pending_content(_workflow(created_by="@victim:server"))
    real_event = scheduled_task_state_event(TASK_ID, content, room_id=ROOM_ID, sender=HUMAN_ID)
    client = _forged_envelope_client(content, nio.RoomGetEventResponse.from_dict(real_event))

    assert await scheduling.get_scheduled_task(client, ROOM_ID, TASK_ID, runtime_paths) is None
    client.room_get_event.assert_awaited_once_with(ROOM_ID, f"$state_{TASK_ID}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fetched_fields", "room_state_fields"),
    [
        pytest.param({"content": {"status": "cancelled"}}, None, id="other-content"),
        pytest.param({"state_key": "other_task"}, None, id="other-task"),
        pytest.param({"type": "m.room.message"}, None, id="other-type"),
        pytest.param(None, None, id="refused-and-absent-from-room-state"),
        pytest.param(None, {"content": {"status": "cancelled"}}, id="refused-and-other-room-state-content"),
    ],
)
async def test_task_whose_event_fetched_by_id_does_not_match_is_a_read_error(
    tmp_path: Path,
    fetched_fields: dict[str, Any] | None,
    room_state_fields: dict[str, Any] | None,
) -> None:
    """The scheduler fails closed when the event named by the state read is missing or is not that task state.

    A refused event read falls back to current room state, which must hold this task with the same content.
    """
    runtime_paths = schedule_runtime_paths(tmp_path)
    content = _pending_content(_workflow(created_by="@victim:server"))
    event = scheduled_task_state_event(TASK_ID, content, room_id=ROOM_ID, sender=SCHEDULE_WRITER_ID)
    fetched = (
        nio.RoomGetEventError("not found", "M_NOT_FOUND")
        if fetched_fields is None
        else nio.RoomGetEventResponse.from_dict({**event, **fetched_fields})
    )
    room_state = [] if room_state_fields is None else [{**event, **room_state_fields}]
    client = _forged_envelope_client(content, fetched, room_state)

    with pytest.raises(RuntimeError, match=rf"did not match its event '\$state_{TASK_ID}'"):
        await scheduling.get_scheduled_task(client, ROOM_ID, TASK_ID, runtime_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("room_state_sender", "expected_creator"),
    [
        pytest.param(SCHEDULE_WRITER_ID, "@alice:server", id="bot-written"),
        pytest.param(HUMAN_ID, None, id="human-written"),
    ],
)
async def test_task_event_refused_by_id_takes_its_sender_from_current_room_state(
    tmp_path: Path,
    room_state_sender: str,
    expected_creator: str | None,
) -> None:
    """When history visibility hides the named event, the sender comes from the task in current room state."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    content = _pending_content(_workflow(created_by="@alice:server"))
    current = scheduled_task_state_event(TASK_ID, content, room_id=ROOM_ID, sender=room_state_sender)
    client = _forged_envelope_client(content, nio.RoomGetEventError("not found", "M_NOT_FOUND"), [current])

    task = await scheduling.get_scheduled_task(client, ROOM_ID, TASK_ID, runtime_paths)

    assert (None if task is None else task.workflow.created_by) == expected_creator
    client.room_get_state.assert_awaited_once_with(ROOM_ID)


@pytest.mark.asyncio
async def test_superseded_bot_task_named_by_state_content_never_fires(tmp_path: Path) -> None:
    """On a homeserver that ignores format=event, state content naming an older bot-written version is refused.

    The writer copies a superseded, genuinely bot-authored version's event ID and content into current state.
    """
    runtime_paths = schedule_runtime_paths(tmp_path)
    workflow = _workflow(created_by="@victim:server")
    superseded = scheduled_task_state_event(TASK_ID, _pending_content(workflow), room_id=ROOM_ID)
    superseded["event_id"] = "$superseded"
    current_content = {"event_id": "$superseded", "content": superseded["content"]}
    create_content = {"room_version": "11"}
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    client.homeserver = "https://matrix.example"

    async def ignore_format_event(_response_class: type, _method: str, path: str, **_kwargs: object) -> object:
        if path.startswith(CREATE_STATE_PATH):
            return nio.RoomGetStateEventResponse(dict(create_content), "m.room.create", "", ROOM_ID)
        return nio.RoomGetStateEventResponse(dict(current_content), "com.mindroom.scheduled.task", TASK_ID, ROOM_ID)

    client._send.side_effect = ignore_format_event
    client.room_get_event.side_effect = None
    client.room_get_event.return_value = nio.RoomGetEventResponse.from_dict(superseded)
    client.room_get_state_event.side_effect = None
    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        {"membership": "join"},
        "m.room.member",
        "@victim:server",
        ROOM_ID,
    )

    with pytest.raises(RuntimeError, match="ignores format=event"):
        await scheduling.get_scheduled_task(client, ROOM_ID, TASK_ID, runtime_paths)
    # The runner keeps retrying the refused read; stop it at its first retry wait.
    with (
        patch(
            "mindroom.scheduling_executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute,
        patch("mindroom.scheduling.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)),
        pytest.raises(asyncio.CancelledError),
    ):
        await scheduling._run_once_task(
            client,
            TASK_ID,
            workflow,
            Config(),
            runtime_paths,
            make_conversation_reader_mock(),
        )

    execute.assert_not_awaited()
    client.room_put_state.assert_not_awaited()
