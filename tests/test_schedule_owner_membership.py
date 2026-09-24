"""Schedules stop when their creator no longer belongs to the target room."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom import scheduling
from mindroom.agent_reply_membership import AgentReplyMembershipIndex
from mindroom.bot_runtime_view import BotRuntimeState
from mindroom.config.access import ResponderAccessConfig
from mindroom.config.main import Config
from mindroom.constants import resolve_runtime_paths
from mindroom.event_journal import EventJournalStore
from mindroom.matrix.conversation_hydration import ConversationHydrator
from mindroom.matrix.conversation_reads import ConversationReader
from mindroom.matrix.relation_lookup import RelationLookup
from mindroom.message_target import MessageTarget
from mindroom.orchestrator import _MultiAgentOrchestrator
from mindroom.recurring_schedule import RecurringOccurrence, _RecurringCheckpoint
from mindroom.scheduling_executor import ScheduledWorkflowOutcome
from mindroom.tool_system.runtime_context import ToolRuntimeContext, build_scheduling_runtime_from_tool_runtime_context
from tests.conftest import make_conversation_reader_mock, make_matrix_client_mock
from tests.identity_helpers import persist_entity_accounts
from tests.scheduling_helpers import schedule_runtime_paths, scheduled_task_state_response

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from mindroom.constants import RuntimePaths


_ROUTER_ID = "@router:server"


@pytest.fixture
def owner_membership_runtime_paths(tmp_path: Path) -> RuntimePaths:
    """Provide runtime identity paths for direct membership reconciler tests."""
    return schedule_runtime_paths(tmp_path, _ROUTER_ID)


def _owner_schedule(
    memberships: list[dict[str, Any] | nio.RoomGetStateEventError | Exception],
    *,
    schedule_type: Literal["once", "cron"] = "once",
    created_by: str | None = "@alice:server",
    writer_id: str = _ROUTER_ID,
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
        assert event_type == "m.room.member"
        assert state_key == created_by
        membership = memberships.pop(0) if len(memberships) > 1 else memberships[0]
        if isinstance(membership, Exception):
            raise membership
        if isinstance(membership, nio.RoomGetStateEventError):
            return membership
        return nio.RoomGetStateEventResponse(membership, event_type, state_key, room_id)

    async def read_room_state(room_id: str) -> nio.RoomGetStateResponse:
        assert room_id == "!test:server"
        return scheduled_task_state_response(room_id, {"owner_task": state.copy()}, sender=writer_id)

    async def write_state(room_id: str, event_type: str, state_key: str, content: dict[str, Any]) -> object:
        assert (room_id, event_type, state_key) == ("!test:server", "com.mindroom.scheduled.task", "owner_task")
        state.clear()
        state.update(content)
        return nio.RoomPutStateResponse("$state", room_id)

    client = make_matrix_client_mock(user_id=_ROUTER_ID)
    client.homeserver = "https://matrix.example"
    client.user_id = _ROUTER_ID
    client.device_id = "TEST"
    client.rooms = {}
    client.room_send.return_value = nio.RoomSendResponse("$message", "!test:server")
    client.room_get_state.side_effect = read_room_state
    client.room_get_state_event.side_effect = read_state
    client.room_put_state.side_effect = write_state
    return client, workflow, state


@pytest.mark.asyncio
@pytest.mark.parametrize("membership", ["leave", "ban", "invite", "knock"])
async def test_departed_owner_cancels_persisted_schedule(
    membership: str,
    owner_membership_runtime_paths: RuntimePaths,
) -> None:
    """Leaving, removal, or deactivation must retire the creator's pending task."""
    client, workflow, state = _owner_schedule([{"membership": membership}])

    task = await scheduling._reconcile_runnable_task_retrying(
        client,
        "!test:server",
        "owner_task",
        config=Config(),
        runtime_paths=owner_membership_runtime_paths,
    )

    assert task is None
    assert state["status"] == "cancelled"
    assert state["workflow"] == workflow.model_dump_json()
    assert state["created_at"] == "2026-09-01T00:00:00+00:00"


