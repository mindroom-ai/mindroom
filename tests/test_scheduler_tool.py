"""Tests for shared schedule entrypoint and scheduler tool."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.authorization import ensure_room_membership_synced
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.constants import SILENT_SCHEDULE_NO_REPLY_TOKEN
from mindroom.custom_tools.scheduler import SchedulerTools
from mindroom.hooks import HookBindings
from mindroom.message_target import MessageTarget
from mindroom.scheduling.errors import ScheduledTaskNotFoundError, SchedulingError
from mindroom.scheduling.tasks import SchedulingRuntime, _extract_mentioned_agents_from_text
from mindroom.tool_system.invocation import ToolCallContext, run_tool_call
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    build_scheduling_runtime_from_tool_runtime_context,
    tool_runtime_context,
)
from tests.agent_tool_helpers import toolkit_offers
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import entity_ids, persist_entity_accounts


def _bind_runtime_paths(config: Config) -> Config:
    runtime_paths = test_runtime_paths(Path(tempfile.mkdtemp()))
    bound = bind_runtime_paths(config, runtime_paths)
    persist_entity_accounts(bound, runtime_paths)
    return bound


def _make_context(config: Config, *, matrix_admin: object | None = None) -> ToolRuntimeContext:
    return make_test_tool_runtime_context(
        agent_name="general",
        target=MessageTarget.resolve(
            room_id="!room:localhost",
            thread_id="$thread",
            reply_to_event_id=None,
        ),
        requester_id="@user:localhost",
        client=make_matrix_client_mock(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        relations=make_relation_lookup(),
        conversation_reader=make_conversation_reader_mock(),
        room=MagicMock(),
        storage_path=None,
        hooks=HookBindings(matrix_admin=matrix_admin),
    )


def test_extract_mentioned_agents_from_text() -> None:
    """Agent mentions should be extracted from scheduling text."""
    config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    result = _extract_mentioned_agents_from_text(
        "in 5 minutes @general check deployment",
        config,
        runtime_paths_for(config),
    )
    expected_agent = entity_ids(config, runtime_paths_for(config))["general"]
    assert result == [expected_agent]


def test_scheduler_tool_requires_explicit_delivery_mode() -> None:
    """The model-facing schema must not silently choose a delivery scope."""
    function = toolkit_offers(SchedulerTools(), "schedule")

    assert set(function.parameters["required"]) == {"request", "new_thread"}
    properties = function.parameters["properties"]
    new_thread_description = properties["new_thread"]["description"].lower()
    silent_description = properties["silent"]["description"].lower()
    assert "silent" in new_thread_description
    assert "room-level root" in new_thread_description
    assert "hidden" in silent_description
    assert "whitespace" in silent_description
    assert SILENT_SCHEDULE_NO_REPLY_TOKEN.lower() in silent_description
    assert "findings" in silent_description
    assert "failures" in silent_description
    assert "infer" in silent_description


@pytest.mark.asyncio
async def test_scheduler_tool_uses_shared_backend() -> None:
    """Tool should call the same scheduling backend path as !schedule, running saved tasks on the orchestrator's runtime."""
    tools = SchedulerTools()
    config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    orchestrator = MagicMock()
    context = replace(_make_context(config), config_provider=lambda: config, orchestrator=orchestrator)

    with (
        patch(
            "mindroom.custom_tools.scheduler.schedule_task",
            new=AsyncMock(return_value="✅ Scheduled"),
        ) as mock_schedule,
        tool_runtime_context(context),
    ):
        result = await tools.schedule("tomorrow at 3pm check deployment", new_thread=False)
        new_thread_result = await tools.schedule(
            "tomorrow at 4pm check deployment",
            new_thread=True,
            silent=True,
        )
        limited_result = await tools.schedule(
            "every 25 minutes poll the queue",
            new_thread=False,
            history_limit=0,
            model="cheap",
        )

    assert result == "✅ Scheduled"
    assert new_thread_result == "✅ Scheduled"
    assert limited_result == "✅ Scheduled"
    assert mock_schedule.await_count == 3
    first_call = mock_schedule.await_args_list[0].kwargs.copy()
    second_call = mock_schedule.await_args_list[1].kwargs.copy()
    third_call = mock_schedule.await_args_list[2].kwargs.copy()
    expected_runtime = SchedulingRuntime(
        client=context.client,
        config=context.config,
        runtime_paths=context.runtime_paths,
        room=context.room,
        conversation_reader=context.conversation_reader,
        agent_reply_memberships=context.agent_reply_memberships,
        responder_candidates_for_room=context.responder_candidates_for_current_room,
        task_runtime=orchestrator.scheduled_tasks,
        agent_name=context.agent_name,
    )
    for call_kwargs in (first_call, second_call, third_call):
        assert call_kwargs.pop("runtime") == expected_runtime
    assert first_call == {
        "room_id": context.room_id,
        "thread_id": context.resolved_thread_id,
        "scheduled_by": context.requester_id,
        "full_text": "tomorrow at 3pm check deployment",
        "new_thread": False,
        "history_limit": None,
        "silent": None,
        "model": None,
    }
    assert second_call == {
        "room_id": context.room_id,
        "thread_id": context.resolved_thread_id,
        "scheduled_by": context.requester_id,
        "full_text": "tomorrow at 4pm check deployment",
        "new_thread": True,
        "history_limit": None,
        "silent": True,
        "model": None,
    }
    assert third_call == {
        "room_id": context.room_id,
        "thread_id": context.resolved_thread_id,
        "scheduled_by": context.requester_id,
        "full_text": "every 25 minutes poll the queue",
        "new_thread": False,
        "history_limit": 0,
        "silent": None,
        "model": "cheap",
    }


