"""Tests for scheduling functionality that actually exercise the real code."""

from __future__ import annotations

import contextvars
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import SCHEDULED_TASK_EVENT_TYPE, resolve_runtime_paths
from mindroom.scheduling import runtime as scheduling_runtime
from mindroom.scheduling import tasks as scheduling
from mindroom.scheduling.errors import SchedulingError
from mindroom.scheduling.executor import ScheduledWorkflowOutcome
from mindroom.scheduling.ownership import room_from_state, task_owner
from mindroom.scheduling.recurring_schedule import RecurringOccurrence, _RecurringCheckpoint
from mindroom.scheduling.runtime import ScheduledTaskRuntime, _TaskRunner
from mindroom.scheduling.tasks import (
    CronSchedule,
    ScheduledTaskRecord,
    ScheduledWorkflow,
    SchedulingRuntime,
    _validate_parsed_workflow,
    build_edited_scheduled_workflow,
    build_scheduled_task_read_model,
    cancel_all_scheduled_tasks,
    cancel_scheduled_task,
    edit_scheduled_task,
    get_pending_schedule_thread_ids_for_room,
    get_scheduled_task,
    get_scheduled_tasks_for_room,
    list_scheduled_tasks,
    persist_scheduled_task_state,
    save_edited_scheduled_task,
    schedule_task,
    scheduled_task_read_sort_key,
    scheduled_task_timezone,
)
from tests.authorization_helpers import (
    make_test_scheduling_runtime,
)
from tests.bot_helpers import _visible_message
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    serve_conversation_reader,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts
from tests.scheduling_helpers import (
    SCHEDULE_WRITER_ID,
    joined_member_state,
    run_cron_task,
    run_once_task,
    schedule_runtime_paths,
    scheduled_task_runtime,
    scheduling_bot,
    serve_full_room_state,
    serve_task_state_events,
)
from tests.tool_approval_helpers import scheduled_call_approvals

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.matrix.conversation_reads import ConversationReader


def _runtime_paths() -> object:
    return resolve_runtime_paths(config_path=Path("config.yaml"), process_env={})


def _test_runtime_paths(tmp_path: Path) -> RuntimePaths:
    return schedule_runtime_paths(tmp_path)


def _conversation_reader(*, latest_thread_event_id: str | None = None) -> AsyncMock:
    reader = AsyncMock()
    reader.latest_thread_event_id = AsyncMock(return_value=latest_thread_event_id)
    return reader


def test_silent_new_thread_confirmation_describes_actual_visible_placement() -> None:
    """The confirmation must not promise a thread beneath a hidden trigger."""
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="check the queue",
        description="queue check",
        new_thread=True,
        silent=True,
    )

    response = scheduling._scheduled_task_response_text(
        workflow,
        task_id="task1234",
        new_thread=True,
        config=Config(),
    )

    assert "**Mode:** Silent (hidden trigger; no-report final omitted)" in response
    assert "**Delivery:** Room-level roots for findings/failures" in response
    assert "New thread per fire" not in response


def test_recurring_confirmation_names_the_tasks_timezone() -> None:
    """The confirmation an edit or the scheduler tool posts says which zone its wall time is in."""
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="0"),
        message="post the standup",
        description="Standup",
        timezone="Asia/Tokyo",
    )

    response = scheduling._scheduled_task_response_text(
        workflow,
        task_id="task1234",
        new_thread=False,
        config=Config(timezone="UTC"),
    )

    assert "✅ Scheduled recurring task: **At 09:00 (Asia/Tokyo)**\n" in response


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
    conversation_reader: ConversationReader | None = None,
    task_runtime: ScheduledTaskRuntime | None = None,
    agent_name: str | None = None,
) -> SchedulingRuntime:
    return make_test_scheduling_runtime(
        client=client or AsyncMock(),
        config=config or MagicMock(),
        runtime_paths=runtime_paths or _runtime_paths(),
        room=room or MagicMock(),
        conversation_reader=conversation_reader or make_conversation_reader_mock(),
        task_runtime=task_runtime,
        agent_name=agent_name,
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


def _recording_state_writer(
    room_state: dict[str, dict[str, Any]],
    *,
    should_succeed: bool = True,
) -> tuple[ScheduledTaskRuntime, AsyncMock]:
    """Return a runtime whose ready router may write the room's schedule state, and the router's client.

    The router's writes land in the shared ``room_state`` store, so later reads see them.
    """
    router = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    room = _matrix_room("!test:server")
    room.power_levels.users[SCHEDULE_WRITER_ID] = 100
    router.rooms = {"!test:server": room}

    async def put_room_state(
        *,
        room_id: str,
        event_type: str,
        content: dict[str, Any],
        state_key: str,
    ) -> nio.RoomPutStateResponse | nio.RoomPutStateError:
        if not should_succeed:
            return nio.RoomPutStateError("forbidden", "M_FORBIDDEN")
        room_state[state_key] = {
            "type": event_type,
            "state_key": state_key,
            "content": dict(content),
            "room_id": room_id,
            "event_id": f"$router_{state_key}",
            "sender": SCHEDULE_WRITER_ID,
            "origin_server_ts": 1234567890,
        }
        return nio.RoomPutStateResponse(f"$router_{state_key}", room_id)

    router.room_put_state = AsyncMock(side_effect=put_room_state)
    return scheduled_task_runtime(_runtime_paths(), scheduling_bot(router), config=Config()), router


def _room_state_response(room_id: str, room_state: dict[str, dict[str, Any]]) -> nio.RoomGetStateResponse:
    return nio.RoomGetStateResponse.from_dict(list(room_state.values()), room_id=room_id)


async def _forbidden_state_write(*_args: object, **_kwargs: object) -> nio.RoomPutStateError:
    return nio.RoomPutStateError("forbidden", "M_FORBIDDEN")


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

    once_model = build_scheduled_task_read_model(once_record, timezone="UTC", current_time=current_time)
    cron_model = build_scheduled_task_read_model(cron_record, timezone="UTC", current_time=current_time)

    assert once_model.task_id == "once123"
    assert once_model.status == "cancelled"
    assert once_model.next_run_at == datetime(2026, 1, 2, 9, 30, tzinfo=UTC)
    assert once_model.cron_expression is None
    assert once_model.new_thread is True
    assert once_model.silent is False
    assert cron_model.cron_expression == "0 9 * * *"
    assert cron_model.cron_description == "At 09:00 (UTC)"
    assert cron_model.next_run_at == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    assert cron_model.created_by == "@user:server"
    assert cron_model.thread_id == "$thread1"
    assert cron_model.silent is False
    assert sorted([once_model, cron_model], key=scheduled_task_read_sort_key) == [cron_model, once_model]


def test_build_edited_scheduled_workflow_preserves_metadata_and_strips_text() -> None:
    """Patch-style edits should preserve ownership/thread metadata while normalizing text."""
    existing = ScheduledWorkflow(
        schedule_type="cron",
        is_conditional=True,
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="original message",
        description="Original description",
        history_limit=3,
        created_by="@user:server",
        thread_id="$thread1",
        room_id="!old:server",
        new_thread=True,
        timezone="Europe/Amsterdam",
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
        is_conditional=True,
        cron_schedule=CronSchedule(minute="30", hour="8", day="*", month="*", weekday="1-5"),
        execute_at=None,
        message="updated message",
        description="updated message",
        history_limit=3,
        created_by="@user:server",
        thread_id="$thread1",
        room_id="!new:server",
        new_thread=True,
        timezone="Europe/Amsterdam",
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

    with pytest.raises(SchedulingError, match="Changing schedule_type is not supported"):
        build_edited_scheduled_workflow(existing_once, room_id="!room:server", schedule_type="cron")

    with pytest.raises(SchedulingError, match="cron_expression is only valid for cron schedules"):
        build_edited_scheduled_workflow(existing_once, room_id="!room:server", cron_expression="0 9 * * *")

    with pytest.raises(SchedulingError, match="execute_at is only valid for one-time schedules"):
        build_edited_scheduled_workflow(
            existing_cron,
            room_id="!room:server",
            execute_at=datetime(2026, 1, 3, 9, 30, tzinfo=UTC),
        )

    with pytest.raises(SchedulingError, match="message cannot be empty"):
        build_edited_scheduled_workflow(existing_once, room_id="!room:server", message="   ")


def test_impossible_cron_rejected_on_create_and_edit_paths() -> None:
    """Syntactically valid but impossible crons (e.g. Feb 31) must be rejected, not persisted."""
    existing_cron = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="original message",
        description="Original description",
    )

    with pytest.raises(SchedulingError, match="Invalid cron expression"):
        build_edited_scheduled_workflow(existing_cron, room_id="!room:server", cron_expression="0 0 31 2 *")

    impossible_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="0", day="31", month="2", weekday="*"),
        message="check email",
        description="Check email",
    )
    with pytest.raises(SchedulingError, match="Invalid cron expression"):
        _validate_parsed_workflow(impossible_workflow)

    assert _validate_parsed_workflow(existing_cron) is None


def _restoring_router(client: AsyncMock, tmp_path: Path) -> tuple[ScheduledTaskRuntime, object]:
    """Return a runtime whose router restores rooms through ``client``, and the router bot."""
    client.user_id = SCHEDULE_WRITER_ID
    bot = scheduling_bot(client)
    return scheduled_task_runtime(_test_runtime_paths(tmp_path), bot, config=Config()), bot


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
async def test_restore_starts_overdue_and_recurring_runners_without_firing(
    tmp_path: Path,
    schedule_type: str,
) -> None:
    """Restoring an overdue one-time or a recurring task starts its runner; nothing fires during the restore."""
    client = AsyncMock()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type=schedule_type,
        execute_at=datetime.now(UTC) - timedelta(minutes=5) if schedule_type == "once" else None,
        cron_schedule=CronSchedule(minute="0", hour="9") if schedule_type == "cron" else None,
        message="Send the overdue reminder",
        description="Overdue reminder",
        thread_id="$thread123",
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task_overdue",
                    "content": {"workflow": workflow.model_dump_json(), "status": "pending"},
                    "event_id": "$state_task_overdue",
                    "sender": SCHEDULE_WRITER_ID,
                    "origin_server_ts": 1234567890,
                },
            ],
            room_id="!test:server",
        ),
    )
    runtime, bot = _restoring_router(client, tmp_path)

    with (
        patch.object(runtime, "_start_runner", return_value=True) as start,
        patch("mindroom.scheduling.executor.execute_scheduled_workflow") as execute,
    ):
        restored = await runtime.restore_room(bot, "!test:server")

    assert restored == 1
    start.assert_called_once_with("router", "task_overdue", workflow)
    execute.assert_not_called()


