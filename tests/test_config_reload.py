"""Tests for config auto-reload and room membership updates."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from contextlib import suppress
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

import mindroom.orchestrator as orchestrator_module
import mindroom.tool_system.plugin_imports as plugin_module
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig, CultureConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME, STREAM_STATUS_KEY, STREAM_STATUS_PENDING
from mindroom.file_watcher import _tree_snapshot
from mindroom.hooks import EVENT_MESSAGE_RECEIVED, HookRegistry
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration.config_updates import ConfigUpdatePlan, _get_changed_agents
from mindroom.orchestration.plugin_watch import watch_plugins_task
from mindroom.orchestration.runtime import create_logged_task
from mindroom.orchestrator import _ConfigReloadDrainState, _MultiAgentOrchestrator, _watch_skills_task
from mindroom.tool_system.plugins import PluginReloadResult
from mindroom.tool_system.skills import _get_plugin_skill_roots, set_plugin_skill_roots
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_send_response_mock,
    make_event_cache_mock,
    make_event_cache_write_coordinator_mock,
    orchestrator_runtime_paths,
    runtime_paths_for,
    test_runtime_paths,
)


def _runtime_bound_config(config: Config, runtime_root: Path | None = None) -> Config:
    """Return a runtime-bound config for reload tests."""
    runtime_paths = test_runtime_paths(runtime_root or Path(tempfile.mkdtemp()))
    return bind_runtime_paths(config, runtime_paths)


def setup_test_bot(bot: AgentBot, mock_client: AsyncMock) -> None:
    """Helper to setup a test bot with required attributes."""
    bot.client = mock_client
    bot.event_cache = make_event_cache_mock()
    bot.event_cache_write_coordinator = make_event_cache_write_coordinator_mock()


@pytest.mark.asyncio
async def test_watch_skills_task_snapshots_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skills watcher should not run recursive skill snapshots on the asyncio loop."""

    class DummyOrchestrator:
        running = True

    dummy_orchestrator = DummyOrchestrator()
    snapshot_function_calls: list[object] = []
    clear_calls = 0

    def direct_snapshot_call() -> object:
        raise AssertionError

    async def fake_to_thread(function: object, *args: object, **kwargs: object) -> tuple[tuple[str, int, int], ...]:
        del args, kwargs
        snapshot_function_calls.append(function)
        if len(snapshot_function_calls) == 1:
            return (("before", 1, 1),)
        dummy_orchestrator.running = False
        return (("after", 2, 2),)

    async def fake_sleep(delay: float) -> None:
        del delay

    def fake_clear_skill_cache() -> None:
        nonlocal clear_calls
        clear_calls += 1

    monkeypatch.setattr(orchestrator_module, "get_skill_snapshot", direct_snapshot_call)
    monkeypatch.setattr(orchestrator_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(orchestrator_module, "clear_skill_cache", fake_clear_skill_cache)

    await _watch_skills_task(dummy_orchestrator)  # type: ignore[arg-type]

    assert snapshot_function_calls == [direct_snapshot_call, direct_snapshot_call]
    assert clear_calls == 1


def _write_plugin_removal_test_files(tmp_path: Path) -> Path:
    """Create one minimal plugin used by config-reload teardown tests."""
    plugin_root = tmp_path / "plugins" / "removed-task-plugin"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "removed-task-plugin", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    return hooks_path


def _write_plugin_removal_test_config(tmp_path: Path, *, with_plugin: bool) -> None:
    """Write one minimal config for config-reload plugin teardown tests."""
    config_data = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-4-6"}},
        "router": {"model": "default"},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Helpful",
                "model": "default",
                "rooms": ["lobby"],
            },
        },
        "authorization": {"global_users": ["@owner:localhost"]},
        "plugins": [{"path": "./plugins/removed-task-plugin"}] if with_plugin else [],
    }
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(config_data, sort_keys=False),
        encoding="utf-8",
    )


async def _noop_prepare_user_account(
    self: _MultiAgentOrchestrator,
    config: Config,
    *,
    update_runtime_state: bool,
) -> None:
    del self, config, update_runtime_state


async def _noop_sync_mcp_manager(
    self: _MultiAgentOrchestrator,
    config: Config,
) -> set[str]:
    del self, config
    return set()


async def _noop_sync_event_cache_service(
    self: _MultiAgentOrchestrator,
    config: Config,
) -> None:
    del self, config


async def _noop_sync_runtime_support_services(
    self: _MultiAgentOrchestrator,
    config: Config,
    *,
    start_watcher: bool,
) -> None:
    del self, config, start_watcher


async def _noop_setup_rooms_and_memberships(
    self: _MultiAgentOrchestrator,
    bots: list[AgentBot],
) -> None:
    del self, bots


async def _noop_emit_config_reloaded(
    self: _MultiAgentOrchestrator,
    *,
    new_config: Config,
    changed_entities: set[str],
    added_entities: set[str],
    removed_entities: set[str],
    plugin_changes: tuple[str, ...],
) -> None:
    del self, new_config, changed_entities, added_entities, removed_entities, plugin_changes


def _noop_start_sync_task(
    self: _MultiAgentOrchestrator,
    entity_name: str,
    bot: AgentBot,
) -> None:
    del self, entity_name, bot


async def _noop_try_start(self: AgentBot) -> bool:
    self.running = True
    return True


async def _noop_stop(self: AgentBot, reason: str = "shutdown") -> None:
    del reason
    self.running = False


