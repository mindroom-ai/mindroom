"""Tests for the restart-resume plugin."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, Mock

import pytest

import mindroom.tool_system.plugins as plugin_module
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.hooks import AgentLifecycleContext, HookRegistry
from mindroom.logging_config import get_logger
from mindroom.thread_tags import THREAD_TAGS_EVENT_TYPE, ThreadTagRecord
from mindroom.tool_system.plugins import load_plugins
from mindroom.tool_system.skills import _get_plugin_skill_roots, set_plugin_skill_roots
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from collections.abc import Generator
    from types import ModuleType

    from mindroom.constants import RuntimePaths


def _plugin_root() -> Path:
    return Path(__file__).resolve().parents[1] / "plugins" / "restart-resume"


def _copy_plugin_root(tmp_path: Path) -> Path:
    copied_root = tmp_path / "plugins" / "restart-resume"
    shutil.copytree(_plugin_root(), copied_root)
    return copied_root


def _ready_context(
    config: Config,
    runtime_paths: RuntimePaths,
    *,
    settings: dict[str, object] | None = None,
    rooms: tuple[str, ...] = ("!room:localhost",),
    joined_room_ids: tuple[str, ...] | None = None,
    room_state_querier: object | None = None,
    room_state_putter: object | None = None,
    message_sender: object | None = None,
    logger: object | None = None,
) -> AgentLifecycleContext:
    return AgentLifecycleContext(
        event_name="bot:ready",
        plugin_name="restart-notify",
        settings={} if settings is None else settings,
        config=config,
        runtime_paths=runtime_paths,
        logger=get_logger("tests.restart_resume").bind(event_name="bot:ready") if logger is None else logger,
        correlation_id="corr-restart-resume-ready",
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_sender=message_sender,
        entity_name="router",
        entity_type="agent",
        rooms=rooms,
        matrix_user_id="@mindroom_router:localhost",
        joined_room_ids=rooms if joined_room_ids is None else joined_room_ids,
    )


def _pending_restart_record() -> ThreadTagRecord:
    return ThreadTagRecord(
        set_by="@user:localhost",
        set_at=datetime(2026, 4, 9, 18, 0, tzinfo=UTC),
    )


def _pending_restart_state_key(thread_id: str = "$thread1") -> str:
    return json.dumps([thread_id, "pending-restart"], separators=(",", ":"))


def _pending_restart_state(thread_id: str = "$thread1") -> dict[str, dict[str, object]]:
    return {
        _pending_restart_state_key(thread_id): _pending_restart_record().model_dump(mode="json", exclude_none=True),
    }


def _legacy_pending_restart_state(thread_id: str = "$thread1") -> dict[str, dict[str, object]]:
    return {
        thread_id: {
            "tags": {
                "pending-restart": _pending_restart_record().model_dump(mode="json", exclude_none=True),
            },
        },
    }


def _room_state_bindings(
    room_states: dict[str, dict[str, dict[str, object]]],
) -> tuple[AsyncMock, AsyncMock]:
    async def query_room_state(
        room_id: str,
        event_type: str,
        state_key: str | None = None,
    ) -> dict[str, object] | None:
        assert event_type == THREAD_TAGS_EVENT_TYPE
        room_state = room_states.get(room_id, {})
        if state_key is None:
            return dict(room_state)

        content = room_state.get(state_key)
        if content is None:
            return None
        return dict(content)

    async def put_room_state(
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, object],
    ) -> bool:
        assert event_type == THREAD_TAGS_EVENT_TYPE
        room_states.setdefault(room_id, {})[state_key] = dict(content)
        return True

    return AsyncMock(side_effect=query_room_state), AsyncMock(side_effect=put_room_state)


@pytest.fixture
def loaded_restart_resume(
    tmp_path: Path,
) -> Generator[tuple[Config, RuntimePaths, HookRegistry, ModuleType], None, None]:
    """Load the restart-resume plugin into an isolated runtime."""
    runtime_paths = test_runtime_paths(tmp_path)
    plugin_root = _copy_plugin_root(tmp_path)
    config = bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            plugins=[str(plugin_root)],
        ),
        runtime_paths,
    )

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()

    try:
        plugins = load_plugins(config, runtime_paths_for(config))
        registry = HookRegistry.from_plugins(plugins)
        hooks_module = plugin_module._MODULE_IMPORT_CACHE[plugin_root / "hooks.py"].module
        yield config, runtime_paths_for(config), registry, hooks_module
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)


@pytest.mark.asyncio
async def test_notify_uses_joined_room_ids_not_aliases(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """Verify query_room_state is called with real room IDs from joined_room_ids, not aliases from rooms."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume
    room_state_querier = AsyncMock(return_value={})
    ctx = _ready_context(
        config,
        runtime_paths,
        rooms=("lobby",),
        joined_room_ids=("!real-room:localhost",),
        room_state_querier=room_state_querier,
        room_state_putter=AsyncMock(return_value=True),
        message_sender=AsyncMock(return_value="$notify"),
    )

    await hooks_module.notify_after_restart(ctx)

    room_state_querier.assert_awaited_once_with(
        "!real-room:localhost",
        "com.mindroom.thread.tags",
        None,
    )