_TURN_OWNER: contextvars.ContextVar[object | None] = contextvars.ContextVar("test_schedule_turn_owner", default=None)


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
async def test_task_scheduled_during_a_turn_does_not_inherit_the_turn_context(
    schedule_type: str,
    tmp_path: Path,
) -> None:
    """A schedule outlives the turn that creates it, whose contextvars hold the turn's Agent and tools."""
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type=schedule_type,
        execute_at=datetime.now(UTC) + timedelta(hours=1) if schedule_type == "once" else None,
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*")
        if schedule_type == "cron"
        else None,
        message="reminder",
        description="reminder",
        room_id="!test:server",
    )
    observed: list[object | None] = []

    async def runner(*_args: object, **_kwargs: object) -> None:
        observed.append(_TURN_OWNER.get())

    runtime = scheduled_task_runtime(_test_runtime_paths(tmp_path), config=Config())
    token = _TURN_OWNER.set(object())
    try:
        with patch.object(_TaskRunner, "run_once", runner), patch.object(_TaskRunner, "run_cron", runner):
            assert runtime._start_runner("router", "task_from_turn", workflow)
    finally:
        _TURN_OWNER.reset(token)
    _runner, task = runtime._runners.pop("task_from_turn")
    await task

    assert observed == [None]


def _pending_once_workflow() -> ScheduledWorkflow:
    return ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        message="reminder",
        description="reminder",
        room_id="!test:server",
    )


def _owner_rule_setup(tmp_path: Path) -> tuple[ScheduledTaskRuntime, dict[str, str]]:
    """Return a runtime over a configured router and agent, and their Matrix IDs by entity name."""
    config = Config(agents={"general": AgentConfig(display_name="General")})
    runtime_paths = _test_runtime_paths(tmp_path)
    ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(config, runtime_paths).items()}
    return scheduled_task_runtime(runtime_paths, config=config), ids


def _schedule_room_state(
    ids: dict[str, str],
    *,
    router_level: int | None,
    events_default: int = 0,
    router_joined: bool = True,
) -> list[dict[str, Any]]:
    """Return the task room's full state: the agent joined at 100, the router at a power level, or absent when None."""
    users = {ids["general"]: 100}
    members = [ids["general"]]
    if router_level is not None:
        users[ids["router"]] = router_level
        if router_joined:
            members.append(ids["router"])
    power_levels = {"users": users, "state_default": 50, "events_default": events_default}
    events = [("m.room.power_levels", "", power_levels)]
    events += [("m.room.member", member, {"membership": "join"}) for member in members]
    return [
        {
            "type": event_type,
            "state_key": state_key,
            "content": content,
            "sender": ids["general"],
            "event_id": f"${event_type}:{state_key}",
            "origin_server_ts": 1,
            "room_id": "!test:server",
        }
        for event_type, state_key, content in events
    ]


async def _save_agent_schedule(
    runtime: ScheduledTaskRuntime,
    state: list[dict[str, Any]],
    writer_id: str,
) -> tuple[MagicMock, AsyncMock]:
    """Save one schedule as the agent and return the runtime's runner start and the agent's client.

    The agent's synced view of the room has no members, as a lazy-loading sync leaves it, so only the full state
    can tell where the router is.
    """
    client = make_matrix_client_mock(user_id=writer_id)
    client.rooms = {"!test:server": nio.MatrixRoom("!test:server", writer_id)}
    client.room_get_state.return_value = nio.RoomGetStateResponse.from_dict(state, room_id="!test:server")
    client.room_put_state.return_value = nio.RoomPutStateResponse("$state", "!test:server")
    with patch.object(runtime, "_start_runner", return_value=True) as start:
        await scheduling._save_pending_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="agent_task",
            workflow=_pending_once_workflow(),
            config=Config(),
            runtime_paths=runtime.runtime_paths,
            task_runtime=runtime,
        )
    start.assert_called_once()
    assert start.call_args.args[1] == "agent_task"
    return start, client


@pytest.mark.asyncio
async def test_schedule_created_by_an_agent_is_owned_by_the_router_where_it_can_serve_the_room(tmp_path: Path) -> None:
    """An agent's schedule runs under the router when the room's full state lets the router write and trigger it."""
    runtime, ids = _owner_rule_setup(tmp_path)

    start, client = await _save_agent_schedule(runtime, _schedule_room_state(ids, router_level=100), ids["general"])

    assert start.call_args.args[0] == "router"
    assert client.room_put_state.await_args.kwargs["content"]["owner"] == ids["router"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("router_level", "events_default", "router_joined"),
    [(None, 0, True), (100, 0, False), (0, 0, True), (50, 100, True)],
    ids=["router-never-joined", "router-left-with-power", "router-cannot-write-state", "router-cannot-send-trigger"],
)
async def test_schedule_in_a_room_the_router_cannot_serve_is_owned_by_its_creator(
    tmp_path: Path,
    router_level: int | None,
    events_default: int,
    router_joined: bool,
) -> None:
    """Without a router that can write the room's schedule state and send its trigger, the saving agent owns the task."""
    runtime, ids = _owner_rule_setup(tmp_path)
    state = _schedule_room_state(
        ids,
        router_level=router_level,
        events_default=events_default,
        router_joined=router_joined,
    )

    start, client = await _save_agent_schedule(runtime, state, ids["general"])

    assert start.call_args.args[0] == "general"
    assert client.room_put_state.await_args.kwargs["content"]["owner"] == ids["general"]


@pytest.mark.asyncio
async def test_a_schedule_is_not_saved_when_its_room_state_cannot_be_read(tmp_path: Path) -> None:
    """Without the room's full state there is no owner to store, so nothing is saved."""
    runtime, ids = _owner_rule_setup(tmp_path)
    client = make_matrix_client_mock(user_id=ids["general"])
    client.room_get_state.return_value = nio.RoomGetStateError("forbidden", "M_FORBIDDEN")

    with pytest.raises(SchedulingError, match="to choose which bot runs the task"):
        await scheduling._save_pending_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="agent_task",
            workflow=_pending_once_workflow(),
            config=Config(),
            runtime_paths=runtime.runtime_paths,
            task_runtime=runtime,
        )

    client.room_put_state.assert_not_awaited()


def test_owner_rule_never_hands_a_task_to_a_bot_other_than_the_router_or_its_creator(tmp_path: Path) -> None:
    """A task whose creator may not send its trigger stays with the creator instead of moving to another joined bot."""
    _runtime, ids = _owner_rule_setup(tmp_path)
    other = "@mindroom_other:server"
    state = _schedule_room_state(ids, router_level=None, events_default=100)
    state[0]["content"]["users"] = {other: 100, ids["general"]: 0}
    state.append({**state[1], "state_key": other, "event_id": "$other"})
    room = room_from_state("!test:server", ids["general"], state)

    owner = task_owner(room, _pending_once_workflow(), creator_id=ids["general"], router_id=ids["router"])

    assert other in room.users
    assert owner == ids["general"]


@pytest.mark.asyncio
async def test_restore_room_uses_canonical_state_parser_for_mixed_records(tmp_path: Path) -> None:
    """Restore should skip non-pending and malformed records while restoring valid pending records."""
    client = AsyncMock()
    cron_workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Run the daily report",
        description="Daily report",
        thread_id="$thread123",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "malformed_content",
                "content": "not a dict",
                "event_id": "$state_malformed_content",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567888,
            },
            {
                "type": SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "old_cancelled",
                "content": {
                    "status": "cancelled",
                },
                "event_id": "$state_cancelled",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567889,
            },
            {
                "type": SCHEDULED_TASK_EVENT_TYPE,
                "state_key": "task_cron",
                "content": {
                    "workflow": cron_workflow.model_dump_json(),
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
                },
                "event_id": "$state_task_cron",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567890,
            },
        ],
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=state_response)
    runtime, bot = _restoring_router(client, tmp_path)

    with patch.object(runtime, "_start_runner", return_value=True) as mock_start:
        restored = await runtime.restore_room(bot, "!test:server")

    assert restored == 1
    assert [call.args[1] for call in mock_start.call_args_list] == ["task_cron"]