def _patch_orchestrator_plugin_update_test_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch bot and orchestrator runtime helpers unrelated to plugin teardown."""
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._prepare_user_account",
        _noop_prepare_user_account,
    )
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._sync_mcp_manager",
        _noop_sync_mcp_manager,
    )
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._sync_event_cache_service",
        _noop_sync_event_cache_service,
    )
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._sync_runtime_support_services",
        _noop_sync_runtime_support_services,
    )
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._setup_rooms_and_memberships",
        _noop_setup_rooms_and_memberships,
    )
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._emit_config_reloaded",
        _noop_emit_config_reloaded,
    )
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._start_sync_task",
        _noop_start_sync_task,
    )
    monkeypatch.setattr("mindroom.bot.AgentBot.try_start", _noop_try_start)
    monkeypatch.setattr("mindroom.bot.TeamBot.try_start", _noop_try_start)
    monkeypatch.setattr("mindroom.bot.AgentBot.stop", _noop_stop)
    monkeypatch.setattr("mindroom.bot.TeamBot.stop", _noop_stop)


def test_config_reload_drain_state_tracks_wait_warning_force_and_reset() -> None:
    """Drain-state helpers should model wait, warning, force, and reset transitions."""
    state = _ConfigReloadDrainState()

    assert state.waiting_for_idle is False
    assert state.should_reset_for_request(1.0) is False

    state.begin_wait(now=10.0, requested_at=1.0)

    assert state.waiting_for_idle is True
    assert state.should_reset_for_request(1.0) is False
    assert state.should_reset_for_request(2.0) is True
    assert (
        state.should_warn(
            now=10.5,
            warning_after_seconds=1.0,
            warning_interval_seconds=10.0,
        )
        is False
    )
    assert (
        state.should_warn(
            now=11.0,
            warning_after_seconds=1.0,
            warning_interval_seconds=10.0,
        )
        is True
    )

    state.mark_warning(11.0)

    assert (
        state.should_warn(
            now=15.0,
            warning_after_seconds=1.0,
            warning_interval_seconds=10.0,
        )
        is False
    )
    assert (
        state.should_warn(
            now=21.0,
            warning_after_seconds=1.0,
            warning_interval_seconds=10.0,
        )
        is True
    )
    assert state.should_force_reload(now=11.9, force_after_seconds=2.0) is False
    assert state.should_force_reload(now=12.0, force_after_seconds=2.0) is True

    state.reset()

    assert state.waiting_for_idle is False
    assert state.should_reset_for_request(2.0) is False


@pytest.mark.asyncio
async def test_plugin_watcher_debounces_changes_and_ignores_unconfigured_roots(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plugin watcher should coalesce local edits and ignore unconfigured plugin roots."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.01)

    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "demo", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text("VALUE = 1\n", encoding="utf-8")
    ignored_root = tmp_path / "plugins" / "ignored"
    ignored_root.mkdir(parents=True)
    (ignored_root / "hooks.py").write_text("VALUE = 1\n", encoding="utf-8")

    config = _runtime_bound_config(Config(plugins=["./plugins/demo"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config))
    orchestrator.config = config
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = ()) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        reload_seen.set()
        return PluginReloadResult(HookRegistry.empty(), (), 0)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.05)
        (ignored_root / "hooks.py").write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.sleep(0.08)
        assert reload_calls == []

        (plugin_root / "hooks.py").write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.sleep(0.005)
        (plugin_root / "helper.py").write_text("VALUE = 3\n", encoding="utf-8")
        await asyncio.wait_for(reload_seen.wait(), timeout=1)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert len(reload_calls) == 1
    source, changed_paths = reload_calls[0]
    assert source == "watcher"
    assert (plugin_root / "hooks.py") in changed_paths
    assert (plugin_root / "helper.py") in changed_paths
    assert all(path.is_relative_to(plugin_root) for path in changed_paths)


@pytest.mark.asyncio
async def test_plugin_watcher_tracks_configured_absolute_root_outside_config_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plugin watcher should follow configured absolute roots, not just config_dir/plugins."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.01)

    runtime_root = tmp_path / "runtime"
    external_plugin_root = tmp_path / "external-plugin"
    external_plugin_root.mkdir(parents=True)
    (external_plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "external-demo", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    hooks_path = external_plugin_root / "hooks.py"
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

    config = _runtime_bound_config(Config(plugins=[str(external_plugin_root)]), runtime_root)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config))
    orchestrator.config = config
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = ()) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        reload_seen.set()
        return PluginReloadResult(HookRegistry.empty(), (), 0)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.05)
        hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.wait_for(reload_seen.wait(), timeout=1)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert len(reload_calls) == 1
    source, changed_paths = reload_calls[0]
    assert source == "watcher"
    assert changed_paths == (hooks_path,)


@pytest.mark.asyncio
async def test_plugin_watcher_catches_first_save_after_startup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A save immediately after watcher startup should still trigger one reload."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.01)

    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "demo", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

    config = _runtime_bound_config(Config(plugins=["./plugins/demo"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config))
    orchestrator.config = config
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = ()) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        reload_seen.set()
        return PluginReloadResult(HookRegistry.empty(), (), 0)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.01)
        hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.wait_for(reload_seen.wait(), timeout=1)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert reload_calls == [("watcher", (hooks_path,))]


@pytest.mark.asyncio
async def test_plugin_watcher_catches_first_save_after_config_switch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A save under a newly configured plugin root should not be absorbed into its first baseline."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.01)

    first_root = tmp_path / "plugins" / "first"
    first_root.mkdir(parents=True)
    (first_root / "mindroom.plugin.json").write_text(
        '{"name": "first", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    (first_root / "hooks.py").write_text("VALUE = 1\n", encoding="utf-8")

    second_root = tmp_path / "plugins" / "second"
    second_root.mkdir(parents=True)
    (second_root / "mindroom.plugin.json").write_text(
        '{"name": "second", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    second_hooks_path = second_root / "hooks.py"
    second_hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

    first_config = _runtime_bound_config(Config(plugins=["./plugins/first"]), tmp_path)
    second_config = _runtime_bound_config(Config(plugins=["./plugins/second"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(first_config))
    orchestrator.config = first_config
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = ()) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        reload_seen.set()
        return PluginReloadResult(HookRegistry.empty(), (), 0)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.08)
        orchestrator.config = second_config
        orchestrator._sync_plugin_watch_roots(second_config)
        await asyncio.sleep(0.01)
        second_hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.wait_for(reload_seen.wait(), timeout=1)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert len(reload_calls) == 1
    source, changed_paths = reload_calls[0]
    assert source == "watcher"
    assert second_hooks_path in changed_paths
    assert all(path.is_relative_to(second_root) for path in changed_paths)


@pytest.mark.asyncio
async def test_plugin_watcher_does_not_reload_on_config_switch_without_plugin_edit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Switching configured plugin roots should not trigger a watcher reload on its own."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.01)

    first_root = tmp_path / "plugins" / "first"
    first_root.mkdir(parents=True)
    (first_root / "mindroom.plugin.json").write_text(
        '{"name": "first", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    (first_root / "hooks.py").write_text("VALUE = 1\n", encoding="utf-8")

    second_root = tmp_path / "plugins" / "second"
    second_root.mkdir(parents=True)
    (second_root / "mindroom.plugin.json").write_text(
        '{"name": "second", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    (second_root / "hooks.py").write_text("VALUE = 1\n", encoding="utf-8")

    first_config = _runtime_bound_config(Config(plugins=["./plugins/first"]), tmp_path)
    second_config = _runtime_bound_config(Config(plugins=["./plugins/second"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(first_config))
    orchestrator.config = first_config
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = ()) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        return PluginReloadResult(HookRegistry.empty(), (), 0)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.08)
        orchestrator.config = second_config
        orchestrator._sync_plugin_watch_roots(second_config)
        await asyncio.sleep(0.25)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert reload_calls == []


@pytest.mark.asyncio
async def test_plugin_watcher_ignores_cache_artifacts_created_during_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plugin watcher should not self-trigger on cache artifacts created during reload."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.01)

    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "demo", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

    config = _runtime_bound_config(Config(plugins=["./plugins/demo"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config))
    orchestrator.config = config
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = ()) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        if len(reload_calls) == 1:
            pycache_dir = plugin_root / "__pycache__"
            pycache_dir.mkdir(exist_ok=True)
            (pycache_dir / "hooks.cpython-312.pyc").write_bytes(b"compiled")
            ruff_cache_dir = plugin_root / ".ruff_cache"
            ruff_cache_dir.mkdir(exist_ok=True)
            (ruff_cache_dir / "hooks.json").write_text("{}", encoding="utf-8")
            mypy_cache_dir = plugin_root / ".mypy_cache"
            mypy_cache_dir.mkdir(exist_ok=True)
            (mypy_cache_dir / "meta.json").write_text("{}", encoding="utf-8")
            pytest_cache_dir = plugin_root / ".pytest_cache" / "v" / "cache"
            pytest_cache_dir.mkdir(parents=True, exist_ok=True)
            (pytest_cache_dir / "nodeids").write_text("[]", encoding="utf-8")
            reload_seen.set()
        return PluginReloadResult(HookRegistry.empty(), (), 0)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.05)
        hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.wait_for(reload_seen.wait(), timeout=1)
        await asyncio.sleep(0.08)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert len(reload_calls) == 1
    source, changed_paths = reload_calls[0]
    assert source == "watcher"
    assert hooks_path in changed_paths
    assert all("__pycache__" not in path.parts for path in changed_paths)
    assert all(".ruff_cache" not in path.parts for path in changed_paths)
    assert all(".mypy_cache" not in path.parts for path in changed_paths)
    assert all(".pytest_cache" not in path.parts for path in changed_paths)


@pytest.mark.asyncio
async def test_manual_plugin_reload_consumes_pending_watcher_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A manual reload should consume the current dirty save so the watcher does not reload it again."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.05)

    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "demo", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

    config = _runtime_bound_config(Config(plugins=["./plugins/demo"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config))
    orchestrator.config = config
    orchestrator.running = True

    reload_call_count = 0

    def record_reload(config: Config, runtime_paths: object) -> PluginReloadResult:
        nonlocal reload_call_count
        del config, runtime_paths
        reload_call_count += 1
        return PluginReloadResult(HookRegistry.empty(), ("demo",), 0)

    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.05)
        with patch("mindroom.orchestrator.reload_plugins", side_effect=record_reload):
            hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
            await asyncio.sleep(0.02)
            await orchestrator.reload_plugins_now(source="command")
            await asyncio.sleep(0.12)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert reload_call_count == 1


def test_plugin_tree_snapshot_ignores_git_metadata(tmp_path: Path) -> None:
    """Plugin tree snapshots should ignore Git metadata files inside watched repos."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")
    git_dir = plugin_root / ".git"
    git_dir.mkdir()
    git_head = git_dir / "HEAD"
    git_head.write_text("ref: refs/heads/main\n", encoding="utf-8")
    git_ref = git_dir / "refs" / "heads" / "main"
    git_ref.parent.mkdir(parents=True)
    git_ref.write_text("deadbeef\n", encoding="utf-8")

    snapshot = _tree_snapshot(plugin_root)

    assert hooks_path in snapshot
    assert git_head not in snapshot
    assert git_ref not in snapshot