@pytest.mark.asyncio
@pytest.mark.parametrize("created_by", ["@alice:server", "@bob:server"])
async def test_joined_owner_schedule_stays_pending(
    created_by: str,
    owner_membership_runtime_paths: RuntimePaths,
) -> None:
    """Any joined owner keeps their schedule usable."""
    client, _, state = _owner_schedule([{"membership": "join"}], created_by=created_by)

    task = await scheduling._reconcile_runnable_task_retrying(
        client,
        "!test:server",
        "owner_task",
        config=Config(),
        runtime_paths=owner_membership_runtime_paths,
    )

    assert task is not None
    assert state["status"] == "pending"
    client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_unowned_schedule_is_cancelled_instead_of_running(
    owner_membership_runtime_paths: RuntimePaths,
) -> None:
    """A schedule without a recorded creator has no requester to run as."""
    client, workflow, state = _owner_schedule([{"membership": "join"}], created_by=None)

    task = await scheduling._reconcile_runnable_task_retrying(
        client,
        "!test:server",
        "owner_task",
        config=Config(),
        runtime_paths=owner_membership_runtime_paths,
    )

    assert task is None
    assert state["status"] == "cancelled"
    assert state["workflow"] == workflow.model_dump_json()
    client.room_get_state_event.assert_not_awaited()


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
    runtime_paths = schedule_runtime_paths(tmp_path, _ROUTER_ID)
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
    owner_membership_runtime_paths: RuntimePaths,
) -> None:
    """Lookup failures must neither run the task nor cancel a potentially valid owner."""
    client, _, state = _owner_schedule([unavailable, {"membership": "join"}])

    async def wait_for_retry(_delay: float) -> None:
        assert state["status"] == "pending"
        client.room_put_state.assert_not_awaited()

    with patch("mindroom.scheduling.asyncio.sleep", side_effect=wait_for_retry) as sleep:
        task = await scheduling._reconcile_runnable_task_retrying(
            client,
            "!test:server",
            "owner_task",
            config=Config(),
            runtime_paths=owner_membership_runtime_paths,
        )

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
    runtime_paths = schedule_runtime_paths(tmp_path, _ROUTER_ID)
    existing = await scheduling.get_scheduled_task(client, "!test:server", "owner_task", Config(), runtime_paths)
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
            await scheduling.save_edited_scheduled_task(
                client,
                "!test:server",
                "owner_task",
                updated,
                existing,
                Config(),
                runtime_paths,
            )
            resume_lookup.set()

    execute.assert_awaited_once()
    assert execute.await_args.args[1] == updated
    assert state["status"] == "completed"
    assert state["workflow"] == updated.model_dump_json()


@pytest.mark.asyncio
async def test_edit_cannot_resurrect_schedule_while_cancellation_is_persisting(
    owner_membership_runtime_paths: RuntimePaths,
) -> None:
    """Runtime cancellation and a separate API client must serialize their writes."""
    client, workflow, state = _owner_schedule([{"membership": "leave"}])
    existing = await scheduling.get_scheduled_task(
        client,
        "!test:server",
        "owner_task",
        Config(),
        owner_membership_runtime_paths,
    )
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
    api_client.room_get_state.side_effect = client.room_get_state.side_effect
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
                Config(),
                owner_membership_runtime_paths,
            )

    async with asyncio.timeout(2), asyncio.TaskGroup() as tasks:
        tasks.create_task(
            scheduling._reconcile_runnable_task_retrying(
                client,
                "!test:server",
                "owner_task",
                config=Config(),
                runtime_paths=owner_membership_runtime_paths,
            ),
        )
        await write_started.wait()
        tasks.create_task(edit())
        await edit_started.wait()
        resume_write.set()

    assert state["status"] == "cancelled"
    api_client.room_put_state.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_during_cancellation_retry_invalidates_old_departure(
    owner_membership_runtime_paths: RuntimePaths,
) -> None:
    """Retry sleep must allow a rejoined creator to replace the departed workflow."""
    client, workflow, state = _owner_schedule([{"membership": "leave"}, {"membership": "join"}])
    existing = await scheduling.get_scheduled_task(
        client,
        "!test:server",
        "owner_task",
        Config(),
        owner_membership_runtime_paths,
    )
    assert existing is not None
    updated = workflow.model_copy(update={"message": "New request after rejoining"})
    write_state = client.room_put_state.side_effect
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("unavailable", "M_UNKNOWN")

    async def edit_on_retry(_delay: float) -> None:
        client.room_put_state.side_effect = write_state
        await scheduling.save_edited_scheduled_task(
            client,
            "!test:server",
            "owner_task",
            updated,
            existing,
            Config(),
            owner_membership_runtime_paths,
        )

    with patch("mindroom.scheduling.asyncio.sleep", side_effect=edit_on_retry):
        runnable = await scheduling._reconcile_runnable_task_retrying(
            client,
            "!test:server",
            "owner_task",
            config=Config(),
            runtime_paths=owner_membership_runtime_paths,
        )

    assert runnable is not None
    assert runnable.workflow == updated
    assert state["status"] == "pending"