@pytest.mark.asyncio
async def test_list_scheduled_tasks_real_implementation(tmp_path: Path) -> None:
    """Test list_scheduled_tasks with real implementation, only mocking Matrix API."""
    # Create mock client
    client = AsyncMock()

    # Create workflows
    workflow1 = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Test message 1",
        description="Test task 1",
        thread_id="$thread123",
        room_id="!test:server",
    )

    workflow2 = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Test message 2",
        description="Test task 2",
        thread_id="$thread456",
        room_id="!test:server",
    )

    workflow3 = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=1),
        message="Test message 3",
        description="Test task 3",
        thread_id="$thread123",
        room_id="!test:server",
    )

    workflow4 = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(hours=2),
        message="Room-level current-scope task",
        description="Room-level task",
        thread_id=None,
        room_id="!test:server",
    )

    workflow5 = ScheduledWorkflow(
        created_by="@user:server",
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
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "workflow": workflow1.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task1",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567890,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task2",
                "content": {
                    "workflow": workflow2.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task2",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567891,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task3",
                "content": {
                    "workflow": workflow3.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task3",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567892,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task4",
                "content": {
                    "workflow": workflow4.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task4",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567893,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task5",
                "content": {
                    "workflow": workflow5.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task5",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567894,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task6",
                "content": {
                    "status": "completed",  # This one is completed, should not appear
                },
                "event_id": "$state_task6",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567895,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    # Test listing tasks for thread123
    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=_test_runtime_paths(tmp_path),
        thread_id="$thread123",
        config=Config(),
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
        runtime_paths=_test_runtime_paths(tmp_path),
        thread_id="$thread456",
        config=Config(),
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
async def test_list_scheduled_tasks_no_tasks(tmp_path: Path) -> None:
    """Test list_scheduled_tasks when there are no tasks."""
    client = AsyncMock()

    # Empty response
    mock_response = nio.RoomGetStateResponse.from_dict([], room_id="!test:server")
    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=_test_runtime_paths(tmp_path),
        thread_id="$thread123",
        config=Config(),
    )

    assert result == "No scheduled tasks found."


@pytest.mark.asyncio
async def test_list_scheduled_tasks_tasks_in_other_threads(tmp_path: Path) -> None:
    """Test list_scheduled_tasks when all tasks are in other threads."""
    client = AsyncMock()

    workflow = ScheduledWorkflow(
        created_by="@user:server",
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
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "workflow": workflow.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task1",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567890,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=_test_runtime_paths(tmp_path),
        thread_id="$thread123",  # Looking for thread123, but task is in thread456
        config=Config(),
    )

    assert "No scheduled tasks in this thread" in result
    assert "1 task(s) scheduled in other threads" in result


@pytest.mark.asyncio
async def test_list_scheduled_tasks_error_response(tmp_path: Path) -> None:
    """Test list_scheduled_tasks when Matrix returns an error."""
    client = AsyncMock()

    # Return an error response
    error_response = nio.RoomGetStateError.from_dict({"error": "Not authorized"}, room_id="!test:server")
    client.room_get_state = AsyncMock(return_value=error_response)

    with pytest.raises(SchedulingError) as exc_info:
        await list_scheduled_tasks(
            client=client,
            room_id="!test:server",
            runtime_paths=_test_runtime_paths(tmp_path),
            thread_id="$thread123",
            config=Config(),
        )

    assert str(exc_info.value) == "Unable to retrieve scheduled tasks."


@pytest.mark.asyncio
async def test_list_scheduled_tasks_invalid_task_data(tmp_path: Path) -> None:
    """Test list_scheduled_tasks handles invalid task data gracefully."""
    client = AsyncMock()

    valid_workflow = ScheduledWorkflow(
        created_by="@user:server",
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
                "sender": SCHEDULE_WRITER_ID,
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
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567891,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task3",
                "content": {
                    "workflow": valid_workflow.model_dump_json(),
                    "status": "pending",
                },
                "event_id": "$state_task3",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567892,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=_test_runtime_paths(tmp_path),
        thread_id="$thread123",
        config=Config(),
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
    client.room_get_state_event.side_effect = joined_member_state
    config = Config()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
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
        patch("mindroom.scheduling.runtime.get_scheduled_task", side_effect=_fetch_task),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute_mock,
        patch("mindroom.scheduling.tasks.asyncio.sleep", new=AsyncMock()),
    ):
        await run_once_task(
            client,
            "task_once_cancelled",
            workflow,
            config,
            _runtime_paths(),
            _conversation_reader(),
        )

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_task_executes_latest_state_workflow() -> None:
    """One-time tasks should execute using the latest persisted workflow data."""
    client = make_matrix_client_mock()
    client.room_get_state_event.side_effect = joined_member_state
    config = Config()
    initial_workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Old message",
        description="Old description",
        room_id="!test:server",
        thread_id="$thread123",
    )
    updated_workflow = ScheduledWorkflow(
        created_by="@user:server",
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
        patch("mindroom.scheduling.runtime.get_scheduled_task", side_effect=_fetch_task),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute_mock,
    ):
        await run_once_task(
            client,
            "task_once_updated",
            initial_workflow,
            config,
            _runtime_paths(),
            _conversation_reader(),
        )

    execute_mock.assert_awaited_once()
    executed_workflow = execute_mock.await_args.args[1]
    assert executed_workflow.message == "Updated message"
    assert executed_workflow.description == "Updated description"


@pytest.mark.asyncio
async def test_run_once_task_retries_transient_state_read_failure(tmp_path: Path) -> None:
    """An unreadable checkpoint must keep the in-memory schedule retry-owned."""
    client = AsyncMock()
    serve_task_state_events(client)
    config = Config()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) - timedelta(seconds=1),
        message="Run after retry",
        description="Retry state read",
        room_id="!test:server",
        thread_id="$thread123",
    )
    state_response = nio.RoomGetStateEventResponse(
        content={
            "task_id": "task_once_retry",
            "workflow": workflow.model_dump_json(),
            "status": "pending",
        },
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key="task_once_retry",
        room_id="!test:server",
    )
    task_reads = iter(
        [
            nio.RoomGetStateEventError(message="rate limited", status_code="M_LIMIT_EXCEEDED"),
            state_response,
        ],
    )

    async def read_state(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type == "m.room.member":
            return joined_member_state(room_id, event_type, state_key)
        return next(task_reads)

    client.room_get_state_event.side_effect = read_state
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$completed"},
        room_id="!test:server",
    )

    with (
        patch("mindroom.scheduling.tasks.asyncio.sleep", new=AsyncMock()) as sleep,
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute,
        patch(
            "mindroom.scheduling.executor.send_scheduled_failure_notice",
            new=AsyncMock(),
        ) as failure_notice,
    ):
        await run_once_task(
            client,
            "task_once_retry",
            workflow,
            config,
            _test_runtime_paths(tmp_path),
            _conversation_reader(),
        )

    sleep.assert_awaited_once()
    assert sum(SCHEDULED_TASK_EVENT_TYPE in call.args[2] for call in client._send.await_args_list) == 2
    execute.assert_awaited_once()
    failure_notice.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_once_task_marks_completed_after_success() -> None:
    """One-time tasks should overwrite pending state with completed after firing."""
    client = make_matrix_client_mock()
    client.room_get_state_event.side_effect = joined_member_state
    client.room_put_state = AsyncMock()
    config = Config()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
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
            "mindroom.scheduling.runtime.get_scheduled_task",
            new=AsyncMock(side_effect=[pending_record, pending_record]),
        ),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute_mock,
    ):
        await run_once_task(
            client,
            "task_once_completed",
            workflow,
            config,
            _runtime_paths(),
            _conversation_reader(),
        )

    execute_mock.assert_awaited_once()
    client.room_put_state.assert_awaited_once()
    put_kwargs = client.room_put_state.await_args.kwargs
    assert put_kwargs["room_id"] == "!test:server"
    assert put_kwargs["event_type"] == SCHEDULED_TASK_EVENT_TYPE
    assert put_kwargs["state_key"] == "task_once_completed"
    assert put_kwargs["content"]["status"] == "completed"
    assert put_kwargs["content"]["workflow"] == workflow.model_dump_json()
    assert put_kwargs["content"]["created_at"] == pending_record.created_at.isoformat()


@pytest.mark.asyncio
async def test_run_once_task_marks_failed_after_execution_failure() -> None:
    """One-time tasks should overwrite pending state with failed when firing fails."""
    client = make_matrix_client_mock()
    client.room_get_state_event.side_effect = joined_member_state
    client.room_put_state = AsyncMock()
    config = Config()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
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
            "mindroom.scheduling.runtime.get_scheduled_task",
            new=AsyncMock(side_effect=[pending_record, pending_record]),
        ),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="failed", failure_reason="send failed")),
        ) as execute_mock,
    ):
        await run_once_task(
            client,
            "task_once_failed",
            workflow,
            config,
            _runtime_paths(),
            _conversation_reader(),
        )

    execute_mock.assert_awaited_once()
    client.room_put_state.assert_awaited_once()
    put_kwargs = client.room_put_state.await_args.kwargs
    assert put_kwargs["state_key"] == "task_once_failed"
    assert put_kwargs["content"]["status"] == "failed"
    assert put_kwargs["content"]["workflow"] == workflow.model_dump_json()


@pytest.mark.asyncio
async def test_run_cron_task_executes_latest_state_workflow(tmp_path: Path) -> None:
    """Recurring tasks should execute using the latest persisted workflow data."""
    client = AsyncMock()
    client.room_get_state_event.side_effect = joined_member_state
    config = Config()
    client.homeserver = "https://example.org"
    client.user_id = "@router:example.org"
    client.device_id = "TEST_DEVICE"
    initial_workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Old recurring message",
        description="Old recurring description",
        room_id="!test:server",
        thread_id="$thread123",
    )
    updated_workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Updated recurring message",
        description="Updated recurring description",
        room_id="!test:server",
        thread_id="$thread123",
    )

    occurrence = RecurringOccurrence(
        tmp_path / "checkpoint.json",
        _RecurringCheckpoint("workflow", datetime.now(UTC) - timedelta(seconds=1)),
    )

    async def _fetch_task(*_args: object, **_kwargs: object) -> ScheduledTaskRecord:
        return _record("task_cron_updated", updated_workflow, status="pending")

    with (
        patch("mindroom.scheduling.runtime.get_scheduled_task", side_effect=_fetch_task),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute_mock,
        patch("mindroom.scheduling.runtime.plan_recurring_occurrence", return_value=occurrence),
    ):
        await run_cron_task(
            client,
            "task_cron_updated",
            initial_workflow,
            config,
            _test_runtime_paths(tmp_path),
            _conversation_reader(),
        )

    execute_mock.assert_awaited_once()
    executed_workflow = execute_mock.await_args.args[1]
    assert executed_workflow.message == "Updated recurring message"
    assert executed_workflow.description == "Updated recurring description"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule", ["once", "cron"])
@pytest.mark.parametrize(
    ("error", "text"),
    [(RuntimeError("homeserver down"), "homeserver down"), (TimeoutError(), "TimeoutError")],
    ids=["with_text", "without_text"],
)
async def test_a_task_whose_run_raises_reports_why_it_failed(
    tmp_path: Path,
    schedule: str,
    error: Exception,
    text: str,
) -> None:
    """A task whose run raises posts a notice with the exception's text, or its type name when it has none."""
    client = AsyncMock()
    client.room_get_state_event.side_effect = joined_member_state
    client.homeserver = "https://example.org"
    client.user_id = "@router:example.org"
    client.device_id = "TEST_DEVICE"
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type=schedule,
        execute_at=datetime.now(UTC) - timedelta(seconds=1) if schedule == "once" else None,
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*")
        if schedule == "cron"
        else None,
        message="Check the queue",
        description="Queue check",
        room_id="!test:server",
        thread_id="$thread123",
    )
    occurrence = RecurringOccurrence(
        tmp_path / "checkpoint.json",
        _RecurringCheckpoint("workflow", datetime.now(UTC) - timedelta(seconds=1)),
    )

    async def _fetch_task(*_args: object, **_kwargs: object) -> ScheduledTaskRecord:
        return _record("task_failing", workflow, status="pending")

    with (
        patch("mindroom.scheduling.runtime.get_scheduled_task", side_effect=_fetch_task),
        patch("mindroom.scheduling.executor.execute_scheduled_workflow", new=AsyncMock(side_effect=error)),
        patch("mindroom.scheduling.runtime.plan_recurring_occurrence", return_value=occurrence),
        patch("mindroom.scheduling.executor.send_scheduled_failure_notice", new=AsyncMock()) as failure_notice,
    ):
        if schedule == "once":
            await run_once_task(
                client,
                "task_failing",
                workflow,
                Config(),
                _test_runtime_paths(tmp_path),
                _conversation_reader(),
            )
        else:
            await run_cron_task(
                client,
                "task_failing",
                workflow,
                Config(),
                _test_runtime_paths(tmp_path),
                _conversation_reader(),
            )

    failure_notice.assert_awaited_once()
    assert failure_notice.await_args.args[3].endswith(f"\nTask ID: task_failing\nError: {text}")


