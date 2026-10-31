"""The scheduled-task runtime owns every runner and gates each runner's use of its owner's live bot."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, patch

import nio
import pytest
from structlog.testing import capture_logs

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.orchestration.entity_fleet import _FleetShutdown
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.scheduling import runtime as scheduling_runtime
from mindroom.scheduling.executor import ScheduledWorkflowOutcome
from mindroom.scheduling.tasks import (
    ScheduledTaskRecord,
    ScheduledWorkflow,
    _save_pending_scheduled_task,
    cancel_all_scheduled_tasks,
    cancel_scheduled_task,
    get_scheduled_task,
    save_edited_scheduled_task,
)
from tests.conftest import install_bots, make_matrix_client_mock, orchestrator_runtime_paths
from tests.identity_helpers import entity_ids
from tests.scheduling_helpers import (
    persist_schedule_writer,
    scheduled_task_runtime,
    scheduled_task_state_event,
    scheduling_bot,
    serve_task_state_events,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.bot import AgentBot
    from mindroom.constants import RuntimePaths
    from mindroom.scheduling.runtime import ScheduledTaskRuntime

ROOM_ID = "!room:localhost"
TASK_ID = "task1"


@pytest.fixture(autouse=True)
def _fast_owner_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let a waiting runner notice its owner again within one loop turn."""
    monkeypatch.setattr(scheduling_runtime, "_OWNER_POLL_INTERVAL_SECONDS", 0)


def _config_and_ids(tmp_path: Path) -> tuple[Config, RuntimePaths, dict[str, str]]:
    config = Config(agents={"general": AgentConfig(display_name="General")})
    runtime_paths = orchestrator_runtime_paths(tmp_path)
    ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(config, runtime_paths).items()}
    return config, runtime_paths, ids


def _workflow(*, due: bool = True) -> ScheduledWorkflow:
    return ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + (timedelta(seconds=-1) if due else timedelta(hours=1)),
        message="Water the plants",
        description="Plants",
        room_id=ROOM_ID,
        thread_id="$thread",
        created_by="@alice:localhost",
    )


def _record(workflow: ScheduledWorkflow) -> ScheduledTaskRecord:
    return ScheduledTaskRecord(TASK_ID, ROOM_ID, "pending", None, workflow)


class _Bots(dict[str, "AgentBot"]):
    """The fleet's live bots, which a test replaces the way a restart does."""


def _runtime(runtime_paths: RuntimePaths, config: Config, bots: _Bots) -> ScheduledTaskRuntime:
    runtime = scheduled_task_runtime(runtime_paths, config=config)
    runtime.bots = lambda: bots
    return runtime


async def _save_task(runtime: ScheduledTaskRuntime, client: nio.AsyncClient, workflow: ScheduledWorkflow) -> None:
    """Save a pending task as a scheduling command does, without starting its runner."""
    with patch.object(runtime, "_start_runner", return_value=True):
        await _save_pending_scheduled_task(
            client,
            ROOM_ID,
            TASK_ID,
            workflow,
            runtime.config_provider(),
            runtime.runtime_paths,
            task_runtime=runtime,
        )


def _stopped(bot: AgentBot) -> AgentBot:
    bot.running = False
    bot.first_sync_complete = False
    return bot


@pytest.mark.asyncio
async def test_runner_waits_through_its_owners_restart_and_fires_once_on_the_new_client(tmp_path: Path) -> None:
    """A DM reminder whose agent restarts fires once, through the replacement's client, never the closed one."""
    config, runtime_paths, _ids = _config_and_ids(tmp_path)
    old_bot = _stopped(scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general"))
    bots = _Bots(general=old_bot)
    runtime = _runtime(runtime_paths, config, bots)
    workflow = _workflow()
    owner_polled = asyncio.Event()
    real_ready_bot = runtime._ready_bot

    def ready_bot(entity_name: str) -> AgentBot | None:
        owner_polled.set()
        return real_ready_bot(entity_name)

    with (
        patch.object(runtime, "_ready_bot", side_effect=ready_bot),
        patch("mindroom.scheduling.runtime._get_pending_task_record", new=AsyncMock(return_value=_record(workflow))),
        patch("mindroom.scheduling.runtime._scheduled_task_creator_is_joined", new=AsyncMock(return_value=True)),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute,
        patch("mindroom.scheduling.runtime._save_task_status", new=AsyncMock()) as save_status,
    ):
        assert runtime._start_runner("general", TASK_ID, workflow)
        _runner, task = runtime._runners[TASK_ID]
        await owner_polled.wait()
        execute.assert_not_awaited()
        new_bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
        bots["general"] = new_bot
        await task

    execute.assert_awaited_once()
    assert execute.await_args.args[0] is new_bot.client
    assert save_status.await_args.args[0] is new_bot.client
    assert save_status.await_args.args[2] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["first_sync_complete", "running"])
