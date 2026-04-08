# ruff: noqa: D103
"""Tests for the thread-snooze plugin."""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

import mindroom.tool_system.plugins as plugin_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.hooks import AgentLifecycleContext, HookRegistry, ToolAfterCallContext
from mindroom.logging_config import get_logger
from mindroom.thread_tags import ThreadTagRecord, ThreadTagsState
from mindroom.tool_system.metadata import _TOOL_REGISTRY, TOOL_METADATA, get_tool_by_name
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from mindroom.tool_system.skills import _get_plugin_skill_roots, set_plugin_skill_roots
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from mindroom.constants import RuntimePaths


@dataclass(frozen=True)
class _LoadedThreadSnooze:
    config: Config
    runtime_paths: RuntimePaths
    registry: HookRegistry
    plugin_root: Path
    hooks_module: ModuleType
    tools_module: ModuleType


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1] / "plugins" / "thread-snooze"


def _copy_plugin_root(tmp_path: Path) -> Path:
    copied_root = tmp_path / "plugins" / "thread-snooze"
    shutil.copytree(_plugin_root(), copied_root)
    return copied_root


def _record(
    *,
    note: str | None = None,
    data: dict[str, object] | None = None,
) -> ThreadTagRecord:
    return ThreadTagRecord(
        set_by="@user:localhost",
        set_at=datetime(2026, 4, 7, 20, 0, tzinfo=UTC),
        note=note,
        data=data or {},
    )


def _state(thread_root_id: str, **tags: ThreadTagRecord) -> ThreadTagsState:
    return ThreadTagsState(
        room_id="!room:localhost",
        thread_root_id=thread_root_id,
        tags=tags,
    )


def _snooze_state_record(until: str) -> dict[str, object]:
    return _record(data={"until": until}).model_dump(mode="json", exclude_none=True)


def _snooze_state_map(thread_root_id: str, until: str) -> dict[str, dict[str, object]]:
    return {
        json.dumps([thread_root_id, "snoozed"], separators=(",", ":")): _snooze_state_record(until),
    }


def _tool_context(
    loaded: _LoadedThreadSnooze,
    *,
    thread_id: str | None = "$thread-root",
    resolved_thread_id: str | None = "$thread-root",
) -> ToolRuntimeContext:
    return ToolRuntimeContext(
        agent_name="code",
        room_id="!room:localhost",
        thread_id=thread_id,
        resolved_thread_id=resolved_thread_id,
        requester_id="@user:localhost",
        client=AsyncMock(),
        config=loaded.config,
        runtime_paths=loaded.runtime_paths,
        room=MagicMock(),
        reply_to_event_id=None,
        storage_path=None,
        hook_message_sender=AsyncMock(return_value="$hook-event"),
        correlation_id="corr-thread-snooze-tool",
    )


def _ready_context(
    loaded: _LoadedThreadSnooze,
    *,
    rooms: tuple[str, ...] = ("!room:localhost",),
    joined_room_ids: tuple[str, ...] | None = None,
    room_state_querier: object | None = None,
    room_state_putter: object | None = None,
    message_sender: object | None = None,
) -> AgentLifecycleContext:
    return AgentLifecycleContext(
        event_name="bot:ready",
        plugin_name="thread-snooze",
        settings={},
        config=loaded.config,
        runtime_paths=loaded.runtime_paths,
        logger=get_logger("tests.thread_snooze").bind(event_name="bot:ready"),
        correlation_id="corr-thread-snooze-ready",
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_sender=message_sender,
        entity_name="code",
        entity_type="agent",
        rooms=rooms,
        matrix_user_id="@mindroom_code:localhost",
        joined_room_ids=rooms if joined_room_ids is None else joined_room_ids,
    )


def _after_call_context(
    loaded: _LoadedThreadSnooze,
    *,
    result: object,
    arguments: dict[str, object] | None = None,
    blocked: bool = False,
    error: BaseException | None = None,
) -> ToolAfterCallContext:
    return ToolAfterCallContext(
        tool_name="tag_thread",
        arguments=arguments or {},
        agent_name="code",
        room_id="!room:localhost",
        thread_id="$thread-root",
        requester_id="@user:localhost",
        session_id="session-1",
        result=result,
        error=error,
        blocked=blocked,
        duration_ms=1.0,
        plugin_name="thread-snooze",
        config=loaded.config,
        runtime_paths=loaded.runtime_paths,
        logger=get_logger("tests.thread_snooze").bind(event_name="tool:after_call"),
        correlation_id="corr-thread-snooze-after",
        room_state_putter=AsyncMock(return_value=True),
        message_sender=AsyncMock(return_value="$hook-event"),
    )