@pytest.mark.asyncio
async def test_run_cron_task_keeps_pending_state_after_success(tmp_path: Path) -> None:
    """Recurring tasks should keep their pending state after firing."""
    client = AsyncMock()
    client.room_get_state_event.side_effect = joined_member_state
    client.room_put_state = AsyncMock()
    config = Config()
    client.homeserver = "https://example.org"
    client.user_id = "@router:example.org"
    client.device_id = "TEST_DEVICE"
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="9", day="*", month="*", weekday="*"),
        message="Recurring message",
        description="Recurring description",
        room_id="!test:server",
        thread_id="$thread123",
    )
    pending_record = _record("task_cron_pending", workflow, status="pending")

    occurrence = RecurringOccurrence(
        tmp_path / "checkpoint.json",
        _RecurringCheckpoint("workflow", datetime.now(UTC) - timedelta(seconds=1)),
    )

    with (
        patch(
            "mindroom.scheduling.runtime.get_scheduled_task",
            new=AsyncMock(side_effect=[pending_record, pending_record]),
        ),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute_mock,
        patch("mindroom.scheduling.runtime.plan_recurring_occurrence", return_value=occurrence),
    ):
        await run_cron_task(
            client,
            "task_cron_pending",
            workflow,
            config,
            _test_runtime_paths(tmp_path),
            _conversation_reader(),
        )

    execute_mock.assert_awaited_once()
    client.room_put_state.assert_not_awaited()


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
        patch("mindroom.scheduling.runtime.get_scheduled_task", side_effect=_fetch_task),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute_mock,
    ):
        await run_cron_task(
            client,
            "task_cron_cancelled",
            workflow,
            config,
            _runtime_paths(),
            _conversation_reader(),
        )

    execute_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_scheduled_task_persists_through_the_router_when_active_agent_lacks_state_power(
    tmp_path: Path,
) -> None:
    """Cancelling a task falls back to a bot that may write the room's state after active-client permission failure."""
    client = AsyncMock()
    serve_task_state_events(client)
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    room_state: dict[str, dict[str, Any]] = {}
    state_writers, router = _recording_state_writer(room_state)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        created_by="@user:server",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="cancel me",
        description="cancel me",
        room_id="!test:server",
        thread_id="$thread",
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "task_id": "taskcancel",
                "workflow": workflow.model_dump_json(),
                "status": "pending",
                "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC).isoformat(),
            },
            event_type=SCHEDULED_TASK_EVENT_TYPE,
            state_key="taskcancel",
            room_id="!test:server",
        ),
    )

    with patch.object(state_writers, "cancel_runner") as cancel_runner:
        result = await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="taskcancel",
            runtime_paths=_test_runtime_paths(tmp_path),
            task_runtime=state_writers,
        )

    assert result == "✅ Cancelled task `taskcancel`"
    cancel_runner.assert_called_once_with("taskcancel")
    client.room_put_state.assert_awaited_once()
    router.room_put_state.assert_awaited_once()
    assert room_state["taskcancel"]["content"]["status"] == "cancelled"
    assert room_state["taskcancel"]["content"]["workflow"] == workflow.model_dump_json()


@pytest.mark.asyncio
async def test_cancel_scheduled_task_returns_error_when_state_write_fails(tmp_path: Path) -> None:
    """Failed Matrix state writes must not claim a task was cancelled."""
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    serve_task_state_events(client)
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="cancel me",
        description="cancel me",
        room_id="!test:server",
        thread_id="$thread",
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "task_id": "taskcancel",
                "workflow": workflow.model_dump_json(),
                "status": "pending",
            },
            event_type=SCHEDULED_TASK_EVENT_TYPE,
            state_key="taskcancel",
            room_id="!test:server",
        ),
    )

    with pytest.raises(SchedulingError) as exc_info:
        await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="taskcancel",
            runtime_paths=_test_runtime_paths(tmp_path),
        )

    assert str(exc_info.value).startswith("Failed to cancel task `taskcancel`")