@pytest.mark.asyncio
async def test_stale_edit_cannot_overwrite_a_newer_workflow(owner_membership_runtime_paths: RuntimePaths) -> None:
    """Slow parsing must not overwrite an intervening committed edit."""
    client, workflow, state = _owner_schedule([{"membership": "join"}])
    existing = await scheduling.get_scheduled_task(
        client,
        "!test:server",
        "owner_task",
        Config(),
        owner_membership_runtime_paths,
    )
    assert existing is not None
    updated = workflow.model_copy(update={"message": "First edit"})
    await scheduling.save_edited_scheduled_task(
        client,
        "!test:server",
        "owner_task",
        updated,
        existing,
        Config(),
        owner_membership_runtime_paths,
    )

    with pytest.raises(ValueError, match="changed"):
        await scheduling.save_edited_scheduled_task(
            client,
            "!test:server",
            "owner_task",
            workflow,
            existing,
            Config(),
            owner_membership_runtime_paths,
        )

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
    runtime_paths = schedule_runtime_paths(tmp_path, _ROUTER_ID)
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
async def test_cancellation_write_failure_retries_without_reviving_departed_owner(
    owner_membership_runtime_paths: RuntimePaths,
) -> None:
    """A failed cancellation write must stay retry-owned even if the user rejoins."""
    client, _, state = _owner_schedule([{"membership": "leave"}, {"membership": "join"}])
    write_state = client.room_put_state.side_effect
    client.room_put_state.side_effect = None
    client.room_put_state.return_value = nio.RoomPutStateError("unavailable", "M_UNKNOWN")

    async def recover_state_write(_delay: float) -> None:
        assert state["status"] == "pending"
        client.room_put_state.side_effect = write_state

    with patch("mindroom.scheduling.asyncio.sleep", side_effect=recover_state_write) as sleep:
        task = await scheduling._reconcile_runnable_task_retrying(
            client,
            "!test:server",
            "owner_task",
            config=Config(),
            runtime_paths=owner_membership_runtime_paths,
        )

    assert task is None
    assert state["status"] == "cancelled"
    sleep.assert_awaited_once()


_HUMAN_ALIAS = "@bridge_alice:server"
_NONHUMAN_ALIASES = (
    "@bridgebot:server",
    "@persisted_helper:server",
    "@mindroom_helper:server",
    "@persisted_router:server",
    "@internal:server",
)


def _alias_schedule_config(tmp_path: Path) -> tuple[Config, RuntimePaths]:
    runtime_paths = resolve_runtime_paths(
        config_path=tmp_path / "config.yaml",
        storage_path=tmp_path / "storage",
        process_env={"MATRIX_HOMESERVER": "https://server"},
    )
    config = Config.model_validate(
        {
            "agents": {"helper": {"display_name": "Helper"}},
            "bot_accounts": ["@bridgebot:server"],
            "mindroom_user": {"username": "internal"},
            "authorization": {"aliases": {"@alice:server": [_HUMAN_ALIAS, *_NONHUMAN_ALIASES]}},
        },
    )
    persist_entity_accounts(
        config,
        runtime_paths,
        usernames={"router": "persisted_router", "helper": "persisted_helper"},
    )
    return config, runtime_paths