@pytest.fixture
def loaded_thread_snooze(tmp_path: Path) -> Generator[_LoadedThreadSnooze, None, None]:
    """Load the thread-snooze plugin into an isolated runtime rooted at tmp_path."""
    runtime_paths = test_runtime_paths(tmp_path)
    plugin_root = _copy_plugin_root(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            plugins=[str(plugin_root)],
        ),
        runtime_paths,
    )

    original_registry = _TOOL_REGISTRY.copy()
    original_metadata = TOOL_METADATA.copy()
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        registry = HookRegistry.from_plugins(plugins)
        hooks_module = plugin_module._MODULE_IMPORT_CACHE[plugin_root / "hooks.py"].module
        tools_module = plugin_module._MODULE_IMPORT_CACHE[plugin_root / "tools.py"].module
        yield _LoadedThreadSnooze(
            config=config,
            runtime_paths=runtime_paths_for(config),
            registry=registry,
            plugin_root=plugin_root,
            hooks_module=hooks_module,
            tools_module=tools_module,
        )
    finally:
        hooks_module = plugin_module._MODULE_IMPORT_CACHE.get(plugin_root / "hooks.py")
        if hooks_module is not None:
            for task in list(hooks_module.module._snooze_tasks.values()):
                task.cancel()
            hooks_module.module._snooze_tasks.clear()
        _TOOL_REGISTRY.clear()
        _TOOL_REGISTRY.update(original_registry)
        TOOL_METADATA.clear()
        TOOL_METADATA.update(original_metadata)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