@pytest.mark.asyncio
async def test_cancel_scheduled_task_keeps_transient_state_read_retryable(tmp_path: Path) -> None:
    """A transient Matrix read failure must not become a terminal not-found result."""
    client = AsyncMock()
    serve_task_state_events(client)
    client.room_get_state_event.return_value = nio.RoomGetStateEventError(
        message="rate limited",
        status_code="M_LIMIT_EXCEEDED",
    )

    with pytest.raises(RuntimeError, match="Failed to get scheduled task"):
        await cancel_scheduled_task(
            client=client,
            room_id="!test:server",
            task_id="taskcancel",
            runtime_paths=_test_runtime_paths(tmp_path),
        )

    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks(tmp_path: Path) -> None:
    """Test cancel_all_scheduled_tasks functionality."""
    # Create mock client
    client = AsyncMock()

    # Create workflows for testing
    workflow1 = ScheduledWorkflow(
        schedule_type="once",
        created_by="@user:server",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Test message 1",
        description="Test task 1",
        thread_id="$thread123",
        room_id="!test:server",
    )

    workflow2 = ScheduledWorkflow(
        schedule_type="once",
        created_by="@user:server",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="Test message 2",
        description="Test task 2",
        thread_id="$thread456",
        room_id="!test:server",
    )

    # Create a proper RoomGetStateResponse with scheduled tasks
    mock_response = nio.RoomGetStateResponse.from_dict(
        [
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task1",
                "content": {
                    "task_id": "task1",
                    "workflow": workflow1.model_dump_json(),
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
                },
                "event_id": "$state_task1",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567890,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task2",
                "content": {
                    "task_id": "task2",
                    "workflow": workflow2.model_dump_json(),
                    "status": "pending",
                    "created_at": datetime.now(UTC).isoformat(),
                },
                "event_id": "$state_task2",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567891,
            },
            {
                "type": "com.mindroom.scheduled.task",
                "state_key": "task3",
                "content": {
                    "task_id": "task3",
                    "workflow": workflow1.model_dump_json(),
                    "status": "cancelled",  # Already cancelled
                    "created_at": datetime.now(UTC).isoformat(),
                },
                "event_id": "$state_task3",
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567892,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)
    client.room_put_state = AsyncMock(
        return_value=nio.RoomPutStateResponse.from_dict({"event_id": "$event123"}, room_id="!test:server"),
    )

    result = await cancel_all_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=_test_runtime_paths(tmp_path),
    )

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


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_persists_through_the_router_when_active_agent_lacks_state_power(
    tmp_path: Path,
) -> None:
    """Cancel-all uses the same schedule-state persistence fallback."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    room_state: dict[str, dict[str, Any]] = {}
    state_writers, router = _recording_state_writer(room_state)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        created_by="@user:server",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="cancel all",
        description="cancel all",
        room_id="!test:server",
        thread_id="$thread",
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task1",
                    "content": {
                        "task_id": "task1",
                        "workflow": workflow.model_dump_json(),
                        "status": "pending",
                    },
                    "event_id": "$state_task1",
                    "sender": SCHEDULE_WRITER_ID,
                    "origin_server_ts": 1234567890,
                },
            ],
            room_id="!test:server",
        ),
    )

    with patch.object(state_writers, "cancel_runner") as cancel_runner:
        result = await cancel_all_scheduled_tasks(
            client=client,
            room_id="!test:server",
            runtime_paths=_test_runtime_paths(tmp_path),
            task_runtime=state_writers,
        )

    assert result == "✅ Cancelled 1 scheduled task(s)"
    cancel_runner.assert_called_once_with("task1")
    router.room_put_state.assert_awaited_once()
    assert room_state["task1"]["content"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_returns_error_when_state_write_fails(tmp_path: Path) -> None:
    """Cancel-all must not report success when pending task state cannot be written."""
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="cancel all",
        description="cancel all",
        room_id="!test:server",
        thread_id="$thread",
    )
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": "task1",
                    "content": {
                        "task_id": "task1",
                        "workflow": workflow.model_dump_json(),
                        "status": "pending",
                    },
                    "event_id": "$state_task1",
                    "sender": SCHEDULE_WRITER_ID,
                    "origin_server_ts": 1234567890,
                },
            ],
            room_id="!test:server",
        ),
    )

    with pytest.raises(SchedulingError) as exc_info:
        await cancel_all_scheduled_tasks(
            client=client,
            room_id="!test:server",
            runtime_paths=_test_runtime_paths(tmp_path),
        )

    assert str(exc_info.value) == "Failed to cancel 1 scheduled task(s)"


@pytest.mark.asyncio
async def test_get_scheduled_tasks_for_room_skips_cancelled_without_workflow(tmp_path: Path) -> None:
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
                "sender": SCHEDULE_WRITER_ID,
                "origin_server_ts": 1234567890,
            },
        ],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    tasks = await get_scheduled_tasks_for_room(
        client=client,
        room_id="!test:server",
        runtime_paths=_test_runtime_paths(tmp_path),
        include_non_pending=True,
    )

    assert tasks == []


@pytest.mark.asyncio
async def test_get_pending_schedule_thread_ids_excludes_new_threads_and_non_pending(tmp_path: Path) -> None:
    """Only pending schedules targeting an existing scope should suppress todo pokes."""
    client = AsyncMock()

    def event(
        task_id: str,
        workflow: ScheduledWorkflow,
        *,
        status: str = "pending",
    ) -> dict[str, object]:
        return {
            "type": SCHEDULED_TASK_EVENT_TYPE,
            "state_key": task_id,
            "content": {
                "task_id": task_id,
                "workflow": workflow.model_dump_json(),
                "status": status,
            },
            "event_id": f"$state_{task_id}",
            "sender": SCHEDULE_WRITER_ID,
            "origin_server_ts": 1234567890,
        }

    workflow_fields = {
        "schedule_type": "once",
        "execute_at": datetime.now(UTC) + timedelta(minutes=5),
        "message": "Continue work",
        "description": "Continue work",
        "room_id": "!test:server",
        "created_by": "@user:server",
    }
    response = nio.RoomGetStateResponse.from_dict(
        [
            event("thread", ScheduledWorkflow(**workflow_fields, thread_id="$thread")),
            event("main", ScheduledWorkflow(**workflow_fields, thread_id="main")),
            event("new", ScheduledWorkflow(**workflow_fields, new_thread=True)),
            event("cancelled", ScheduledWorkflow(**workflow_fields, thread_id="$cancelled"), status="cancelled"),
        ],
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=response)

    thread_ids = await get_pending_schedule_thread_ids_for_room(client, "!test:server", _test_runtime_paths(tmp_path))

    assert thread_ids == frozenset({"$thread", None})
    client.room_get_state.assert_awaited_once_with("!test:server")


@pytest.mark.asyncio
async def test_get_pending_schedule_thread_ids_raises_on_room_state_error(tmp_path: Path) -> None:
    """The todo scanner must be able to observe and log a failed Matrix state read."""
    client = AsyncMock()
    error_response = nio.RoomGetStateError.from_dict(
        {"error": "Not authorized"},
        room_id="!test:server",
    )
    client.room_get_state = AsyncMock(return_value=error_response)

    with pytest.raises(RuntimeError, match="Failed to get scheduled task state"):
        await get_pending_schedule_thread_ids_for_room(client, "!test:server", _test_runtime_paths(tmp_path))


@pytest.mark.asyncio
async def test_get_scheduled_task_raises_on_transient_matrix_error(tmp_path: Path) -> None:
    """A transient checkpoint read failure must not look like an absent task."""
    client = AsyncMock()
    client.room_get_state_event.return_value = nio.RoomGetStateEventError.from_dict(
        {"errcode": "M_LIMIT_EXCEEDED", "error": "Slow down"},
        "!test:server",
    )

    with pytest.raises(RuntimeError, match="Failed to get scheduled task"):
        await get_scheduled_task(client, "!test:server", "task123", _test_runtime_paths(tmp_path))


@pytest.mark.asyncio
async def test_cancel_all_scheduled_tasks_no_tasks(tmp_path: Path) -> None:
    """Test cancel_all_scheduled_tasks when no tasks exist."""
    # Create mock client
    client = AsyncMock()

    # Create empty response
    mock_response = nio.RoomGetStateResponse.from_dict(
        [],
        room_id="!test:server",
    )

    client.room_get_state = AsyncMock(return_value=mock_response)

    result = await cancel_all_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=_test_runtime_paths(tmp_path),
    )

    # Should indicate no tasks to cancel
    assert result == "No scheduled tasks to cancel."

    # Verify room_put_state was never called
    client.room_put_state.assert_not_called()


@pytest.mark.asyncio
async def test_edit_scheduled_task_reuses_existing_thread(tmp_path: Path) -> None:
    """Editing should keep the task ID and original thread context."""
    client = AsyncMock()
    serve_task_state_events(client)
    room = MagicMock()
    config = MagicMock()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Initial message",
        description="Initial task",
        thread_id="$original_thread",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateEventResponse(
        content={"status": "pending", "workflow": workflow.model_dump_json()},
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(return_value=state_response)

    with patch(
        "mindroom.scheduling.tasks.schedule_task",
        new=AsyncMock(return_value="✅ Scheduled"),
    ) as mock_schedule:
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=_test_runtime_paths(tmp_path),
                room=room,
            ),
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
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
    assert call_kwargs["existing_task"].task_id == "task123"
    assert call_kwargs["existing_task"].workflow.thread_id == "$original_thread"
    assert call_kwargs["history_limit"] is None
    assert call_kwargs["silent"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original_thread", "new_thread"),
    [(None, False), ("$original_thread", False), (None, True)],
)
async def test_threaded_schedule_edit_preserves_persisted_placement(
    tmp_path: Path,
    original_thread: str | None,
    new_thread: bool,
) -> None:
    """Editing the description must not move a saved schedule to the editing thread."""
    client = AsyncMock()
    serve_task_state_events(client)
    room_state: dict[str, dict[str, Any]] = {}
    state_writers, _router = _recording_state_writer(room_state)
    client.room_get_state_event = AsyncMock(
        side_effect=lambda room_id, event_type, state_key: nio.RoomGetStateEventResponse(
            content=room_state[state_key]["content"],
            room_id=room_id,
            event_type=event_type,
            state_key=state_key,
        ),
    )
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="check the queue",
        description="Original description",
        room_id="!test:server",
        thread_id=original_thread,
        new_thread=new_thread,
        created_by="@alice:server",
    )
    await persist_scheduled_task_state(
        client=client,
        owner_id=None,
        room_id="!test:server",
        task_id="task123",
        workflow=workflow,
        writers=state_writers,
        config=Config(),
    )
    parsed = workflow.model_copy(update={"description": "Updated description"})
    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=parsed)),
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=_matrix_room("!test:server"),
                task_runtime=state_writers,
            ),
            room_id="!test:server",
            task_id="task123",
            full_text="change the description to Updated description",
            scheduled_by="@alice:server",
        )

    assert "Updated task" in result
    saved = await get_scheduled_task(client, "!test:server", "task123", runtime_paths)
    assert saved is not None
    assert saved.workflow.description == "Updated description"
    assert saved.workflow.execute_at == workflow.execute_at
    assert saved.workflow.thread_id == original_thread
    assert saved.workflow.new_thread is new_thread


@pytest.mark.asyncio
async def test_parsed_schedule_never_carries_a_call_approval(tmp_path: Path) -> None:
    """An edit never becomes pre-approved, even when the parsed schedule claims it is."""
    client = AsyncMock()
    serve_task_state_events(client)
    room_state: dict[str, dict[str, Any]] = {}
    state_writers, _router = _recording_state_writer(room_state)
    client.room_get_state_event = AsyncMock(
        side_effect=lambda room_id, event_type, state_key: nio.RoomGetStateEventResponse(
            content=room_state[state_key]["content"],
            room_id=room_id,
            event_type=event_type,
            state_key=state_key,
        ),
    )
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="@assistant call add",
        description="Pre-approved add",
        room_id="!test:server",
        thread_id="$thread",
        created_by="@alice:server",
    )
    await persist_scheduled_task_state(
        client=client,
        owner_id=None,
        room_id="!test:server",
        task_id="task123",
        workflow=workflow,
        writers=state_writers,
        config=Config(),
    )
    withdraw = AsyncMock()
    parsed = workflow.model_copy(update={"description": "Edited", "pre_approved_call": True})
    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=parsed)),
        scheduled_call_approvals(withdraw_scheduled_call_approval=withdraw),
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=_matrix_room("!test:server"),
                task_runtime=state_writers,
            ),
            room_id="!test:server",
            task_id="task123",
            full_text="rename it Edited",
            scheduled_by="@alice:server",
        )

    assert "Updated task" in result
    withdraw.assert_not_awaited()
    saved = await get_scheduled_task(client, "!test:server", "task123", runtime_paths)
    assert saved is not None
    assert saved.workflow.description == "Edited"
    assert saved.workflow.pre_approved_call is False


@pytest.mark.asyncio
async def test_edit_scheduled_task_forwards_history_limit_override(tmp_path: Path) -> None:
    """An explicit history limit on edit must reach the shared scheduling backend."""
    client = AsyncMock()
    serve_task_state_events(client)
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Initial message",
        description="Initial task",
        thread_id="$original_thread",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateEventResponse(
        content={"status": "pending", "workflow": workflow.model_dump_json()},
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(return_value=state_response)

    with patch(
        "mindroom.scheduling.tasks.schedule_task",
        new=AsyncMock(return_value="✅ Scheduled"),
    ) as mock_schedule:
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(client=client, runtime_paths=_test_runtime_paths(tmp_path)),
            room_id="!test:server",
            task_id="task123",
            full_text="keep the same schedule",
            scheduled_by="@user:server",
            history_limit=2,
        )

    assert "✅ Updated task `task123`." in result
    assert mock_schedule.await_args.kwargs["history_limit"] == 2


@pytest.mark.asyncio
async def test_edit_scheduled_task_preserves_new_thread_mode(tmp_path: Path) -> None:
    """Editing a new-thread schedule should not repopulate thread_id from the editor context."""
    client = AsyncMock()
    serve_task_state_events(client)
    room = MagicMock()
    config = MagicMock()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="Initial message",
        description="Initial task",
        thread_id=None,
        room_id="!test:server",
        new_thread=True,
    )
    state_response = nio.RoomGetStateEventResponse(
        content={"status": "pending", "workflow": workflow.model_dump_json()},
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(return_value=state_response)

    with patch(
        "mindroom.scheduling.tasks.schedule_task",
        new=AsyncMock(return_value="✅ Scheduled"),
    ) as mock_schedule:
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=_test_runtime_paths(tmp_path),
                room=room,
            ),
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
        )

    assert "✅ Updated task `task123`." in result
    call_kwargs = mock_schedule.await_args.kwargs
    assert call_kwargs["thread_id"] is None
    assert call_kwargs["new_thread"] is True


@pytest.mark.asyncio
async def test_edit_scheduled_task_persists_through_the_router_and_preserves_omitted_silent_mode(
    tmp_path: Path,
) -> None:
    """Editing preserves omitted fields while persisting through a bot that may write the room's state."""
    client = AsyncMock()
    serve_task_state_events(client)
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    room_state: dict[str, dict[str, Any]] = {}
    client.room_get_state = AsyncMock(side_effect=lambda room_id: _room_state_response(room_id, room_state))
    state_writers, router = _recording_state_writer(room_state)
    room = _matrix_room("!test:server")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", role="Test assistant")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    existing_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="old message",
        description="old task",
        created_by="@alice:server",
        thread_id="$thread",
        room_id="!test:server",
        silent=True,
    )
    client.room_get_state_event = AsyncMock(
        return_value=nio.RoomGetStateEventResponse(
            content={
                "task_id": "taskedit",
                "workflow": existing_workflow.model_dump_json(),
                "status": "pending",
                "created_at": datetime(2026, 1, 1, 12, 0, tzinfo=UTC).isoformat(),
            },
            event_type=SCHEDULED_TASK_EVENT_TYPE,
            state_key="taskedit",
            room_id="!test:server",
        ),
    )
    updated_workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=10),
        message="updated message",
        description="updated task",
    )

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=updated_workflow)),
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=room,
                task_runtime=state_writers,
            ),
            room_id="!test:server",
            task_id="taskedit",
            full_text="in 10 minutes update logs",
            scheduled_by="@alice:server",
        )

    assert "✅ Updated task `taskedit`." in result
    router.room_put_state.assert_awaited_once()
    tasks = await get_scheduled_tasks_for_room(client=client, room_id="!test:server", runtime_paths=runtime_paths)
    assert [task.task_id for task in tasks] == ["taskedit"]
    assert tasks[0].workflow.message == "updated message"
    assert tasks[0].workflow.silent is True