def _set_authoritative_alias_memberships(client: AsyncMock, memberships: dict[str, object]) -> None:
    read_state = client.room_get_state_event.side_effect

    async def read_membership(room_id: str, event_type: str, state_key: str = "") -> object:
        if event_type != "m.room.member":
            return await read_state(room_id, event_type, state_key)
        assert room_id == "!test:server"
        membership = memberships.get(state_key, "leave")
        if isinstance(membership, Exception):
            raise membership
        if isinstance(membership, nio.RoomGetStateEventError):
            return membership
        content = {"membership": membership} if isinstance(membership, str) else membership
        return nio.RoomGetStateEventResponse(content, event_type, state_key, room_id)

    client.room_get_state_event.side_effect = read_membership


async def _run_alias_schedule(
    client: AsyncMock,
    workflow: scheduling.ScheduledWorkflow,
    config: Config,
    runtime_paths: RuntimePaths,
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_retry: Callable[[float], Awaitable[None]] | None = None,
) -> list[str | None]:
    delivered_owners: list[str | None] = []
    occurrence = RecurringOccurrence(
        runtime_paths.storage_root / "checkpoint.json",
        _RecurringCheckpoint("workflow", datetime.now(UTC) - timedelta(minutes=1)),
    )

    async def deliver(
        _client: object,
        current: scheduling.ScheduledWorkflow,
        *_args: object,
        **_kwargs: object,
    ) -> ScheduledWorkflowOutcome:
        delivered_owners.append(current.created_by)
        return ScheduledWorkflowOutcome(status="delivered")

    async def plan(*_args: object, **_kwargs: object) -> RecurringOccurrence:
        return occurrence

    async def unexpected_retry(_delay: float) -> None:
        pytest.fail("A proven membership result must not keep the schedule waiting")

    monkeypatch.setattr(scheduling.scheduling_executor, "execute_scheduled_workflow", deliver)
    monkeypatch.setattr(scheduling, "plan_recurring_occurrence", plan)
    monkeypatch.setattr(scheduling.asyncio, "sleep", on_retry or unexpected_retry)
    if workflow.schedule_type == "once":
        await scheduling._run_once_task(
            client,
            "owner_task",
            workflow,
            config,
            runtime_paths,
            make_conversation_reader_mock(),
        )
    else:
        await scheduling._run_cron_task(
            client,
            "owner_task",
            workflow,
            {},
            config,
            runtime_paths,
            make_conversation_reader_mock(),
        )
    return delivered_owners


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
@pytest.mark.parametrize(
    ("joined_id", "canonical_unknown", "allowed"),
    [
        pytest.param("@alice:server", False, True, id="canonical-only"),
        pytest.param(_HUMAN_ALIAS, False, True, id="alias-only"),
        pytest.param(_HUMAN_ALIAS, True, True, id="alias-proves-join-despite-unknown-canonical"),
        pytest.param(None, False, False, id="neither-joined"),
        *[pytest.param(nonhuman, False, False, id=f"excluded-{nonhuman}") for nonhuman in _NONHUMAN_ALIASES],
    ],
)
async def test_schedule_runners_apply_human_alias_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schedule_type: Literal["once", "cron"],
    joined_id: str | None,
    *,
    canonical_unknown: bool,
    allowed: bool,
) -> None:
    """A live human alias can keep automation runnable; bot aliases cannot."""
    client, workflow, state = _owner_schedule(
        [{"membership": "leave"}],
        schedule_type=schedule_type,
        writer_id="@persisted_router:server",
    )
    config, runtime_paths = _alias_schedule_config(tmp_path)
    memberships: dict[str, object] = {joined_id: "join"} if joined_id is not None else {}
    if canonical_unknown:
        memberships["@alice:server"] = nio.RoomGetStateEventError("missing", "M_NOT_FOUND")
    _set_authoritative_alias_memberships(client, memberships)

    delivered = await _run_alias_schedule(client, workflow, config, runtime_paths, monkeypatch)

    assert delivered == (["@alice:server"] if allowed else [])
    if allowed:
        assert state["status"] == ("completed" if schedule_type == "once" else "pending")
    else:
        assert state["status"] == "cancelled"
    assert scheduling.ScheduledWorkflow.model_validate_json(state["workflow"]).created_by == "@alice:server"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
