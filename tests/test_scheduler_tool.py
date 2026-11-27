"""Tests for shared schedule entrypoint and scheduler tool."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

import mindroom.tools  # noqa: F401
from mindroom.agents import AgentBuild, create_agent
from mindroom.authorization import ensure_room_membership_synced
from mindroom.config.agent import AgentConfig
from mindroom.config.approval import ApprovalRuleConfig, ToolApprovalConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import SILENT_SCHEDULE_NO_REPLY_TOKEN
from mindroom.custom_tools.scheduler import SchedulerTools
from mindroom.hooks import HookBindings
from mindroom.message_target import MessageTarget
from mindroom.scheduling.errors import ScheduledTaskNotFoundError, SchedulingError
from mindroom.scheduling.tasks import _extract_mentioned_agents_from_text
from mindroom.tool_system.invocation import CallResult, ToolCallContext, run_tool_call
from mindroom.tool_system.plugins import config_plugins
from mindroom.tool_system.runtime_context import (
    ToolRuntimeContext,
    tool_runtime_context,
)
from tests.agent_tool_helpers import toolkit_offers
from tests.authorization_helpers import (
    make_test_tool_runtime_context,
)
from tests.conftest import (
    bind_runtime_paths,
    install_model_providers,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_relation_lookup,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.fakes.scripted_model import ScriptedModel
from tests.identity_helpers import entity_ids
from tests.turn_model_helpers import turn_model


def _bind_runtime_paths(config: Config) -> Config:
    return bind_runtime_paths(config, test_runtime_paths(Path(tempfile.mkdtemp())))


def _general_config() -> Config:
    return _bind_runtime_paths(Config(agents={"general": AgentConfig(display_name="General Agent")}))


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


def _build_agent(
    config: Config,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_name: str = "general",
    supports_native_tool_approval: bool = True,
    agent_mode: str = "standard",
) -> AgentBuild:
    install_model_providers(monkeypatch, {"default": ScriptedModel([])})
    paths = runtime_paths_for(config)
    return create_agent(
        agent_name,
        config,
        paths,
        None,
        supports_native_tool_approval=supports_native_tool_approval,
        agent_mode=agent_mode,  # type: ignore[arg-type]
        knowledge=None,
        model=turn_model(config, paths, agent_name),
        plugins=config_plugins(config, paths),
    )


def test_extract_mentioned_agents_from_text() -> None:
    """Agent mentions should be extracted from scheduling text."""
    config = _general_config()
    result = _extract_mentioned_agents_from_text(
        "in 5 minutes @general check deployment",
        config,
        runtime_paths_for(config),
    )
    expected_agent = entity_ids(config, runtime_paths_for(config))["general"]
    assert result == [expected_agent]


def test_no_scheduled_mentions_need_no_prepared_accounts(tmp_path: Path) -> None:
    """Plain scheduling text does not require looking up any Matrix accounts."""
    config = Config(agents={"general": AgentConfig(display_name="General Agent")})

    assert _extract_mentioned_agents_from_text("remind me tomorrow", config, test_runtime_paths(tmp_path)) == []


def test_scheduler_tool_schema_and_delivery_contracts() -> None:
    """The consolidated scheduler schema exposes action enum and documents delivery & silent contracts."""
    function = toolkit_offers(SchedulerTools(), "scheduler")

    assert set(function.parameters["required"]) == {"action"}
    properties = function.parameters["properties"]
    assert properties["action"]["enum"] == ["create", "edit", "list", "cancel"]
    new_thread_description = properties["new_thread"]["description"].lower()
    silent_description = properties["silent"]["description"].lower()
    assert "required" in new_thread_description
    assert "silent" in new_thread_description
    assert "room-level root" in new_thread_description
    assert "hidden" in silent_description
    assert "whitespace" in silent_description
    assert SILENT_SCHEDULE_NO_REPLY_TOKEN.lower() in silent_description
    assert "findings" in silent_description
    assert "failures" in silent_description
    assert "defaults to `false`" in silent_description


@pytest.mark.asyncio
async def test_scheduler_tool_create_uses_shared_backend() -> None:
    """Create action should call the scheduling backend path, running saved tasks on the orchestrator's runtime."""
    tools = SchedulerTools()
    config = _general_config()
    orchestrator = MagicMock()
    context = replace(
        _make_context(config),
        target=MessageTarget.resolve(room_id="!room:localhost", thread_id="$thread", reply_to_event_id="$answered"),
        config_provider=lambda: config,
        orchestrator=orchestrator,
    )

    with (
        patch(
            "mindroom.custom_tools.scheduler.schedule_task",
            new=AsyncMock(return_value="✅ Scheduled"),
        ) as mock_schedule,
        tool_runtime_context(context),
    ):
        result = await tools.scheduler(
            action="create",
            message="check deployment",
            new_thread=False,
            execute_at="2026-11-25T15:00:00Z",
        )
        new_thread_result = await tools.scheduler(
            action="create",
            message="check deployment",
            new_thread=True,
            delay_seconds=3600,
            description="Hourly check",
            timezone="America/Los_Angeles",
            silent=True,
        )
        limited_result = await tools.scheduler(
            action="create",
            message="poll the queue",
            new_thread=False,
            cron="*/25 * * * *",
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
    for call_kwargs in (first_call, second_call, third_call):
        assert call_kwargs.pop("context") == context
    assert first_call == {
        "message": "check deployment",
        "new_thread": False,
        "execute_at": "2026-11-25T15:00:00Z",
        "delay_seconds": None,
        "cron": None,
        "description": None,
        "timezone": None,
        "history_limit": None,
        "silent": False,
        "model": None,
    }
    assert second_call == {
        "message": "check deployment",
        "new_thread": True,
        "execute_at": None,
        "delay_seconds": 3600,
        "cron": None,
        "description": "Hourly check",
        "timezone": "America/Los_Angeles",
        "history_limit": None,
        "silent": True,
        "model": None,
    }
    assert third_call == {
        "message": "poll the queue",
        "new_thread": False,
        "execute_at": None,
        "delay_seconds": None,
        "cron": "*/25 * * * *",
        "description": None,
        "timezone": None,
        "history_limit": 0,
        "silent": False,
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

    candidates = await context.responder_candidates_for_current_room(room, requester_id)

    assert candidates == [general_id]
    assert client.joined_members.await_count == 1


@pytest.mark.asyncio
async def test_scheduler_tool_uses_current_config_for_authorization() -> None:
    """A long-lived tool context must not schedule with its construction-time policy."""
    tools = SchedulerTools()
    old_config = _general_config()
    current_config = bind_runtime_paths(
        Config(
            timezone="America/Los_Angeles",
            agents={"general": AgentConfig(display_name="Current Agent", rooms=["!room:localhost"])},
        ),
        runtime_paths_for(old_config),
    )
    general_id = entity_ids(current_config, runtime_paths_for(old_config))["general"]
    room = nio.MatrixRoom(room_id="!room:localhost", own_user_id=general_id.full_id)
    room.add_member(general_id.full_id, "Current Agent", None)
    room.members_synced = True
    client = make_matrix_client_mock(user_id=general_id.full_id)
    client.room_put_state = AsyncMock(return_value=nio.RoomPutStateResponse("$state", "!room:localhost"))
    context = replace(
        _make_context(old_config),
        client=client,
        room=room,
        config_provider=lambda: current_config,
    )

    with tool_runtime_context(context):
        result = await tools.scheduler(action="create", message="check deployment", new_thread=False, cron="0 9 * * *")

    assert "America/Los_Angeles" in result


@pytest.mark.asyncio
async def test_scheduler_tool_edit_calls_backend() -> None:
    """Edit action should call edit_scheduled_task with correct arguments."""
    tools = SchedulerTools()
    config = _general_config()
    context = replace(_make_context(config), config_provider=lambda: config)

    with (
        patch(
            "mindroom.custom_tools.scheduler.edit_scheduled_task",
            new=AsyncMock(return_value="✅ Updated task `task123`."),
        ) as mock_edit,
        tool_runtime_context(context),
    ):
        result = await tools.scheduler(
            action="edit",
            task_id="task123",
            message="check deployment",
            execute_at="2026-11-25T09:00:00Z",
        )
        limited_result = await tools.scheduler(
            action="edit",
            task_id="task123",
            description="Updated label",
            history_limit=5,
            silent=True,
        )
        visible_result = await tools.scheduler(
            action="edit",
            task_id="task123",
            cron="0 10 * * *",
            timezone="America/Los_Angeles",
            clear_history_limit=True,
            silent=False,
            model="cheap",
        )

    assert "Updated" in result
    assert "Updated" in limited_result
    assert "Updated" in visible_result
    assert mock_edit.await_count == 3
    first_call = mock_edit.await_args_list[0].kwargs.copy()
    second_call = mock_edit.await_args_list[1].kwargs.copy()
    third_call = mock_edit.await_args_list[2].kwargs.copy()
    for call_kwargs in (first_call, second_call, third_call):
        assert call_kwargs.pop("context") == context
    assert first_call == {
        "task_id": "task123",
        "message": "check deployment",
        "description": None,
        "execute_at": "2026-11-25T09:00:00Z",
        "delay_seconds": None,
        "cron": None,
        "timezone": None,
        "history_limit": None,
        "clear_history_limit": False,
        "silent": None,
        "model": None,
    }
    assert second_call == {
        "task_id": "task123",
        "message": None,
        "description": "Updated label",
        "execute_at": None,
        "delay_seconds": None,
        "cron": None,
        "timezone": None,
        "history_limit": 5,
        "clear_history_limit": False,
        "silent": True,
        "model": None,
    }
    assert third_call == {
        "task_id": "task123",
        "message": None,
        "description": None,
        "execute_at": None,
        "delay_seconds": None,
        "cron": "0 10 * * *",
        "timezone": "America/Los_Angeles",
        "history_limit": None,
        "clear_history_limit": True,
        "silent": False,
        "model": "cheap",
    }


@pytest.mark.asyncio
async def test_scheduler_tool_list_calls_backend() -> None:
    """List action should call list_scheduled_tasks with correct arguments."""
    tools = SchedulerTools()
    config = _general_config()
    context = _make_context(config)

    with (
        patch(
            "mindroom.custom_tools.scheduler.list_scheduled_tasks",
            new=AsyncMock(return_value="**Scheduled Tasks:**\n• `abc` - in 5 minutes"),
        ) as mock_list,
        tool_runtime_context(context),
    ):
        result = await tools.scheduler(action="list")

    assert "Scheduled Tasks" in result
    mock_list.assert_awaited_once_with(
        client=context.client,
        room_id=context.room_id,
        runtime_paths=context.runtime_paths,
        thread_id=context.resolved_thread_id,
        config=context.config,
    )


@pytest.mark.asyncio
async def test_scheduler_tool_cancel_calls_backend() -> None:
    """Cancel action should call cancel_scheduled_task with correct arguments, ending the runner on the orchestrator's runtime."""
    tools = SchedulerTools()
    config = _general_config()
    orchestrator = MagicMock()
    context = replace(_make_context(config), orchestrator=orchestrator)

    with (
        patch(
            "mindroom.custom_tools.scheduler.cancel_scheduled_task",
            new=AsyncMock(return_value="✅ Cancelled task `task123`"),
        ) as mock_cancel,
        tool_runtime_context(context),
    ):
        result = await tools.scheduler(action="cancel", task_id="task123")

    assert "Cancelled" in result
    mock_cancel.assert_awaited_once_with(
        client=context.client,
        room_id=context.room_id,
        task_id="task123",
        runtime_paths=context.runtime_paths,
        task_runtime=orchestrator.scheduled_tasks,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "expected_match"),
    [
        # create: missing required params or disallowed params
        ({"action": "create", "new_thread": False, "delay_seconds": 60}, "`message` is required"),
        ({"action": "create", "message": "   ", "new_thread": False, "delay_seconds": 60}, "`message` is required"),
        ({"action": "create", "message": "ping", "delay_seconds": 60}, "`new_thread` is required"),
        ({"action": "create", "task_id": "t1", "message": "ping", "new_thread": False}, "`task_id`"),
        (
            {"action": "create", "message": "ping", "new_thread": False, "clear_history_limit": True},
            "`clear_history_limit`",
        ),
        # edit: missing task_id or disallowed new_thread
        ({"action": "edit", "message": "updated"}, "`task_id` is required"),
        ({"action": "edit", "task_id": "  ", "message": "updated"}, "`task_id` is required"),
        ({"action": "edit", "task_id": "t1", "new_thread": False}, "`new_thread`"),
        # list: rejects any non-default param
        ({"action": "list", "task_id": "t1"}, "`task_id`"),
        ({"action": "list", "new_thread": False}, "`new_thread`"),
        ({"action": "list", "silent": False}, "`silent`"),
        ({"action": "list", "clear_history_limit": True}, "`clear_history_limit`"),
        ({"action": "list", "delay_seconds": 60}, "`delay_seconds`"),
        # cancel: missing task_id or any extra param
        ({"action": "cancel"}, "`task_id` is required"),
        ({"action": "cancel", "task_id": "   "}, "`task_id` is required"),
        ({"action": "cancel", "task_id": "t1", "message": "nope"}, "`message`"),
        ({"action": "cancel", "task_id": "t1", "silent": False}, "`silent`"),
        ({"action": "cancel", "task_id": "t1", "clear_history_limit": True}, "`clear_history_limit`"),
    ],
)
async def test_scheduler_tool_validates_action_parameters(
    kwargs: dict[str, object],
    expected_match: str,
) -> None:
    """Mismatched or missing parameters per action fail cleanly with SchedulingError."""
    tools = SchedulerTools()
    config = _general_config()

    with (
        tool_runtime_context(_make_context(config)),
        pytest.raises(SchedulingError, match=expected_match),
    ):
        await tools.scheduler(**kwargs)  # type: ignore[arg-type]


async def _call(name: str, arguments: dict[str, object]) -> CallResult:
    """Run one scheduler function as an agent run calls it, under no tool-call policy."""
    return await run_tool_call(
        toolkit_offers(SchedulerTools(), name),
        arguments,
        ToolCallContext(run_id="run", session_id="session", call_id="call-1", user_id=None),
        middleware=(),
    )


_NEEDS_ROOM = [
    ("scheduler", {"action": "create", "message": "check logs", "new_thread": False, "delay_seconds": 600}),
    (
        "schedule_tool_call",
        {
            "tool_name": "post_slack_message",
            "arguments_json": "{}",
            "execute_at": "2030-01-02T09:00:00-05:00",
            "description": "Morning DM",
        },
    ),
    ("scheduler", {"action": "edit", "task_id": "task123", "delay_seconds": 600}),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        *_NEEDS_ROOM,
        ("scheduler", {"action": "list"}),
        ("scheduler", {"action": "cancel", "task_id": "task123"}),
        ("run_scheduled_call", {"task_id": "task123"}),
    ],
)
async def test_scheduler_call_outside_a_conversation_fails(name: str, arguments: dict[str, object]) -> None:
    """A scheduler call made where scheduling cannot run is a failed call that says why."""
    result = await _call(name, arguments)

    assert result.status == "failed"
    assert result.failure_text == "Scheduler tool is unavailable in this context."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "operation", "arguments", "error"),
    [
        (
            "scheduler",
            "schedule_task",
            {"action": "create", "message": "next message", "new_thread": False, "delay_seconds": 60},
            SchedulingError("Failed to schedule: schedule is not valid"),
        ),
        (
            "scheduler",
            "edit_scheduled_task",
            {"action": "edit", "task_id": "task123", "message": "check deployment"},
            SchedulingError("Failed to edit task `task123`."),
        ),
        (
            "scheduler",
            "list_scheduled_tasks",
            {"action": "list"},
            SchedulingError("Unable to retrieve scheduled tasks."),
        ),
        (
            "scheduler",
            "cancel_scheduled_task",
            {"action": "cancel", "task_id": "task123"},
            ScheduledTaskNotFoundError("task123"),
        ),
    ],
)
async def test_scheduler_operation_failure_is_a_failed_call_with_its_reason(
    name: str,
    operation: str,
    arguments: dict[str, object],
    error: SchedulingError,
) -> None:
    """A scheduling operation that did not happen fails the call with the operation's reason as its text."""
    config = _general_config()
    with (
        patch(f"mindroom.custom_tools.scheduler.{operation}", new=AsyncMock(side_effect=error)),
        tool_runtime_context(_make_context(config)),
    ):
        result = await _call(name, arguments)

    assert result.status == "failed"
    assert result.failure_text == str(error)