def test_parse_snooze_until_accepts_aware_datetimes(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    parsed = loaded_thread_snooze.hooks_module.parse_snooze_until("2026-04-10T09:00:00+00:00")

    assert parsed == datetime(2026, 4, 10, 9, 0, tzinfo=UTC)


def test_parse_snooze_until_treats_naive_datetimes_as_utc(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    parsed = loaded_thread_snooze.hooks_module.parse_snooze_until("2026-04-10T09:00:00")

    assert parsed == datetime(2026, 4, 10, 9, 0, tzinfo=UTC)


def test_parse_snooze_until_accepts_past_datetimes(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    parsed = loaded_thread_snooze.hooks_module.parse_snooze_until("2020-04-10T09:00:00+00:00")

    assert parsed == datetime(2020, 4, 10, 9, 0, tzinfo=UTC)


def test_parse_snooze_until_rejects_invalid_values(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    assert loaded_thread_snooze.hooks_module.parse_snooze_until("not-a-date") is None
    assert loaded_thread_snooze.hooks_module.parse_snooze_until("2026-04-10") is None


def test_bot_ready_hook_runs_for_all_agents(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    ready_hooks = loaded_thread_snooze.registry.hooks_for("bot:ready")

    assert len(ready_hooks) == 1
    assert ready_hooks[0].hook_name == "thread-snooze-resume"
    assert ready_hooks[0].agents is None
    assert ready_hooks[0].timeout_ms == 120000


@pytest.mark.asyncio
async def test_spawn_snooze_task_replaces_existing_task(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    original_task = hooks._spawn_snooze_task(
        "!room:localhost",
        "$thread-root",
        datetime.now(UTC) + timedelta(minutes=5),
        wake=AsyncMock(),
        logger=get_logger("tests.thread_snooze"),
    )
    replacement_task = hooks._spawn_snooze_task(
        "!room:localhost",
        "$thread-root",
        datetime.now(UTC) + timedelta(minutes=5),
        wake=AsyncMock(),
        logger=get_logger("tests.thread_snooze"),
    )

    await asyncio.sleep(0)

    assert original_task.cancelled()
    assert hooks._snooze_tasks[("!room:localhost", "$thread-root")] is replacement_task
    hooks._cancel_snooze_task("!room:localhost", "$thread-root")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancel_snooze_task_cancels_and_forgets_task(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    task = hooks._spawn_snooze_task(
        "!room:localhost",
        "$thread-root",
        datetime.now(UTC) + timedelta(minutes=5),
        wake=AsyncMock(),
        logger=get_logger("tests.thread_snooze"),
    )

    hooks._cancel_snooze_task("!room:localhost", "$thread-root")
    await asyncio.sleep(0)

    assert task.cancelled()
    assert ("!room:localhost", "$thread-root") not in hooks._snooze_tasks


@pytest.mark.asyncio
async def test_past_due_snooze_task_fires_immediately(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    wake = AsyncMock()
    task = hooks._spawn_snooze_task(
        "!room:localhost",
        "$thread-root",
        datetime.now(UTC) - timedelta(seconds=1),
        wake=wake,
        logger=get_logger("tests.thread_snooze"),
    )

    await asyncio.wait_for(task, timeout=1)

    wake.assert_awaited_once()
    assert ("!room:localhost", "$thread-root") not in hooks._snooze_tasks


@pytest.mark.asyncio
async def test_cancelled_snooze_task_does_not_wake(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    wake = AsyncMock()
    hooks._spawn_snooze_task(
        "!room:localhost",
        "$thread-root",
        datetime.now(UTC) + timedelta(minutes=5),
        wake=wake,
        logger=get_logger("tests.thread_snooze"),
    )

    hooks._cancel_snooze_task("!room:localhost", "$thread-root")
    await asyncio.sleep(0)

    wake.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_thread_clears_tags_and_notifies(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    query_room_state = AsyncMock(
        side_effect=[
            _snooze_state_map("$thread-root", until.isoformat()),
            _snooze_state_map("$thread-root", until.isoformat()),
            {},
        ],
    )
    put_room_state = AsyncMock(return_value=True)
    send_message = AsyncMock(return_value="$wake")

    await hooks._wake_thread(
        room_id="!room:localhost",
        thread_root_id="$thread-root",
        expected_until=until,
        query_room_state=query_room_state,
        send_message=send_message,
        put_room_state=put_room_state,
        logger=get_logger("tests.thread_snooze"),
    )

    assert put_room_state.await_args_list == [
        call(
            "!room:localhost",
            "com.mindroom.thread.tags",
            json.dumps(["$thread-root", "snoozed"], separators=(",", ":")),
            {},
        ),
    ]
    send_message.assert_awaited_once_with("!room:localhost", "\u23f0 Snooze expired", "$thread-root")


@pytest.mark.asyncio
async def test_wake_thread_retries_after_room_state_write_failure(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    query_room_state = AsyncMock(return_value=_snooze_state_map("$thread-root", until.isoformat()))
    put_room_state = AsyncMock(return_value=False)
    send_message = AsyncMock(return_value=None)

    with patch.object(hooks, "_spawn_snooze_task") as spawn:
        await hooks._wake_thread(
            room_id="!room:localhost",
            thread_root_id="$thread-root",
            expected_until=until,
            query_room_state=query_room_state,
            send_message=send_message,
            put_room_state=put_room_state,
            logger=get_logger("tests.thread_snooze"),
        )

    assert put_room_state.await_args_list == [
        call(
            "!room:localhost",
            "com.mindroom.thread.tags",
            json.dumps(["$thread-root", "snoozed"], separators=(",", ":")),
            {},
        ),
    ]
    spawn.assert_called_once()
    assert spawn.call_args.args[:2] == ("!room:localhost", "$thread-root")
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_thread_retries_after_room_state_query_none(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    query_room_state = AsyncMock(return_value=None)
    put_room_state = AsyncMock(return_value=True)
    send_message = AsyncMock(return_value=None)

    with patch.object(hooks, "_spawn_snooze_task") as spawn:
        await hooks._wake_thread(
            room_id="!room:localhost",
            thread_root_id="$thread-root",
            expected_until=until,
            query_room_state=query_room_state,
            send_message=send_message,
            put_room_state=put_room_state,
            logger=get_logger("tests.thread_snooze"),
        )

    spawn.assert_called_once()
    put_room_state.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_thread_retries_after_transport_exception(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    put_room_state = AsyncMock(return_value=True)
    send_message = AsyncMock(return_value=None)

    with patch.object(hooks, "_spawn_snooze_task") as spawn:
        await hooks._wake_thread(
            room_id="!room:localhost",
            thread_root_id="$thread-root",
            expected_until=until,
            query_room_state=AsyncMock(side_effect=RuntimeError("boom")),
            send_message=send_message,
            put_room_state=put_room_state,
            logger=get_logger("tests.thread_snooze"),
        )

    spawn.assert_called_once()
    put_room_state.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_thread_retry_eventually_sends_notice_after_send_failure(
    loaded_thread_snooze: _LoadedThreadSnooze,
) -> None:
    hooks = loaded_thread_snooze.hooks_module
    until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    query_room_state = AsyncMock(
        side_effect=[
            _snooze_state_map("$thread-root", until.isoformat()),
            _snooze_state_map("$thread-root", until.isoformat()),
            {},
            {},
        ],
    )
    put_room_state = AsyncMock(return_value=True)
    send_message = AsyncMock(side_effect=[RuntimeError("boom"), "$wake"])
    retry_wake = None

    def capture_retry(
        room_id: str,
        thread_root_id: str,
        retry_at: datetime,
        *,
        wake: hooks.WakeCallback,
        logger: object,
    ) -> MagicMock:
        nonlocal retry_wake
        retry_wake = wake
        assert room_id == "!room:localhost"
        assert thread_root_id == "$thread-root"
        assert retry_at > datetime.now(UTC)
        assert logger is not None
        return MagicMock()

    with patch.object(hooks, "_spawn_snooze_task", side_effect=capture_retry):
        await hooks._wake_thread(
            room_id="!room:localhost",
            thread_root_id="$thread-root",
            expected_until=until,
            query_room_state=query_room_state,
            send_message=send_message,
            put_room_state=put_room_state,
            logger=get_logger("tests.thread_snooze"),
        )

    assert retry_wake is not None
    await retry_wake()

    assert send_message.await_args_list == [
        call("!room:localhost", "\u23f0 Snooze expired", "$thread-root"),
        call("!room:localhost", "\u23f0 Snooze expired", "$thread-root"),
    ]
    put_room_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        json.dumps(["$thread-root", "snoozed"], separators=(",", ":")),
        {},
    )


@pytest.mark.asyncio
async def test_wake_thread_retry_after_verify_read_failure_still_sends_notice(
    loaded_thread_snooze: _LoadedThreadSnooze,
) -> None:
    hooks = loaded_thread_snooze.hooks_module
    until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    query_room_state = AsyncMock(
        side_effect=[
            _snooze_state_map("$thread-root", until.isoformat()),
            _snooze_state_map("$thread-root", until.isoformat()),
            RuntimeError("verify read failed after successful put"),
            {},
        ],
    )
    put_room_state = AsyncMock(return_value=True)
    send_message = AsyncMock(return_value="$wake")
    retry_wake = None

    def capture_retry(
        room_id: str,
        thread_root_id: str,
        retry_at: datetime,
        *,
        wake: hooks.WakeCallback,
        logger: object,
    ) -> MagicMock:
        nonlocal retry_wake
        retry_wake = wake
        assert room_id == "!room:localhost"
        assert thread_root_id == "$thread-root"
        assert retry_at > datetime.now(UTC)
        assert logger is not None
        return MagicMock()

    with patch.object(hooks, "_spawn_snooze_task", side_effect=capture_retry):
        await hooks._wake_thread(
            room_id="!room:localhost",
            thread_root_id="$thread-root",
            expected_until=until,
            query_room_state=query_room_state,
            send_message=send_message,
            put_room_state=put_room_state,
            logger=get_logger("tests.thread_snooze"),
        )

    assert retry_wake is not None
    await retry_wake()

    assert send_message.await_count == 1
    put_room_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        json.dumps(["$thread-root", "snoozed"], separators=(",", ":")),
        {},
    )


@pytest.mark.asyncio
async def test_failed_wake_keeps_retry_task_registered(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    expected_until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    query_room_state = AsyncMock(return_value=_snooze_state_map("$thread-root", expected_until.isoformat()))
    put_room_state = AsyncMock(return_value=False)
    send_message = AsyncMock(return_value=None)

    async def wake() -> None:
        await hooks._wake_thread(
            room_id="!room:localhost",
            thread_root_id="$thread-root",
            expected_until=expected_until,
            query_room_state=query_room_state,
            send_message=send_message,
            put_room_state=put_room_state,
            logger=get_logger("tests.thread_snooze"),
        )

    task = hooks._spawn_snooze_task(
        "!room:localhost",
        "$thread-root",
        datetime.now(UTC) - timedelta(seconds=1),
        wake=wake,
        logger=get_logger("tests.thread_snooze"),
    )

    await asyncio.sleep(0)

    retry_task = hooks._snooze_tasks[("!room:localhost", "$thread-root")]
    assert retry_task is not task
    assert not retry_task.done()
    send_message.assert_not_awaited()
    hooks._cancel_snooze_task("!room:localhost", "$thread-root")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_failed_snooze_task_stays_registered_on_exception(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module

    async def wake() -> None:
        msg = "boom"
        raise RuntimeError(msg)

    task = hooks._spawn_snooze_task(
        "!room:localhost",
        "$thread-root",
        datetime.now(UTC) - timedelta(seconds=1),
        wake=wake,
        logger=get_logger("tests.thread_snooze"),
    )

    with pytest.raises(RuntimeError, match="boom"):
        await task

    assert hooks._snooze_tasks[("!room:localhost", "$thread-root")] is task
    hooks._cancel_snooze_task("!room:localhost", "$thread-root")
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_wake_thread_skips_stale_snooze_state(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    original_until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    newer_until = original_until + timedelta(hours=1)
    query_room_state = AsyncMock(return_value=_snooze_state_map("$thread-root", newer_until.isoformat()))
    put_room_state = AsyncMock(return_value=True)
    send_message = AsyncMock(return_value="$wake")

    await hooks._wake_thread(
        room_id="!room:localhost",
        thread_root_id="$thread-root",
        expected_until=original_until,
        query_room_state=query_room_state,
        send_message=send_message,
        put_room_state=put_room_state,
        logger=get_logger("tests.thread_snooze"),
    )

    put_room_state.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_thread_skips_concurrent_resnooze_before_remove(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    original_until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    newer_until = original_until + timedelta(hours=1)
    query_room_state = AsyncMock(
        side_effect=[
            _snooze_state_map("$thread-root", original_until.isoformat()),
            _snooze_state_map("$thread-root", newer_until.isoformat()),
        ],
    )
    put_room_state = AsyncMock(return_value=True)
    send_message = AsyncMock(return_value="$wake")

    await hooks._wake_thread(
        room_id="!room:localhost",
        thread_root_id="$thread-root",
        expected_until=original_until,
        query_room_state=query_room_state,
        send_message=send_message,
        put_room_state=put_room_state,
        logger=get_logger("tests.thread_snooze"),
    )

    put_room_state.assert_not_awaited()
    send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_wake_thread_clears_legacy_snooze_state(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    hooks = loaded_thread_snooze.hooks_module
    until = datetime(2026, 4, 10, 9, 0, tzinfo=UTC)
    query_room_state = AsyncMock(
        side_effect=[
            {
                "$thread-root": {
                    "tags": {
                        "snoozed": {
                            "set_by": "@user:localhost",
                            "set_at": "2026-04-07T20:00:00+00:00",
                            "data": {"until": until.isoformat()},
                        },
                    },
                },
            },
            {
                "$thread-root": {
                    "tags": {
                        "snoozed": {
                            "set_by": "@user:localhost",
                            "set_at": "2026-04-07T20:00:00+00:00",
                            "data": {"until": until.isoformat()},
                        },
                    },
                },
            },
            {},
        ],
    )
    put_room_state = AsyncMock(return_value=True)
    send_message = AsyncMock(return_value="$wake")

    await hooks._wake_thread(
        room_id="!room:localhost",
        thread_root_id="$thread-root",
        expected_until=until,
        query_room_state=query_room_state,
        send_message=send_message,
        put_room_state=put_room_state,
        logger=get_logger("tests.thread_snooze"),
    )

    put_room_state.assert_awaited_once_with(
        "!room:localhost",
        "com.mindroom.thread.tags",
        json.dumps(["$thread-root", "snoozed"], separators=(",", ":")),
        {},
    )
    send_message.assert_awaited_once_with("!room:localhost", "\u23f0 Snooze expired", "$thread-root")


@pytest.mark.asyncio
async def test_bot_ready_rescans_future_snoozes(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    room_state_querier = AsyncMock(return_value=_snooze_state_map("$thread-root", until.isoformat()))
    ctx = _ready_context(
        loaded_thread_snooze,
        room_state_querier=room_state_querier,
        room_state_putter=AsyncMock(return_value=True),
        message_sender=AsyncMock(return_value="$wake"),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn:
        await loaded_thread_snooze.hooks_module.resume_snoozed_threads(ctx)

    spawn.assert_called_once()
    assert spawn.call_args.args[:3] == ("!room:localhost", "$thread-root", until)


@pytest.mark.asyncio
async def test_bot_ready_rescans_joined_room_ids_when_configured_rooms_are_aliases(
    loaded_thread_snooze: _LoadedThreadSnooze,
) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    room_state_querier = AsyncMock(return_value=_snooze_state_map("$thread-root", until.isoformat()))
    ctx = _ready_context(
        loaded_thread_snooze,
        rooms=("lobby",),
        joined_room_ids=("!room:localhost",),
        room_state_querier=room_state_querier,
        room_state_putter=AsyncMock(return_value=True),
        message_sender=AsyncMock(return_value="$wake"),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn:
        await loaded_thread_snooze.hooks_module.resume_snoozed_threads(ctx)

    room_state_querier.assert_awaited_once_with("!room:localhost", "com.mindroom.thread.tags", None)
    spawn.assert_called_once()
    assert spawn.call_args.args[:3] == ("!room:localhost", "$thread-root", until)


@pytest.mark.asyncio
async def test_bot_ready_rescans_legacy_snoozes(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    room_state_querier = AsyncMock(
        return_value={
            "$thread-root": {
                "tags": {
                    "snoozed": {
                        "set_by": "@user:localhost",
                        "set_at": "2026-04-07T20:00:00+00:00",
                        "data": {"until": until.isoformat()},
                    },
                },
            },
        },
    )
    ctx = _ready_context(
        loaded_thread_snooze,
        room_state_querier=room_state_querier,
        room_state_putter=AsyncMock(return_value=True),
        message_sender=AsyncMock(return_value="$wake"),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn:
        await loaded_thread_snooze.hooks_module.resume_snoozed_threads(ctx)

    spawn.assert_called_once()
    assert spawn.call_args.args[:3] == ("!room:localhost", "$thread-root", until)


@pytest.mark.asyncio
async def test_bot_ready_fires_expired_snoozes_immediately(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    until = datetime.now(UTC) - timedelta(seconds=1)
    room_state_querier = AsyncMock(return_value=_snooze_state_map("$thread-root", until.isoformat()))
    ctx = _ready_context(
        loaded_thread_snooze,
        room_state_querier=room_state_querier,
        room_state_putter=AsyncMock(return_value=True),
        message_sender=AsyncMock(return_value="$wake"),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_wake_thread", new=AsyncMock()) as wake:
        await loaded_thread_snooze.hooks_module.resume_snoozed_threads(ctx)
        await asyncio.sleep(0)

    wake.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_ready_continues_after_room_state_exception(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    room_state_querier = AsyncMock(
        side_effect=[
            RuntimeError("boom"),
            _snooze_state_map("$thread-root", until.isoformat()),
        ],
    )
    ctx = _ready_context(
        loaded_thread_snooze,
        rooms=("!first:localhost", "!second:localhost"),
        room_state_querier=room_state_querier,
        room_state_putter=AsyncMock(return_value=True),
        message_sender=AsyncMock(return_value="$wake"),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn:
        await loaded_thread_snooze.hooks_module.resume_snoozed_threads(ctx)

    spawn.assert_called_once()
    assert spawn.call_args.args[:3] == ("!second:localhost", "$thread-root", until)


@pytest.mark.asyncio
async def test_bot_ready_skips_invalid_until_values(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    room_state_querier = AsyncMock(
        return_value=_snooze_state_map("$thread-root", "not-a-date"),
    )
    ctx = _ready_context(
        loaded_thread_snooze,
        room_state_querier=room_state_querier,
        room_state_putter=AsyncMock(return_value=True),
        message_sender=AsyncMock(return_value="$wake"),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn:
        await loaded_thread_snooze.hooks_module.resume_snoozed_threads(ctx)

    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_after_call_detects_manual_snooze_tags(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    ctx = _after_call_context(
        loaded_thread_snooze,
        arguments={"tag": "snoozed", "data": {"until": until.isoformat()}},
        result=json.dumps(
            {
                "status": "ok",
                "room_id": "!room:other",
                "thread_id": "$other-thread",
                "tags": {"snoozed": {"data": {"until": until.isoformat()}}},
            },
        ),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn:
        await loaded_thread_snooze.hooks_module.schedule_manual_snooze_tag(ctx)

    spawn.assert_called_once()
    assert spawn.call_args.args[:3] == ("!room:other", "$other-thread", until)


@pytest.mark.asyncio
async def test_after_call_ignores_failed_tag_results(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    until = datetime.now(UTC) + timedelta(hours=1)
    ctx = _after_call_context(
        loaded_thread_snooze,
        arguments={"tag": "snoozed", "data": {"until": until.isoformat()}},
        result=json.dumps({"status": "error", "message": "boom"}),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn:
        await loaded_thread_snooze.hooks_module.schedule_manual_snooze_tag(ctx)

    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_after_call_ignores_missing_until(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    ctx = _after_call_context(
        loaded_thread_snooze,
        arguments={"tag": "snoozed", "data": {}},
        result=json.dumps(
            {
                "status": "ok",
                "room_id": "!room:localhost",
                "thread_id": "$thread-root",
                "tags": {"snoozed": {"data": {}}},
            },
        ),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn:
        await loaded_thread_snooze.hooks_module.schedule_manual_snooze_tag(ctx)

    spawn.assert_not_called()


@pytest.mark.asyncio
async def test_after_call_cancels_manual_unsnooze_tags(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    ctx = _after_call_context(
        loaded_thread_snooze,
        arguments={"tag": "snoozed"},
        result=json.dumps(
            {
                "status": "ok",
                "room_id": "!room:other",
                "thread_id": "$other-thread",
                "tag": "snoozed",
                "tags": {},
            },
        ),
    )

    with patch.object(loaded_thread_snooze.hooks_module, "_cancel_snooze_task") as cancel:
        ctx.tool_name = "untag_thread"
        await loaded_thread_snooze.hooks_module.schedule_manual_snooze_tag(ctx)

    cancel.assert_called_once_with("!room:other", "$other-thread")


@pytest.mark.asyncio
async def test_snooze_thread_sets_tags_and_spawns_task(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    tool = get_tool_by_name("thread_snooze", loaded_thread_snooze.runtime_paths, worker_target=None)
    assert tool is not None
    until = datetime.now(UTC) + timedelta(hours=1)
    context = _tool_context(loaded_thread_snooze)

    with (
        patch.object(
            loaded_thread_snooze.tools_module,
            "set_thread_tag",
            new=AsyncMock(return_value=_state("$thread-root", snoozed=_record(data={"until": until.isoformat()}))),
        ) as set_thread_tag,
        patch.object(loaded_thread_snooze.hooks_module, "_spawn_snooze_task") as spawn,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.snooze_thread(until.isoformat(), note="Later"))

    assert payload["status"] == "ok"
    assert payload["action"] == "snooze"
    assert payload["thread_id"] == "$thread-root"
    assert payload["until"] == until.isoformat()
    assert set_thread_tag.await_args_list == [
        call(
            context.client,
            context.room_id,
            "$thread-root",
            "snoozed",
            set_by=context.requester_id,
            note="Later",
            data={"until": until.isoformat()},
        ),
    ]
    spawn.assert_called_once()


@pytest.mark.asyncio
async def test_snooze_thread_rolls_back_tag_when_wake_scheduling_fails(
    loaded_thread_snooze: _LoadedThreadSnooze,
) -> None:
    tool = get_tool_by_name("thread_snooze", loaded_thread_snooze.runtime_paths, worker_target=None)
    assert tool is not None
    until = datetime.now(UTC) + timedelta(hours=1)
    context = _tool_context(loaded_thread_snooze)

    with (
        patch.object(
            loaded_thread_snooze.tools_module,
            "set_thread_tag",
            new=AsyncMock(return_value=_state("$thread-root", snoozed=_record(data={"until": until.isoformat()}))),
        ),
        patch.object(
            loaded_thread_snooze.tools_module,
            "remove_thread_tag",
            new=AsyncMock(return_value=_state("$thread-root")),
        ) as remove_thread_tag,
        patch.object(
            loaded_thread_snooze.hooks_module,
            "_spawn_snooze_task",
            side_effect=RuntimeError("wake failed"),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.snooze_thread(until.isoformat(), note="Later"))

    assert payload["status"] == "error"
    assert payload["action"] == "snooze"
    assert "wake failed" in payload["message"]
    remove_thread_tag.assert_awaited_once_with(
        context.client,
        context.room_id,
        "$thread-root",
        "snoozed",
        requester_user_id=context.requester_id,
    )


@pytest.mark.asyncio
async def test_snooze_thread_reports_error_when_rollback_also_fails(
    loaded_thread_snooze: _LoadedThreadSnooze,
) -> None:
    tool = get_tool_by_name("thread_snooze", loaded_thread_snooze.runtime_paths, worker_target=None)
    assert tool is not None
    until = datetime.now(UTC) + timedelta(hours=1)
    context = _tool_context(loaded_thread_snooze)

    with (
        patch.object(
            loaded_thread_snooze.tools_module,
            "set_thread_tag",
            new=AsyncMock(return_value=_state("$thread-root", snoozed=_record(data={"until": until.isoformat()}))),
        ),
        patch.object(
            loaded_thread_snooze.tools_module,
            "remove_thread_tag",
            new=AsyncMock(side_effect=loaded_thread_snooze.tools_module.ThreadTagsError("rollback failed")),
        ) as remove_thread_tag,
        patch.object(loaded_thread_snooze.tools_module.LOGGER, "exception") as log_exception,
        patch.object(
            loaded_thread_snooze.hooks_module,
            "_spawn_snooze_task",
            side_effect=RuntimeError("wake failed"),
        ),
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.snooze_thread(until.isoformat(), note="Later"))

    assert payload["status"] == "error"
    assert payload["action"] == "snooze"
    assert "wake failed" in payload["message"]
    remove_thread_tag.assert_awaited_once()
    log_exception.assert_called_once()


@pytest.mark.asyncio
async def test_snooze_thread_rejects_past_datetimes(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    tool = get_tool_by_name("thread_snooze", loaded_thread_snooze.runtime_paths, worker_target=None)
    assert tool is not None

    with (
        patch.object(loaded_thread_snooze.tools_module, "set_thread_tag", new=AsyncMock()) as set_thread_tag,
        tool_runtime_context(_tool_context(loaded_thread_snooze)),
    ):
        payload = json.loads(await tool.snooze_thread("2020-04-10T09:00:00+00:00"))

    assert payload["status"] == "error"
    assert "future" in payload["message"]
    set_thread_tag.assert_not_awaited()


@pytest.mark.asyncio
async def test_snooze_thread_requires_thread_context(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    tool = get_tool_by_name("thread_snooze", loaded_thread_snooze.runtime_paths, worker_target=None)
    assert tool is not None

    with tool_runtime_context(_tool_context(loaded_thread_snooze, thread_id=None, resolved_thread_id=None)):
        payload = json.loads(await tool.snooze_thread("2026-04-10T09:00:00+00:00"))

    assert payload["status"] == "error"
    assert "active thread" in payload["message"]


@pytest.mark.asyncio
async def test_unsnooze_thread_removes_tags_and_cancels_task(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    tool = get_tool_by_name("thread_snooze", loaded_thread_snooze.runtime_paths, worker_target=None)
    assert tool is not None
    context = _tool_context(loaded_thread_snooze)

    with (
        patch.object(
            loaded_thread_snooze.tools_module,
            "get_thread_tags",
            new=AsyncMock(
                return_value=_state(
                    "$thread-root",
                    snoozed=_record(data={"until": "2026-04-10T09:00:00+00:00"}),
                ),
            ),
        ),
        patch.object(
            loaded_thread_snooze.tools_module,
            "remove_thread_tag",
            new=AsyncMock(return_value=_state("$thread-root")),
        ) as remove_thread_tag,
        patch.object(loaded_thread_snooze.hooks_module, "_cancel_snooze_task") as cancel,
        tool_runtime_context(context),
    ):
        payload = json.loads(await tool.unsnooze_thread())

    assert payload["status"] == "ok"
    assert payload["action"] == "unsnooze"
    cancel.assert_called_once_with(context.room_id, "$thread-root")
    assert remove_thread_tag.await_args_list == [
        call(
            context.client,
            context.room_id,
            "$thread-root",
            "snoozed",
            requester_user_id=context.requester_id,
        ),
    ]


@pytest.mark.asyncio
async def test_unsnooze_thread_keeps_wake_task_when_tag_clear_fails(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    tool = get_tool_by_name("thread_snooze", loaded_thread_snooze.runtime_paths, worker_target=None)
    assert tool is not None

    with (
        patch.object(
            loaded_thread_snooze.tools_module,
            "get_thread_tags",
            new=AsyncMock(
                return_value=_state(
                    "$thread-root",
                    snoozed=_record(data={"until": "2026-04-10T09:00:00+00:00"}),
                ),
            ),
        ),
        patch.object(
            loaded_thread_snooze.tools_module,
            "remove_thread_tag",
            new=AsyncMock(side_effect=loaded_thread_snooze.tools_module.ThreadTagsError("boom")),
        ),
        patch.object(loaded_thread_snooze.hooks_module, "_cancel_snooze_task") as cancel,
        tool_runtime_context(_tool_context(loaded_thread_snooze)),
    ):
        payload = json.loads(await tool.unsnooze_thread())

    assert payload["status"] == "error"
    assert payload["action"] == "unsnooze"
    assert payload["message"] == "boom"
    cancel.assert_not_called()


@pytest.mark.asyncio
async def test_unsnooze_thread_errors_when_not_snoozed(loaded_thread_snooze: _LoadedThreadSnooze) -> None:
    tool = get_tool_by_name("thread_snooze", loaded_thread_snooze.runtime_paths, worker_target=None)
    assert tool is not None

    with (
        patch.object(loaded_thread_snooze.tools_module, "get_thread_tags", new=AsyncMock(return_value=None)),
        patch.object(loaded_thread_snooze.hooks_module, "_cancel_snooze_task") as cancel,
        tool_runtime_context(_tool_context(loaded_thread_snooze)),
    ):
        payload = json.loads(await tool.unsnooze_thread())

    assert payload["status"] == "error"
    assert payload["message"] == "Thread is not snoozed."
    cancel.assert_not_called()