@pytest.mark.asyncio
async def test_edit_scheduled_task_rejects_non_pending(tmp_path: Path) -> None:
    """Editing should fail for cancelled/completed tasks."""
    client = AsyncMock()
    serve_task_state_events(client)
    room = MagicMock()
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        message="original task",
        description="original task",
        room_id="!test:server",
    )
    state_response = nio.RoomGetStateEventResponse(
        content={"status": "cancelled", "workflow": workflow.model_dump_json()},
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    client.room_get_state_event = AsyncMock(return_value=state_response)

    with pytest.raises(SchedulingError) as exc_info:
        await edit_scheduled_task(
            runtime=_scheduling_runtime(client=client, runtime_paths=_test_runtime_paths(tmp_path), room=room),
            room_id="!test:server",
            task_id="task123",
            full_text="tomorrow at 9am updated task",
            scheduled_by="@user:server",
        )

    assert str(exc_info.value) == "Task `task123` cannot be edited because it is `cancelled`."


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_preserves_created_at(tmp_path: Path) -> None:
    """Editing should keep created_at metadata from the original task."""
    client = AsyncMock()
    serve_task_state_events(client)
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!test:server",
    )
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    existing_workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
        message="original message",
        description="original description",
        thread_id="$thread1",
        room_id="!test:server",
    )
    updated_workflow = ScheduledWorkflow(
        created_by="@user:server",
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

    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "status": "pending",
            "workflow": existing_task.workflow.model_dump_json(),
            "created_at": created_at.isoformat(),
        },
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    updated_task = await save_edited_scheduled_task(
        client=client,
        workflow=updated_workflow,
        existing_task=existing_task,
        runtime_paths=_test_runtime_paths(tmp_path),
        config=Config(),
    )

    assert updated_task.created_at == created_at
    assert updated_task.workflow == updated_workflow
    client.room_put_state.assert_awaited_once()
    assert client.room_put_state.await_args.kwargs["content"]["created_at"] == created_at.isoformat()


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_is_state_only(tmp_path: Path) -> None:
    """State-only edits should not require runtime-only scheduling collaborators."""
    client = AsyncMock()
    serve_task_state_events(client)
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!test:server",
    )
    created_at = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    existing_task = ScheduledTaskRecord(
        task_id="task123",
        room_id="!test:server",
        status="pending",
        created_at=created_at,
        workflow=ScheduledWorkflow(
            created_by="@user:server",
            schedule_type="once",
            execute_at=datetime(2026, 2, 1, 10, 0, tzinfo=UTC),
            message="original message",
            description="original description",
            thread_id="$thread1",
            room_id="!test:server",
        ),
    )
    updated_workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="once",
        execute_at=datetime(2026, 2, 1, 11, 0, tzinfo=UTC),
        message="updated message",
        description="updated description",
        thread_id="$thread1",
        room_id="!test:server",
    )

    client.room_get_state_event.return_value = nio.RoomGetStateEventResponse(
        content={
            "status": "pending",
            "workflow": existing_task.workflow.model_dump_json(),
            "created_at": created_at.isoformat(),
        },
        event_type=SCHEDULED_TASK_EVENT_TYPE,
        state_key="task123",
        room_id="!test:server",
    )
    updated_task = await save_edited_scheduled_task(
        client=client,
        workflow=updated_workflow,
        existing_task=existing_task,
        runtime_paths=_test_runtime_paths(tmp_path),
        config=Config(),
    )

    assert updated_task.created_at == created_at
    assert updated_task.workflow == updated_workflow
    client.room_put_state.assert_awaited_once()


@pytest.mark.asyncio
async def test_save_edited_scheduled_task_rejects_schedule_type_change(tmp_path: Path) -> None:
    """Editing should reject switching between once and cron schedule types."""
    client = AsyncMock()
    serve_task_state_events(client)
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

    with pytest.raises(SchedulingError, match="Changing schedule_type is not supported"):
        await save_edited_scheduled_task(
            client=client,
            workflow=updated_workflow,
            existing_task=existing_task,
            runtime_paths=_test_runtime_paths(tmp_path),
            config=Config(),
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
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[],
        ),
        patch(
            "mindroom.scheduling.tasks._extract_mentioned_agents_from_text",
            return_value=[],
        ),
        pytest.raises(SchedulingError) as exc_info,
    ):
        await schedule_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@blocked:server",
            full_text="remind me in 5 minutes to check logs",
        )

    assert str(exc_info.value) == "No agents or teams in this room are allowed to reply to you."


@pytest.mark.asyncio
async def test_schedule_task_blocked_sender_new_thread_returns_error() -> None:
    """new_thread mode should also return a clean error when the sender has no visible agents."""
    client = AsyncMock()
    room = MagicMock()
    config = MagicMock()

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[],
        ),
        patch(
            "mindroom.scheduling.tasks._extract_mentioned_agents_from_text",
            return_value=[],
        ),
        pytest.raises(SchedulingError) as exc_info,
    ):
        await schedule_task(
            runtime=_scheduling_runtime(client=client, config=config, room=room),
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@blocked:server",
            full_text="remind me in 5 minutes",
            new_thread=True,
        )

    assert str(exc_info.value) == "No agents or teams in this room are allowed to reply to you."


@pytest.mark.asyncio
async def test_persist_scheduled_task_state_raises_on_matrix_error() -> None:
    """Schedule persistence must fail when Matrix rejects the state write."""
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")

    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="check logs",
        description="check logs",
        room_id="!test:server",
    )

    with pytest.raises(SchedulingError, match="Failed to persist scheduled task state"):
        await persist_scheduled_task_state(
            client=client,
            owner_id=None,
            room_id="!test:server",
            task_id="task1234",
            workflow=workflow,
            config=Config(),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("writer_level", "hint"),
    [
        (
            0,
            f"Ask a room admin to raise the power level of {SCHEDULE_WRITER_ID} in this room from 0 to 50, then retry.",
        ),
        (50, f"Ensure {SCHEDULE_WRITER_ID} is joined with permission to write room state, then retry."),
    ],
    ids=["under-powered", "powered"],
)
async def test_state_write_error_names_the_power_grant_the_writer_lacks(writer_level: int, hint: str) -> None:
    """A refused state write names the exact grant only when the writing account lacks the power."""
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    room = _matrix_room("!test:server")
    room.power_levels.defaults.state_default = 50
    room.power_levels.users[SCHEDULE_WRITER_ID] = writer_level
    client.rooms["!test:server"] = room
    client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")

    with pytest.raises(SchedulingError, match="Failed to persist scheduled task state") as exc_info:
        await scheduling._put_scheduled_task_state_content(
            client=client,
            room_id="!test:server",
            task_id="taskfail",
            content={},
        )

    assert str(exc_info.value).endswith(hint)


@pytest.mark.asyncio
async def test_agent_state_write_error_names_the_router_grant() -> None:
    """A refused agent write asks for the router's grant, which unblocks every schedule in the room."""
    router_client = make_matrix_client_mock(user_id="@mindroom_router:server")
    router_room = _matrix_room("!test:server")
    router_room.power_levels.defaults.state_default = 50
    router_room.power_levels.users[router_client.user_id] = 0
    router_client.rooms = {"!test:server": router_room}
    writers = scheduled_task_runtime(_runtime_paths(), scheduling_bot(router_client), config=Config())
    agent_client = make_matrix_client_mock(user_id="@mindroom_general:server")
    agent_client.room_put_state.return_value = nio.RoomPutStateError("forbidden", "M_FORBIDDEN")

    with pytest.raises(SchedulingError, match="Failed to persist scheduled task state") as exc_info:
        await scheduling._put_scheduled_task_state_content(
            client=agent_client,
            room_id="!test:server",
            task_id="taskfail",
            content={},
            writers=writers,
        )

    router_client.room_put_state.assert_not_awaited()

    assert str(exc_info.value).endswith(
        "Ask a room admin to raise the power level of @mindroom_router:server in this room from 0 to 50, then retry.",
    )


@pytest.mark.asyncio
async def test_a_status_write_describes_a_task_without_a_timezone_in_the_current_global_one() -> None:
    """A runner's status write keeps a legacy task's recurring description in the timezone the config has now."""
    client = make_matrix_client_mock(user_id="@mindroom_router:server")
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!test:server",
    )
    workflow = ScheduledWorkflow(
        created_by="@user:server",
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="0", month="1"),
        message="check logs",
        description="check logs",
        room_id="!test:server",
    )
    writers = scheduled_task_runtime(_runtime_paths(), scheduling_bot(client), config=Config(timezone="Asia/Tokyo"))

    await scheduling_runtime._save_task_status(client, _record("taskcron", workflow), "failed", writers=writers)

    content = client.room_put_state.await_args.kwargs["content"]
    assert (content["status"], content["cron_description"]) == ("failed", "At 09:00, only in January (Asia/Tokyo)")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent_level", "agent_writes"),
    [(100, True), (0, False), (None, False)],
    ids=["agent-may-write", "agent-under-powered", "agent-not-joined"],
)
async def test_restored_task_state_refused_to_the_router_lands_through_a_bot_the_room_lets_write_it(
    agent_level: int | None,
    agent_writes: bool,
) -> None:
    """A router that cannot write state in a room only its creating agent administers writes through that agent."""
    router_client = make_matrix_client_mock(user_id="@mindroom_router:server")
    router_client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    router_room = _matrix_room("!test:server")
    router_room.power_levels.defaults.state_default = 50
    router_client.rooms["!test:server"] = router_room
    agent_client = make_matrix_client_mock(user_id="@mindroom_general:server")
    agent_client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!test:server",
    )
    agent_client.rooms = {}
    if agent_level is not None:
        agent_room = _matrix_room("!test:server")
        agent_room.power_levels.defaults.state_default = 50
        agent_room.power_levels.users[agent_client.user_id] = agent_level
        agent_client.rooms["!test:server"] = agent_room
    writers = scheduled_task_runtime(
        _runtime_paths(),
        scheduling_bot(router_client),
        scheduling_bot(agent_client, agent_name="general"),
        config=Config(),
    )
    record = _record("taskdone", _pending_once_workflow())

    async def save() -> None:
        await scheduling_runtime._save_task_status(
            client=router_client,
            task=record,
            status="completed",
            writers=writers,
        )

    if agent_writes:
        await save()
        assert agent_client.room_put_state.await_args.kwargs["state_key"] == "taskdone"
        assert agent_client.room_put_state.await_args.kwargs["content"]["status"] == "completed"
    else:
        with pytest.raises(SchedulingError, match="Failed to persist scheduled task state"):
            await save()
        agent_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_timezone", "expected"),
    [(None, "At 09:00, only in January (Asia/Tokyo)"), ("Europe/London", "At 00:00, only in January (Europe/London)")],
)
async def test_persist_scheduled_task_state_includes_cron_description(task_timezone: str | None, expected: str) -> None:
    """Matrix state describes a recurring schedule in the task's own timezone, else the global one."""
    client = AsyncMock()
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!test:server",
    )
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="0", month="1"),
        message="check logs",
        description="check logs",
        room_id="!test:server",
        timezone=task_timezone,
    )

    await persist_scheduled_task_state(
        client=client,
        owner_id=None,
        room_id="!test:server",
        task_id="task1234",
        workflow=workflow,
        config=Config(timezone="Asia/Tokyo"),
    )

    content = client.room_put_state.await_args.kwargs["content"]
    assert content["cron_description"] == expected


@pytest.mark.asyncio
async def test_persist_scheduled_task_state_omits_cron_description_for_one_time_task() -> None:
    """One-time state must not expose a stale or contradictory cron description."""
    client = AsyncMock()
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!test:server",
    )
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        cron_schedule=CronSchedule(minute="0", hour="9"),
        message="check logs",
        description="check logs",
        room_id="!test:server",
    )

    await persist_scheduled_task_state(
        client=client,
        owner_id=None,
        room_id="!test:server",
        task_id="task1234",
        workflow=workflow,
        config=Config(),
    )

    content = client.room_put_state.await_args.kwargs["content"]
    assert content["cron_description"] is None