@pytest.mark.asyncio
@pytest.mark.parametrize(("name", "arguments"), _NEEDS_ROOM)
async def test_creating_or_editing_a_schedule_without_a_room_fails(name: str, arguments: dict[str, object]) -> None:
    """Creating or editing a schedule needs the conversation's room."""
    config = _general_config()

    with tool_runtime_context(replace(_make_context(config), room=None)):
        result = await _call(name, arguments)

    assert (result.status, result.failure_text) == ("failed", "Scheduler tool is unavailable in this context.")


@pytest.mark.asyncio
async def test_listing_schedules_needs_no_room() -> None:
    """Listing schedules needs only the conversation's context, not its room."""
    config = _general_config()

    with (
        patch("mindroom.custom_tools.scheduler.list_scheduled_tasks", new=AsyncMock(return_value="listed")),
        tool_runtime_context(replace(_make_context(config), room=None)),
    ):
        result = await _call("scheduler", {"action": "list"})

    assert (result.status, result.value) == ("ok", "listed")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("schedule_task", {"action": "create", "message": "ping", "new_thread": False, "delay_seconds": True}),
        (
            "schedule_task",
            {"action": "create", "message": "ping", "new_thread": False, "delay_seconds": 60, "history_limit": True},
        ),
        ("edit_scheduled_task", {"action": "edit", "task_id": "task123", "delay_seconds": True}),
        ("edit_scheduled_task", {"action": "edit", "task_id": "task123", "history_limit": True}),
    ],
)
async def test_scheduler_tool_rejects_booleans_for_integer_parameters(
    operation: str,
    arguments: dict[str, object],
) -> None:
    """The offered scheduler tool rejects booleans for integer parameters instead of coercing True to 1."""
    config = _general_config()
    with (
        patch(f"mindroom.custom_tools.scheduler.{operation}", new=AsyncMock()) as backend,
        tool_runtime_context(_make_context(config)),
    ):
        result = await _call("scheduler", arguments)

    assert result.status == "failed"
    assert "Input should be a valid integer" in (result.failure_text or "")
    backend.assert_not_awaited()