@pytest.mark.asyncio
async def test_scheduler_tool_reuses_failed_turn_membership_snapshot() -> None:
    """A turn-bound scheduler runtime must not retry its failed membership refresh."""
    requester_id = "@user:localhost"
    config = _bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            administrators=[requester_id],
        ),
    )
    general_id = entity_ids(config, runtime_paths_for(config))["general"]
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id=general_id.full_id)
    room.add_member(general_id.full_id, "General Agent", None)
    room.add_member(requester_id, "User", None)
    client = AsyncMock()
    client.joined_members.side_effect = TimeoutError("membership lookup timed out")
    context = replace(
        _make_context(config),
        client=client,
        requester_id=requester_id,
        room=room,
        membership_turn_id="$turn",
    )

    assert not await ensure_room_membership_synced(client, room, sender_id=requester_id)

    runtime = build_scheduling_runtime_from_tool_runtime_context(context)
    candidates = await runtime.responder_candidates_for_room(room, requester_id)

    assert candidates == [general_id]
    assert client.joined_members.await_count == 1


@pytest.mark.asyncio
async def test_scheduler_tool_uses_current_config_for_authorization() -> None:
    """A long-lived tool context must not schedule with its construction-time policy."""
    tools = SchedulerTools()
    old_config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    current_config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="Current Agent")}))
    context = replace(
        _make_context(old_config),
        config_provider=lambda: current_config,
    )

    with (
        patch(
            "mindroom.custom_tools.scheduler.schedule_task",
            new=AsyncMock(return_value="✅ Scheduled"),
        ) as mock_schedule,
        tool_runtime_context(context),
    ):
        await tools.schedule("tomorrow at 3pm check deployment", new_thread=False)

    runtime = mock_schedule.await_args.kwargs["runtime"]
    assert runtime.config is current_config


@pytest.mark.asyncio
async def test_edit_schedule_tool_calls_backend() -> None:
    """Edit tool should call edit_scheduled_task with correct arguments."""
    tools = SchedulerTools()
    config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    context = replace(_make_context(config), config_provider=lambda: config)

    with (
        patch(
            "mindroom.custom_tools.scheduler.edit_scheduled_task",
            new=AsyncMock(return_value="✅ Updated task `task123`."),
        ) as mock_edit,
        tool_runtime_context(context),
    ):
        result = await tools.edit_schedule("task123", "tomorrow at 9am check deployment")
        limited_result = await tools.edit_schedule(
            "task123",
            "keep the same schedule",
            history_limit=5,
            silent=True,
        )
        visible_result = await tools.edit_schedule("task123", "make it visible", silent=False, model="cheap")

    assert "Updated" in result
    assert "Updated" in limited_result
    assert "Updated" in visible_result
    expected_runtime = SchedulingRuntime(
        client=context.client,
        config=context.config,
        runtime_paths=context.runtime_paths,
        room=context.room,
        conversation_reader=context.conversation_reader,
        agent_reply_memberships=context.agent_reply_memberships,
        responder_candidates_for_room=context.responder_candidates_for_current_room,
        agent_name=context.agent_name,
    )
    assert mock_edit.await_count == 3
    first_call = mock_edit.await_args_list[0].kwargs.copy()
    second_call = mock_edit.await_args_list[1].kwargs.copy()
    third_call = mock_edit.await_args_list[2].kwargs.copy()
    for call_kwargs in (first_call, second_call, third_call):
        assert call_kwargs.pop("runtime") == expected_runtime
    assert first_call == {
        "room_id": context.room_id,
        "task_id": "task123",
        "full_text": "tomorrow at 9am check deployment",
        "scheduled_by": context.requester_id,
        "history_limit": None,
        "silent": None,
        "model": None,
    }
    assert second_call == {
        "room_id": context.room_id,
        "task_id": "task123",
        "full_text": "keep the same schedule",
        "scheduled_by": context.requester_id,
        "history_limit": 5,
        "silent": True,
        "model": None,
    }
    assert third_call == {
        "room_id": context.room_id,
        "task_id": "task123",
        "full_text": "make it visible",
        "scheduled_by": context.requester_id,
        "history_limit": None,
        "silent": False,
        "model": "cheap",
    }