async def test_overdue_task_waits_until_its_owner_runs_and_finished_its_first_sync(
    tmp_path: Path,
    missing: str,
) -> None:
    """A task restored before its owner synced, or while it is not running, fires only once both hold."""
    config, runtime_paths, _ids = _config_and_ids(tmp_path)
    bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
    setattr(bot, missing, False)
    runtime = _runtime(runtime_paths, config, _Bots(general=bot))
    workflow = _workflow()
    owner_polled = asyncio.Event()
    real_ready_bot = runtime._ready_bot

    def ready_bot(entity_name: str) -> AgentBot | None:
        owner_polled.set()
        return real_ready_bot(entity_name)

    with (
        patch.object(runtime, "_ready_bot", side_effect=ready_bot),
        patch("mindroom.scheduling.runtime._get_pending_task_record", new=AsyncMock(return_value=_record(workflow))),
        patch("mindroom.scheduling.runtime._scheduled_task_creator_is_joined", new=AsyncMock(return_value=True)),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(return_value=ScheduledWorkflowOutcome(status="delivered")),
        ) as execute,
        patch("mindroom.scheduling.runtime._save_task_status", new=AsyncMock()),
    ):
        assert runtime._start_runner("general", TASK_ID, workflow)
        _runner, task = runtime._runners[TASK_ID]
        await owner_polled.wait()
        execute.assert_not_awaited()
        setattr(bot, missing, True)
        await task

    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_release_fences_the_owner_then_waits_for_its_in_flight_operation(tmp_path: Path) -> None:
    """A stopping owner admits no new operation, and its stop waits until the operation in flight ends."""
    config, runtime_paths, _ids = _config_and_ids(tmp_path)
    bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(general=bot))
    operation_started = asyncio.Event()
    finish_operation = asyncio.Event()

    async def in_flight_operation() -> None:
        async with runtime.operation("general"):
            operation_started.set()
            await finish_operation.wait()

    operation = asyncio.create_task(in_flight_operation())
    await operation_started.wait()
    release = asyncio.create_task(runtime.release({"general"}))
    await asyncio.sleep(0)

    assert runtime._ready_bot("general") is None
    assert bot in runtime._fenced
    assert not release.done()
    finish_operation.set()
    await release
    await operation


@pytest.mark.asyncio
async def test_release_stops_waiting_at_its_bound(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An operation that outlives the stop budget does not hold the owner's stop."""
    config, runtime_paths, _ids = _config_and_ids(tmp_path)
    monkeypatch.setattr(scheduling_runtime, "SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS", 0.01)
    bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(general=bot))
    operation_started = asyncio.Event()
    finish_operation = asyncio.Event()

    async def stuck_operation() -> None:
        async with runtime.operation("general"):
            operation_started.set()
            await finish_operation.wait()

    operation = asyncio.create_task(stuck_operation())
    await operation_started.wait()

    await runtime.release({"general"})

    assert not operation.done()
    finish_operation.set()
    await operation


@pytest.mark.asyncio
async def test_one_time_send_cut_off_by_its_owners_stop_is_failed_and_never_resent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-time trigger whose client closed under it is marked failed once the owner runs again."""
    config, runtime_paths, _ids = _config_and_ids(tmp_path)
    monkeypatch.setattr(scheduling_runtime, "SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS", 0.01)
    old_bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
    bots = _Bots(general=old_bot)
    runtime = _runtime(runtime_paths, config, bots)
    workflow = _workflow()
    send_started = asyncio.Event()
    client_closed = asyncio.Event()

    async def send_cut_off(*_args: object, **_kwargs: object) -> ScheduledWorkflowOutcome:
        send_started.set()
        await client_closed.wait()
        return ScheduledWorkflowOutcome(status="failed", failure_reason="client closed")

    with (
        patch("mindroom.scheduling.runtime._get_pending_task_record", new=AsyncMock(return_value=_record(workflow))),
        patch("mindroom.scheduling.runtime._scheduled_task_creator_is_joined", new=AsyncMock(return_value=True)),
        patch(
            "mindroom.scheduling.executor.execute_scheduled_workflow",
            new=AsyncMock(side_effect=send_cut_off),
        ) as send,
        patch("mindroom.scheduling.runtime._save_task_status", new=AsyncMock()) as save_status,
    ):
        assert runtime._start_runner("general", TASK_ID, workflow)
        _runner, task = runtime._runners[TASK_ID]
        await send_started.wait()
        await runtime.release({"general"})
        bots["general"] = new_bot = scheduling_bot(
            make_matrix_client_mock(user_id="@general:localhost"),
            agent_name="general",
        )
        client_closed.set()
        await task

    send.assert_awaited_once()
    save_status.assert_awaited_once()
    assert save_status.await_args.args[0] is new_bot.client
    assert save_status.await_args.args[2] == "failed"


@pytest.mark.asyncio
async def test_status_write_cut_off_by_its_owners_stop_is_written_again_by_the_replacement(tmp_path: Path) -> None:
    """After a send, only the status write repeats when the owner's stop closed its client under it."""
    config, runtime_paths, _ids = _config_and_ids(tmp_path)
    old_bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
    bots = _Bots(general=old_bot)
    runtime = _runtime(runtime_paths, config, bots)
    new_bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
    writers: list[object] = []

    async def write_status(client: object, *_args: object, **_kwargs: object) -> None:
        writers.append(client)
        if client is old_bot.client:
            runtime._fenced.add(old_bot)
            bots["general"] = new_bot
            msg = "client closed"
            raise RuntimeError(msg)

    with patch("mindroom.scheduling.runtime._save_task_status", new=AsyncMock(side_effect=write_status)):
        await scheduling_runtime._TaskRunner(runtime, "general", ROOM_ID, TASK_ID)._save_status(
            _record(_workflow()),
            "completed",
        )

    assert writers == [old_bot.client, new_bot.client]