@pytest.mark.asyncio
async def test_per_room_error_handling(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """One room's failure should not block processing of other rooms."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume

    queried_room_ids: list[str] = []
    room_states = {"!good-room:localhost": _pending_restart_state()}

    async def room_state_side_effect(room_id: str, _event_type: str, _state_key: str | None = None) -> dict | None:
        queried_room_ids.append(room_id)
        if room_id == "!fail-room:localhost":
            msg = "Simulated Matrix error"
            raise RuntimeError(msg)
        return dict(room_states.get(room_id, {}))

    async def put_room_state(
        room_id: str,
        event_type: str,
        state_key: str,
        content: dict[str, object],
    ) -> bool:
        assert event_type == THREAD_TAGS_EVENT_TYPE
        room_states.setdefault(room_id, {})[state_key] = dict(content)
        return True

    room_state_querier = AsyncMock(side_effect=room_state_side_effect)
    room_state_putter = AsyncMock(side_effect=put_room_state)
    message_sender = AsyncMock(return_value="$notify")
    ctx = _ready_context(
        config,
        runtime_paths,
        rooms=("lobby", "dev"),
        joined_room_ids=("!fail-room:localhost", "!good-room:localhost"),
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_sender=message_sender,
    )

    await hooks_module.notify_after_restart(ctx)

    assert {"!fail-room:localhost", "!good-room:localhost"} <= set(queried_room_ids)
    # The good room's thread was notified
    message_sender.assert_awaited_once()
    room_state_putter.assert_awaited_once_with(
        "!good-room:localhost",
        THREAD_TAGS_EVENT_TYPE,
        _pending_restart_state_key(),
        {},
    )


@pytest.mark.asyncio
async def test_notify_clears_restart_tag_after_successful_notification(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """Successful notifications should remove the restart tag."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume
    room_states = {"!room:localhost": _pending_restart_state()}
    room_state_querier, room_state_putter = _room_state_bindings(room_states)
    ctx = _ready_context(
        config,
        runtime_paths,
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_sender=AsyncMock(return_value="$notify"),
    )

    await hooks_module.notify_after_restart(ctx)

    room_state_putter.assert_awaited_once_with(
        "!room:localhost",
        THREAD_TAGS_EVENT_TYPE,
        _pending_restart_state_key(),
        {},
    )
    assert room_states["!room:localhost"] == {_pending_restart_state_key(): {}}


@pytest.mark.asyncio
async def test_notify_normalizes_mixed_case_configured_tag_name(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """Mixed-case configured tags should match normalized room-state tags without KeyError."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume
    room_states = {"!room:localhost": _pending_restart_state()}
    room_state_querier, room_state_putter = _room_state_bindings(room_states)
    message_sender = AsyncMock(return_value="$notify")
    ctx = _ready_context(
        config,
        runtime_paths,
        settings={"tag": "Pending-Restart"},
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_sender=message_sender,
    )

    await hooks_module.notify_after_restart(ctx)

    message_sender.assert_awaited_once()
    assert message_sender.await_args.args[:3] == (
        "!room:localhost",
        "🔄 Restart completed — this thread's `pending-restart` changes are now live.",
        "$thread1",
    )
    room_state_putter.assert_awaited_once_with(
        "!room:localhost",
        THREAD_TAGS_EVENT_TYPE,
        _pending_restart_state_key(),
        {},
    )
    assert room_states["!room:localhost"] == {_pending_restart_state_key(): {}}


@pytest.mark.asyncio
async def test_notify_processes_legacy_per_thread_tag_state(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """Legacy per-thread thread-tag events should still be discovered and cleared."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume
    room_states = {"!room:localhost": _legacy_pending_restart_state()}
    room_state_querier, room_state_putter = _room_state_bindings(room_states)
    message_sender = AsyncMock(return_value="$notify")
    ctx = _ready_context(
        config,
        runtime_paths,
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_sender=message_sender,
    )

    await hooks_module.notify_after_restart(ctx)

    message_sender.assert_awaited_once()
    assert message_sender.await_args.args[:3] == (
        "!room:localhost",
        "🔄 Restart completed — this thread's `pending-restart` changes are now live.",
        "$thread1",
    )
    assert message_sender.await_args.kwargs == {"trigger_dispatch": True}
    room_state_putter.assert_awaited_once_with(
        "!room:localhost",
        THREAD_TAGS_EVENT_TYPE,
        _pending_restart_state_key(),
        {},
    )
    assert room_states["!room:localhost"] == {
        "$thread1": {
            "tags": {
                "pending-restart": _pending_restart_record().model_dump(mode="json", exclude_none=True),
            },
        },
        _pending_restart_state_key(): {},
    }


@pytest.mark.asyncio
async def test_notify_does_not_count_thread_when_tag_clear_fails(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """A failed tag clear must not be logged as a successful notification."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume
    logger = Mock()
    room_state_putter = AsyncMock(return_value=False)
    message_sender = AsyncMock(return_value="$notify")
    ctx = _ready_context(
        config,
        runtime_paths,
        room_state_querier=AsyncMock(return_value=_pending_restart_state()),
        room_state_putter=room_state_putter,
        message_sender=message_sender,
        logger=logger,
    )

    await hooks_module.notify_after_restart(ctx)

    message_sender.assert_awaited_once()
    room_state_putter.assert_awaited_once_with(
        "!room:localhost",
        THREAD_TAGS_EVENT_TYPE,
        _pending_restart_state_key(),
        {},
    )
    logger.info.assert_not_called()
    logger.warning.assert_any_call(
        "Failed to clear restart tag after notification",
        room_id="!room:localhost",
        thread_id="$thread1",
        exc_info=True,
    )


@pytest.mark.asyncio
async def test_notify_after_restart_respects_existing_claim_file(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """An actively held claim should suppress duplicate startup scans."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume
    room_state_querier = AsyncMock(return_value=_pending_restart_state())
    room_state_putter = AsyncMock(return_value=True)
    message_sender = AsyncMock(return_value="$notify")
    ctx = _ready_context(
        config,
        runtime_paths,
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_sender=message_sender,
    )
    claim_path = ctx.state_root / ".restart-claim"
    claim_fd = os.open(str(claim_path), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(claim_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    try:
        await hooks_module.notify_after_restart(ctx)
    finally:
        os.close(claim_fd)

    assert claim_path.exists()
    room_state_querier.assert_not_awaited()
    room_state_putter.assert_not_awaited()
    message_sender.assert_not_awaited()


@pytest.mark.asyncio
async def test_notify_after_restart_recovers_from_stale_claim_file(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """A stale claim file from a crashed worker should not block restart notifications."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume
    room_states = {"!room:localhost": _pending_restart_state()}
    room_state_querier, room_state_putter = _room_state_bindings(room_states)
    message_sender = AsyncMock(return_value="$notify")
    ctx = _ready_context(
        config,
        runtime_paths,
        room_state_querier=room_state_querier,
        room_state_putter=room_state_putter,
        message_sender=message_sender,
    )
    claim_path = ctx.state_root / ".restart-claim"
    claim_path.write_text("12345\n")
    stale_time = time.time() - hooks_module.CLAIM_STALE_AFTER_SECONDS - 1
    os.utime(claim_path, (stale_time, stale_time))

    await hooks_module.notify_after_restart(ctx)

    message_sender.assert_awaited_once()
    room_state_putter.assert_awaited_once_with(
        "!room:localhost",
        THREAD_TAGS_EVENT_TYPE,
        _pending_restart_state_key(),
        {},
    )
    assert not claim_path.exists()
    assert room_states["!room:localhost"] == {_pending_restart_state_key(): {}}


@pytest.mark.asyncio
async def test_notify_after_restart_does_not_leave_claim_file_when_room_state_querier_missing(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """Missing room-state helpers must not leave a stale startup claim behind."""
    config, runtime_paths, _registry, hooks_module = loaded_restart_resume
    logger = Mock()
    room_state_putter = AsyncMock(return_value=True)
    message_sender = AsyncMock(return_value="$notify")
    ctx = _ready_context(
        config,
        runtime_paths,
        room_state_putter=room_state_putter,
        message_sender=message_sender,
        logger=logger,
    )

    await hooks_module.notify_after_restart(ctx)

    assert not (ctx.state_root / ".restart-claim").exists()
    room_state_putter.assert_not_awaited()
    message_sender.assert_not_awaited()
    logger.warning.assert_called_once_with("No room state querier — cannot scan for pending-restart threads")


@pytest.mark.asyncio
async def test_hook_has_router_agent_filter(
    loaded_restart_resume: tuple[Config, RuntimePaths, HookRegistry, ModuleType],
) -> None:
    """Verify the hook decorator restricts to the router agent."""
    _config, _runtime_paths, registry, _hooks_module = loaded_restart_resume

    ready_hooks = registry._hooks_by_event.get("bot:ready", ())
    notify_hook = next(h for h in ready_hooks if h.hook_name == "notify-after-restart")
    assert notify_hook.agents == ("router",)
