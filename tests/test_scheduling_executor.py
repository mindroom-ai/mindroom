"""Tests for firing one scheduled task through the scheduling executor."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import pytest

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import ORIGINAL_SENDER_KEY, SOURCE_KIND_KEY
from mindroom.dispatch_source import SCHEDULED_SOURCE_KIND
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
) -> ScheduledWorkflow:
    return ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC),
        message=message,
        description="executor test task",
        room_id=room_id,
        thread_id=thread_id,
        new_thread=new_thread,
        created_by="@user:localhost",
    )


def _conversation_cache(*, latest_thread_event_id: str | None = None) -> AsyncMock:
    access = AsyncMock()
    access.get_latest_thread_event_id_if_needed.return_value = latest_thread_event_id
    access.notify_outbound_message = Mock()
    return access


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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with patch(
        "mindroom.hooks.sender._send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            conversation_cache,
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


@pytest.mark.asyncio
async def test_fire_new_thread_task_posts_room_level_message(tmp_path: Path) -> None:
    """new_thread tasks deliver the raw message at room level without the automated wrapper."""
    config = _config(tmp_path)
    workflow = _workflow("Kick off the weekly report", thread_id=None, new_thread=True)
    conversation_cache = _conversation_cache()

    with patch(
        "mindroom.hooks.sender._send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            conversation_cache,
        )

    assert outcome.delivered is True
    conversation_cache.get_latest_thread_event_id_if_needed.assert_not_awaited()
    content = mock_send.await_args.args[2]
    assert "⏰ [Automated Task]" not in content["body"]
    assert "m.relates_to" not in content


@pytest.mark.asyncio
async def test_fire_task_without_room_id_is_typed_failure(tmp_path: Path) -> None:
    """A workflow without a room is a typed failure and never touches Matrix."""
    config = _config(tmp_path)
    workflow = _workflow("Orphaned task", room_id=None, thread_id=None)

    with patch("mindroom.hooks.sender._send_message_result", new=AsyncMock()) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            _conversation_cache(),
        )

    assert outcome.delivered is False
    assert outcome.failure_reason == "missing room_id"
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_delivery_returning_none_yields_failure_and_notice(tmp_path: Path) -> None:
    """A send that returns no delivered event produces a failure outcome plus a visible notice."""
    config = _config(tmp_path)
    workflow = _workflow("Check the queue depth")
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with patch(
        "mindroom.hooks.sender._send_message_result",
        new=AsyncMock(side_effect=[None, None]),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            conversation_cache,
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
        "mindroom.hooks.sender._send_message_result",
        new=AsyncMock(side_effect=RuntimeError("boom")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            workflow,
            config,
            runtime_paths_for(config),
            _conversation_cache(latest_thread_event_id="$latest"),
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
        "mindroom.hooks.sender._send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$delivered")),
    ) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Prepare the agenda"),
            config,
            runtime_paths_for(config),
            _conversation_cache(latest_thread_event_id="$latest"),
            task_id="task-hooked",
        )

    assert outcome.delivered is True
    assert seen == [("task-hooked", "$thread")]
    assert "Prepare the agenda (hooked)" in mock_send.await_args.args[2]["body"]


@pytest.mark.asyncio
async def test_hook_suppression_is_undelivered_outcome(tmp_path: Path) -> None:
    """Hook suppression yields an undelivered outcome without sending anything."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def suppress(ctx: ScheduleFiredContext) -> None:
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [suppress])]))

    with patch("mindroom.hooks.sender._send_message_result", new=AsyncMock()) as mock_send:
        outcome = await execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Do not send"),
            config,
            runtime_paths_for(config),
            _conversation_cache(),
        )

    assert outcome.delivered is False
    assert outcome.failure_reason == "suppressed by hook"
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_send_scheduled_failure_notice_follows_workflow_target(tmp_path: Path) -> None:
    """Runner failure notices follow the workflow thread and reply to its latest event."""
    config = _config(tmp_path)
    workflow = _workflow("Recurring job")
    target = MessageTarget.for_scheduled_task(workflow)
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with patch(
        "mindroom.hooks.sender._send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$notice")),
    ) as mock_send:
        await send_scheduled_failure_notice(
            AsyncMock(),
            workflow,
            target,
            "❌ Recurring task failed: executor test task\nTask ID: task-9\nError: boom",
            config,
            conversation_cache,
        )

    mock_send.assert_awaited_once()
    content = mock_send.await_args.args[2]
    assert content["body"].startswith("❌ Recurring task failed: executor test task")
    assert content["m.relates_to"]["event_id"] == "$thread"
    assert content["m.relates_to"]["m.in_reply_to"]["event_id"] == "$latest"