@pytest.mark.parametrize(
    "unavailable",
    [
        pytest.param(nio.RoomGetStateEventError("missing", "M_NOT_FOUND"), id="missing"),
        pytest.param(nio.RoomGetStateEventError("forbidden", "M_FORBIDDEN"), id="forbidden"),
        pytest.param(OSError("connection lost"), id="transport-error"),
        pytest.param({}, id="malformed-state"),
    ],
)
async def test_unknown_alias_membership_retries_without_cancelling_schedule(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schedule_type: Literal["once", "cron"],
    unavailable: object,
) -> None:
    """An absent canonical identity cannot prove departure while its human alias is unknown."""
    client, workflow, state = _owner_schedule(
        [{"membership": "leave"}],
        schedule_type=schedule_type,
        writer_id="@persisted_router:server",
    )
    config, runtime_paths = _alias_schedule_config(tmp_path)
    memberships: dict[str, object] = {_HUMAN_ALIAS: unavailable}
    _set_authoritative_alias_memberships(client, memberships)
    retry_statuses: list[str] = []

    async def recover_membership(_delay: float) -> None:
        retry_statuses.append(state["status"])
        memberships[_HUMAN_ALIAS] = "join"

    delivered = await _run_alias_schedule(
        client,
        workflow,
        config,
        runtime_paths,
        monkeypatch,
        on_retry=recover_membership,
    )

    assert retry_statuses == ["pending"]
    assert delivered == ["@alice:server"]
    assert state["status"] == ("completed" if schedule_type == "once" else "pending")


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
async def test_joined_alias_departure_cancels_schedule_before_next_fire(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schedule_type: Literal["once", "cron"],
) -> None:
    """A previously joined alias must still be joined when the task is about to fire."""
    client, workflow, state = _owner_schedule(
        [{"membership": "leave"}],
        schedule_type=schedule_type,
        writer_id="@persisted_router:server",
    )
    config, runtime_paths = _alias_schedule_config(tmp_path)
    memberships: dict[str, object] = {_HUMAN_ALIAS: "join"}
    _set_authoritative_alias_memberships(client, memberships)
    read_state = client.room_get_state_event.side_effect
    observed_memberships: list[str] = []

    async def leave_after_first_check(room_id: str, event_type: str, state_key: str = "") -> object:
        response = await read_state(room_id, event_type, state_key)
        if event_type == "m.room.member" and state_key == _HUMAN_ALIAS:
            observed_memberships.append(response.content["membership"])
            memberships[_HUMAN_ALIAS] = "leave"
        return response

    client.room_get_state_event.side_effect = leave_after_first_check

    delivered = await _run_alias_schedule(client, workflow, config, runtime_paths, monkeypatch)

    assert observed_memberships == ["join", "leave"]
    assert delivered == []
    assert state["status"] == "cancelled"
    assert scheduling.ScheduledWorkflow.model_validate_json(state["workflow"]).created_by == "@alice:server"