def _pending_record(workflow: ScheduledWorkflow) -> Callable[..., Awaitable[ScheduledTaskRecord]]:
    async def read(_client: object, _room_id: str, task_id: str, _paths: object) -> ScheduledTaskRecord:
        return ScheduledTaskRecord(task_id, ROOM_ID, "pending", None, workflow)

    return read


@pytest.mark.asyncio
async def test_removed_agent_fails_each_task_it_runs_and_says_so_where_it_posts(tmp_path: Path) -> None:
    """A removed agent's tasks are never handed to another bot; each one it runs is failed with a notice."""
    config, runtime_paths, ids = _config_and_ids(tmp_path)
    removed_bot = scheduling_bot(make_matrix_client_mock(user_id=ids["general"]), agent_name="general")
    router_bot = scheduling_bot(make_matrix_client_mock(user_id=ids["router"]))
    runtime = _runtime(runtime_paths, config, _Bots(general=removed_bot, router=router_bot))
    workflow = _workflow(due=False)

    async def wait_forever(*_args: object) -> None:
        await asyncio.Event().wait()

    with patch.object(scheduling_runtime._TaskRunner, "run_once", wait_forever):
        assert runtime._start_runner("general", TASK_ID, workflow)
        assert runtime._start_runner("router", "router_task", workflow)
    _runner, removed_task = runtime._runners[TASK_ID]

    with (
        patch("mindroom.scheduling.runtime._get_pending_task_record", new=_pending_record(workflow)),
        patch("mindroom.scheduling.runtime._save_task_status", new=AsyncMock()) as save_status,
        patch("mindroom.scheduling.executor.send_scheduled_failure_notice", new=AsyncMock()) as notice,
    ):
        await runtime.fail_removed({"general"})

    assert removed_task.cancelled()
    assert list(runtime._runners) == ["router_task"]
    save_status.assert_awaited_once()
    assert save_status.await_args.args[0] is removed_bot.client
    assert save_status.await_args.args[1].task_id == TASK_ID
    assert save_status.await_args.args[2] == "failed"
    notice.assert_awaited_once()
    assert notice.await_args.args[0] is removed_bot.client
    assert notice.await_args.args[1].resolved_thread_id == "$thread"
    assert "❌ Schedule failed because its agent was removed: " in notice.await_args.args[2]
    await runtime.stop()


@pytest.mark.asyncio
async def test_removal_settles_every_task_within_one_bound_however_many_the_agent_runs(tmp_path: Path) -> None:
    """Ordinary homeserver latency per task must not leave a removed agent's later tasks pending without a notice."""
    config, runtime_paths, ids = _config_and_ids(tmp_path)
    removed_bot = scheduling_bot(make_matrix_client_mock(user_id=ids["general"]), agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(general=removed_bot))
    workflow = _workflow(due=False)
    task_ids = [f"task_{index}" for index in range(4)]
    read = _pending_record(workflow)

    async def wait_forever(*_args: object) -> None:
        await asyncio.Event().wait()

    with patch.object(scheduling_runtime._TaskRunner, "run_once", wait_forever):
        for task_id in task_ids:
            assert runtime._start_runner("general", task_id, workflow)

    async def slow_read(client: object, room_id: str, task_id: str, paths: object) -> ScheduledTaskRecord:
        # Each settlement takes well over a quarter of the bound, so settling one after another would overrun it.
        await asyncio.sleep(0.15)
        return await read(client, room_id, task_id, paths)

    with (
        patch.object(scheduling_runtime, "SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS", 0.4),
        patch("mindroom.scheduling.runtime._get_pending_task_record", new=slow_read),
        patch("mindroom.scheduling.runtime._save_task_status", new=AsyncMock()) as save_status,
        patch("mindroom.scheduling.executor.send_scheduled_failure_notice", new=AsyncMock()) as notice,
    ):
        await asyncio.wait_for(runtime.fail_removed({"general"}), timeout=5)

    assert save_status.await_count == len(task_ids)
    assert notice.await_count == len(task_ids)
    await runtime.stop()