@pytest.mark.asyncio
async def test_plugin_watcher_does_not_retry_failed_reload_without_new_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """One broken save should trigger one failed reload attempt until another change arrives."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.01)

    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "demo", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

    config = _runtime_bound_config(Config(plugins=["./plugins/demo"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config))
    orchestrator.config = config
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    first_reload_seen = asyncio.Event()
    second_reload_seen = asyncio.Event()
    error_message = "broken plugin"

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = ()) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        if len(reload_calls) == 1:
            first_reload_seen.set()
        if len(reload_calls) == 2:
            second_reload_seen.set()
        raise RuntimeError(error_message)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.05)
        hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.wait_for(first_reload_seen.wait(), timeout=1)
        await asyncio.sleep(0.08)

        assert len(reload_calls) == 1

        hooks_path.write_text("VALUE = 3\n", encoding="utf-8")
        await asyncio.wait_for(second_reload_seen.wait(), timeout=1)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert len(reload_calls) == 2
    assert reload_calls[0][0] == "watcher"
    assert reload_calls[1][0] == "watcher"


@pytest.mark.asyncio
async def test_reload_plugins_now_deactivates_broken_plugin_after_failure(tmp_path: Path) -> None:
    """A failed explicit reload should deactivate the broken plugin instead of leaving old hooks live."""
    plugin_root = tmp_path / "plugins" / "broken-reload"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "broken-reload", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    hooks_path = (plugin_root / "hooks.py").resolve()
    hooks_path.write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )
    config = _runtime_bound_config(Config(plugins=["./plugins/broken-reload"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config))
    orchestrator.config = config
    orchestrator.running = True

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    shared_task = asyncio.create_task(asyncio.Event().wait())
    try:
        initial = await orchestrator.reload_plugins_now(source="test")
        assert initial.active_plugin_names == ("broken-reload",)
        assert len(orchestrator.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED)) == 1

        hooks_module = plugin_module._MODULE_IMPORT_CACHE[hooks_path].module
        hooks_module._AUTO_POKE_TASK = shared_task

        hooks_path.unlink()

        with pytest.raises(plugin_module.PluginValidationError, match="Plugin hooks module not found"):
            await orchestrator.reload_plugins_now(source="test")
        await asyncio.sleep(0)

        assert shared_task.cancelled()
        assert orchestrator.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED) == ()
    finally:
        await asyncio.gather(shared_task, return_exceptions=True)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_reload_plugins_now_deactivates_all_plugins_when_degraded_reload_still_fails(
    tmp_path: Path,
) -> None:
    """Unrecoverable reload failures should clear the live plugin registry."""
    first_root = tmp_path / "plugins" / "first"
    first_root.mkdir(parents=True)
    (first_root / "mindroom.plugin.json").write_text(
        '{"name": "first", "hooks_module": "hooks.py", "skills": ["skills"]}',
        encoding="utf-8",
    )
    (first_root / "hooks.py").write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n"
        "    return 'first'\n",
        encoding="utf-8",
    )
    first_skills = first_root / "skills" / "first-skill"
    first_skills.mkdir(parents=True)
    (first_skills / "SKILL.md").write_text(
        "---\nname: first-skill\ndescription: test\n---\n",
        encoding="utf-8",
    )

    second_root = tmp_path / "plugins" / "second"
    second_root.mkdir(parents=True)
    second_manifest_path = second_root / "mindroom.plugin.json"
    second_manifest_path.write_text(
        '{"name": "second", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    (second_root / "hooks.py").write_text(
        "from mindroom.hooks import hook\n"
        "\n"
        "@hook('message:received')\n"
        "async def audit(ctx):\n"
        "    del ctx\n"
        "    return 'second'\n",
        encoding="utf-8",
    )

    config = _runtime_bound_config(Config(plugins=["./plugins/first", "./plugins/second"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config))
    orchestrator.config = config
    orchestrator.running = True

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    try:
        initial = await orchestrator.reload_plugins_now(source="test")
        assert initial.active_plugin_names == ("first", "second")
        assert [hook.plugin_name for hook in orchestrator.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED)] == [
            "first",
            "second",
        ]
        assert _get_plugin_skill_roots() == [first_root / "skills"]

        second_manifest_path.write_text(
            '{"name": "first", "hooks_module": "hooks.py", "skills": []}',
            encoding="utf-8",
        )

        with pytest.raises(plugin_module.PluginValidationError, match="Duplicate plugin manifest names configured"):
            await orchestrator.reload_plugins_now(source="test")

        assert orchestrator.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED) == ()
        assert _get_plugin_skill_roots() == []
    finally:
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_update_config_cancels_tasks_for_removed_plugins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Config-driven plugin removal should tear down the old live plugin runtime."""
    hooks_path = _write_plugin_removal_test_files(tmp_path)
    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    task = asyncio.create_task(asyncio.Event().wait())
    try:
        _write_plugin_removal_test_config(tmp_path, with_plugin=True)
        _patch_orchestrator_plugin_update_test_runtime(monkeypatch)
        orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
        await orchestrator.initialize()

        hooks_module = plugin_module._MODULE_IMPORT_CACHE[hooks_path.resolve()].module
        package_root = plugin_module._MODULE_IMPORT_CACHE[hooks_path.resolve()].module_name.split(".", 1)[0]
        hooks_module._AUTO_POKE_TASK = task

        _write_plugin_removal_test_config(tmp_path, with_plugin=False)
        updated = await orchestrator.update_config()
        await asyncio.sleep(0)

        assert updated is True
        assert task.cancelled()
        assert hooks_path.resolve() not in plugin_module._MODULE_IMPORT_CACHE
        assert not any(
            module_name == package_root or module_name.startswith(f"{package_root}.") for module_name in sys.modules
        )
        assert not orchestrator.hook_registry.has_hooks(EVENT_MESSAGE_RECEIVED)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_update_config_serializes_live_plugin_reload_against_staged_plugin_commit(
    tmp_path: Path,
) -> None:
    """A concurrent live reload must wait until the staged config reload commit finishes."""
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    hooks_path = plugin_root / "hooks.py"
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name":"demo","hooks_module":"hooks.py","skills":[]}',
        encoding="utf-8",
    )
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")

    other_root = tmp_path / "plugins" / "other"
    other_root.mkdir(parents=True)
    (other_root / "mindroom.plugin.json").write_text(
        '{"name":"other","hooks_module":"hooks.py","skills":[]}',
        encoding="utf-8",
    )
    (other_root / "hooks.py").write_text("OTHER = 1\n", encoding="utf-8")

    current_config = _runtime_bound_config(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "test", "id": "test-model"}},
            plugins=["./plugins/demo"],
        ),
        tmp_path,
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "test", "id": "test-model"}},
            plugins=["./plugins/demo", "./plugins/other"],
        ),
        tmp_path,
    )

    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(current_config))
    orchestrator.config = current_config
    orchestrator.running = True

    plan = ConfigUpdatePlan(
        new_config=new_config,
        changed_mcp_servers=set(),
        all_new_entities=set(),
        entities_to_restart=set(),
        new_entities=set(),
        removed_entities=set(),
        mindroom_user_changed=False,
        matrix_room_access_changed=False,
        matrix_space_changed=False,
        authorization_changed=False,
    )

    original_plugin_roots = _get_plugin_skill_roots()
    original_plugin_cache = plugin_module._PLUGIN_CACHE.copy()
    original_module_cache = plugin_module._MODULE_IMPORT_CACHE.copy()
    original_modules = set(sys.modules)
    reload_task: asyncio.Task[PluginReloadResult] | None = None
    try:
        await orchestrator.reload_plugins_now(source="initial")
        assert plugin_module._MODULE_IMPORT_CACHE[hooks_path.resolve()].module.VALUE == 1

        async def start_blocked_reload(*_args: object, **_kwargs: object) -> set[str]:
            nonlocal reload_task
            hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
            reload_task = asyncio.create_task(orchestrator.reload_plugins_now(source="interleaved"))
            await asyncio.sleep(0)
            assert reload_task is not None
            assert not reload_task.done()
            return set()

        with (
            patch("mindroom.orchestrator.load_config", return_value=new_config),
            patch("mindroom.orchestrator.build_config_update_plan", return_value=plan),
            patch.object(
                orchestrator,
                "_stop_entities_before_mcp_sync",
                new=AsyncMock(side_effect=start_blocked_reload),
            ),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_sync_event_cache_service", new=AsyncMock()),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
            patch.object(orchestrator, "_emit_config_reloaded", new=AsyncMock()),
        ):
            updated = await orchestrator.update_config()

        assert updated is False
        assert reload_task is not None
        result = await reload_task

        assert result.active_plugin_names == ("demo", "other")
        assert plugin_module._MODULE_IMPORT_CACHE[hooks_path.resolve()].module.VALUE == 2
    finally:
        if reload_task is not None:
            await asyncio.gather(reload_task, return_exceptions=True)
        plugin_module._PLUGIN_CACHE.clear()
        plugin_module._PLUGIN_CACHE.update(original_plugin_cache)
        plugin_module._MODULE_IMPORT_CACHE.clear()
        plugin_module._MODULE_IMPORT_CACHE.update(original_module_cache)
        set_plugin_skill_roots(original_plugin_roots)
        for module_name in set(sys.modules) - original_modules:
            if module_name.startswith("mindroom_plugin_"):
                sys.modules.pop(module_name, None)