@pytest.mark.asyncio
@pytest.mark.parametrize("schedule_type", ["once", "cron"])
@pytest.mark.parametrize("managed_runtime", [False, True], ids=["live-bot", "orchestrator"])
async def test_running_schedule_stops_after_live_human_alias_revocation(  # noqa: PLR0915
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    schedule_type: Literal["once", "cron"],
    *,
    managed_runtime: bool,
) -> None:
    """A joined bridge identity loses schedule authority when its live alias grant is removed."""
    client, workflow, state = _owner_schedule(
        [{"membership": "leave"}],
        schedule_type=schedule_type,
        writer_id="@persisted_router:server",
    )
    config, runtime_paths = _alias_schedule_config(tmp_path)
    config.agents["helper"].rooms = ["!test:server"]
    config.agents["helper"].access = ResponderAccessConfig(users=["@alice:server"])
    config.scheduler_catch_up_grace_seconds = 300
    memberships: dict[str, object] = {_HUMAN_ALIAS: "join"}
    _set_authoritative_alias_memberships(client, memberships)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=runtime_paths, api_enabled=False) if managed_runtime else None
    if orchestrator is not None:
        orchestrator.config = config
    live_runtime = BotRuntimeState(
        client=client,
        config=config,
        runtime_paths=runtime_paths,
        agent_reply_memberships=AgentReplyMembershipIndex(),
        enable_streaming=False,
        orchestrator=orchestrator,
    )
    alias_lookup_started = asyncio.Event()
    finish_alias_lookup = asyncio.Event()
    read_state = client.room_get_state_event.side_effect

    async def read_membership_across_reload(room_id: str, event_type: str, state_key: str = "") -> object:
        response = await read_state(room_id, event_type, state_key)
        if event_type == "m.room.member" and state_key == _HUMAN_ALIAS:
            alias_lookup_started.set()
            await finish_alias_lookup.wait()
        return response

    client.room_get_state_event.side_effect = read_membership_across_reload
    delivered_owners: list[str | None] = []

    async def parsed_workflow(*_args: object, **_kwargs: object) -> scheduling.ScheduledWorkflow:
        return workflow

    async def deliver(
        _client: object,
        current: scheduling.ScheduledWorkflow,
        *_args: object,
        **_kwargs: object,
    ) -> ScheduledWorkflowOutcome:
        delivered_owners.append(current.created_by)
        raise asyncio.CancelledError

    monkeypatch.setattr(scheduling, "_parse_workflow_schedule", parsed_workflow)
    monkeypatch.setattr(scheduling.scheduling_executor, "execute_scheduled_workflow", deliver)
    journal = EventJournalStore.open_sqlite(tmp_path / "schedule-journal.sqlite3")
    principal = journal.principal("@router:server")
    reader = ConversationReader(
        store=principal,
        hydrator=ConversationHydrator(store=principal, runtime=live_runtime, self_sender="@router:server"),
    )
    task: asyncio.Task | None = None
    try:
        context = ToolRuntimeContext(
            agent_name="helper",
            target=MessageTarget.resolve("!test:server", None, None),
            requester_id="@alice:server",
            client=client,
            config=config,
            runtime_paths=runtime_paths,
            conversation_reader=reader,
            relations=RelationLookup(store=principal, runtime=live_runtime),
            agent_reply_memberships=live_runtime.agent_reply_memberships,
            room=nio.MatrixRoom("!test:server", "@router:server"),
            config_provider=lambda: live_runtime.config,
            orchestrator=orchestrator,
        )
        if schedule_type == "cron":
            assert workflow.cron_schedule is not None
            await scheduling.plan_recurring_occurrence(
                runtime_paths,
                homeserver=client.homeserver,
                sender=client.user_id,
                room_id="!test:server",
                task_id="owner_task",
                workflow_json=workflow.model_dump_json(),
                cron=workflow.cron_schedule.to_cron_string(),
                now=datetime.now(UTC) - timedelta(minutes=2),
                grace_seconds=300,
            )
        task_id, response = await scheduling.schedule_task(
            runtime=build_scheduling_runtime_from_tool_runtime_context(context),
            room_id="!test:server",
            thread_id=None,
            scheduled_by="@alice:server",
            full_text="Check the queue",
            task_id="owner_task",
        )
        assert task_id == "owner_task", response
        task = scheduling._running_tasks[task_id]
        await asyncio.wait_for(alias_lookup_started.wait(), timeout=2)

        updated_config = config.model_copy(deep=True)
        updated_config.authorization.aliases = {}
        if orchestrator is not None:
            # A retired bot generation can retain its old local config while its schedules survive.
            orchestrator.config = updated_config
        else:
            live_runtime.config = updated_config
        finish_alias_lookup.set()
        with suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

        assert memberships[_HUMAN_ALIAS] == "join"
        assert config.authorization.aliases
        assert delivered_owners == []
        assert state["status"] == "cancelled"
        assert scheduling.ScheduledWorkflow.model_validate_json(state["workflow"]).created_by == "@alice:server"
    finally:
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await journal.close()
