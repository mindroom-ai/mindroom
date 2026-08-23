"""Tests for firing one scheduled task through the scheduling executor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import (
    ORIGINAL_SENDER_KEY,
    PER_FIRE_THREAD_ROOT_KEY,
    SCHEDULED_HISTORY_LIMIT_KEY,
    SILENT_SCHEDULE_EVENT_TYPE,
    SOURCE_KIND_KEY,
)
from mindroom.dispatch_source import SCHEDULED_SOURCE_KIND, SILENT_SCHEDULE_SOURCE_KIND
from mindroom.entity_resolution import entity_identity_registry
from mindroom.hooks import EVENT_SCHEDULE_FIRED, HookRegistry, ScheduleFiredContext, hook
from mindroom.message_target import MessageTarget
from mindroom.scheduling import ScheduledWorkflow
from mindroom.scheduling_executor import (
    execute_scheduled_workflow,
    send_scheduled_failure_notice,
    set_scheduling_hook_registry,
)
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.identity_helpers import persist_entity_accounts

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(Config(), test_runtime_paths(tmp_path))


def _agent_config(tmp_path: Path) -> Config:
    config = bind_runtime_paths(
        Config(
            agents={"research": AgentConfig(display_name="Research")},
            models={"default": ModelConfig(provider="test", id="test-model")},
        ),
        test_runtime_paths(tmp_path),
    )
    persist_entity_accounts(
        config,
        runtime_paths_for(config),
        usernames={"router": "router", "research": "research"},
    )
    return config


def _workflow(
    message: str,
    *,
    room_id: str | None = "!room:localhost",
    thread_id: str | None = "$thread",
    new_thread: bool = False,
    history_limit: int | None = None,
    silent: bool = False,
) -> ScheduledWorkflow:
    return ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC),
        message=message,
        description="executor test task",
        history_limit=history_limit,
        room_id=room_id,
        thread_id=thread_id,
        new_thread=new_thread,
        silent=silent,
        created_by="@user:localhost",
    )


def _conversation_reader(*, latest_thread_event_id: str | None = None) -> AsyncMock:
    reader = AsyncMock()
    reader.latest_thread_event_id.return_value = latest_thread_event_id
    return reader


def _plugin(name: str, callbacks: list[object]) -> object:
    return type(
        "PluginStub",
        (),
        {
            "name": name,
            "discovered_hooks": tuple(callbacks),
            "entry_config": type("Entry", (), {"settings": {}, "hooks": {}})(),
            "plugin_order": 0,
        },
    )()


@pytest.fixture(autouse=True)
def reset_schedule_registry() -> Generator[None, None, None]:
    """Keep the module-global scheduling hook registry isolated per test."""
    set_scheduling_hook_registry(HookRegistry.empty())
    yield
    set_scheduling_hook_registry(HookRegistry.empty())


@pytest.mark.asyncio
async def test_fire_task_with_valid_agent_delivers_in_thread(tmp_path: Path) -> None:
    """Firing a task targeting a known agent delivers the automated message into the thread."""
    config = _agent_config(tmp_path)
    workflow = _workflow("@research Summarize today's AI news")
    conversation_reader = _conversation_reader(latest_thread_event_id="$latest")

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            conversation_reader,
            task_id="task-1",
        )

    assert outcome.delivered is True
    assert outcome.failure_reason is None
    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[1] == "!room:localhost"
    content = mock_send.await_args.args[2]
    assert content["body"].startswith("⏰ [Automated Task]\n")
    registry = entity_identity_registry(config, runtime_paths_for(config))
    assert registry.current_id("research").full_id in content["body"]
    assert content["m.relates_to"]["event_id"] == "$thread"
    assert content[ORIGINAL_SENDER_KEY] == "@user:localhost"
    assert content[SOURCE_KIND_KEY] == SCHEDULED_SOURCE_KIND
    assert SCHEDULED_HISTORY_LIMIT_KEY not in content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("silent", "message_type", "source_kind"),
    [
        (False, "m.room.message", SCHEDULED_SOURCE_KIND),
        (True, SILENT_SCHEDULE_EVENT_TYPE, SILENT_SCHEDULE_SOURCE_KIND),
    ],
)
async def test_schedule_transport_preserves_requester_and_history_metadata(
    tmp_path: Path,
    silent: bool,
    message_type: str,
    source_kind: str,
) -> None:
    """Visible and silent fires retain provenance while choosing their transport."""
    config = _config(tmp_path)
    workflow = _workflow("Reconcile the queue", history_limit=5, silent=silent)

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            _conversation_reader(latest_thread_event_id="$latest"),
        )

    assert outcome.delivered is True
    content = mock_send.await_args.args[2]
    assert content[ORIGINAL_SENDER_KEY] == "@user:localhost"
    assert content[SCHEDULED_HISTORY_LIMIT_KEY] == 5
    assert content[SOURCE_KIND_KEY] == source_kind
    assert content["m.relates_to"]["event_id"] == "$thread"
    assert mock_send.await_args.kwargs["message_type"] == message_type


@pytest.mark.asyncio
@pytest.mark.parametrize("history_limit", [0, 5])
async def test_fire_task_with_history_limit_annotates_message_content(tmp_path: Path, history_limit: int) -> None:
    """A per-schedule history limit rides on the fired message so dispatch can cap that turn."""
    config = _agent_config(tmp_path)
    workflow = _workflow("@research Poll the queue", history_limit=history_limit)
    conversation_reader = _conversation_reader(latest_thread_event_id="$latest")

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            conversation_reader,
            task_id="task-1",
        )

    assert outcome.delivered is True
    content = mock_send.await_args.args[2]
    assert content[SOURCE_KIND_KEY] == SCHEDULED_SOURCE_KIND
    assert content[SCHEDULED_HISTORY_LIMIT_KEY] == history_limit


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_thread_id", [None, "$stale-thread"])
async def test_fire_new_thread_task_posts_room_level_message(tmp_path: Path, stale_thread_id: str | None) -> None:
    """new_thread tasks deliver a relation-free root even when a stale thread_id is persisted."""
    config = _config(tmp_path)
    workflow = _workflow("Kick off the weekly report", thread_id=stale_thread_id, new_thread=True)
    conversation_reader = _conversation_reader()

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            conversation_reader,
        )

    assert outcome.delivered is True
    conversation_reader.latest_thread_event_id.assert_not_awaited()
    content = mock_send.await_args.args[2]
    assert "⏰ [Automated Task]" not in content["body"]
    assert "m.relates_to" not in content
    assert content[PER_FIRE_THREAD_ROOT_KEY] is True


@pytest.mark.asyncio
async def test_silent_new_thread_fire_does_not_claim_a_visible_per_fire_root(tmp_path: Path) -> None:
    """A silent root starts no visible per-fire thread ownership."""
    config = _config(tmp_path)
    workflow = _workflow("Reconcile the queue", thread_id=None, new_thread=True, silent=True)

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            _conversation_reader(),
        )

    assert outcome.delivered is True
    content = mock_send.await_args.args[2]
    assert PER_FIRE_THREAD_ROOT_KEY not in content
    assert mock_send.await_args.kwargs["message_type"] == SILENT_SCHEDULE_EVENT_TYPE


@pytest.mark.asyncio
async def test_fire_room_level_task_without_new_thread_keeps_room_scope(tmp_path: Path) -> None:
    """A room-level task without new_thread does not claim a per-fire root."""
    config = _config(tmp_path)
    workflow = _workflow("Check the shared queue", thread_id=None, new_thread=False)

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            _conversation_reader(),
        )

    assert outcome.delivered is True
    content = mock_send.await_args.args[2]
    assert "m.relates_to" not in content
    assert PER_FIRE_THREAD_ROOT_KEY not in content


@pytest.mark.asyncio
async def test_fire_task_without_room_id_is_typed_failure(tmp_path: Path) -> None:
    """A workflow without a room is a typed failure and never touches Matrix."""
    config = _config(tmp_path)
    workflow = _workflow("Orphaned task", room_id=None, thread_id=None)

    with patch("mindroom.scheduling_executor.send_matrix_message", new=AsyncMock()) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            _conversation_reader(),
        )

    assert outcome.delivered is False
    assert outcome.failure_reason == "missing room_id"
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_delivery_returning_none_yields_failure_and_notice(tmp_path: Path) -> None:
    """A send that returns no delivered event produces a failure outcome plus a visible notice."""
    config = _config(tmp_path)
    workflow = _workflow("Check the queue depth")
    conversation_reader = _conversation_reader(latest_thread_event_id="$latest")

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=[None, None]),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            conversation_reader,
        )

    assert outcome.delivered is False
    assert outcome.failure_reason == "Failed to send scheduled workflow message to Matrix"
    assert mock_send.await_count == 2
    notice_content = mock_send.await_args_list[1].args[2]
    assert notice_content["body"] == (
        "❌ Scheduled task failed: executor test task\nError: Failed to send scheduled workflow message to Matrix"
    )
    assert notice_content["m.relates_to"]["event_id"] == "$thread"


@pytest.mark.asyncio
async def test_delivery_exception_yields_failure_without_raising(tmp_path: Path) -> None:
    """Send errors, including a failing notice send, never escape the executor."""
    config = _config(tmp_path)
    workflow = _workflow("Check the queue depth")

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            _conversation_reader(latest_thread_event_id="$latest"),
        )

    assert outcome.delivered is False
    assert outcome.failure_reason == "boom"
    assert mock_send.await_count == 2  # original send plus the (also failing) notice


@pytest.mark.asyncio
async def test_hook_emission_fires_with_task_context(tmp_path: Path) -> None:
    """schedule:fired hooks run for fired tasks and can rewrite the delivered message."""
    seen: list[tuple[str, str | None]] = []

    @hook(EVENT_SCHEDULE_FIRED)
    async def rewrite(ctx: ScheduleFiredContext) -> None:
        seen.append((ctx.task_id, ctx.thread_id))
        ctx.message_text = f"{ctx.message_text} (hooked)"

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [rewrite])]))

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Prepare the agenda"),
            config,
            runtime_paths_for(config),
            _conversation_reader(latest_thread_event_id="$latest"),
            task_id="task-hooked",
        )

    assert outcome.delivered is True
    assert seen == [("task-hooked", "$thread")]
    assert "Prepare the agenda (hooked)" in mock_send.await_args.args[2]["body"]


@pytest.mark.asyncio
async def test_silent_hook_transform_is_sent_as_custom_event(tmp_path: Path) -> None:
    """A hook rewrite stays in the silent event payload."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def rewrite(ctx: ScheduleFiredContext) -> None:
        ctx.message_text = "Transformed schedule"

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [rewrite])]))

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Original schedule", silent=True),
            config,
            runtime_paths_for(config),
            _conversation_reader(latest_thread_event_id="$latest"),
        )

    assert outcome.delivered is True
    assert "Transformed schedule" in mock_send.await_args.args[2]["body"]
    assert mock_send.await_args.kwargs["message_type"] == SILENT_SCHEDULE_EVENT_TYPE


