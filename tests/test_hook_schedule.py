"""Tests for schedule hook integration."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

import pytest

from mindroom.config.main import Config
from mindroom.hooks import EVENT_SCHEDULE_FIRED, HookRegistry, ScheduleFiredContext, hook
from mindroom.scheduling import ScheduledWorkflow, _execute_scheduled_workflow, set_scheduling_hook_registry
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator
    from pathlib import Path


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(Config(), runtime_paths)


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


def _workflow(message: str) -> ScheduledWorkflow:
    return ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC),
        message=message,
        description="hooked schedule",
        room_id="!room:localhost",
        thread_id="$thread",
        created_by="@user:localhost",
    )


@pytest.fixture(autouse=True)
def reset_schedule_registry() -> Generator[None, None, None]:
    """Keep the module-global scheduling registry isolated per test."""
    set_scheduling_hook_registry(HookRegistry.empty())
    yield
    set_scheduling_hook_registry(HookRegistry.empty())


@pytest.mark.asyncio
async def test_schedule_hook_rewrites_message_text(tmp_path: Path) -> None:
    """schedule:fired hooks should be able to rewrite the synthetic message body."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def rewrite(ctx: ScheduleFiredContext) -> None:
        ctx.message_text = f"{ctx.message_text} with agenda"

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [rewrite])]))

    with patch("mindroom.scheduling.send_message", new=AsyncMock()) as mock_send:
        await _execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Prepare for meeting"),
            config,
            runtime_paths_for(config),
        )

    content = mock_send.await_args.args[2]
    assert "Prepare for meeting with agenda" in content["body"]


@pytest.mark.asyncio
async def test_schedule_hook_can_suppress_synthetic_message(tmp_path: Path) -> None:
    """schedule:fired hooks should be able to suppress downstream message creation."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def suppress(ctx: ScheduleFiredContext) -> None:
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [suppress])]))

    with patch("mindroom.scheduling.send_message", new=AsyncMock()) as mock_send:
        await _execute_scheduled_workflow(AsyncMock(), _workflow("Do not send"), config, runtime_paths_for(config))

    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_hook_send_message_inherits_context_thread_id(tmp_path: Path) -> None:
    """schedule:fired hook sends should default to the workflow thread."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def notify(ctx: ScheduleFiredContext) -> None:
        await ctx.send_message(ctx.room_id, "resume")
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [notify])]))
    client = AsyncMock()
    client.user_id = "@mindroom_router:localhost"

    with (
        patch(
            "mindroom.hooks.sender.get_latest_thread_event_id_if_needed",
            new=AsyncMock(return_value="$latest"),
        ) as mock_latest_thread,
        patch("mindroom.hooks.sender.send_message", new=AsyncMock(return_value="$hook-event")) as mock_hook_send,
        patch("mindroom.scheduling.send_message", new=AsyncMock()) as mock_schedule_send,
    ):
        await _execute_scheduled_workflow(client, _workflow("Resume work"), config, runtime_paths_for(config))

    mock_latest_thread.assert_awaited_once_with(client, "!room:localhost", "$thread")
    mock_schedule_send.assert_not_called()
    content = mock_hook_send.await_args.args[2]
    assert content["body"] == "resume"
    assert content["m.relates_to"]["event_id"] == "$thread"


@pytest.mark.asyncio
async def test_schedule_hook_send_message_allows_explicit_room_level_opt_out(tmp_path: Path) -> None:
    """schedule:fired hook sends should stay room-level when thread_id=None is explicit."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def notify(ctx: ScheduleFiredContext) -> None:
        await ctx.send_message(ctx.room_id, "room-level", thread_id=None)
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [notify])]))
    client = AsyncMock()
    client.user_id = "@mindroom_router:localhost"

    with (
        patch(
            "mindroom.hooks.sender.get_latest_thread_event_id_if_needed",
            new=AsyncMock(return_value=None),
        ) as mock_latest_thread,
        patch("mindroom.hooks.sender.send_message", new=AsyncMock(return_value="$hook-event")) as mock_hook_send,
        patch("mindroom.scheduling.send_message", new=AsyncMock()) as mock_schedule_send,
    ):
        await _execute_scheduled_workflow(client, _workflow("Resume work"), config, runtime_paths_for(config))

    mock_latest_thread.assert_awaited_once_with(client, "!room:localhost", None)
    mock_schedule_send.assert_not_called()
    content = mock_hook_send.await_args.args[2]
    assert content["body"] == "room-level"
    assert "m.relates_to" not in content


@pytest.mark.asyncio
async def test_schedule_hook_send_message_can_trigger_dispatch(tmp_path: Path) -> None:
    """schedule:fired hooks should be able to request dispatch-triggering sends."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def notify(ctx: ScheduleFiredContext) -> None:
        await ctx.send_message(ctx.room_id, "dispatch", trigger_dispatch=True)
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [notify])]))
    client = AsyncMock()
    client.user_id = "@mindroom_router:localhost"

    with (
        patch(
            "mindroom.hooks.sender.get_latest_thread_event_id_if_needed",
            new=AsyncMock(return_value="$latest"),
        ),
        patch("mindroom.hooks.sender.send_message", new=AsyncMock(return_value="$hook-event")) as mock_hook_send,
        patch("mindroom.scheduling.send_message", new=AsyncMock()) as mock_schedule_send,
    ):
        await _execute_scheduled_workflow(client, _workflow("Resume work"), config, runtime_paths_for(config))

    mock_schedule_send.assert_not_called()
    content = mock_hook_send.await_args.args[2]
    assert content["body"] == "dispatch"
    assert content["com.mindroom.source_kind"] == "hook_dispatch"
