"""Pin the exact reply each failed schedule command posts, through the command handler and the real operations."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.commands.handler import handle_command
from mindroom.commands.parsing import command_parser
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.event_journal import ConversationPage, RefreshRequest
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.message_target import MessageTarget
from mindroom.scheduling.runtime import ScheduledTaskRuntime
from mindroom.scheduling.tasks import CronSchedule, ScheduledWorkflow
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import bind_runtime_paths, make_conversation_reader_mock, make_matrix_client_mock
from tests.identity_helpers import entity_ids
from tests.scheduling_helpers import (
    SCHEDULE_WRITER_ID,
    schedule_runtime_paths,
    scheduled_task_state_event,
    serve_full_room_state,
    serve_room_schedule_state,
    serve_stored_task,
    serve_task_state_events,
)

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.constants import RuntimePaths
    from mindroom.matrix.identity import MatrixID

ROOM_ID = "!test:server"
THREAD_ID = "$thread"
USER_ID = "@user:server"
TASK_ID = "task123"
_STATE_ERROR = nio.RoomGetStateError.from_dict({"errcode": "M_FORBIDDEN", "error": "Not authorized"}, room_id=ROOM_ID)
_STATE_WRITE_REFUSED = nio.RoomPutStateError(message="denied", status_code="M_FORBIDDEN")
_TASK_READ_LIMITED = nio.RoomGetStateEventError(message="rate limited", status_code="M_LIMIT_EXCEEDED")
_PARSE_FAILURE = (
    "Could not determine when to run the one-time task.\n\n"
    '💡 Include an explicit time like "today at 11:45 PM" or "in 10 minutes".'
)


def _config(runtime_paths: RuntimePaths) -> Config:
    return bind_runtime_paths(
        Config(
            agents={
                "helper": AgentConfig(display_name="Helper", role="Helps"),
                "writer": AgentConfig(display_name="Writer", role="Writes"),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )


def _workflow(**overrides: Any) -> ScheduledWorkflow:  # noqa: ANN401
    fields: dict[str, Any] = {
        "schedule_type": "once",
        "execute_at": datetime.now(UTC) + timedelta(minutes=5),
        "message": "check logs",
        "description": "check logs",
        "created_by": USER_ID,
        "thread_id": THREAD_ID,
        "room_id": ROOM_ID,
    }
    return ScheduledWorkflow(**(fields | overrides))


def _serve_task(client: AsyncMock, workflow: ScheduledWorkflow | None, *, status: str = "pending") -> None:
    """Serve one stored task to the scheduler's single-task reads, or report it missing."""
    content = (
        None if workflow is None else {"task_id": TASK_ID, "status": status, "workflow": workflow.model_dump_json()}
    )
    serve_stored_task(client, ROOM_ID, TASK_ID, content)


def _parse_returning(workflow: ScheduledWorkflow) -> AsyncMock:
    """Answer the schedule parser's model call with ``workflow``."""
    return AsyncMock(return_value=SimpleNamespace(content=workflow, usage=None))


async def _reply(
    tmp_path: Path,
    body: str,
    *,
    client: AsyncMock,
    responders: list[MatrixID] | None = None,
    acts_for_requester: bool = False,
    conversation_reader: ConversationReader | None = None,
) -> str:
    """Run one chat command through the command handler and return the reply it posts."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    config = _config(runtime_paths)
    ids = entity_ids(config, runtime_paths)
    scheduled_tasks = MagicMock(spec=ScheduledTaskRuntime)
    scheduled_tasks.other_state_writer.return_value = nullcontext()
    scheduled_tasks.router_client.return_value = None
    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        logger=MagicMock(),
        conversation_reader=conversation_reader or make_conversation_reader_mock(),
        stable_target=MessageTarget.resolve(ROOM_ID, THREAD_ID, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
        responder_candidates_for_room=AsyncMock(return_value=[ids["helper"]] if responders is None else responders),
        scheduled_tasks=scheduled_tasks,
    )
    command = command_parser.parse(body)
    assert command is not None
    with (
        patch("mindroom.scheduling.tasks.provider_registry.build_model_provider", return_value=MagicMock()),
        patch("mindroom.scheduling.tasks.record_system_usage", new=AsyncMock()),
    ):
        await handle_command(
            context=context,
            room=nio.MatrixRoom(ROOM_ID, SCHEDULE_WRITER_ID),
            event=SimpleNamespace(sender=USER_ID, event_id="$event", body=body, source={"content": {"body": body}}),
            command=command,
            requester_user_id=USER_ID,
            acts_for_requester=acts_for_requester,
        )
    return context.send_response.await_args.args[0]


@pytest.mark.asyncio
async def test_schedule_without_any_responder(tmp_path: Path) -> None:
    """A requester no agent may answer is told so."""
    reply = await _reply(tmp_path, "!schedule in 5 minutes check logs", client=make_matrix_client_mock(), responders=[])

    assert reply == "❌ No agents or teams in this room are allowed to reply to you."


@pytest.mark.asyncio
async def test_schedule_the_parser_cannot_time(tmp_path: Path) -> None:
    """A request the parser cannot place in time shows the parser's reason and suggestion."""
    with patch("mindroom.scheduling.tasks.complete_prompt", new=_parse_returning(_workflow(execute_at=None))):
        reply = await _reply(tmp_path, "!schedule sometime check logs", client=make_matrix_client_mock())

    assert reply == f"❌ {_PARSE_FAILURE}"


