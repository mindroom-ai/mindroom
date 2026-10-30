"""Test helpers for scheduled-task state written by MindRoom bot accounts."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, unquote, urlsplit

import nio

from mindroom.bot import AgentBot
from mindroom.constants import ROUTER_AGENT_NAME, resolve_runtime_paths
from mindroom.hooks import HookRegistry, HookRegistryState
from mindroom.matrix.identity import MatrixID, managed_account_key
from mindroom.matrix.state import MatrixState
from mindroom.scheduling import runtime as scheduling_runtime
from mindroom.scheduling.runtime import ScheduledTaskRuntime, _TaskRunner

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.hooks import HookMatrixAdmin, HookRegistryLifetime
    from mindroom.matrix.conversation_reads import ConversationReader
    from mindroom.scheduling.tasks import ScheduledTaskRecord, ScheduledWorkflow

SCHEDULE_WRITER_ID = "@mindroom_router:server"
SCHEDULED_TASK_EVENT_TYPE = "com.mindroom.scheduled.task"
_STATE_EVENT_PATH_PREFIX = "/_matrix/client/v3/rooms/"


def persist_schedule_writer(
    runtime_paths: RuntimePaths,
    writer_id: str = SCHEDULE_WRITER_ID,
    *,
    entity_name: str = ROUTER_AGENT_NAME,
) -> None:
    """Persist one managed bot account as ``writer_id`` so the scheduler trusts its task state."""
    matrix_id = MatrixID.parse(writer_id)
    state = MatrixState.load(runtime_paths=runtime_paths)
    state.add_account(managed_account_key(entity_name), matrix_id.username, "test-password", domain=matrix_id.domain)
    state.save(runtime_paths=runtime_paths)


def schedule_runtime_paths(tmp_path: Path, writer_id: str = SCHEDULE_WRITER_ID) -> RuntimePaths:
    """Return isolated runtime paths whose persisted router account writes scheduled-task state."""
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={},
    )
    persist_schedule_writer(runtime_paths, writer_id)
    return runtime_paths


def scheduled_task_state_event(
    task_id: str,
    content: dict[str, Any],
    *,
    room_id: str = "!test:server",
    sender: str = SCHEDULE_WRITER_ID,
) -> dict[str, Any]:
    """Return one scheduled-task state event as full room-state reads report it."""
    return {
        "type": SCHEDULED_TASK_EVENT_TYPE,
        "state_key": task_id,
        "content": content,
        "sender": sender,
        "event_id": f"$state_{task_id}",
        "origin_server_ts": 1234567890,
        "room_id": room_id,
    }


def room_create_state_response(room_id: str, *, full_event: bool) -> nio.RoomGetStateEventResponse:
    """Answer the scheduler's homeserver check on a room's create event as a server honouring ``format=event``."""
    content = {"room_version": "11"}
    body = (
        {
            "type": "m.room.create",
            "state_key": "",
            "content": content,
            "sender": SCHEDULE_WRITER_ID,
            "event_id": "$create",
            "origin_server_ts": 1,
            "room_id": room_id,
        }
        if full_event
        else content
    )
    return nio.RoomGetStateEventResponse(body, "m.room.create", "", room_id)


def serve_full_room_state(client: Any, room_id: str, events: list[dict[str, Any]] | None = None) -> None:  # noqa: ANN401
    """Answer the full room-state read that decides a new task's owner; without events no router has joined."""
    client.room_get_state = AsyncMock(return_value=nio.RoomGetStateResponse.from_dict(events or [], room_id=room_id))


def joined_member_state(room_id: str, event_type: str, state_key: str = "") -> nio.RoomGetStateEventResponse:
    """Report every requested member as joined, for runners that check creator membership."""
    assert event_type == "m.room.member"
    return nio.RoomGetStateEventResponse({"membership": "join"}, event_type, state_key, room_id)


def serve_task_state_events(client: Any, *, sender: str = SCHEDULE_WRITER_ID) -> AsyncMock:  # noqa: ANN401
    """Answer the scheduler's ``format=event`` task reads from the client's ``room_get_state_event`` mock.

    Homeservers return the whole state event for ``format=event``.
    This wraps each mocked state content in that envelope with ``sender``, so the existing
    ``room_get_state_event`` mock stays the single source of room state and its call count
    still measures state-event requests.
    The scheduler's check of the room's create event is answered as a homeserver honouring ``format=event``.
    Other private sends fall through to the previous mock.
    """
    fallback = client._send

    async def send(response_class: type, method: str, path: str, *args: object, **kwargs: object) -> object:
        url = urlsplit(path)
        full_event = parse_qs(url.query) == {"format": ["event"]}
        parts = [unquote(part) for part in url.path.removeprefix(_STATE_EVENT_PATH_PREFIX).split("/")]
        if url.path.startswith(_STATE_EVENT_PATH_PREFIX) and parts[1:] == ["state", "m.room.create"]:
            return room_create_state_response(parts[0], full_event=full_event)
        if not full_event or not url.path.startswith(_STATE_EVENT_PATH_PREFIX):
            return await fallback(response_class, method, path, *args, **kwargs)
        assert response_class is nio.RoomGetStateEventResponse
        assert method == "GET"
        room_id, state, event_type, state_key = (
            unquote(part) for part in url.path.removeprefix(_STATE_EVENT_PATH_PREFIX).split("/")
        )
        assert state == "state"
        response = await client.room_get_state_event(room_id=room_id, event_type=event_type, state_key=state_key)
        if not isinstance(response, nio.RoomGetStateEventResponse):
            return response
        event = scheduled_task_state_event(state_key, response.content, room_id=room_id, sender=sender)
        return nio.RoomGetStateEventResponse(event, event_type, state_key, room_id)

    client._send = AsyncMock(side_effect=send)
    return client._send


