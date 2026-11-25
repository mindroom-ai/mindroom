"""Pin the exact reply each failed schedule command posts, through the command handler and the real operations."""

from __future__ import annotations

from contextlib import nullcontext
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import aiohttp
import nio
import pytest

from mindroom.commands.handler import handle_command
from mindroom.commands.parsing import command_parser
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.message_target import MessageTarget
from mindroom.scheduling.runtime import ScheduledTaskRuntime
from mindroom.scheduling.tasks import CronSchedule, ScheduledWorkflow
from tests.authorization_helpers import make_test_command_handler_context
from tests.conftest import bind_runtime_paths, make_matrix_client_mock
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
_BROKEN_RESPONSE = aiohttp.ClientPayloadError("Response payload is not completed")


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


async def _reply(
    tmp_path: Path,
    body: str,
    *,
    client: AsyncMock,
    responders: list[MatrixID] | None = None,
    acts_for_requester: bool = False,
    steward: AsyncMock | None = None,
) -> str:
    """Run one chat command through the command handler and return the reply it posts."""
    runtime_paths = schedule_runtime_paths(tmp_path)
    config = _config(runtime_paths)
    ids = entity_ids(config, runtime_paths)
    scheduled_tasks = MagicMock(spec=ScheduledTaskRuntime)
    # The answering agent is the only bot of its room, and so its steward, unless a test names another.
    scheduled_tasks.lend_client.side_effect = lambda _room_id, _own: nullcontext(steward or client)
    context = make_test_command_handler_context(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        logger=MagicMock(),
        stable_target=MessageTarget.resolve(ROOM_ID, THREAD_ID, "$event"),
        record_handled_turn=AsyncMock(),
        record_command_result=AsyncMock(),
        send_response=AsyncMock(return_value="$reply"),
        responder_candidates_for_room=AsyncMock(return_value=[ids["helper"]] if responders is None else responders),
        scheduled_tasks=scheduled_tasks,
    )
    command = command_parser.parse(body)
    assert command is not None
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
@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (_TASK_READ_LIMITED, str(_TASK_READ_LIMITED)),
        (_BROKEN_RESPONSE, "ClientPayloadError: Response payload is not completed"),
    ],
    ids=["error response", "broken-off response"],
)
async def test_cancel_whose_task_cannot_be_read(
    tmp_path: Path,
    failure: object,
    reason: str,
) -> None:
    """A cancellation whose task read fails or raises says why, and changes nothing."""
    client = make_matrix_client_mock()
    client.room_get_state_event = AsyncMock(side_effect=[failure])
    serve_task_state_events(client)

    reply = await _reply(tmp_path, f"!cancel_schedule {TASK_ID}", client=client)

    assert reply == f"❌ Failed to get scheduled task `{TASK_ID}` from room `{ROOM_ID}`: {reason}"
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["!list_schedules", "!cancel_schedule all"])
async def test_list_or_cancel_all_whose_room_state_cannot_be_read(tmp_path: Path, body: str) -> None:
    """Listing or cancelling every schedule in a room whose state cannot be read says why."""
    client = make_matrix_client_mock()
    client.room_get_state = AsyncMock(return_value=_STATE_ERROR)

    reply = await _reply(tmp_path, body, client=client)

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
async def test_state_write_timeout_gives_no_moderator_advice(tmp_path: Path) -> None:
    """A cancellation that fails for another reason than power reports the failure, without moderator advice."""
    client = make_matrix_client_mock()
    _serve_task(client, _workflow())
    client.room_put_state = AsyncMock(side_effect=TimeoutError("slow"))

    reply = await _reply(tmp_path, f"!cancel_schedule {TASK_ID}", client=client)

    assert reply == (
        f"❌ Failed to cancel task `{TASK_ID}`: Failed to persist scheduled task state for task `{TASK_ID}`: "
        "TimeoutError: slow."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("write", "expected"),
    [
        (nio.RoomPutStateResponse(event_id="$cancelled", room_id=ROOM_ID), f"✅ Cancelled task `{TASK_ID}`"),
        (_STATE_WRITE_REFUSED, "I need moderator rights in this room to save schedules."),
    ],
    ids=["written", "refused"],
)
async def test_schedule_state_is_written_by_the_room_steward(tmp_path: Path, write: object, expected: str) -> None:
    """The room's steward writes a command's schedule state, and its refusal alone is the moderator-rights answer.

    The answering agent never tries the write itself, whatever power it has.
    """
    client = make_matrix_client_mock()
    _serve_task(client, _workflow())
    steward = make_matrix_client_mock(user_id="@steward:server")
    steward.room_put_state = AsyncMock(return_value=write)

    reply = await _reply(tmp_path, f"!cancel_schedule {TASK_ID}", client=client, steward=steward)

    assert reply == expected
    steward.room_put_state.assert_awaited_once()
    assert steward.room_put_state.await_args.kwargs["content"]["status"] == "cancelled"
    client.room_put_state.assert_not_awaited()


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
@pytest.mark.parametrize(
    ("write", "expected"),
    [
        (AsyncMock(return_value=_STATE_WRITE_REFUSED), "I need moderator rights in this room to save schedules."),
        (AsyncMock(side_effect=TimeoutError("slow")), "❌ Failed to cancel 2 scheduled task(s)"),
        (
            AsyncMock(side_effect=[_STATE_WRITE_REFUSED, TimeoutError("slow")]),
            "❌ Failed to cancel 2 scheduled task(s)",
        ),
        (
            AsyncMock(
                side_effect=[nio.RoomPutStateResponse(event_id="$cancelled", room_id=ROOM_ID), _STATE_WRITE_REFUSED],
            ),
            "✅ Cancelled 1 scheduled task(s)\n⚠️ Failed to cancel 1 task(s)",
        ),
    ],
    ids=["all-refused-for-power", "all-timed-out", "mixed", "one-cancelled-one-refused"],
)
async def test_cancel_all_reports_what_failed(tmp_path: Path, write: AsyncMock, expected: str) -> None:
    """Cancelling every schedule counts the failures, unless none was cancelled and power is all that is missing."""
    client = make_matrix_client_mock()
    _serve_pending_tasks(client, "first", "second")
    client.room_put_state = write

    reply = await _reply(tmp_path, "!cancel_schedule all", client=client)

    assert reply == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("!list_schedules", "No scheduled tasks found."),
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
