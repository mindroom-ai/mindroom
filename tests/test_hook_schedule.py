"""Tests for schedule hook integration."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.hooks import EVENT_SCHEDULE_FIRED, HookRegistry, ScheduleFiredContext, build_hook_matrix_admin, hook
from mindroom.logging_config import setup_logging
from mindroom.scheduling import (
    CronSchedule,
    ScheduledWorkflow,
    _execute_scheduled_workflow,
    _run_cron_task,
    _run_once_task,
    set_scheduling_hook_registry,
)
from tests.conftest import (
    bind_runtime_paths,
    delivered_matrix_side_effect,
    make_event_cache_mock,
    runtime_paths_for,
    test_runtime_paths,
)

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


def _conversation_cache(*, latest_thread_event_id: str | None = None) -> AsyncMock:
    access = AsyncMock()
    access.get_latest_thread_event_id_if_needed.return_value = latest_thread_event_id
    access.notify_outbound_message = Mock()
    return access


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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with (
        patch(
            "mindroom.scheduling.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$scheduled")),
        ) as mock_send,
    ):
        await _execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Prepare for meeting"),
            config,
            runtime_paths_for(config),
            conversation_cache,
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

    with patch(
        "mindroom.scheduling.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$scheduled")),
    ) as mock_send:
        await _execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Do not send"),
            config,
            runtime_paths_for(config),
            _conversation_cache(),
        )

    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_schedule_hook_suppression_log_includes_workflow_thread_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Suppression logs should carry the workflow room/thread context."""

    @hook(EVENT_SCHEDULE_FIRED)
    async def suppress(ctx: ScheduleFiredContext) -> None:
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [suppress])]))
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=runtime_paths_for(config))
    capsys.readouterr()

    with patch(
        "mindroom.scheduling.send_message_result",
        new=AsyncMock(side_effect=delivered_matrix_side_effect("$scheduled")),
    ):
        await _execute_scheduled_workflow(
            AsyncMock(),
            _workflow("Do not send"),
            config,
            runtime_paths_for(config),
            _conversation_cache(),
        )

    payloads = [json.loads(line) for line in capsys.readouterr().err.strip().splitlines()]
    suppression_payload = next(
        payload for payload in payloads if payload["event"] == "Scheduled workflow suppressed by hook"
    )

    assert suppression_payload["room_id"] == "!room:localhost"
    assert suppression_payload["thread_id"] == "$thread"


@pytest.mark.asyncio
async def test_one_time_task_cancel_log_includes_workflow_thread_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Runner cancellation logs should include one-time workflow room/thread context."""
    config = _config(tmp_path)
    workflow = ScheduledWorkflow(
        schedule_type="once",
        execute_at=datetime.now(UTC) + timedelta(seconds=30),
        message="Later",
        description="cancelled one-time",
        room_id="!room:localhost",
        thread_id="$thread",
        created_by="@user:localhost",
    )
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=runtime_paths_for(config))
    capsys.readouterr()

    async def fake_get_pending_task_record(**_: object) -> SimpleNamespace:
        return SimpleNamespace(workflow=workflow)

    with patch("mindroom.scheduling._get_pending_task_record", new=fake_get_pending_task_record):
        task = asyncio.create_task(
            _run_once_task(
                AsyncMock(),
                "task-1",
                workflow,
                config,
                runtime_paths_for(config),
                make_event_cache_mock(),
                _conversation_cache(),
            ),
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    payloads = [json.loads(line) for line in capsys.readouterr().err.strip().splitlines()]
    cancel_payload = next(payload for payload in payloads if payload["event"] == "one_time_task_cancelled")

    assert cancel_payload["room_id"] == "!room:localhost"
    assert cancel_payload["thread_id"] == "$thread"


@pytest.mark.asyncio
async def test_cron_task_cancel_log_includes_workflow_thread_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Runner cancellation logs should include recurring workflow room/thread context."""
    config = _config(tmp_path)
    workflow = ScheduledWorkflow(
        schedule_type="cron",
        cron_schedule=CronSchedule(),
        message="Recurring",
        description="cancelled recurring",
        room_id="!room:localhost",
        thread_id="$thread",
        created_by="@user:localhost",
    )
    monkeypatch.setenv("MINDROOM_LOG_FORMAT", "json")
    setup_logging(level="INFO", runtime_paths=runtime_paths_for(config))
    capsys.readouterr()

    async def fake_get_pending_task_record(**_: object) -> SimpleNamespace:
        return SimpleNamespace(workflow=workflow)

    with patch("mindroom.scheduling._get_pending_task_record", new=fake_get_pending_task_record):
        task = asyncio.create_task(
            _run_cron_task(
                AsyncMock(),
                "task-1",
                workflow,
                {},
                config,
                runtime_paths_for(config),
                _conversation_cache(),
            ),
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    payloads = [json.loads(line) for line in capsys.readouterr().err.strip().splitlines()]
    cancel_payload = next(payload for payload in payloads if payload["event"] == "cron_task_cancelled")

    assert cancel_payload["room_id"] == "!room:localhost"
    assert cancel_payload["thread_id"] == "$thread"


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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with (
        patch(
            "mindroom.hooks.sender.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$hook-event")),
        ) as mock_hook_send,
        patch(
            "mindroom.scheduling.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$scheduled")),
        ) as mock_schedule_send,
    ):
        await _execute_scheduled_workflow(
            client,
            _workflow("Resume work"),
            config,
            runtime_paths_for(config),
            conversation_cache,
        )

    conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with(
        "!room:localhost",
        "$thread",
    )
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
    conversation_cache = _conversation_cache(latest_thread_event_id=None)

    with (
        patch(
            "mindroom.hooks.sender.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$hook-event")),
        ) as mock_hook_send,
        patch(
            "mindroom.scheduling.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$scheduled")),
        ) as mock_schedule_send,
    ):
        await _execute_scheduled_workflow(
            client,
            _workflow("Resume work"),
            config,
            runtime_paths_for(config),
            conversation_cache,
        )

    conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with(
        "!room:localhost",
        None,
    )
    mock_schedule_send.assert_not_called()
    content = mock_hook_send.await_args.args[2]
    assert content["body"] == "room-level"
    assert "m.relates_to" not in content