def test_scheduled_call_helpers_hidden_without_gated_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agents with no schedulable approval-gated tools hide schedule_tool_call and run_scheduled_call."""
    config = _bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["scheduler", "calculator", "thread_model"],
                    delegate_to=["helper"],
                    include_default_tools=False,
                ),
                "helper": AgentConfig(display_name="Helper Agent"),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            # Even an approval rule on a controls_run function or delegation does not enable scheduled-call helpers.
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="thread_model_write", action="require_approval")],
            ),
        ),
    )

    build = _build_agent(config, monkeypatch)

    assert "scheduler" in build.spec.functions
    assert "schedule_tool_call" not in build.spec.functions
    assert "run_scheduled_call" not in build.spec.functions
    assert all(mw.__name__ != "bind_live_functions" for mw in build.tool_policy)


def test_scheduled_call_helpers_included_when_agent_has_policy_gated_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent with a schedulable policy-gated tool automatically gets schedule_tool_call and run_scheduled_call."""
    config = _bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["scheduler", "calculator"],
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="calculate", action="require_approval")],
            ),
        ),
    )

    build = _build_agent(config, monkeypatch)

    assert {"scheduler", "schedule_tool_call", "run_scheduled_call"} <= set(build.spec.functions)
    assert any(mw.__name__ == "bind_live_functions" for mw in build.tool_policy)