@pytest.mark.asyncio
async def test_queued_config_reload_waits_for_in_flight_response_without_event_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """Queued reloads should wait for tracked responses even without a Matrix event ID."""
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)

    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    bot = AgentBot(
        agent_user=mock_agent_users["agent1"],
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    setup_test_bot(bot, AsyncMock())
    bot._send_response = AsyncMock(return_value=None)
    install_send_response_mock(bot, bot._send_response)

    response_started = asyncio.Event()
    release_response = asyncio.Event()

    async def response_function(message_id: str | None) -> None:
        assert message_id is None
        response_started.set()
        await release_response.wait()

    response_task = asyncio.create_task(
        bot._response_runner.run_cancellable_response(
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            response_function=response_function,
            thinking_message="Thinking...",
        ),
    )

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.running = True
    orchestrator.agent_bots["agent1"] = bot
    orchestrator.update_config = AsyncMock(return_value=True)

    try:
        await asyncio.wait_for(response_started.wait(), timeout=1)
        bot._send_response.assert_awaited_once()
        assert bot.in_flight_response_count == 1

        orchestrator.request_config_reload()
        task = orchestrator._config_reload_task
        assert task is not None

        await asyncio.sleep(0.05)
        orchestrator.update_config.assert_not_awaited()

        release_response.set()
        await asyncio.wait_for(response_task, timeout=1)
        await asyncio.wait_for(task, timeout=1)

        orchestrator.update_config.assert_awaited_once()
    finally:
        release_response.set()
        await asyncio.gather(response_task, return_exceptions=True)
        for cleanup_task in bot.stop_manager.cleanup_tasks:
            cleanup_task.cancel()
        await asyncio.gather(*bot.stop_manager.cleanup_tasks, return_exceptions=True)
        await orchestrator._cancel_config_reload_task()


@pytest.mark.asyncio
async def test_queued_config_reload_surfaces_stuck_drain_and_forces_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Queued reloads should warn and then force through a wedged drain."""
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DRAIN_WARNING_AFTER_SECONDS", 0.02)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DRAIN_WARNING_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS", 0.04)

    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestrator.logger", logger_mock)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.running = True

    mock_bot = MagicMock(spec=AgentBot)
    mock_bot.in_flight_response_count = 1
    orchestrator.agent_bots["agent1"] = mock_bot
    orchestrator.update_config = AsyncMock(return_value=True)

    orchestrator.request_config_reload()
    task = orchestrator._config_reload_task
    assert task is not None

    await asyncio.wait_for(task, timeout=1)

    orchestrator.update_config.assert_awaited_once()
    assert any(
        call.args and call.args[0] == "Configuration reload still waiting for active responses to finish"
        for call in logger_mock.warning.call_args_list
    )
    assert any(
        call.args and call.args[0] == "Forcing configuration reload while responses are still active"
        for call in logger_mock.error.call_args_list
    )


@pytest.mark.asyncio
async def test_queued_config_reload_resets_drain_window_for_new_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A newer config change should get a fresh drain timeout window."""
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.005)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DRAIN_WARNING_AFTER_SECONDS", 1.0)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DRAIN_WARNING_INTERVAL_SECONDS", 1.0)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DRAIN_FORCE_AFTER_SECONDS", 0.12)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.running = True

    mock_bot = MagicMock(spec=AgentBot)
    mock_bot.in_flight_response_count = 1
    orchestrator.agent_bots["agent1"] = mock_bot

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    update_called_at: float | None = None

    async def fake_update_config() -> bool:
        nonlocal update_called_at
        update_called_at = loop.time()
        return True

    orchestrator.update_config = AsyncMock(side_effect=fake_update_config)

    orchestrator.request_config_reload()
    await asyncio.sleep(0.06)
    orchestrator.request_config_reload()

    task = orchestrator._config_reload_task
    assert task is not None
    await asyncio.wait_for(task, timeout=1)

    assert update_called_at is not None
    assert update_called_at - started_at >= 0.16
    orchestrator.update_config.assert_awaited_once()