@pytest.mark.asyncio
async def test_list_schedules_tool_calls_backend() -> None:
    """List tool should call list_scheduled_tasks with correct arguments."""
    tools = SchedulerTools()
    config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    context = _make_context(config)

    with (
        patch(
            "mindroom.custom_tools.scheduler.list_scheduled_tasks",
            new=AsyncMock(return_value="**Scheduled Tasks:**\n• `abc` - in 5 minutes"),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        result = await tools.list_schedules()

    assert "Scheduled Tasks" in result
    mock_list.assert_awaited_once_with(
        client=context.client,
        room_id=context.room_id,
        runtime_paths=context.runtime_paths,
        thread_id=context.resolved_thread_id,
        config=context.config,
    )


@pytest.mark.asyncio
async def test_cancel_schedule_tool_calls_backend() -> None:
    """Cancel tool should call cancel_scheduled_task with correct arguments, ending the runner on the orchestrator's runtime."""
    tools = SchedulerTools()
    config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    orchestrator = MagicMock()
    context = replace(_make_context(config), orchestrator=orchestrator)

    with (
        patch(
            "mindroom.custom_tools.scheduler.cancel_scheduled_task",
            new=AsyncMock(return_value="✅ Cancelled task `task123`"),
        ) as mock_cancel,
        tool_runtime_context(context),
    ):
        result = await tools.cancel_schedule("task123")

    assert "Cancelled" in result
    mock_cancel.assert_awaited_once_with(
        client=context.client,
        room_id=context.room_id,
        task_id="task123",
        runtime_paths=context.runtime_paths,
        task_runtime=orchestrator.scheduled_tasks,
    )


def _call_context() -> ToolCallContext:
    return ToolCallContext(run_id="run", session_id="session", call_id="call-1", user_id=None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("schedule", {"request": "in 10 minutes check logs", "new_thread": False}),
        ("edit_schedule", {"task_id": "task123", "request": "in 10 minutes check logs"}),
        ("list_schedules", {}),
        ("cancel_schedule", {"task_id": "task123"}),
        ("run_scheduled_call", {"task_id": "task123"}),
    ],
)
async def test_scheduler_call_outside_a_conversation_fails(name: str, arguments: dict[str, object]) -> None:
    """A scheduler call made where scheduling cannot run is a failed call that says why."""
    result = await run_tool_call(toolkit_offers(SchedulerTools(), name), arguments, _call_context(), middleware=())

    assert result.status == "failed"
    assert result.failure_text == "Scheduler tool is unavailable in this context."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "operation", "arguments", "error", "text"),
    [
        (
            "schedule",
            "schedule_task",
            {"request": "next message", "new_thread": False},
            SchedulingError("Failed to schedule: schedule is not valid"),
            "Failed to schedule: schedule is not valid",
        ),
        (
            "edit_schedule",
            "edit_scheduled_task",
            {"task_id": "task123", "request": "tomorrow at 9am check deployment"},
            SchedulingError("Failed to edit task `task123`."),
            "Failed to edit task `task123`.",
        ),
        (
            "list_schedules",
            "list_scheduled_tasks",
            {},
            SchedulingError("Unable to retrieve scheduled tasks."),
            "Unable to retrieve scheduled tasks.",
        ),
        (
            "cancel_schedule",
            "cancel_scheduled_task",
            {"task_id": "task123"},
            ScheduledTaskNotFoundError("task123"),
            "Task `task123` not found.",
        ),
    ],
)
async def test_scheduler_operation_failure_is_a_failed_call_with_its_reason(
    name: str,
    operation: str,
    arguments: dict[str, object],
    error: SchedulingError,
    text: str,
) -> None:
    """A scheduling operation that did not happen fails the call with the operation's reason as its text."""
    config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    with (
        patch(f"mindroom.custom_tools.scheduler.{operation}", new=AsyncMock(side_effect=error)),
        tool_runtime_context(_make_context(config)),
    ):
        result = await run_tool_call(toolkit_offers(SchedulerTools(), name), arguments, _call_context(), middleware=())

    assert result.status == "failed"
    assert result.failure_text == text


@pytest.mark.asyncio
async def test_scheduling_in_a_context_without_a_room_fails_while_reading_schedules_works() -> None:
    """Creating or editing a schedule needs the conversation's room; listing and cancelling need only its context."""
    config = _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))
    tools = SchedulerTools()

    with (
        patch("mindroom.custom_tools.scheduler.list_scheduled_tasks", new=AsyncMock(return_value="listed")),
        tool_runtime_context(replace(_make_context(config), room=None)),
    ):
        scheduled = await run_tool_call(
            toolkit_offers(tools, "schedule"),
            {"request": "in 10 minutes check logs", "new_thread": False},
            _call_context(),
            middleware=(),
        )
        listed = await run_tool_call(toolkit_offers(tools, "list_schedules"), {}, _call_context(), middleware=())

    assert (scheduled.status, scheduled.failure_text) == ("failed", "Scheduler tool is unavailable in this context.")
    assert (listed.status, listed.value) == ("ok", "listed")