def test_scheduled_call_helpers_included_for_confirm_gated_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """An agent with a ToolGate.CONFIRM tool (like self_config) keeps schedule_tool_call and run_scheduled_call."""
    config = _bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["scheduler"],
                    allow_self_config=True,
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
    )

    build = _build_agent(config, monkeypatch)

    assert {"scheduler", "schedule_tool_call", "run_scheduled_call"} <= set(build.spec.functions)


def test_scheduled_call_helpers_included_for_deferred_gated_tool(monkeypatch: pytest.MonkeyPatch) -> None:
    """An approval-gated deferred toolkit still enables schedule_tool_call and run_scheduled_call."""
    config = _bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["scheduler", {"calculator": {"defer": True}}],
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="calculate", action="require_approval")],
            ),
        ),
    )

    build = _build_agent(config, monkeypatch)

    assert {"scheduler", "schedule_tool_call", "run_scheduled_call"} <= set(build.spec.functions)


def test_scheduled_call_helpers_included_when_explicitly_requested_via_include_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit include_tools keeps scheduled-call helpers even when no other tool is approval-gated."""
    config = _bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=[{"scheduler": {"include_tools": ["scheduler", "schedule_tool_call"]}}],
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
    )

    build = _build_agent(config, monkeypatch)

    assert "scheduler" in build.spec.functions
    assert "schedule_tool_call" in build.spec.functions
    assert "run_scheduled_call" not in build.spec.functions
    assert any(mw.__name__ == "bind_live_functions" for mw in build.tool_policy)


@pytest.mark.asyncio
async def test_scheduled_call_helpers_in_minimal_mode_respect_cli_deferred_gated_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In minimal mode, CLI-deferred scheduler omits helpers without gated tools and keeps them with a CLI-deferred gated tool."""
    ungated_config = _bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=[{"scheduler": {"defer": True}}, {"calculator": {"defer": True}}],
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
    )
    ungated_build = _build_agent(ungated_config, monkeypatch, agent_mode="minimal")
    ungated_scheduler = next(d for d in ungated_build.surface.deferred if d.name == "scheduler")
    ungated_adapted = await ungated_scheduler.materialize()
    assert [f.name for f in ungated_adapted.functions] == ["scheduler"]

    gated_config = _bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=[{"scheduler": {"defer": True}}, {"calculator": {"defer": True}}],
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
            tool_approval=ToolApprovalConfig(
                rules=[ApprovalRuleConfig(match="calculate", action="require_approval")],
            ),
        ),
    )
    gated_build = _build_agent(gated_config, monkeypatch, agent_mode="minimal")
    gated_scheduler = next(d for d in gated_build.surface.deferred if d.name == "scheduler")
    gated_adapted = await gated_scheduler.materialize()
    assert {f.name for f in gated_adapted.functions} == {"scheduler", "schedule_tool_call", "run_scheduled_call"}


def test_explicit_include_tools_in_saved_settings_keeps_scheduled_call_helper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Saved dashboard `include_tools` on `scheduler` is honored when pruning scheduled-call helpers."""
    from mindroom.credentials.manager import get_runtime_credentials_manager  # noqa: PLC0415

    runtime_paths = test_runtime_paths(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={
                "general": AgentConfig(
                    display_name="General Agent",
                    tools=["scheduler"],
                    include_default_tools=False,
                ),
            },
            models={"default": ModelConfig(provider="openai", id="test-model")},
        ),
        runtime_paths,
    )
    get_runtime_credentials_manager(runtime_paths).save_credentials(
        "scheduler",
        {"include_tools": ["scheduler", "run_scheduled_call"]},
    )

    build = _build_agent(config, monkeypatch)

    assert "scheduler" in build.spec.functions
    assert "run_scheduled_call" in build.spec.functions
    assert "schedule_tool_call" not in build.spec.functions
    assert any(mw.__name__ == "bind_live_functions" for mw in build.tool_policy)