@pytest.mark.asyncio
async def test_request_config_reload_ignores_changes_while_startup_is_in_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Queued reloads should not start before the orchestrator is running."""
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestrator.logger", logger_mock)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.update_config = AsyncMock(return_value=True)

    orchestrator.request_config_reload()

    assert orchestrator._config_reload_requested_at is None
    assert orchestrator._config_reload_task is None
    orchestrator.update_config.assert_not_awaited()
    assert any(
        call.args and call.args[0] == "Ignoring config change while startup is still in progress"
        for call in logger_mock.info.call_args_list
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("task_attr", "cancel_method_name", "task_name"),
    [
        ("_config_reload_task", "_cancel_config_reload_task", "config_reload"),
    ],
)
async def test_detached_task_cancel_logs_exception_instead_of_suppressing_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    task_attr: str,
    cancel_method_name: str,
    task_name: str,
) -> None:
    """Detached task cancellation should log unexpected failures and keep shutdown moving."""
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestration.runtime.logger", logger_mock)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    started = asyncio.Event()

    async def fail_during_cancel() -> None:
        started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError as err:
            msg = "boom"
            raise RuntimeError(msg) from err

    setattr(
        orchestrator,
        task_attr,
        create_logged_task(
            fail_during_cancel(),
            name=task_name,
            failure_message=f"{task_name} failed",
        ),
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    await getattr(orchestrator, cancel_method_name)()

    assert getattr(orchestrator, task_attr) is None
    assert any(
        call.args
        and call.args[0] == "Detached task failed while being cancelled"
        and call.kwargs.get("task_name") == task_name
        for call in logger_mock.debug.call_args_list
    )
    logger_mock.exception.assert_not_called()


@pytest.mark.asyncio
async def test_queued_config_reload_coalesces_rapid_changes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Multiple quick config changes should produce one reload."""
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.05)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.running = True

    mock_bot = MagicMock(spec=AgentBot)
    mock_bot.in_flight_response_count = 0
    orchestrator.agent_bots["agent1"] = mock_bot

    update_started = asyncio.Event()

    async def fake_update_config() -> bool:
        update_started.set()
        return True

    orchestrator.update_config = AsyncMock(side_effect=fake_update_config)

    orchestrator.request_config_reload()
    task = orchestrator._config_reload_task
    assert task is not None

    await asyncio.sleep(0.02)
    orchestrator.request_config_reload()

    await asyncio.wait_for(update_started.wait(), timeout=1)
    await asyncio.wait_for(task, timeout=1)

    orchestrator.update_config.assert_awaited_once()


def test_get_changed_agents_detects_culture_config_updates() -> None:
    """Agent restarts should trigger when their culture mode/assignment changes."""
    old_config = _runtime_bound_config(
        Config(
            agents={
                "agent1": AgentConfig(display_name="Agent 1"),
            },
            cultures={
                "engineering": CultureConfig(
                    description="Engineering standards",
                    agents=["agent1"],
                    mode="automatic",
                ),
            },
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={
                "agent1": AgentConfig(display_name="Agent 1"),
            },
            cultures={
                "engineering": CultureConfig(
                    description="Engineering standards",
                    agents=["agent1"],
                    mode="agentic",
                ),
            },
        ),
    )

    changed = _get_changed_agents(old_config, new_config, agent_bots={"agent1": AsyncMock()})
    assert changed == {"agent1"}


def test_get_changed_agents_detects_tool_override_updates() -> None:
    """Agent restarts should trigger when authored tool overrides change."""
    old_config = _runtime_bound_config(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    tools=[{"shell": {"enable_run_shell_command": False}}],
                ),
            },
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    tools=[{"shell": {"enable_run_shell_command": True}}],
                ),
            },
        ),
    )

    changed = _get_changed_agents(old_config, new_config, agent_bots={"agent1": AsyncMock()})
    assert changed == {"agent1"}


@pytest.fixture
def initial_config() -> Config:
    """Initial configuration with some agents and rooms."""
    return _runtime_bound_config(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    rooms=["room1", "room2"],
                ),
                "agent2": AgentConfig(
                    display_name="Agent 2",
                    role="Another test agent",
                    rooms=["room1"],
                ),
            },
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Test team",
                    agents=["agent1", "agent2"],
                    rooms=["room3"],
                ),
            },
            models={
                "default": ModelConfig(
                    provider="ollama",
                    id="llama3.2",
                    host="http://localhost:11434",
                ),
            },
        ),
    )


@pytest.fixture
def updated_config() -> Config:
    """Updated configuration with changed room assignments."""
    return _runtime_bound_config(
        Config(
            agents={
                "agent1": AgentConfig(
                    display_name="Agent 1",
                    role="Test agent",
                    rooms=["room1", "room4"],  # Changed: removed room2, added room4
                ),
                "agent2": AgentConfig(
                    display_name="Agent 2",
                    role="Another test agent",
                    rooms=["room2", "room3"],  # Changed: removed room1, added room2 and room3
                ),
                "agent3": AgentConfig(  # New agent
                    display_name="Agent 3",
                    role="New agent",
                    rooms=["room5"],
                ),
            },
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Test team",
                    agents=["agent1", "agent2", "agent3"],  # Added agent3
                    rooms=["room3", "room6"],  # Added room6
                ),
            },
            models={
                "default": ModelConfig(
                    provider="ollama",
                    id="llama3.2",
                    host="http://localhost:11434",
                ),
            },
        ),
    )


@pytest.fixture
def mock_agent_users() -> dict[str, AgentMatrixUser]:
    """Create mock agent users."""
    return {
        ROUTER_AGENT_NAME: AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id=f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
            display_name="RouterAgent",
            password=TEST_PASSWORD,
        ),
        "agent1": AgentMatrixUser(
            agent_name="agent1",
            user_id="@mindroom_agent1:localhost",
            display_name="Agent 1",
            password=TEST_PASSWORD,
        ),
        "agent2": AgentMatrixUser(
            agent_name="agent2",
            user_id="@mindroom_agent2:localhost",
            display_name="Agent 2",
            password=TEST_PASSWORD,
        ),
        "agent3": AgentMatrixUser(
            agent_name="agent3",
            user_id="@mindroom_agent3:localhost",
            display_name="Agent 3",
            password=TEST_PASSWORD,
        ),
        "team1": AgentMatrixUser(
            agent_name="team1",
            user_id="@mindroom_team1:localhost",
            display_name="Team 1",
            password=TEST_PASSWORD,
        ),
    }