def scheduling_bot(
    client: Any,  # noqa: ANN401
    *,
    agent_name: str = ROUTER_AGENT_NAME,
    conversation_reader: ConversationReader | None = None,
) -> AgentBot:
    """Return a started, synced bot double whose client and reader scheduled-task runners borrow."""
    bot = MagicMock(spec=AgentBot)
    bot.agent_name = agent_name
    bot.client = client
    bot.running = True
    bot.first_sync_complete = True
    bot.conversation_reader = conversation_reader if conversation_reader is not None else AsyncMock()
    return bot


def scheduled_task_runtime(
    runtime_paths: RuntimePaths,
    *bots: AgentBot,
    config: Config | None = None,
    config_provider: Callable[[], Config | None] | None = None,
    matrix_admin: HookMatrixAdmin | None = None,
    hooks: HookRegistryLifetime | None = None,
) -> ScheduledTaskRuntime:
    """Return the scheduled-task owner over these bots, reading ``config`` unless a provider is given."""
    bots_by_name = {bot.agent_name: bot for bot in bots}
    return ScheduledTaskRuntime(
        runtime_paths=runtime_paths,
        config_provider=config_provider if config_provider is not None else lambda: config,
        bots=lambda: bots_by_name,
        matrix_admin=lambda: matrix_admin,
        hooks=hooks if hooks is not None else HookRegistryState(HookRegistry.empty()).lifetime(),
    )


def task_runner(
    client: Any,  # noqa: ANN401
    task_id: str,
    room_id: str,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_reader: ConversationReader | None = None,
    matrix_admin: HookMatrixAdmin | None = None,
    *,
    config_provider: Callable[[], Config | None] | None = None,
    hooks: HookRegistryLifetime | None = None,
) -> _TaskRunner:
    """Return one task's runner owned by a ready router bot on ``client``."""
    bot = scheduling_bot(client, conversation_reader=conversation_reader)
    runtime = scheduled_task_runtime(
        runtime_paths,
        bot,
        config=config,
        config_provider=config_provider,
        matrix_admin=matrix_admin,
        hooks=hooks,
    )
    return _TaskRunner(runtime, ROUTER_AGENT_NAME, room_id, task_id)


async def run_once_task(
    client: Any,  # noqa: ANN401
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_reader: ConversationReader | None = None,
    matrix_admin: HookMatrixAdmin | None = None,
    *,
    config_provider: Callable[[], Config | None] | None = None,
    hooks: HookRegistryLifetime | None = None,
) -> None:
    """Run one one-time task to completion on a ready router bot."""
    assert workflow.room_id is not None
    runner = task_runner(
        client,
        task_id,
        workflow.room_id,
        config,
        runtime_paths,
        conversation_reader,
        matrix_admin,
        config_provider=config_provider,
        hooks=hooks,
    )
    await runner.run_once(workflow)


async def run_cron_task(
    client: Any,  # noqa: ANN401
    task_id: str,
    workflow: ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    conversation_reader: ConversationReader | None = None,
    matrix_admin: HookMatrixAdmin | None = None,
    *,
    config_provider: Callable[[], Config | None] | None = None,
    hooks: HookRegistryLifetime | None = None,
) -> None:
    """Run one recurring task on a ready router bot until it ends or completes its first fire."""
    assert workflow.room_id is not None
    runner = task_runner(
        client,
        task_id,
        workflow.room_id,
        config,
        runtime_paths,
        conversation_reader,
        matrix_admin,
        config_provider=config_provider,
        hooks=hooks,
    )
    complete = scheduling_runtime.complete_recurring_occurrence

    async def complete_then_stop(*args: object, **kwargs: object) -> None:
        await complete(*args, **kwargs)
        raise _FiredOnceError

    with (
        patch.object(scheduling_runtime, "complete_recurring_occurrence", complete_then_stop),
        suppress(_FiredOnceError),
    ):
        await runner.run_cron(workflow)


class _FiredOnceError(BaseException):
    """Ends a test's recurring runner after one completed fire; a BaseException, so the runner reports no failure."""


async def runnable_task(
    client: Any,  # noqa: ANN401
    room_id: str,
    task_id: str,
    *,
    config: Config,
    runtime_paths: RuntimePaths,
    config_provider: Callable[[], Config | None] | None = None,
) -> ScheduledTaskRecord | None:
    """Return a task's runnable state the way its runner reconciles it before waiting or firing."""
    runner = task_runner(client, task_id, room_id, config, runtime_paths, config_provider=config_provider)
    return await runner._runnable_task()