@pytest.mark.asyncio
async def test_removal_stops_waiting_for_a_stalled_homeserver_and_leaves_the_task_pending(tmp_path: Path) -> None:
    """A removal that cannot settle its tasks in time lets the reload go on and leaves the task pending."""
    config, runtime_paths, _ids = _config_and_ids(tmp_path)
    removed_bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(general=removed_bot))
    workflow = _workflow(due=False)

    async def wait_forever(*_args: object, **_kwargs: object) -> None:
        await asyncio.Event().wait()

    with patch.object(scheduling_runtime._TaskRunner, "run_once", wait_forever):
        assert runtime._start_runner("general", TASK_ID, workflow)

    with (
        patch.object(scheduling_runtime, "SYNC_SHUTDOWN_PREPARATION_TIMEOUT_SECONDS", 0.05),
        patch("mindroom.scheduling.runtime._get_pending_task_record", new=wait_forever),
        patch("mindroom.scheduling.executor.send_scheduled_failure_notice", new=AsyncMock()) as notice,
    ):
        await asyncio.wait_for(runtime.fail_removed({"general"}), timeout=5)

    assert runtime._runners == {}
    notice.assert_not_awaited()
    await runtime.stop()


@pytest.mark.asyncio
async def test_restore_names_a_room_whose_state_it_cannot_read(tmp_path: Path) -> None:
    """A room whose state a joining bot cannot read starts no runner, and the log says which room."""
    config, runtime_paths, ids = _config_and_ids(tmp_path)
    bot = scheduling_bot(make_matrix_client_mock(user_id=ids["general"]), agent_name="general")
    bot.client.room_get_state = AsyncMock(return_value=nio.RoomGetStateError("unavailable", "M_UNKNOWN"))
    runtime = _runtime(runtime_paths, config, _Bots(general=bot))

    with capture_logs() as logs:
        restored = await runtime.restore_room(bot, ROOM_ID)

    assert restored == 0
    assert not runtime._runners
    assert [log["room_id"] for log in logs if log["event"] == "scheduled_task_room_state_unread"] == [ROOM_ID]