@pytest.mark.asyncio
async def test_schedule_hook_exposes_matrix_admin(tmp_path: Path) -> None:
    """schedule:fired hooks should expose the router-backed matrix admin helper."""
    resolved_aliases: list[str | None] = []

    @hook(EVENT_SCHEDULE_FIRED)
    async def inspect(ctx: ScheduleFiredContext) -> None:
        assert ctx.matrix_admin is not None
        resolved_aliases.append(await ctx.matrix_admin.resolve_alias("#personal-user:localhost"))
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [inspect])]))
    client = AsyncMock()
    client.user_id = "@mindroom_router:localhost"
    client.homeserver = "http://localhost:8008"
    client.room_resolve_alias.return_value = nio.RoomResolveAliasResponse(
        room_alias="#personal-user:localhost",
        room_id="!personal:localhost",
        servers=["localhost"],
    )

    await _execute_scheduled_workflow(
        client,
        _workflow("Resume work"),
        config,
        runtime_paths_for(config),
        _conversation_cache(),
        matrix_admin=build_hook_matrix_admin(client, runtime_paths_for(config)),
    )

    assert resolved_aliases == ["!personal:localhost"]
    client.room_resolve_alias.assert_awaited_once_with("#personal-user:localhost")


@pytest.mark.asyncio
async def test_schedule_hook_matrix_admin_is_unavailable_without_router_binding(tmp_path: Path) -> None:
    """schedule:fired hooks should not fabricate admin access from a non-router client."""
    saw_matrix_admin: list[bool] = []

    @hook(EVENT_SCHEDULE_FIRED)
    async def inspect(ctx: ScheduleFiredContext) -> None:
        saw_matrix_admin.append(ctx.matrix_admin is not None)
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [inspect])]))
    client = AsyncMock()
    client.user_id = "@mindroom_general:localhost"
    client.homeserver = "http://agent.local:8008"

    await _execute_scheduled_workflow(
        client,
        _workflow("Resume work"),
        config,
        runtime_paths_for(config),
        _conversation_cache(),
    )

    assert saw_matrix_admin == [False]


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
    conversation_cache = _conversation_cache(latest_thread_event_id="$latest")

    with (
        patch(
            "mindroom.hooks.sender.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$hook-event")),
        ) as mock_hook_send,
        patch(
            "mindroom.scheduling.send_message_result",
            new=AsyncMock(side_effect=delivered_matrix_side_effect("$scheduled")),
        ) as mock_schedule_send,
    ):
        await _execute_scheduled_workflow(
            client,
            _workflow("Resume work"),
            config,
            runtime_paths_for(config),
            conversation_cache,
        )

    mock_schedule_send.assert_not_called()
    content = mock_hook_send.await_args.args[2]
    assert content["body"] == "dispatch"
    assert content["com.mindroom.source_kind"] == "hook_dispatch"


@pytest.mark.asyncio
async def test_schedule_hook_room_state_helpers_use_live_client(tmp_path: Path) -> None:
    """schedule:fired hooks should get bound room-state helpers from the scheduler client."""
    seen: list[tuple[dict[str, object] | None, bool]] = []

    @hook(EVENT_SCHEDULE_FIRED)
    async def inspect(ctx: ScheduleFiredContext) -> None:
        query_result = await ctx.query_room_state("!room:localhost", "m.room.name", "")
        put_result = await ctx.put_room_state(
            "!room:localhost",
            "com.mindroom.thread.tags",
            "$thread",
            {"tags": {"pending": True}},
        )
        seen.append((query_result, put_result))
        ctx.suppress = True

    config = _config(tmp_path)
    set_scheduling_hook_registry(HookRegistry.from_plugins([_plugin("schedule-plugin", [inspect])]))
    client = AsyncMock()
    client.user_id = "@mindroom_router:localhost"
    client.room_get_state_event.return_value = SimpleNamespace(content={"name": "Lobby"})
    client.room_put_state.return_value = object()

    await _execute_scheduled_workflow(
        client,
        _workflow("Resume work"),
        config,
        runtime_paths_for(config),
        _conversation_cache(),
    )

    assert seen == [({"name": "Lobby"}, True)]
    client.room_get_state_event.assert_awaited_once_with("!room:localhost", "m.room.name", "")
    client.room_put_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        {"tags": {"pending": True}},
        state_key="$thread",
    )