@pytest.mark.asyncio
async def test_schedule_task_persists_through_the_router_when_active_agent_lacks_state_power(tmp_path: Path) -> None:
    """A successful schedule must be visible in Matrix state even when the active agent cannot write state."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    room_state: dict[str, dict[str, Any]] = {}
    client.room_get_state = AsyncMock(side_effect=lambda room_id: _room_state_response(room_id, room_state))
    state_writers, router = _recording_state_writer(room_state)
    room = _matrix_room("!test:server")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", role="Test assistant")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="check logs",
        description="check logs",
    )

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch.object(ScheduledTaskRuntime, "start_new_task"),
        patch("mindroom.scheduling.tasks.uuid.uuid4", return_value="task1234"),
    ):
        message = await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=room,
                task_runtime=state_writers,
            ),
            room_id="!test:server",
            thread_id="$thread",
            scheduled_by="@alice:server",
            full_text="in 5 minutes check logs",
        )

    assert "**Task ID:** `task1234`" in message
    assert "✅ Scheduled" in message
    assert "**Delivery:** Current room/thread scope" in message
    router.room_put_state.assert_awaited_once()
    tasks = await get_scheduled_tasks_for_room(client=client, room_id="!test:server", runtime_paths=runtime_paths)
    assert [task.task_id for task in tasks] == ["task1234"]
    persisted = tasks[0]
    assert persisted.workflow.created_by == "@alice:server"
    assert persisted.workflow.room_id == "!test:server"
    assert persisted.workflow.thread_id == "$thread"
    assert persisted.workflow.new_thread is False
    listed = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread",
        config=config,
    )
    assert "`task1234`" in listed


@pytest.mark.asyncio
async def test_schedule_task_explicit_history_limit_overrides_parse_and_round_trips(tmp_path: Path) -> None:
    """An explicit history limit wins over the parse, persists to Matrix state, and surfaces in listings."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    room_state: dict[str, dict[str, Any]] = {}
    client.room_get_state = AsyncMock(side_effect=lambda room_id: _room_state_response(room_id, room_state))
    state_writers, _router = _recording_state_writer(room_state)
    room = _matrix_room("!test:server")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", role="Test assistant")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="*/25"),
        message="poll the queue",
        description="Queue poller",
    )

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch.object(ScheduledTaskRuntime, "start_new_task"),
        patch("mindroom.scheduling.tasks.uuid.uuid4", return_value="task1234"),
    ):
        message = await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=room,
                task_runtime=state_writers,
            ),
            room_id="!test:server",
            thread_id="$thread",
            scheduled_by="@alice:server",
            full_text="every 25 minutes poll the queue with only the last 5 messages",
            history_limit=5,
            silent=True,
        )

    assert "**Task ID:** `task1234`" in message
    assert "**History:** last 5 messages" in message
    assert "**Mode:** Silent" in message
    tasks = await get_scheduled_tasks_for_room(client=client, room_id="!test:server", runtime_paths=runtime_paths)
    assert [task.task_id for task in tasks] == ["task1234"]
    assert tasks[0].workflow.history_limit == 5
    assert tasks[0].workflow.silent is True
    listed = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread",
        config=config,
    )
    assert "History: last 5 messages" in listed
    assert "Mode: Silent" in listed


@pytest.mark.asyncio
async def test_schedule_task_keeps_parse_produced_history_limit(tmp_path: Path) -> None:
    """Without an explicit override, the parse-produced history limit persists and surfaces."""
    client = AsyncMock()
    serve_full_room_state(client, "!test:server")
    client.room_put_state.return_value = nio.RoomPutStateResponse.from_dict(
        {"event_id": "$state"},
        room_id="!test:server",
    )
    room = _matrix_room("!test:server")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", role="Test assistant")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="*/25"),
        message="poll the queue",
        description="Queue poller",
        history_limit=0,
    )

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch.object(ScheduledTaskRuntime, "start_new_task"),
        patch("mindroom.scheduling.tasks.uuid.uuid4", return_value="task1234"),
    ):
        message = await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=room,
            ),
            room_id="!test:server",
            thread_id="$thread",
            scheduled_by="@alice:server",
            full_text="every 25 minutes poll the queue with no history",
        )

    assert "**Task ID:** `task1234`" in message
    assert "**History:** none" in message
    persisted = json.loads(client.room_put_state.await_args.kwargs["content"]["workflow"])
    assert persisted["history_limit"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_history_limit", [-1, True, 1.5, "5"])
async def test_schedule_task_rejects_invalid_history_limit_before_parsing(
    invalid_history_limit: object,
) -> None:
    """An invalid explicit limit fails fast without spending an AI parse call."""
    parse_mock = AsyncMock()

    with (
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=parse_mock),
        pytest.raises(SchedulingError) as exc_info,
    ):
        await schedule_task(
            runtime=_scheduling_runtime(),
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@user:server",
            full_text="in 5 minutes check logs",
            history_limit=cast("int", invalid_history_limit),
        )

    assert str(exc_info.value) == "history_limit must be a non-negative integer."
    parse_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_task_returns_error_when_state_write_fails_without_a_fallback_writer(tmp_path: Path) -> None:
    """Scheduling must not return a task ID when Matrix state persistence fails."""
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    serve_full_room_state(client, "!test:server")
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    room = _matrix_room("!test:server")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", role="Test assistant")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="check logs",
        description="check logs",
    )

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch.object(ScheduledTaskRuntime, "start_new_task") as start_task,
        patch("mindroom.scheduling.tasks.uuid.uuid4", return_value="taskfail"),
        pytest.raises(SchedulingError) as exc_info,
    ):
        await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=room,
                task_runtime=scheduled_task_runtime(runtime_paths, config=config),
            ),
            room_id="!test:server",
            thread_id="$thread",
            scheduled_by="@alice:server",
            full_text="in 5 minutes check logs",
        )

    message = str(exc_info.value)
    assert message.startswith("Failed to schedule: ")
    assert "Failed to persist scheduled task state" in message
    assert message.endswith(f"Ensure {SCHEDULE_WRITER_ID} is joined with permission to write room state, then retry.")
    start_task.assert_not_called()


@pytest.mark.asyncio
async def test_state_write_error_includes_hint_when_the_fallback_writer_raises() -> None:
    """A raised write by the bot that may write the room's state must still tell the room admin how to recover."""
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    writer = make_matrix_client_mock(user_id="@mindroom_general:server")
    room = _matrix_room("!test:server")
    room.power_levels.users[writer.user_id] = 100
    writer.rooms = {"!test:server": room}
    writer.room_put_state = AsyncMock(side_effect=RuntimeError("writer unavailable"))
    writers = scheduled_task_runtime(_runtime_paths(), scheduling_bot(writer, agent_name="general"), config=Config())

    with pytest.raises(SchedulingError, match="write failed") as exc_info:
        await scheduling._put_scheduled_task_state_content(
            client=client,
            room_id="!test:server",
            task_id="taskfail",
            content={},
            writers=writers,
        )

    message = str(exc_info.value)
    assert "@mindroom_general:server write failed (RuntimeError: writer unavailable)" in message
    assert message.endswith(f"Ensure {SCHEDULE_WRITER_ID} is joined with permission to write room state, then retry.")


@pytest.mark.asyncio
async def test_schedule_task_returns_error_when_active_write_returns_unexpected_response(tmp_path: Path) -> None:
    """Scheduling must require a positive Matrix state-write response before returning success."""
    client = make_matrix_client_mock(user_id=SCHEDULE_WRITER_ID)
    serve_full_room_state(client, "!test:server")
    client.room_put_state = AsyncMock(return_value=None)
    room = _matrix_room("!test:server")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", role="Test assistant")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(minutes=5),
        message="check logs",
        description="check logs",
    )

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=workflow)),
        patch.object(ScheduledTaskRuntime, "start_new_task") as start_task,
        patch("mindroom.scheduling.tasks.uuid.uuid4", return_value="taskweird"),
        pytest.raises(SchedulingError) as exc_info,
    ):
        await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=room,
                task_runtime=scheduled_task_runtime(runtime_paths, config=config),
            ),
            room_id="!test:server",
            thread_id="$thread",
            scheduled_by="@alice:server",
            full_text="in 5 minutes check logs",
        )

    message = str(exc_info.value)
    assert message.startswith("Failed to schedule: ")
    assert "Failed to persist scheduled task state" in message
    start_task.assert_not_called()


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
            models={"default": ModelConfig(provider="openai", id="test-model")},
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
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=parse_result)),
        patch("mindroom.scheduling.tasks._save_pending_scheduled_task", new=AsyncMock(return_value=None)),
        patch("mindroom.scheduling.tasks.uuid.uuid4", return_value="task12345"),
    ):
        message = await schedule_task(
            runtime=runtime,
            room_id="!test:server",
            thread_id=None,
            scheduled_by=f"@alice:{config.get_domain(runtime_paths)}",
            full_text="in 5 minutes check logs",
        )

    assert "**Task ID:** `task1234`" in message
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
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    ids = entity_ids(
        config,
        runtime_paths,
        usernames={"assistant": "actual_assistant", "writer": "actual_writer"},
    )
    thread_message = _visible_message(
        sender=ids["assistant"].full_id,
        body="earlier",
        event_id="$earlier:localhost",
        timestamp=1,
    )
    conversation_reader = make_conversation_reader_mock()
    serve_conversation_reader(conversation_reader, [thread_message])
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
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            new=AsyncMock(return_value=[ids["assistant"], ids["writer"]]),
        ),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock(return_value=parse_result)),
        patch("mindroom.scheduling.tasks._save_pending_scheduled_task", new=AsyncMock()) as save_task,
    ):
        runtime = _scheduling_runtime(
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            room=room,
            conversation_reader=conversation_reader,
        )
        with pytest.raises(SchedulingError) as exc_info:
            await schedule_task(
                runtime=runtime,
                room_id="!test:server",
                thread_id="$thread",
                scheduled_by="@alice:localhost",
                full_text="in 5 minutes ask writer to check logs",
            )

    message = str(exc_info.value)
    assert message.startswith("Failed to schedule: ")
    assert "@writer is not available in this thread" in message
    save_task.assert_not_awaited()