def _room_state(writer_id: str, agent_id: str, task_content: dict[str, Any], *, agent_level: int) -> list[dict]:
    """Return a room's full state: one agent joined at a power level, no router, and a task ``writer_id`` wrote."""

    def state(event_type: str, state_key: str, content: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": event_type,
            "state_key": state_key,
            "content": content,
            "sender": agent_id,
            "event_id": f"${event_type}:{state_key}",
            "origin_server_ts": 1,
            "room_id": ROOM_ID,
        }

    return [
        state("m.room.member", agent_id, {"membership": "join"}),
        state("m.room.power_levels", "", {"users": {agent_id: agent_level}, "state_default": 50}),
        scheduled_task_state_event(TASK_ID, task_content, room_id=ROOM_ID, sender=writer_id),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("agent_may_write", [True, False])
async def test_restore_fails_a_task_whose_owner_was_removed_while_mindroom_was_down(
    tmp_path: Path,
    agent_may_write: bool,
) -> None:
    """A restoring bot that may write schedule state fails a task whose owner is no longer configured."""
    config, runtime_paths, ids = _config_and_ids(tmp_path)
    retired_id = "@mindroom_retired:localhost"
    persist_schedule_writer(runtime_paths, retired_id, entity_name="retired")
    workflow = _workflow(due=False)
    content = {"task_id": TASK_ID, "workflow": workflow.model_dump_json(), "status": "pending"}
    client = make_matrix_client_mock(user_id=ids["general"])
    client.room_get_state.return_value = nio.RoomGetStateResponse.from_dict(
        _room_state(retired_id, ids["general"], content, agent_level=100 if agent_may_write else 0),
        room_id=ROOM_ID,
    )

    async def read_task(room_id: str, event_type: str, state_key: str = "") -> nio.RoomGetStateEventResponse:
        return nio.RoomGetStateEventResponse(dict(content), event_type, state_key, room_id)

    client.room_get_state_event.side_effect = read_task
    client.room_put_state.return_value = nio.RoomPutStateResponse("$failed", ROOM_ID)
    serve_task_state_events(client, sender=retired_id)
    bot = scheduling_bot(client, agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(general=bot))

    with patch("mindroom.scheduling.executor.send_scheduled_failure_notice", new=AsyncMock()) as notice:
        restored = await runtime.restore_room(bot, ROOM_ID)

    assert restored == 0
    assert not runtime._runners
    if agent_may_write:
        client.room_put_state.assert_awaited_once()
        assert client.room_put_state.await_args.kwargs["content"]["status"] == "failed"
        notice.assert_awaited_once()
        assert "❌ Schedule failed because its agent was removed: " in notice.await_args.args[2]
    else:
        client.room_put_state.assert_not_awaited()
        notice.assert_not_awaited()


@pytest.mark.asyncio
async def test_restore_by_a_bot_that_does_not_own_the_task_starts_no_runner(tmp_path: Path) -> None:
    """Only the owner the rule picks starts a task's runner; another bot joining the room leaves it alone."""
    config, runtime_paths, ids = _config_and_ids(tmp_path)
    workflow = _workflow(due=False)
    content = {"task_id": TASK_ID, "workflow": workflow.model_dump_json(), "status": "pending"}
    # The router is not joined, so the agent that wrote the task owns it.
    state = _room_state(ids["general"], ids["general"], content, agent_level=100)
    router_client = make_matrix_client_mock(user_id=ids["router"])
    router_client.room_get_state.return_value = nio.RoomGetStateResponse.from_dict(state, room_id=ROOM_ID)
    agent_client = make_matrix_client_mock(user_id=ids["general"])
    agent_client.room_get_state.return_value = nio.RoomGetStateResponse.from_dict(state, room_id=ROOM_ID)
    router = scheduling_bot(router_client)
    agent = scheduling_bot(agent_client, agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(router=router, general=agent))

    with patch.object(runtime, "_start_runner", return_value=True) as start:
        assert await runtime.restore_room(router, ROOM_ID) == 0
        assert await runtime.restore_room(agent, ROOM_ID) == 1

    start.assert_called_once_with("general", TASK_ID, workflow)


class _SharedRoom:
    """One room's full state, read and written by every bot's client the way a homeserver would."""

    def __init__(self, members: dict[str, int]) -> None:
        self.events: dict[tuple[str, str], dict[str, Any]] = {}
        self._put("m.room.power_levels", "", {"users": members, "state_default": 50}, next(iter(members)))
        for member in members:
            self._put("m.room.member", member, {"membership": "join"}, member)

    def _put(self, event_type: str, state_key: str, content: dict[str, Any], sender: str) -> None:
        self.events[event_type, state_key] = {
            "type": event_type,
            "state_key": state_key,
            "content": content,
            "sender": sender,
            "event_id": f"${event_type}:{state_key}:{len(self.events)}",
            "origin_server_ts": 1,
            "room_id": ROOM_ID,
        }

    def task(self) -> dict[str, Any]:
        return self.events["com.mindroom.scheduled.task", TASK_ID]

    def client(self, user_id: str) -> AsyncMock:
        """Return a client whose synced view of the room has no members, as a lazy-loading sync leaves it."""
        client = make_matrix_client_mock(user_id=user_id)
        client.rooms = {ROOM_ID: nio.MatrixRoom(ROOM_ID, user_id)}

        async def get_state(room_id: str) -> nio.RoomGetStateResponse:
            return nio.RoomGetStateResponse.from_dict(list(self.events.values()), room_id=room_id)

        async def get_state_event(room_id: str, event_type: str, state_key: str = "") -> nio.RoomGetStateEventResponse:
            if event_type == "m.room.member":
                return nio.RoomGetStateEventResponse({"membership": "join"}, event_type, state_key, room_id)
            return nio.RoomGetStateEventResponse(
                dict(self.events[event_type, state_key]["content"]),
                event_type,
                state_key,
                room_id,
            )

        async def put_state(*, room_id: str, event_type: str, content: dict[str, Any], state_key: str) -> object:
            self._put(event_type, state_key, dict(content), user_id)
            return nio.RoomPutStateResponse(f"${state_key}", room_id)

        client.room_get_state.side_effect = get_state
        client.room_get_state_event.side_effect = get_state_event
        client.room_put_state.side_effect = put_state
        serve_task_state_events(client, sender=user_id)
        return client


def _two_agents(tmp_path: Path) -> tuple[Config, RuntimePaths, dict[str, str]]:
    config = Config(
        agents={"general": AgentConfig(display_name="General"), "helper": AgentConfig(display_name="Helper")},
    )
    runtime_paths = orchestrator_runtime_paths(tmp_path)
    ids = {name: matrix_id.full_id for name, matrix_id in entity_ids(config, runtime_paths).items()}
    return config, runtime_paths, ids


async def _wait_forever(*_args: object) -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_creation_and_restore_pick_the_router_from_full_state_that_a_synced_view_lacks(tmp_path: Path) -> None:
    """A lazy-loaded synced view without the router cannot make creation and restore disagree on the owner."""
    config, runtime_paths, ids = _two_agents(tmp_path)
    room = _SharedRoom({ids["router"]: 100, ids["general"]: 100})
    router = scheduling_bot(room.client(ids["router"]))
    agent = scheduling_bot(room.client(ids["general"]), agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(router=router, general=agent))
    workflow = _workflow(due=False)

    with patch.object(runtime, "_start_runner", return_value=True) as start:
        await _save_pending_scheduled_task(
            agent.client,
            ROOM_ID,
            TASK_ID,
            workflow,
            config,
            runtime_paths,
            task_runtime=runtime,
        )
        restored_by_agent = await runtime.restore_room(agent, ROOM_ID)
        restored_by_router = await runtime.restore_room(router, ROOM_ID)

    assert room.task()["content"]["owner"] == ids["router"]
    assert room.task()["sender"] == ids["general"]
    assert (restored_by_agent, restored_by_router) == (0, 1)
    assert [call.args[0] for call in start.call_args_list] == ["router", "router"]


@pytest.mark.asyncio
async def test_an_edit_by_another_agent_keeps_the_owner_its_runner_and_who_fails_with_removal(tmp_path: Path) -> None:
    """Editing never moves a task: removing the editor leaves it running, and removing its owner fails it."""
    config, runtime_paths, ids = _two_agents(tmp_path)
    room = _SharedRoom({ids["general"]: 100, ids["helper"]: 100})
    owner = scheduling_bot(room.client(ids["general"]), agent_name="general")
    editor = scheduling_bot(room.client(ids["helper"]), agent_name="helper")
    runtime = _runtime(runtime_paths, config, _Bots(general=owner, helper=editor))
    workflow = _workflow(due=False)

    with patch.object(scheduling_runtime._TaskRunner, "run_once", _wait_forever):
        await _save_pending_scheduled_task(
            owner.client,
            ROOM_ID,
            TASK_ID,
            workflow,
            config,
            runtime_paths,
            task_runtime=runtime,
        )
        existing = await get_scheduled_task(editor.client, ROOM_ID, TASK_ID, runtime_paths)
        assert existing is not None
        await save_edited_scheduled_task(
            editor.client,
            workflow.model_copy(update={"message": "Water the plants twice"}),
            existing,
            runtime_paths,
            config=Config(),
            writers=runtime,
        )
        assert await runtime.restore_room(editor, ROOM_ID) == 0
    runner, task = runtime._runners[TASK_ID]

    assert room.task()["sender"] == ids["helper"]
    assert room.task()["content"]["owner"] == ids["general"]
    assert runner.owner == "general"

    with patch("mindroom.scheduling.executor.send_scheduled_failure_notice", new=AsyncMock()) as notice:
        await runtime.fail_removed({"helper"})
        assert runtime._runners[TASK_ID][1] is task
        assert room.task()["content"]["status"] == "pending"
        notice.assert_not_awaited()

        await runtime.fail_removed({"general"})

    assert task.cancelled()
    assert room.task()["content"]["status"] == "failed"
    assert room.task()["content"]["owner"] == ids["general"]
    notice.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelling_a_task_through_another_agent_keeps_its_stored_owner(tmp_path: Path) -> None:
    """Every rewrite of a task keeps the owner it was saved with, whichever bot writes it."""
    config, runtime_paths, ids = _two_agents(tmp_path)
    room = _SharedRoom({ids["general"]: 100, ids["helper"]: 100})
    owner = scheduling_bot(room.client(ids["general"]), agent_name="general")
    other = scheduling_bot(room.client(ids["helper"]), agent_name="helper")
    runtime = _runtime(runtime_paths, config, _Bots(general=owner, helper=other))
    await _save_task(runtime, owner.client, _workflow(due=False))

    result = await cancel_scheduled_task(other.client, ROOM_ID, TASK_ID, runtime_paths, task_runtime=runtime)

    assert result == f"✅ Cancelled task `{TASK_ID}`"
    assert room.task()["sender"] == ids["helper"]
    assert room.task()["content"]["status"] == "cancelled"
    assert room.task()["content"]["owner"] == ids["general"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_all", [False, True])
async def test_a_cancel_during_an_edit_leaves_the_task_cancelled(tmp_path: Path, cancel_all: bool) -> None:
    """A cancellation waits for an edit that is still writing, so the edit's write cannot bring the task back."""
    config, runtime_paths, ids = _two_agents(tmp_path)
    room = _SharedRoom({ids["general"]: 100, ids["helper"]: 100})
    owner = scheduling_bot(room.client(ids["general"]), agent_name="general")
    editor = scheduling_bot(room.client(ids["helper"]), agent_name="helper")
    runtime = _runtime(runtime_paths, config, _Bots(general=owner, helper=editor))
    workflow = _workflow(due=False)
    await _save_task(runtime, owner.client, workflow)
    existing = await get_scheduled_task(editor.client, ROOM_ID, TASK_ID, runtime_paths)
    assert existing is not None
    edit_writing = asyncio.Event()
    finish_edit = asyncio.Event()
    cancel_started = asyncio.Event()
    write_state = editor.client.room_put_state.side_effect

    async def slow_edit_write(**kwargs: Any) -> object:  # noqa: ANN401
        edit_writing.set()
        await finish_edit.wait()
        return await write_state(**kwargs)

    editor.client.room_put_state.side_effect = slow_edit_write

    async def cancel() -> str:
        cancel_started.set()
        if cancel_all:
            return await cancel_all_scheduled_tasks(owner.client, ROOM_ID, runtime_paths, task_runtime=runtime)
        return await cancel_scheduled_task(owner.client, ROOM_ID, TASK_ID, runtime_paths, task_runtime=runtime)

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        tasks.create_task(
            save_edited_scheduled_task(
                editor.client,
                workflow.model_copy(update={"message": "Water the plants twice"}),
                existing,
                runtime_paths,
                config=Config(),
                writers=runtime,
            ),
        )
        await edit_writing.wait()
        cancelled = tasks.create_task(cancel())
        await cancel_started.wait()
        finish_edit.set()

    assert cancelled.result().startswith("✅ Cancelled")
    assert room.task()["content"]["status"] == "cancelled"
    assert ScheduledWorkflow.model_validate_json(room.task()["content"]["workflow"]).message == "Water the plants twice"


@pytest.mark.asyncio
async def test_restore_waits_for_an_edit_still_rescheduling_an_overdue_task(tmp_path: Path) -> None:
    """Restore fails an overdue task only under its lock, so an edit still writing a reschedule wins and gets a runner."""
    config, runtime_paths, ids = _two_agents(tmp_path)
    room = _SharedRoom({ids["general"]: 100, ids["helper"]: 100})
    agent = scheduling_bot(room.client(ids["general"]), agent_name="general")
    editor = scheduling_bot(room.client(ids["helper"]), agent_name="helper")
    runtime = _runtime(runtime_paths, config, _Bots(general=agent, helper=editor))
    overdue = _workflow(due=False).model_copy(update={"execute_at": datetime.now(UTC) - timedelta(days=30)})
    await _save_task(runtime, agent.client, overdue)
    existing = await get_scheduled_task(editor.client, ROOM_ID, TASK_ID, runtime_paths)
    assert existing is not None
    edit_writing = asyncio.Event()
    finish_edit = asyncio.Event()
    write_state = editor.client.room_put_state.side_effect

    async def slow_edit_write(**kwargs: Any) -> object:  # noqa: ANN401
        edit_writing.set()
        await finish_edit.wait()
        return await write_state(**kwargs)

    editor.client.room_put_state.side_effect = slow_edit_write
    restore_waiting = asyncio.Event()
    lock = scheduling_runtime.scheduled_task_state_lock

    def observed_lock(client: object, room_id: str, task_id: str) -> asyncio.Lock:
        restore_waiting.set()
        return lock(client, room_id, task_id)

    with (
        patch.object(scheduling_runtime, "scheduled_task_state_lock", observed_lock),
        patch.object(runtime, "_start_runner", return_value=True) as start_runner,
    ):
        async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
            tasks.create_task(
                save_edited_scheduled_task(
                    editor.client,
                    overdue.model_copy(update={"execute_at": datetime.now(UTC) + timedelta(hours=1)}),
                    existing,
                    runtime_paths,
                    config=Config(),
                    writers=runtime,
                ),
            )
            await edit_writing.wait()
            restoring = tasks.create_task(runtime.restore_room(agent, ROOM_ID))
            waiting = tasks.create_task(restore_waiting.wait())
            await asyncio.wait({restoring, waiting}, return_when=asyncio.FIRST_COMPLETED)
            waiting.cancel()
            finish_edit.set()

    assert restoring.result() == 1
    start_runner.assert_called_once()
    assert room.task()["content"]["status"] == "pending"


@pytest.mark.asyncio
async def test_restore_keeps_the_stored_owner_when_the_room_would_now_pick_another(tmp_path: Path) -> None:
    """A task's owner is decided once: a router that joins and gains power later does not take it over."""
    config, runtime_paths, ids = _two_agents(tmp_path)
    room = _SharedRoom({ids["router"]: 0, ids["general"]: 100})
    router = scheduling_bot(room.client(ids["router"]))
    agent = scheduling_bot(room.client(ids["general"]), agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(router=router, general=agent))
    workflow = _workflow(due=False)
    await _save_task(runtime, agent.client, workflow)
    room.events["m.room.power_levels", ""]["content"]["users"][ids["router"]] = 100

    with patch.object(runtime, "_start_runner", return_value=True) as start:
        assert await runtime.restore_room(router, ROOM_ID) == 0
        assert await runtime.restore_room(agent, ROOM_ID) == 1

    assert room.task()["content"]["owner"] == ids["general"]
    start.assert_called_once_with("general", TASK_ID, workflow)


@pytest.mark.asyncio
@pytest.mark.parametrize(("router_level", "owner"), [(100, "router"), (0, "general")])
async def test_task_saved_without_an_owner_restores_under_the_rule_over_its_writer(
    tmp_path: Path,
    router_level: int,
    owner: str,
) -> None:
    """A task saved before owners were stored runs under the router where it can serve the room, else its writer."""
    config, runtime_paths, ids = _two_agents(tmp_path)
    room = _SharedRoom({ids["router"]: router_level, ids["general"]: 100})
    workflow = _workflow(due=False)
    room._put(
        "com.mindroom.scheduled.task",
        TASK_ID,
        {"task_id": TASK_ID, "workflow": workflow.model_dump_json(), "status": "pending"},
        ids["general"],
    )
    router = scheduling_bot(room.client(ids["router"]))
    agent = scheduling_bot(room.client(ids["general"]), agent_name="general")
    runtime = _runtime(runtime_paths, config, _Bots(router=router, general=agent))

    with patch.object(runtime, "_start_runner", return_value=True) as start:
        restored = {bot.agent_name: await runtime.restore_room(bot, ROOM_ID) for bot in (router, agent)}
        existing = await get_scheduled_task(agent.client, ROOM_ID, TASK_ID, runtime_paths)
        assert existing is not None
        await save_edited_scheduled_task(
            agent.client,
            workflow,
            existing,
            runtime_paths,
            config=Config(),
        )

    assert restored == {"router": int(owner == "router"), "general": int(owner == "general")}
    start.assert_called_once_with(owner, TASK_ID, workflow)
    # The owner is never written back to a task saved without one.
    assert "owner" not in room.task()["content"]


@pytest.mark.asyncio
async def test_orchestrator_fences_changing_entities_and_fails_removed_entities_tasks(tmp_path: Path) -> None:
    """The fleet's change hook fences the stopping bots; its removal hook fails their tasks before approvals settle."""
    orchestrator = _MultiAgentOrchestrator(
        runtime_paths=orchestrator_runtime_paths(tmp_path),
        config=Config(agents={"general": AgentConfig(display_name="General")}),
    )
    bot = scheduling_bot(make_matrix_client_mock(user_id="@general:localhost"), agent_name="general")
    install_bots(orchestrator, {"general": bot})
    order: list[str] = []

    async def fail_removed(names: set[str]) -> None:
        order.append(f"scheduled tasks {sorted(names)}")

    async def reconcile_approvals(names: set[str]) -> set[str]:
        order.append(f"approvals {sorted(names)}")
        return set()

    await orchestrator._entities_changing({"general"})
    with (
        patch.object(orchestrator.scheduled_tasks, "fail_removed", side_effect=fail_removed),
        patch.object(orchestrator._approval_recovery, "reconcile_removed_entities", side_effect=reconcile_approvals),
    ):
        unresolved = await orchestrator._reconcile_removed_entities({"general"})

    assert bot in orchestrator.scheduled_tasks._fenced
    assert unresolved == set()
    assert order == ["scheduled tasks ['general']", "approvals ['general']"]


@pytest.mark.asyncio
async def test_process_shutdown_cancels_runners_before_any_bot_stops(tmp_path: Path) -> None:
    """Runners end before bots stop, so none uses a client process shutdown closes."""
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path), config=Config())
    order: list[str] = []
    runner_cancelled = asyncio.Event()

    async def wait_forever(*_args: object) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            runner_cancelled.set()

    with patch.object(scheduling_runtime._TaskRunner, "run_once", wait_forever):
        assert orchestrator.scheduled_tasks._start_runner("router", TASK_ID, _workflow(due=False))
    await asyncio.sleep(0)

    async def stop_all() -> _FleetShutdown:
        order.append(f"fleet stop, runner cancelled: {runner_cancelled.is_set()}")
        return _FleetShutdown(failures=[], owns_resources=False)

    with patch.object(orchestrator.fleet, "stop_all", side_effect=stop_all):
        await orchestrator.stop()

    assert order == ["fleet stop, runner cancelled: True"]
    assert not orchestrator.scheduled_tasks._runners
    assert not orchestrator.scheduled_tasks._start_runner("router", "late", _workflow(due=False))