@pytest.mark.asyncio
async def test_agent_joins_new_rooms_on_config_reload(  # noqa: C901
    initial_config: Config,  # noqa: ARG001
    updated_config: Config,  # noqa: ARG001
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that agents join new rooms when their configuration is updated."""
    # Track room operations
    joined_rooms: dict[str, list[str]] = {}
    left_rooms: dict[str, list[str]] = {}

    async def mock_join_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    async def mock_leave_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in left_rooms:
            left_rooms[user_id] = []
        left_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _event_cache: object,
        _conversation_cache: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(client: AsyncMock) -> list[str]:
        user_id = client.user_id
        if "agent1" in user_id:
            return ["room1", "room2"]  # agent1 is currently in room1 and room2
        if "agent2" in user_id:
            return ["room1"]  # agent2 is currently in room1
        if "team1" in user_id:
            return ["room3"]  # team1 is currently in room3
        if ROUTER_AGENT_NAME in user_id:
            return ["room1", "room2", "room3"]  # router is in all initial rooms
        return []

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", mock_get_joined_rooms)

    # Create agent1 bot with initial config
    config = _runtime_bound_config(Config(router=RouterConfig(model="default")), tmp_path)
    agent1_bot = AgentBot(
        agent_user=mock_agent_users["agent1"],
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["room1", "room2"],  # Initial rooms
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_agent1:localhost"
    setup_test_bot(agent1_bot, mock_client)

    # Update to new config rooms
    agent1_bot.rooms = ["room1", "room4"]  # New rooms: removed room2, added room4

    # Apply room updates
    await agent1_bot.join_configured_rooms()
    await agent1_bot.leave_unconfigured_rooms()

    # Verify agent1 joined room4 (new room)
    assert "room4" in joined_rooms.get("@mindroom_agent1:localhost", [])
    # Verify agent1 left room2 (no longer configured)
    assert "room2" in left_rooms.get("@mindroom_agent1:localhost", [])


@pytest.mark.asyncio
async def test_router_updates_rooms_on_config_reload(
    initial_config: Config,
    updated_config: Config,
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that the router updates its room list when agents/teams change their rooms."""
    # Track room operations
    joined_rooms: list[str] = []
    left_rooms: list[str] = []

    async def mock_join_room(_client: AsyncMock, room_id: str) -> bool:
        joined_rooms.append(room_id)
        return True

    async def mock_leave_room(_client: AsyncMock, room_id: str) -> bool:
        left_rooms.append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _event_cache: object,
        _conversation_cache: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(_client: AsyncMock) -> list[str]:
        # Router is currently in initial config rooms
        return ["room1", "room2", "room3"]

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", mock_get_joined_rooms)

    # Get initial router rooms
    initial_router_rooms = initial_config.get_all_configured_rooms()
    assert initial_router_rooms == {"room1", "room2", "room3"}

    # Get updated router rooms
    updated_router_rooms = updated_config.get_all_configured_rooms()
    assert updated_router_rooms == {"room1", "room2", "room3", "room4", "room5", "room6"}

    # Create router bot with updated config
    config = _runtime_bound_config(Config(router=RouterConfig(model="default")), tmp_path)
    router_bot = AgentBot(
        agent_user=mock_agent_users[ROUTER_AGENT_NAME],
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=list(updated_router_rooms),
    )
    mock_client = AsyncMock()
    mock_client.user_id = f"@mindroom_{ROUTER_AGENT_NAME}:localhost"
    setup_test_bot(router_bot, mock_client)

    # Apply room updates
    await router_bot.join_configured_rooms()
    await router_bot.leave_unconfigured_rooms()

    # Verify router joined new rooms
    for new_room in ["room4", "room5", "room6"]:
        assert new_room in joined_rooms

    # Router should not leave any rooms (all initial rooms still have agents)
    assert len(left_rooms) == 0


@pytest.mark.asyncio
async def test_new_agent_joins_rooms_on_config_reload(
    initial_config: Config,  # noqa: ARG001
    updated_config: Config,  # noqa: ARG001
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that new agents are created and join their configured rooms."""
    # Track room operations
    joined_rooms: dict[str, list[str]] = {}

    async def mock_ensure_all_agent_users(_homeserver: str) -> dict[str, AgentMatrixUser]:
        # Return both existing and new agent users
        return mock_agent_users

    monkeypatch.setattr("mindroom.matrix.users._ensure_all_agent_users", mock_ensure_all_agent_users)

    async def mock_join_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", mock_join_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _event_cache: object,
        _conversation_cache: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms
    async def mock_get_joined_rooms(_client: AsyncMock) -> list[str]:
        return []  # New agent has no rooms initially

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", mock_get_joined_rooms)

    # Create agent3 bot (new agent in updated config)
    config = _runtime_bound_config(Config(router=RouterConfig(model="default")), tmp_path)
    agent3_bot = AgentBot(
        agent_user=mock_agent_users["agent3"],
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["room5"],
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_agent3:localhost"
    setup_test_bot(agent3_bot, mock_client)

    # Apply room updates for new agent
    await agent3_bot.join_configured_rooms()

    # Verify agent3 joined its configured room
    assert "room5" in joined_rooms.get("@mindroom_agent3:localhost", [])


@pytest.mark.asyncio
async def test_team_room_changes_on_config_reload(
    initial_config: Config,  # noqa: ARG001
    updated_config: Config,  # noqa: ARG001
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that teams update their room memberships when configuration changes."""
    # Track room operations
    joined_rooms: dict[str, list[str]] = {}
    left_rooms: dict[str, list[str]] = {}

    async def mock_join_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in joined_rooms:
            joined_rooms[user_id] = []
        joined_rooms[user_id].append(room_id)
        return True

    async def mock_leave_room(client: AsyncMock, room_id: str) -> bool:
        user_id = client.user_id
        if user_id not in left_rooms:
            left_rooms[user_id] = []
        left_rooms[user_id].append(room_id)
        return True

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _event_cache: object,
        _conversation_cache: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms to simulate current room membership
    async def mock_get_joined_rooms(client: AsyncMock) -> list[str]:
        user_id = client.user_id
        if "team1" in user_id:
            return ["room3"]  # team1 is currently only in room3
        return []

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", mock_get_joined_rooms)

    # Create team1 bot with updated config
    config = _runtime_bound_config(Config(router=RouterConfig(model="default")), tmp_path)
    team1_bot = AgentBot(
        agent_user=mock_agent_users["team1"],
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
        rooms=["room3", "room6"],
    )
    mock_client = AsyncMock()
    mock_client.user_id = "@mindroom_team1:localhost"
    setup_test_bot(team1_bot, mock_client)

    # Apply room updates
    await team1_bot.join_configured_rooms()
    await team1_bot.leave_unconfigured_rooms()

    # Verify team1 joined room6 (new room)
    assert "room6" in joined_rooms.get("@mindroom_team1:localhost", [])
    # Team1 should not leave room3 (still configured)
    assert "room3" not in left_rooms.get("@mindroom_team1:localhost", [])


@pytest.mark.asyncio
@pytest.mark.requires_matrix  # This test requires a real Matrix server or extensive mocking
@pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
async def test_orchestrator_handles_config_reload(  # noqa: PLR0915
    initial_config: Config,
    updated_config: Config,
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that the orchestrator properly handles config reloads and updates all bots."""
    # Track config loads
    config_loads = [initial_config, updated_config]
    load_count = [0]

    def mock_load_config(_config_path: Path | None = None) -> Config:
        result = config_loads[min(load_count[0], len(config_loads) - 1)]
        load_count[0] += 1
        return result

    monkeypatch.setattr("mindroom.config.main.Config.from_yaml", mock_load_config)

    async def mock_ensure_all_agent_users(_homeserver: str) -> dict[str, AgentMatrixUser]:
        return mock_agent_users

    monkeypatch.setattr("mindroom.matrix.users._ensure_all_agent_users", mock_ensure_all_agent_users)

    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock topic generation to avoid calling AI
    async def mock_generate_room_topic_ai(room_key: str, room_name: str, config: Config) -> str:  # noqa: ARG001
        return f"Test topic for {room_name}"

    monkeypatch.setattr("mindroom.topic_generator.generate_room_topic_ai", mock_generate_room_topic_ai)
    monkeypatch.setattr("mindroom.matrix.rooms.generate_room_topic_ai", mock_generate_room_topic_ai)

    # Create orchestrator
    # Mock start/sync at class level so newly created bots during update_config don't perform real login/sync
    # But we need to ensure client gets set when start() is called
    async def mock_start(self: AgentBot) -> None:
        """Mock start that sets a mock client."""
        self.client = AsyncMock()
        self.client.user_id = self.agent_user.user_id
        self.running = True

    monkeypatch.setattr("mindroom.bot.AgentBot.start", mock_start)
    monkeypatch.setattr("mindroom.bot.AgentBot.sync_forever", AsyncMock())
    monkeypatch.setattr("mindroom.bot.TeamBot.start", mock_start)
    monkeypatch.setattr("mindroom.bot.TeamBot.sync_forever", AsyncMock())
    monkeypatch.setattr("mindroom.orchestrator._MultiAgentOrchestrator._ensure_user_account", AsyncMock())
    monkeypatch.setattr("mindroom.orchestrator._MultiAgentOrchestrator._setup_rooms_and_memberships", AsyncMock())

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))

    # Initialize with initial config
    await orchestrator.initialize()

    # Verify initial state
    assert "agent1" in orchestrator.agent_bots
    assert "agent2" in orchestrator.agent_bots
    assert "agent3" not in orchestrator.agent_bots  # Not in initial config
    assert "team1" in orchestrator.agent_bots
    assert ROUTER_AGENT_NAME in orchestrator.agent_bots

    # Check initial room assignments
    assert set(orchestrator.agent_bots["agent1"].rooms) == {"room1", "room2"}
    assert set(orchestrator.agent_bots["agent2"].rooms) == {"room1"}
    assert set(orchestrator.agent_bots["team1"].rooms) == {"room3"}
    assert set(orchestrator.agent_bots[ROUTER_AGENT_NAME].rooms) == {"room1", "room2", "room3"}

    # Create a mock start method that initializes client
    async def mock_start_with_thread_manager(self: AgentBot) -> None:
        """Mock start that initializes client."""
        if not hasattr(self, "client") or self.client is None:
            self.client = AsyncMock()
            self.client.user_id = self.agent_user.user_id

    # Patch AgentBot.start and TeamBot.start to use our mock
    monkeypatch.setattr("mindroom.bot.AgentBot.start", mock_start_with_thread_manager)
    monkeypatch.setattr("mindroom.bot.TeamBot.start", mock_start_with_thread_manager)

    # Mock bot operations for update
    for bot in orchestrator.agent_bots.values():
        monkeypatch.setattr(bot, "stop", AsyncMock())
        monkeypatch.setattr(bot, "start", mock_start_with_thread_manager)
        monkeypatch.setattr(bot, "ensure_user_account", AsyncMock())
        monkeypatch.setattr(bot, "sync_forever", AsyncMock(side_effect=asyncio.CancelledError()))

    # Update config
    updated = await orchestrator.update_config()
    assert updated  # Should return True since config changed

    # Verify updated state
    assert "agent1" in orchestrator.agent_bots
    assert "agent2" in orchestrator.agent_bots
    assert "agent3" in orchestrator.agent_bots  # New agent added
    assert "team1" in orchestrator.agent_bots
    assert ROUTER_AGENT_NAME in orchestrator.agent_bots

    # Check updated room assignments
    assert set(orchestrator.agent_bots["agent1"].rooms) == {"room1", "room4"}
    assert set(orchestrator.agent_bots["agent2"].rooms) == {"room2", "room3"}
    assert set(orchestrator.agent_bots["agent3"].rooms) == {"room5"}
    assert set(orchestrator.agent_bots["team1"].rooms) == {"room3", "room6"}
    assert set(orchestrator.agent_bots[ROUTER_AGENT_NAME].rooms) == {
        "room1",
        "room2",
        "room3",
        "room4",
        "room5",
        "room6",
    }


@pytest.mark.asyncio
async def test_room_membership_state_after_config_update(  # noqa: C901, PLR0915
    initial_config: Config,  # noqa: ARG001
    updated_config: Config,  # noqa: ARG001
    mock_agent_users: dict[str, AgentMatrixUser],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Test that room membership state is correct after config updates."""
    # Simulate room membership state
    room_memberships = {
        "room1": [
            "@mindroom_agent1:localhost",
            "@mindroom_agent2:localhost",
            f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
        ],
        "room2": ["@mindroom_agent1:localhost", f"@mindroom_{ROUTER_AGENT_NAME}:localhost"],
        "room3": ["@mindroom_team1:localhost", f"@mindroom_{ROUTER_AGENT_NAME}:localhost"],
    }

    def update_room_membership(user_id: str, room_id: str, action: str) -> None:
        """Update simulated room membership."""
        if action == "join":
            if room_id not in room_memberships:
                room_memberships[room_id] = []
            if user_id not in room_memberships[room_id]:
                room_memberships[room_id].append(user_id)
        elif action == "leave":
            if room_id in room_memberships and user_id in room_memberships[room_id]:
                room_memberships[room_id].remove(user_id)

    async def mock_join_room(client: AsyncMock, room_id: str) -> bool:
        update_room_membership(client.user_id, room_id, "join")
        return True

    async def mock_leave_room(client: AsyncMock, room_id: str) -> bool:
        update_room_membership(client.user_id, room_id, "leave")
        return True

    monkeypatch.setattr("mindroom.bot_room_lifecycle.join_room", mock_join_room)
    monkeypatch.setattr("mindroom.matrix.rooms.leave_room", mock_leave_room)

    # Mock restore_scheduled_tasks
    async def mock_restore_scheduled_tasks(
        _client: AsyncMock,
        _room_id: str,
        _config: Config,
        _runtime_paths: object,
        _event_cache: object,
        _conversation_cache: object,
    ) -> int:
        return 0

    monkeypatch.setattr("mindroom.bot.restore_scheduled_tasks", mock_restore_scheduled_tasks)

    # Mock resolve_room_aliases
    def mock_resolve_room_aliases(aliases: list[str]) -> list[str]:
        return list(aliases)

    monkeypatch.setattr("mindroom.bot.resolve_room_aliases", mock_resolve_room_aliases)

    # Mock get_joined_rooms based on room_memberships
    async def mock_get_joined_rooms(client: AsyncMock) -> list[str]:
        user_id = client.user_id
        rooms = []
        for room_id, members in room_memberships.items():
            if user_id in members:
                rooms.append(room_id)
        return rooms

    monkeypatch.setattr("mindroom.bot_room_lifecycle.get_joined_rooms", mock_get_joined_rooms)

    # Apply config updates for each bot
    bots_config = {
        "@mindroom_agent1:localhost": {"old": ["room1", "room2"], "new": ["room1", "room4"]},
        "@mindroom_agent2:localhost": {"old": ["room1"], "new": ["room2", "room3"]},
        "@mindroom_agent3:localhost": {"old": [], "new": ["room5"]},
        "@mindroom_team1:localhost": {"old": ["room3"], "new": ["room3", "room6"]},
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost": {
            "old": ["room1", "room2", "room3"],
            "new": ["room1", "room2", "room3", "room4", "room5", "room6"],
        },
    }

    # Simulate config update for each bot
    for user_id, bot_config in bots_config.items():
        mock_client = AsyncMock()
        mock_client.user_id = user_id

        # Determine which agent this is
        if "agent1" in user_id:
            agent_user = mock_agent_users["agent1"]
        elif "agent2" in user_id:
            agent_user = mock_agent_users["agent2"]
        elif "agent3" in user_id:
            agent_user = mock_agent_users["agent3"]
        elif "team1" in user_id:
            agent_user = mock_agent_users["team1"]
        else:
            agent_user = mock_agent_users[ROUTER_AGENT_NAME]

        config = _runtime_bound_config(Config(router=RouterConfig(model="default")), tmp_path)

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            runtime_paths=runtime_paths_for(config),
            rooms=bot_config["new"],
        )
        setup_test_bot(bot, mock_client)

        await bot.join_configured_rooms()
        await bot.leave_unconfigured_rooms()

    # Verify final room membership state
    assert set(room_memberships.get("room1", [])) == {
        "@mindroom_agent1:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room2", [])) == {
        "@mindroom_agent2:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room3", [])) == {
        "@mindroom_agent2:localhost",
        "@mindroom_team1:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room4", [])) == {
        "@mindroom_agent1:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room5", [])) == {
        "@mindroom_agent3:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }
    assert set(room_memberships.get("room6", [])) == {
        "@mindroom_team1:localhost",
        f"@mindroom_{ROUTER_AGENT_NAME}:localhost",
    }


@pytest.mark.asyncio
async def test_in_flight_response_count_nonzero_during_send_response(
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """in_flight_response_count must be >0 even while _send_response is still awaiting."""
    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    bot = AgentBot(
        agent_user=mock_agent_users["agent1"],
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    setup_test_bot(bot, AsyncMock())

    send_entered = asyncio.Event()
    release_send = asyncio.Event()

    async def slow_send(*_args: object, **_kwargs: object) -> str:
        send_entered.set()
        await release_send.wait()
        return "$msg"

    bot._send_response = AsyncMock(side_effect=slow_send)
    install_send_response_mock(bot, bot._send_response)

    async def response_function(message_id: str | None) -> None:
        pass

    task = asyncio.create_task(
        bot._response_runner.run_cancellable_response(
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            response_function=response_function,
            thinking_message="Thinking...",
        ),
    )

    try:
        await asyncio.wait_for(send_entered.wait(), timeout=1)
        # _send_response is blocked, but the pre-tracking sentinel must be visible
        assert bot.in_flight_response_count >= 1
    finally:
        release_send.set()
        await asyncio.gather(task, return_exceptions=True)
        for t in bot.stop_manager.cleanup_tasks:
            t.cancel()
        await asyncio.gather(*bot.stop_manager.cleanup_tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_run_cancellable_response_does_not_depend_on_current_task_lookup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """Response tracking should not depend on asyncio ambient task lookup."""
    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    bot = AgentBot(
        agent_user=mock_agent_users["agent1"],
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    setup_test_bot(bot, AsyncMock())

    def fail_current_task() -> None:
        msg = "_run_cancellable_response should not call asyncio.current_task()"
        raise AssertionError(msg)

    monkeypatch.setattr("mindroom.bot.asyncio.current_task", fail_current_task)

    async def response_function(message_id: str | None) -> None:
        assert message_id is None

    await bot._response_runner.run_cancellable_response(
        room_id="!room:localhost",
        reply_to_event_id="$reply",
        thread_id=None,
        response_function=response_function,
    )


@pytest.mark.asyncio
async def test_run_cancellable_response_marks_thinking_placeholder_pending(
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """Initial thinking messages should carry pending stream metadata for restart-safe classification."""
    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    bot = AgentBot(
        agent_user=mock_agent_users["agent1"],
        storage_path=tmp_path,
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    setup_test_bot(bot, AsyncMock())

    captured_send: dict[str, object] = {}

    async def fake_send_response(
        room_id: str,
        reply_to_event_id: str | None,
        response_text: str,
        thread_id: str | None,
        reply_to_event: object | None = None,
        skip_mentions: bool = False,
        tool_trace: list[object] | None = None,
        extra_content: dict[str, object] | None = None,
        thread_mode_override: str | None = None,
        target: object | None = None,
    ) -> str:
        captured_send["room_id"] = room_id
        captured_send["reply_to_event_id"] = reply_to_event_id
        captured_send["response_text"] = response_text
        captured_send["thread_id"] = thread_id
        captured_send["reply_to_event"] = reply_to_event
        captured_send["skip_mentions"] = skip_mentions
        captured_send["tool_trace"] = tool_trace
        captured_send["extra_content"] = extra_content
        captured_send["thread_mode_override"] = thread_mode_override
        captured_send["target"] = target
        return "$thinking"

    bot._send_response = AsyncMock(side_effect=fake_send_response)
    install_send_response_mock(bot, bot._send_response)

    async def response_function(message_id: str | None) -> None:
        assert message_id == "$thinking"

    await bot._response_runner.run_cancellable_response(
        room_id="!room:localhost",
        reply_to_event_id="$reply",
        thread_id=None,
        response_function=response_function,
        thinking_message="Thinking...",
    )

    assert captured_send["response_text"] == "Thinking..."
    assert captured_send["extra_content"] == {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}


@pytest.mark.asyncio
async def test_failed_update_config_does_not_strand_queued_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A failed update_config must not prevent a subsequently queued reload from running."""
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.running = True

    mock_bot = MagicMock(spec=AgentBot)
    mock_bot.in_flight_response_count = 0
    orchestrator.agent_bots["agent1"] = mock_bot

    call_count = 0

    async def failing_then_succeeding_update() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call fails; queue a new reload during the failure
            orchestrator.request_config_reload()
            msg = "Simulated config update failure"
            raise RuntimeError(msg)
        return True

    orchestrator.update_config = AsyncMock(side_effect=failing_then_succeeding_update)
    orchestrator.request_config_reload()
    task = orchestrator._config_reload_task
    assert task is not None

    await asyncio.wait_for(task, timeout=2)

    assert orchestrator.update_config.await_count == 2


@pytest.mark.asyncio
async def test_config_change_during_update_config_triggers_second_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A config change arriving while update_config runs should cause a second reload."""
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.running = True

    mock_bot = MagicMock(spec=AgentBot)
    mock_bot.in_flight_response_count = 0
    orchestrator.agent_bots["agent1"] = mock_bot

    call_count = 0

    async def update_config_with_second_change() -> bool:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            orchestrator.request_config_reload()
        return True

    orchestrator.update_config = AsyncMock(side_effect=update_config_with_second_change)
    orchestrator.request_config_reload()
    task = orchestrator._config_reload_task
    assert task is not None

    await asyncio.wait_for(task, timeout=2)

    assert orchestrator.update_config.await_count == 2


@pytest.mark.asyncio
async def test_shutdown_during_active_drain_cancels_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Calling stop() during an active drain must cancel the reload without applying it."""
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestrator._CONFIG_RELOAD_IDLE_POLL_SECONDS", 0.01)

    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path))
    orchestrator.running = True

    mock_bot = MagicMock(spec=AgentBot)
    mock_bot.in_flight_response_count = 1  # Never drains
    mock_bot.stop = AsyncMock()
    orchestrator.agent_bots["agent1"] = mock_bot
    orchestrator.update_config = AsyncMock(return_value=True)
    orchestrator.request_config_reload()
    task = orchestrator._config_reload_task
    assert task is not None

    # Let the drain loop start polling
    await asyncio.sleep(0.05)
    orchestrator.update_config.assert_not_awaited()

    # Shutdown
    await orchestrator.stop()

    # The reload task should have been cancelled
    assert task.done()
    orchestrator.update_config.assert_not_awaited()