@pytest.mark.asyncio
async def test_schedule_mentioning_an_unavailable_agent(tmp_path: Path) -> None:
    """A schedule for an agent outside the thread's responders names it and suggests why."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    writer_id = entity_ids(_config(runtime_paths), runtime_paths)["writer"].full_id
    with patch(
        "mindroom.scheduling.tasks.complete_prompt",
        new=_parse_returning(_workflow(message="@writer check logs")),
    ):
        reply = await _reply(
            tmp_path,
            "!schedule in 5 minutes ask writer to check logs",
            client=make_matrix_client_mock(),
        )

    assert reply == (
        "❌ Failed to schedule: The following agents or teams are not available in this thread: "
        f"{writer_id}\n\n💡 @writer is not available in this thread"
    )


@pytest.mark.asyncio
async def test_schedule_whose_room_state_cannot_be_read(tmp_path: Path) -> None:
    """A schedule that cannot choose its runner reports the failed room read."""
    client = make_matrix_client_mock()
    client.room_get_state = AsyncMock(return_value=_STATE_ERROR)
    with patch("mindroom.scheduling.tasks.complete_prompt", new=_parse_returning(_workflow())):
        reply = await _reply(tmp_path, "!schedule in 5 minutes check logs", client=client)

    assert reply == (
        f"❌ Failed to schedule: Could not read the state of room {ROOM_ID} to choose which bot runs the task: "
        f"{_STATE_ERROR}"
    )


def _stale_thread_reader() -> ConversationReader:
    """Return a real reader whose strict read of the thread still has a message awaiting its refetch."""
    pending = RefreshRequest(
        room_id=ROOM_ID,
        thread_id=THREAD_ID,
        logical_event_id="$redacted",
        revision_event_id="$redacted",
        refresh_token=1,
        membership_epoch=0,
    )
    page = ConversationPage(messages=(), refresh_pending=(pending,), next_cursor=None)
    return ConversationReader(
        store=cast("Any", SimpleNamespace(read_conversation=AsyncMock(return_value=page))),
        hydrator=cast("Any", SimpleNamespace(ensure_hydrated=AsyncMock(), resolve_refreshes=AsyncMock())),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "prefix"),
    [
        ("!schedule in 5 minutes check logs", ""),
        (f"!edit_schedule {TASK_ID} in 10 minutes check logs", f"Failed to edit task `{TASK_ID}`.\n\n"),
    ],
)
async def test_schedule_or_edit_whose_thread_cannot_be_read(tmp_path: Path, body: str, prefix: str) -> None:
    """A thread-scoped schedule or edit whose thread read fails says why, and saves nothing."""
    client = make_matrix_client_mock()
    _serve_task(client, _workflow())
    with patch("mindroom.scheduling.tasks.complete_prompt", new=AsyncMock()) as parse:
        reply = await _reply(tmp_path, body, client=client, conversation_reader=_stale_thread_reader())

    assert reply == (
        f"❌ {prefix}Could not read this thread's history: "
        f"Conversation {ROOM_ID}/{THREAD_ID} has 1 message(s) awaiting a server refetch"
    )
    parse.assert_not_awaited()
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_of_a_missing_task(tmp_path: Path) -> None:
    """Editing an unknown task says it was not found."""
    client = make_matrix_client_mock()
    _serve_task(client, None)

    reply = await _reply(tmp_path, f"!edit_schedule {TASK_ID} in 10 minutes check logs", client=client)

    assert reply == f"❌ Task `{TASK_ID}` not found."


@pytest.mark.asyncio
async def test_edit_of_a_cancelled_task(tmp_path: Path) -> None:
    """Editing a task that no longer runs names its status."""
    client = make_matrix_client_mock()
    _serve_task(client, _workflow(), status="cancelled")

    reply = await _reply(tmp_path, f"!edit_schedule {TASK_ID} in 10 minutes check logs", client=client)

    assert reply == f"❌ Task `{TASK_ID}` cannot be edited because it is `cancelled`."


@pytest.mark.asyncio
async def test_edit_of_a_pre_approved_call(tmp_path: Path) -> None:
    """Editing a pre-approved scheduled call is refused with how to change it instead."""
    client = make_matrix_client_mock()
    _serve_task(client, _workflow(pre_approved_call=True))

    reply = await _reply(tmp_path, f"!edit_schedule {TASK_ID} in 10 minutes check logs", client=client)

    assert reply == (
        f"❌ Task `{TASK_ID}` runs a pre-approved tool call, which cannot be edited; "
        "cancel it and schedule the call again."
    )


@pytest.mark.asyncio
async def test_edit_the_parser_cannot_time(tmp_path: Path) -> None:
    """An edit whose new request fails names the task, then the reason."""
    client = make_matrix_client_mock()
    _serve_task(client, _workflow())
    with patch("mindroom.scheduling.tasks.complete_prompt", new=_parse_returning(_workflow(execute_at=None))):
        reply = await _reply(tmp_path, f"!edit_schedule {TASK_ID} sometime check logs", client=client)

    assert reply == f"❌ Failed to edit task `{TASK_ID}`.\n\n{_PARSE_FAILURE}"


@pytest.mark.asyncio
async def test_edit_the_parser_carries_over_unchanged(tmp_path: Path) -> None:
    """An edit whose parse changes nothing but who sent it is refused, and the task stays as stored."""
    client = make_matrix_client_mock()
    stored = _workflow(created_by="@creator:server")
    _serve_task(client, stored)
    # The edit prompt shows the parser only these fields, which it carries over.
    carried_over = ScheduledWorkflow(
        schedule_type="once",
        execute_at=stored.execute_at,
        message=stored.message,
        description=stored.description,
    )
    with patch("mindroom.scheduling.tasks.complete_prompt", new=_parse_returning(carried_over)):
        reply = await _reply(tmp_path, f"!edit_schedule {TASK_ID} whenever you feel like it", client=client)

    assert reply == (
        f"❌ Failed to edit task `{TASK_ID}`.\n\nThe request does not change the task.\n\n"
        "💡 Say what to change, like its time or its message."
    )
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_of_a_task_whose_state_no_longer_parses(tmp_path: Path) -> None:
    """Editing a task whose stored state cannot be understood says so and how to replace it."""
    client = make_matrix_client_mock()
    serve_stored_task(client, ROOM_ID, TASK_ID, {"task_id": TASK_ID, "status": "pending", "workflow": "{not json"})

    reply = await _reply(tmp_path, f"!edit_schedule {TASK_ID} in 10 minutes check logs", client=client)

    assert reply == f"❌ Task `{TASK_ID}` has invalid stored state; cancel it and schedule it again."
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [f"!edit_schedule {TASK_ID} in 10 minutes check logs", f"!cancel_schedule {TASK_ID}"])
async def test_edit_or_cancel_whose_task_cannot_be_read(tmp_path: Path, body: str) -> None:
    """An edit or cancellation whose task read fails says so, and changes nothing."""
    client = make_matrix_client_mock()
    client.room_get_state_event = AsyncMock(return_value=_TASK_READ_LIMITED)
    serve_task_state_events(client)

    reply = await _reply(tmp_path, body, client=client)

    assert reply == f"❌ Failed to get scheduled task '{TASK_ID}' from room '{ROOM_ID}': {_TASK_READ_LIMITED}"
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_whose_room_state_cannot_be_read(tmp_path: Path) -> None:
    """Listing schedules in a room whose state cannot be read fails like every other schedule command."""
    client = make_matrix_client_mock()
    client.room_get_state = AsyncMock(return_value=_STATE_ERROR)

    reply = await _reply(tmp_path, "!list_schedules", client=client)

    assert reply == f"❌ Unable to retrieve scheduled tasks: {_STATE_ERROR}"


@pytest.mark.asyncio
async def test_cancel_of_a_missing_task(tmp_path: Path) -> None:
    """Cancelling an unknown task says it was not found."""
    client = make_matrix_client_mock()
    _serve_task(client, None)

    reply = await _reply(tmp_path, f"!cancel_schedule {TASK_ID}", client=client)

    assert reply == f"❌ Task `{TASK_ID}` not found."


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
async def test_cancel_of_a_task_that_already_ended(tmp_path: Path, status: str) -> None:
    """Cancelling a task that no longer runs names its status and leaves it as stored."""
    client = make_matrix_client_mock()
    _serve_task(client, _workflow(), status=status)

    reply = await _reply(tmp_path, f"!cancel_schedule {TASK_ID}", client=client)

    assert reply == f"❌ Task `{TASK_ID}` cannot be cancelled because it is `{status}`."
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_whose_state_write_is_refused(tmp_path: Path) -> None:
    """A cancellation Matrix refuses to store reports the refusal and how to fix it."""
    client = make_matrix_client_mock()
    _serve_task(client, _workflow())
    client.room_put_state = AsyncMock(return_value=_STATE_WRITE_REFUSED)

    reply = await _reply(tmp_path, f"!cancel_schedule {TASK_ID}", client=client)

    assert reply == (
        f"❌ Failed to cancel task `{TASK_ID}`: Failed to persist scheduled task state for task `{TASK_ID}`: "
        f"RoomPutStateError: M_FORBIDDEN denied. Ensure {client.user_id} is joined with permission to write room "
        "state, then retry."
    )


@pytest.mark.asyncio
async def test_cancel_all_whose_room_state_cannot_be_read(tmp_path: Path) -> None:
    """Cancelling every schedule in a room whose state cannot be read says so."""
    client = make_matrix_client_mock()
    client.room_get_state = AsyncMock(return_value=_STATE_ERROR)

    reply = await _reply(tmp_path, "!cancel_schedule all", client=client)

    assert reply == f"❌ Unable to retrieve scheduled tasks: {_STATE_ERROR}"


def _serve_pending_tasks(client: AsyncMock, *task_ids: str) -> None:
    workflow = _workflow(schedule_type="cron", execute_at=None, cron_schedule=CronSchedule(minute="0", hour="9"))
    events = [
        scheduled_task_state_event(
            task_id,
            {"task_id": task_id, "status": "pending", "workflow": workflow.model_dump_json()},
            room_id=ROOM_ID,
        )
        for task_id in task_ids
    ]
    serve_room_schedule_state(client, ROOM_ID, events)


@pytest.mark.asyncio
async def test_cancel_all_when_every_cancellation_fails(tmp_path: Path) -> None:
    """Cancelling every schedule when none can be stored counts the failures."""
    client = make_matrix_client_mock()
    _serve_pending_tasks(client, "first", "second")
    client.room_put_state = AsyncMock(return_value=_STATE_WRITE_REFUSED)

    reply = await _reply(tmp_path, "!cancel_schedule all", client=client)

    assert reply == "❌ Failed to cancel 2 scheduled task(s)"


@pytest.mark.asyncio
async def test_cancel_all_when_some_cancellations_fail(tmp_path: Path) -> None:
    """Cancelling every schedule when only some can be stored reports both counts."""
    client = make_matrix_client_mock()
    _serve_pending_tasks(client, "first", "second")
    client.room_put_state = AsyncMock(
        side_effect=[
            nio.RoomPutStateResponse(event_id="$cancelled", room_id=ROOM_ID),
            _STATE_WRITE_REFUSED,
        ],
    )

    reply = await _reply(tmp_path, "!cancel_schedule all", client=client)

    assert reply == "✅ Cancelled 1 scheduled task(s)\n⚠️ Failed to cancel 1 task(s)"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("!schedule in 5 minutes check logs", "❌ No agents or teams in this room are allowed to reply to you."),
        ("!list_schedules", "No scheduled tasks found."),
        (f"!edit_schedule {TASK_ID} in 10 minutes check logs", f"❌ Task `{TASK_ID}` not found."),
        (f"!cancel_schedule {TASK_ID}", f"❌ Task `{TASK_ID}` not found."),
        ("!cancel_schedule all", "No scheduled tasks to cancel."),
    ],
)
async def test_an_agent_may_run_every_schedule_command_for_a_human(tmp_path: Path, body: str, expected: str) -> None:
    """Schedule commands an agent posts for a human run as that human's own, unlike admin and pairing commands."""
    client = make_matrix_client_mock()
    _serve_task(client, None)
    serve_full_room_state(client, ROOM_ID)

    reply = await _reply(tmp_path, body, client=client, responders=[], acts_for_requester=True)

    assert reply == expected