@pytest.mark.asyncio
async def test_schedule_model_persists_and_edits(tmp_path: Path) -> None:
    """The chosen model survives state storage and edits, and can be cleared."""
    client = AsyncMock()
    serve_task_state_events(client)
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    room_state: dict[str, dict[str, Any]] = {}
    client.room_get_state = AsyncMock(side_effect=lambda room_id: _room_state_response(room_id, room_state))
    client.room_get_state_event = AsyncMock(
        side_effect=lambda room_id, event_type, state_key: nio.RoomGetStateEventResponse(
            content=room_state[state_key]["content"],
            room_id=room_id,
            event_type=event_type,
            state_key=state_key,
        ),
    )
    state_writers, _router = _recording_state_writer(room_state)
    room = _matrix_room("!test:server")
    runtime_paths = _test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"assistant": AgentConfig(display_name="Assistant", role="Test assistant")},
            models={name: ModelConfig(provider="openai", id=f"{name}-model") for name in ("default", "cheap")},
        ),
        runtime_paths,
    )
    ids = entity_ids(config, runtime_paths)
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="*/25"),
        message="poll the queue",
        description="Queue poller",
    )

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch(
            "mindroom.scheduling.tasks._parse_workflow_schedule",
            new=AsyncMock(side_effect=lambda *_args, **_kwargs: workflow.model_copy(deep=True)),
        ),
        patch.object(ScheduledTaskRuntime, "start_new_task"),
        patch("mindroom.scheduling.tasks.uuid.uuid4", return_value="task1234"),
    ):
        message = await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=room,
                task_runtime=state_writers,
            ),
            room_id="!test:server",
            thread_id="$thread",
            scheduled_by="@alice:server",
            full_text="every 25 minutes poll the queue with only the last 5 messages",
            model="cheap",
            silent=True,
        )

    assert "**Task ID:** `task1234`" in message
    assert "**Model:** cheap" in message
    assert "**Mode:** Silent" in message
    tasks = await get_scheduled_tasks_for_room(client=client, room_id="!test:server", runtime_paths=runtime_paths)
    assert [task.task_id for task in tasks] == ["task1234"]
    assert tasks[0].workflow.model == "cheap"
    assert build_edited_scheduled_workflow(tasks[0].workflow, "!test:server", message="new task").model == "cheap"
    assert tasks[0].workflow.silent is True
    listed = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread",
        config=config,
    )
    assert "Model: cheap" in listed
    assert "Mode: Silent" in listed

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["assistant"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch(
            "mindroom.scheduling.tasks._parse_workflow_schedule",
            new=AsyncMock(side_effect=lambda *_args, **_kwargs: workflow.model_copy(deep=True)),
        ),
    ):
        for model, expected in [(None, "cheap"), ("default", "default"), ("", None)]:
            result = await edit_scheduled_task(
                runtime=_scheduling_runtime(
                    client=client,
                    config=config,
                    runtime_paths=runtime_paths,
                    room=room,
                    task_runtime=state_writers,
                ),
                room_id="!test:server",
                task_id="task1234",
                full_text="keep the same task",
                scheduled_by="@alice:server",
                model=model,
            )
            assert "Updated task" in result
            stored = await get_scheduled_tasks_for_room(
                client=client,
                room_id="!test:server",
                runtime_paths=runtime_paths,
            )
            assert stored[0].workflow.model == expected


@pytest.mark.asyncio
async def test_schedule_rejects_unknown_model_before_parsing() -> None:
    """An unknown model must fail before parsing or writing schedule state."""
    with (
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=AsyncMock()) as parse,
        pytest.raises(SchedulingError) as exc_info,
    ):
        await schedule_task(
            runtime=_scheduling_runtime(),
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@user:server",
            full_text="every hour check logs",
            model="missing-model",
        )
    assert str(exc_info.value).startswith("Unknown model: missing-model.")
    parse.assert_not_awaited()


def _timezone_test_config(runtime_paths: RuntimePaths) -> Config:
    """An install in Los Angeles with one agent, atlas, set to Amsterdam."""
    return bind_runtime_paths(
        Config(
            timezone="America/Los_Angeles",
            agents={"atlas": AgentConfig(display_name="Atlas", role="Test assistant", timezone="Europe/Amsterdam")},
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )


@pytest.mark.asyncio
async def test_a_task_an_agent_schedules_is_parsed_stored_and_shown_in_its_timezone(tmp_path: Path) -> None:
    """The agent's timezone interprets the request, is stored on the task, and describes the task afterwards."""
    client = AsyncMock()
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    room_state: dict[str, dict[str, Any]] = {}
    client.room_get_state = AsyncMock(side_effect=lambda room_id: _room_state_response(room_id, room_state))
    runtime_paths = _test_runtime_paths(tmp_path)
    config = _timezone_test_config(runtime_paths)
    ids = entity_ids(config, runtime_paths)
    # 09:00 in Amsterdam, which is midnight in Los Angeles.
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime(2027, 1, 15, 8, 0, tzinfo=UTC),
        message="@atlas check the deploy",
        description="Deploy check",
    )
    parse = AsyncMock(return_value=workflow)

    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["atlas"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=parse),
        patch.object(ScheduledTaskRuntime, "start_new_task"),
        patch("mindroom.scheduling.tasks.uuid.uuid4", return_value="task1234"),
    ):
        message = await schedule_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=_matrix_room("!test:server"),
                task_runtime=_recording_state_writer(room_state)[0],
                agent_name="atlas",
            ),
            room_id="!test:server",
            thread_id="$thread",
            scheduled_by="@alice:server",
            full_text="tomorrow at 9 check the deploy",
        )

    assert "**Task ID:** `task1234`" in message
    assert parse.await_args.kwargs["timezone"] == "Europe/Amsterdam"
    assert "Scheduled for 2027-01-15 09:00 CET" in message
    [task] = await get_scheduled_tasks_for_room(client=client, room_id="!test:server", runtime_paths=runtime_paths)
    assert task.workflow.timezone == "Europe/Amsterdam"
    assert scheduled_task_timezone(task.workflow, config) == "Europe/Amsterdam"
    listed = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
        thread_id="$thread",
        config=config,
    )
    assert "2027-01-15 09:00 CET" in listed


@pytest.mark.asyncio
async def test_a_stored_task_without_a_timezone_keeps_the_global_timezone(tmp_path: Path) -> None:
    """A task stored without a timezone is shown, and re-parsed on edit, in the global timezone, even by an agent."""
    client = AsyncMock()
    serve_task_state_events(client)
    client.room_put_state = AsyncMock(side_effect=_forbidden_state_write)
    runtime_paths = _test_runtime_paths(tmp_path)
    config = _timezone_test_config(runtime_paths)
    ids = entity_ids(config, runtime_paths)
    stored_workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="17"),
        message="@atlas post the standup",
        description="Standup",
        created_by="@alice:server",
        thread_id="$thread",
        room_id="!test:server",
    ).model_dump(mode="json", exclude={"timezone"})
    room_state: dict[str, dict[str, Any]] = {
        "task1234": {
            "type": SCHEDULED_TASK_EVENT_TYPE,
            "state_key": "task1234",
            "content": {"task_id": "task1234", "status": "pending", "workflow": json.dumps(stored_workflow)},
            "room_id": "!test:server",
            "event_id": "$stored",
            "sender": "@mindroom_router:server",
            "origin_server_ts": 1234567890,
        },
    }
    client.room_get_state = AsyncMock(side_effect=lambda room_id: _room_state_response(room_id, room_state))
    client.room_get_state_event = AsyncMock(
        side_effect=lambda room_id, event_type, state_key: nio.RoomGetStateEventResponse(
            content=room_state[state_key]["content"],
            room_id=room_id,
            event_type=event_type,
            state_key=state_key,
        ),
    )
    [legacy_task] = await get_scheduled_tasks_for_room(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
    )

    assert legacy_task.workflow.timezone is None
    assert scheduled_task_timezone(legacy_task.workflow, config) == "America/Los_Angeles"
    # 17:00 UTC is 09:00 in Los Angeles in winter and 10:00 in summer, never the 18:00 or 19:00 of Amsterdam.
    read_model = build_scheduled_task_read_model(
        legacy_task,
        timezone=scheduled_task_timezone(legacy_task.workflow, config),
        current_time=datetime(2027, 1, 15, tzinfo=UTC),
    )
    assert read_model.cron_description == "At 09:00 (America/Los_Angeles)"

    parse = AsyncMock(side_effect=lambda *_args, **_kwargs: ScheduledWorkflow(**stored_workflow))
    with (
        patch(
            "mindroom.authorization.responder_candidate_entities_with_membership_refresh",
            return_value=[ids["atlas"]],
        ),
        patch("mindroom.scheduling.tasks._extract_mentioned_agents_from_text", return_value=[]),
        patch("mindroom.scheduling.tasks._parse_workflow_schedule", new=parse),
    ):
        result = await edit_scheduled_task(
            runtime=_scheduling_runtime(
                client=client,
                config=config,
                runtime_paths=runtime_paths,
                room=_matrix_room("!test:server"),
                task_runtime=_recording_state_writer(room_state)[0],
                agent_name="atlas",
            ),
            room_id="!test:server",
            task_id="task1234",
            full_text="post the standup at 9 instead",
            scheduled_by="@alice:server",
        )

    assert "Updated task" in result
    assert parse.await_args.kwargs["timezone"] == "America/Los_Angeles"
    [edited_task] = await get_scheduled_tasks_for_room(
        client=client,
        room_id="!test:server",
        runtime_paths=runtime_paths,
    )
    assert edited_task.workflow.timezone is None


@pytest.mark.asyncio
async def test_recurring_task_listing_names_each_tasks_timezone(tmp_path: Path) -> None:
    """Two daily 09:00 tasks in different zones fire hours apart, so every line names its zone, the global one too.

    An agent whose own timezone differs from the global one reads the same listing, so no zone goes unnamed.
    """
    kolkata_task = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="30", hour="3"),
        message="Kolkata standup",
        description="Kolkata standup",
        created_by="@alice:server",
        room_id="!test:server",
        timezone="Asia/Kolkata",
    )
    global_task = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(minute="0", hour="0"),
        message="Global standup",
        description="Global standup",
        created_by="@alice:server",
        room_id="!test:server",
    )
    client = AsyncMock()
    client.room_get_state = AsyncMock(
        return_value=nio.RoomGetStateResponse.from_dict(
            [
                {
                    "type": SCHEDULED_TASK_EVENT_TYPE,
                    "state_key": task_id,
                    "content": {"workflow": workflow.model_dump_json(), "status": "pending"},
                    "event_id": f"$state_{task_id}",
                    "sender": SCHEDULE_WRITER_ID,
                    "origin_server_ts": 1234567890,
                }
                for task_id, workflow in [("kolkata", kolkata_task), ("global", global_task)]
            ],
            room_id="!test:server",
        ),
    )

    listed = await list_scheduled_tasks(
        client=client,
        room_id="!test:server",
        runtime_paths=_test_runtime_paths(tmp_path),
        config=Config(timezone="Asia/Tokyo"),
    )

    assert "• `kolkata` - At 09:00 (Asia/Kolkata)\n" in listed
    assert "• `global` - At 09:00 (Asia/Tokyo)\n" in listed