@pytest.mark.asyncio
async def test_empty_hook_transform_fails_before_silent_trigger_transport(tmp_path: Path) -> None:
    """An empty hook rewrite must not be accepted as a successfully fired silent task."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def empty(ctx: ScheduleFiredContext) -> None:
        ctx.message_text = " \n\t"

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [empty])]))

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$failure")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Original schedule", new_thread=True, silent=True),
            config,
            runtime_paths_for(config),
            _conversation_reader(),
        )

    assert outcome.delivered is False
    assert outcome.failure_reason == "Scheduled workflow message is empty after hooks"
    mock_send.assert_awaited_once()
    assert "message_type" not in mock_send.await_args.kwargs
    assert mock_send.await_args.args[2]["body"].startswith("❌ Scheduled task failed:")


@pytest.mark.asyncio
async def test_hook_suppression_is_undelivered_outcome(tmp_path: Path) -> None:
    """Hook suppression yields an undelivered outcome without sending anything."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def suppress(ctx: ScheduleFiredContext) -> None:
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [suppress])]))

    with patch("mindroom.scheduling_executor.send_matrix_message", new=AsyncMock()) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Do not send", silent=True),
            config,
            runtime_paths_for(config),
            _conversation_reader(),
        )

    assert outcome.delivered is False
    assert outcome.failure_reason == "suppressed by hook"
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_send_scheduled_failure_notice_follows_workflow_target() -> None:
    """Runner failure notices follow the workflow thread and reply to its latest event."""
    workflow = _workflow("Recurring job")
    target = MessageTarget.for_scheduled_task(workflow)
    conversation_reader = _conversation_reader(latest_thread_event_id="$latest")

    with patch(
        "mindroom.scheduling_executor.send_matrix_message",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$notice")),
    ) as mock_send:
        await send_scheduled_failure_notice(
            AsyncMock(),
            workflow,
            target,
            "❌ Recurring task failed: executor test task\nTask ID: task-9\nError: boom",
            conversation_reader,
        )

    mock_send.assert_awaited_once()
    content = mock_send.await_args.args[2]
    assert content["body"].startswith("❌ Recurring task failed: executor test task")
    assert content["m.relates_to"]["event_id"] == "$thread"
    assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$latest"
