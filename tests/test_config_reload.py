"""Tests for config auto-reload and room membership updates."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from contextlib import suppress
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from structlog.testing import capture_logs

import mindroom.orchestrator as orchestrator_module
import mindroom.tool_system.plugin_imports as plugin_module
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig, RoomConfig, TeamConfig
from mindroom.config.calls import CallsConfig, CascadedCallProfile, LiveCallProfile, RealtimeCallProfile
from mindroom.config.knowledge import KnowledgeBaseConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, ModelPricing, RouterConfig
from mindroom.config.voice import SpeechServiceConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.delivery_gateway import SendTextRequest
from mindroom.file_watcher import _tree_snapshot
from mindroom.hooks import EVENT_MESSAGE_RECEIVED, ConfigReloadedContext, hook
from mindroom.matrix.users import AgentMatrixUser
from mindroom.orchestration.config_updates import (
    _AGENT_LIVE_FIELDS,
    _TEAM_LIVE_FIELDS,
    ConfigUpdatePlan,
    build_config_update_plan,
)
from mindroom.orchestration.plugin_watch import (
    _collect_plugin_root_changes,
    _drop_unconfigured_plugin_root_snapshots,
    watch_plugins_task,
)
from mindroom.orchestration.runtime import log_startup_phase_finished, log_startup_phase_started
from mindroom.orchestrator import _MultiAgentOrchestrator, _watch_skills_task
from mindroom.personal_agents import _append_personal_agent_roster_entry
from mindroom.reply_turn.request import ResponseRequest
from mindroom.response_admission import ResponseAdmissionRefusedError
from mindroom.response_sources import Owned, ResponseSources, SourceFate
from mindroom.runtime_shutdown import SYNC_RESTART_SHUTDOWN
from mindroom.startup_errors import PermanentStartupError
from mindroom.tool_system.plugins import PluginGeneration, PluginReloadResult, config_plugins
from tests.bot_helpers import make_test_agent_bot
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    cached_plugin_module,
    initialize_from_config_file,
    install_bots,
    install_send_response_mock,
    orchestrator_runtime_paths,
    published_config_load,
    replace_response_runner_deps,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    write_hook_plugin,
)
from tests.runtime_fakes import ready_runtime
from tests.test_personal_agents_config import BASE as PERSONAL_AGENTS_BASE
from tests.test_personal_agents_config import _entry as personal_agent_entry
from tests.test_plugins import _write_import_task_plugin

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable
    from collections.abc import Set as AbstractSet

    from mindroom.constants import RuntimePaths
    from mindroom.message_target import MessageTarget
    from mindroom.orchestration.entity_fleet import EntityFleet
    from mindroom.orchestration.rooms import RoomSetup


def _calls_for(
    agent_name: str,
    *,
    enabled: bool = True,
    voice: str = "marin",
) -> CallsConfig:
    return CallsConfig(
        enabled=enabled,
        profiles={
            "voice": RealtimeCallProfile(
                backend="realtime",
                model="gpt-realtime",
                credentials_service="openai",
                voice=voice,
            ),
        },
        agents={agent_name: "voice"},
    )


def _delegated_calls_for(agent_name: str, *, model: str | None, backend: str) -> CallsConfig:
    if backend == "live":
        return CallsConfig(
            enabled=True,
            profiles={
                "voice": LiveCallProfile(
                    backend="live",
                    model="gpt-live-1",
                    credentials_service="openai_live",
                    voice="marin",
                    agent_model=model,
                ),
            },
            agents={agent_name: "voice"},
        )
    speech_service = SpeechServiceConfig(
        provider="openai_compatible",
        model="local-speech",
        host="http://127.0.0.1:9000",
    )
    return CallsConfig(
        enabled=True,
        profiles={
            "voice": CascadedCallProfile(
                backend="cascaded",
                model=model,
                stt=speech_service,
                tts=speech_service,
            ),
        },
        agents={agent_name: "voice"},
    )


def _call_bots(entities: Iterable[str], *, in_call: AbstractSet[str] = frozenset()) -> dict[str, AsyncMock]:
    """Return running bots, with the agents in ``in_call`` holding an active voice call."""
    return {
        entity: AsyncMock(active_call_requesters=("@caller:example.org",) if entity in in_call else ())
        for entity in entities
    }


def _runtime_bound_config(config: Config, runtime_root: Path | None = None) -> Config:
    """Return a runtime-bound config for reload tests."""
    runtime_paths = test_runtime_paths(runtime_root or Path(tempfile.mkdtemp()))
    return bind_runtime_paths(config, runtime_paths)


def test_startup_phase_logging_helpers_emit_structured_timing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Startup phase logging helpers should emit one stable structured event pair."""
    logger_mock = MagicMock()
    monkeypatch.setattr("mindroom.orchestration.runtime.logger", logger_mock)

    with patch("mindroom.orchestration.runtime.time.monotonic", side_effect=[10.0, 10.1234]):
        started_at = log_startup_phase_started("phase.name")
        log_startup_phase_finished("phase.name", started_at, status="failed")

    assert started_at == 10.0
    logger_mock.info.assert_any_call("startup_phase_started", phase="phase.name")
    logger_mock.info.assert_any_call(
        "startup_phase_finished",
        phase="phase.name",
        status="failed",
        elapsed_ms=123.4,
    )


@pytest.mark.asyncio
async def test_watch_skills_task_snapshots_off_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skills watcher snapshots off the asyncio loop and keeps watching until cancelled, even after shutdown begins."""

    class DummyOrchestrator:
        running = True

    class StopWatchingError(Exception):
        pass

    dummy_orchestrator = DummyOrchestrator()
    snapshot_function_calls: list[object] = []
    clear_calls = 0
    sleeps = 0

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
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise StopWatchingError

    def fake_clear_skill_cache() -> None:
        nonlocal clear_calls
        clear_calls += 1

    monkeypatch.setattr(orchestrator_module, "get_skill_snapshot", direct_snapshot_call)
    monkeypatch.setattr(orchestrator_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(orchestrator_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(orchestrator_module, "clear_skill_cache", fake_clear_skill_cache)

    with pytest.raises(StopWatchingError):
        await _watch_skills_task(dummy_orchestrator)  # type: ignore[arg-type]

    # Both snapshots run the same off-loop reader, never get_skill_snapshot on the loop itself.
    assert len(snapshot_function_calls) == 2
    assert snapshot_function_calls[0] is snapshot_function_calls[1]
    assert snapshot_function_calls[0] is not direct_snapshot_call
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
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-5"}},
        "router": {"model": "default"},
        "agents": {
            "assistant": {
                "display_name": "Assistant",
                "role": "Helpful",
                "model": "default",
                "rooms": ["lobby"],
            },
        },
        "administrators": ["@owner:localhost"],
        "plugins": [{"path": "./plugins/removed-task-plugin"}] if with_plugin else [],
    }
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump(config_data, sort_keys=False),
        encoding="utf-8",
    )


async def _noop_prepare_managed_accounts(
    config: Config,
    runtime_paths: RuntimePaths,
    entity_names: Iterable[str],
    *,
    include_internal_user: bool,
    update_runtime_state: bool,
) -> dict[str, AgentMatrixUser]:
    del runtime_paths, include_internal_user, update_runtime_state
    return {
        entity_name: AgentMatrixUser(
            agent_name=entity_name,
            user_id=f"@actual_{entity_name}:localhost",
            display_name=config.entity_display_name(entity_name),
            password=TEST_PASSWORD,
        )
        for entity_name in entity_names
    }


async def _noop_sync_mcp_manager(
    self: _MultiAgentOrchestrator,
    config: Config,
) -> set[str]:
    del self, config
    return set()


async def _noop_sync_runtime_support_services(
    self: _MultiAgentOrchestrator,
    config: Config,
    *,
    previous_config: Config | None = None,
) -> None:
    del self, config, previous_config


async def _noop_setup_rooms_and_memberships(self: RoomSetup, bots: list[AgentBot]) -> None:
    del self, bots


def _noop_start_sync(self: EntityFleet, bots: Iterable[AgentBot]) -> None:
    del self, bots


async def _noop_start(self: AgentBot) -> None:
    self.running = True


async def _noop_stop(self: AgentBot, *, shutdown_intent: object | None = None) -> None:
    del shutdown_intent
    self.running = False


def _patch_orchestrator_plugin_update_test_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch bot and orchestrator runtime helpers unrelated to plugin teardown."""
    monkeypatch.setattr("mindroom.orchestrator.prepare_managed_accounts", _noop_prepare_managed_accounts)
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._sync_mcp_manager",
        _noop_sync_mcp_manager,
    )
    monkeypatch.setattr(
        "mindroom.orchestrator._MultiAgentOrchestrator._sync_runtime_support_services",
        _noop_sync_runtime_support_services,
    )
    monkeypatch.setattr(
        "mindroom.orchestration.rooms.RoomSetup.setup",
        _noop_setup_rooms_and_memberships,
    )
    monkeypatch.setattr("mindroom.orchestration.entity_fleet.EntityFleet.start_sync", _noop_start_sync)
    monkeypatch.setattr("mindroom.bot.AgentBot.start", _noop_start)
    monkeypatch.setattr("mindroom.bot.AgentBot.stop", _noop_stop)


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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = (), **_: int) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        reload_seen.set()
        return PluginReloadResult((), 0)

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
async def test_plugin_watcher_idles_until_cancelled_once_shutdown_begins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A watcher that returned at shutdown would be restarted with a warning, so it idles until cancelled."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.01)
    plugin_root = tmp_path / "plugins" / "demo"
    plugin_root.mkdir(parents=True)
    (plugin_root / "mindroom.plugin.json").write_text(
        '{"name": "demo", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )
    (plugin_root / "hooks.py").write_text("VALUE = 1\n", encoding="utf-8")
    config = _runtime_bound_config(Config(plugins=["./plugins/demo"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True
    orchestrator.reload_plugins_now = AsyncMock()
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.05)
        orchestrator.running = False
        (plugin_root / "hooks.py").write_text("VALUE = 2\n", encoding="utf-8")
        await asyncio.sleep(0.1)
        assert not watcher_task.done()
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    orchestrator.reload_plugins_now.assert_not_awaited()


def test_plugin_watcher_drops_unconfigured_root_snapshots(tmp_path: Path) -> None:
    """Plugin watcher snapshot pruning should remove only unconfigured roots."""
    configured_root = tmp_path / "plugins" / "configured"
    removed_root = tmp_path / "plugins" / "removed"
    snapshots = {
        configured_root: {configured_root / "hooks.py": 1},
        removed_root: {removed_root / "hooks.py": 1},
    }

    _drop_unconfigured_plugin_root_snapshots((configured_root,), snapshots)

    assert snapshots == {configured_root: {configured_root / "hooks.py": 1}}


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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = (), **_: int) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        reload_seen.set()
        return PluginReloadResult((), 0)

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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = (), **_: int) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        reload_seen.set()
        return PluginReloadResult((), 0)

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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(first_config), config=first_config)
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = (), **_: int) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        reload_seen.set()
        return PluginReloadResult((), 0)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.08)
        orchestrator.config = second_config
        orchestrator.plugin_watch.sync_roots(second_config)
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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(first_config), config=first_config)
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = (), **_: int) -> PluginReloadResult:
        reload_calls.append((source, changed_paths))
        return PluginReloadResult((), 0)

    orchestrator.reload_plugins_now = AsyncMock(side_effect=record_reload)
    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.08)
        orchestrator.config = second_config
        orchestrator.plugin_watch.sync_roots(second_config)
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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    reload_seen = asyncio.Event()

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = (), **_: int) -> PluginReloadResult:
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
        return PluginReloadResult((), 0)

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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True

    reload_call_count = 0

    def record_reload(config: Config, runtime_paths: object) -> PluginGeneration:
        nonlocal reload_call_count
        del config, runtime_paths
        reload_call_count += 1
        return PluginGeneration()

    watcher_task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.sleep(0.05)
        with patch("mindroom.orchestrator.prepare_plugins", side_effect=record_reload):
            hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
            await asyncio.sleep(0.02)
            await orchestrator.reload_plugins_now(source="command")
            await asyncio.sleep(0.12)
    finally:
        watcher_task.cancel()
        with suppress(asyncio.CancelledError):
            await watcher_task

    assert reload_call_count == 1


@pytest.mark.asyncio
async def test_manual_plugin_reload_interrupts_scripts_before_plugin_publication(tmp_path: Path) -> None:
    """Command and watcher reloads share the fail-closed script boundary."""
    config = _runtime_bound_config(Config(), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True
    order: list[str] = []

    async def interrupt_for_plugin_reload(_runtime: object) -> None:
        order.append("scripts")

    async def finish_reload(_runtime: object) -> None:
        order.append("complete")

    def reload_current_plugins(_config: Config, _runtime_paths: object) -> PluginGeneration:
        order.append("plugins")
        return PluginGeneration()

    with (
        patch.object(
            type(orchestrator.script_runtime),
            "interrupt_for_plugin_reload",
            new=interrupt_for_plugin_reload,
        ),
        patch.object(
            type(orchestrator.script_runtime),
            "finish_reload",
            new=finish_reload,
        ),
        patch("mindroom.orchestrator.prepare_plugins", side_effect=reload_current_plugins),
    ):
        await orchestrator.reload_plugins_now(source="command")

    assert order == ["scripts", "plugins", "complete"]


def _plugin_watcher_race_runtime(tmp_path: Path) -> tuple[_MultiAgentOrchestrator, Config, Path, Path]:
    plugin_root = tmp_path / "plugin"
    plugin_root.mkdir()
    (plugin_root / "mindroom.plugin.json").write_text('{"name":"demo","skills":[]}', encoding="utf-8")
    hooks_path = plugin_root / "hooks.py"
    hooks_path.write_text("VALUE = 1\n", encoding="utf-8")
    config = _runtime_bound_config(Config(plugins=[str(plugin_root)]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True
    return orchestrator, config, plugin_root, hooks_path


@pytest.mark.asyncio
async def test_plugin_watcher_drops_pending_changes_when_reload_commits_during_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A reload committing mid-scan should consume pending edits."""
    monkeypatch.setattr("mindroom.file_watcher._WATCH_SCAN_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr("mindroom.file_watcher._WATCH_TREE_DEBOUNCE_SECONDS", 0.0)
    orchestrator, config, plugin_root, hooks_path = _plugin_watcher_race_runtime(tmp_path)
    second_scan_started = asyncio.Event()
    release_second_scan = asyncio.Event()
    third_scan_started = asyncio.Event()
    collect_count = 0

    async def collect(roots: tuple[Path, ...], _snapshots: dict[Path, dict[Path, int]]) -> set[Path]:
        nonlocal collect_count
        assert roots == (plugin_root,)
        collect_count += 1
        if collect_count == 1:
            return {hooks_path}
        if collect_count == 2:
            second_scan_started.set()
            await release_second_scan.wait()
        else:
            third_scan_started.set()
        return set()

    monkeypatch.setattr("mindroom.orchestration.plugin_watch._collect_plugin_root_changes", collect)
    reload_mock = AsyncMock()
    orchestrator.reload_plugins_now = reload_mock
    task = asyncio.create_task(watch_plugins_task(orchestrator))
    try:
        await asyncio.wait_for(second_scan_started.wait(), timeout=1)
        orchestrator.plugin_watch.replace_snapshots(*orchestrator.plugin_watch.capture(config))
        release_second_scan.set()
        # The scan after the committed reload shows the watcher already handled the pending edit.
        await asyncio.wait_for(third_scan_started.wait(), timeout=1)
    finally:
        task.cancel()
        release_second_scan.set()
        with suppress(asyncio.CancelledError):
            await task
    reload_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_plugin_reload_keeps_edit_landing_while_source_loads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An edit during source loading should remain visible to the watcher."""
    orchestrator, _, plugin_root, hooks_path = _plugin_watcher_race_runtime(tmp_path)
    source_loaded = False

    def snapshot(_root: Path) -> dict[Path, int]:
        return {hooks_path: 2 if source_loaded else 1}

    def load_source(_config: Config, _runtime_paths: object) -> PluginGeneration:
        nonlocal source_loaded
        source_loaded = True
        return PluginGeneration()

    monkeypatch.setattr("mindroom.orchestration.plugin_watch.file_watcher._tree_snapshot", snapshot)
    with patch("mindroom.orchestrator.prepare_plugins", side_effect=load_source):
        await orchestrator.reload_plugins_now(source="command")
    changed_paths = await _collect_plugin_root_changes((plugin_root,), orchestrator.plugin_watch.last_snapshot_by_root)
    assert changed_paths == {hooks_path}


@pytest.mark.asyncio
async def test_watcher_reload_drops_stale_revision_after_waiting_for_config_lock(tmp_path: Path) -> None:
    """A newer publication while lock-blocked should consume watcher work."""
    orchestrator, config, _, hooks_path = _plugin_watcher_race_runtime(tmp_path)
    orchestrator.plugin_watch.sync_roots(config)
    await orchestrator._config_update_lock.acquire()
    task = asyncio.create_task(
        orchestrator.reload_plugins_now(
            source="watcher",
            changed_paths=(hooks_path,),
            expected_revision=orchestrator.plugin_watch.revision,
        ),
    )
    await asyncio.sleep(0)
    orchestrator.plugin_watch.replace_snapshots(*orchestrator.plugin_watch.capture(config))
    orchestrator._config_update_lock.release()
    assert await task is None


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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True

    reload_calls: list[tuple[str, tuple[Path, ...]]] = []
    first_reload_seen = asyncio.Event()
    second_reload_seen = asyncio.Event()
    error_message = "broken plugin"

    async def record_reload(*, source: str, changed_paths: tuple[Path, ...] = (), **_: int) -> PluginReloadResult:
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
async def test_an_explicit_reload_skips_a_broken_plugin_and_ends_its_tasks(tmp_path: Path) -> None:
    """A plugin that broke since the last reload is skipped and reported, and its live module's task ends."""
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
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True

    shared_task = asyncio.create_task(asyncio.Event().wait())
    try:
        initial = await orchestrator.reload_plugins_now(source="test")
        assert initial.active_plugin_names == ("broken-reload",)
        assert len(orchestrator.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED)) == 1

        cached_plugin_module(hooks_path)._AUTO_POKE_TASK = shared_task
        hooks_path.unlink()

        result = await orchestrator.reload_plugins_now(source="test")
        await asyncio.sleep(0)

        assert result.active_plugin_names == ()
        assert [plugin.path for plugin in result.skipped] == ["./plugins/broken-reload"]
        assert shared_task.cancelled()
        assert orchestrator.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED) == ()
    finally:
        shared_task.cancel()
        await asyncio.gather(shared_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_a_reload_with_two_plugins_of_one_name_keeps_the_live_hooks_and_skills(tmp_path: Path) -> None:
    """A reload whose plugins collide on a name publishes nothing, so the live plugins keep their hooks and skills."""
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
    runtime_paths = runtime_paths_for(config)
    orchestrator = _MultiAgentOrchestrator(runtime_paths, config=config)
    orchestrator.running = True

    initial = await orchestrator.reload_plugins_now(source="test")
    assert initial.active_plugin_names == ("first", "second")
    live_hooks = orchestrator.hook_registry
    assert [hook.plugin_name for hook in live_hooks.hooks_for(EVENT_MESSAGE_RECEIVED)] == ["first", "second"]
    assert config_plugins(config, runtime_paths).skill_roots == ((first_root / "skills").resolve(),)

    second_manifest_path.write_text(
        '{"name": "first", "hooks_module": "hooks.py", "skills": []}',
        encoding="utf-8",
    )

    with pytest.raises(plugin_module.PluginValidationError, match="Duplicate plugin manifest names configured"):
        await orchestrator.reload_plugins_now(source="test")

    assert orchestrator.hook_registry is live_hooks
    assert config_plugins(config, runtime_paths).skill_roots == ((first_root / "skills").resolve(),)


@pytest.mark.asyncio
async def test_a_reload_that_fails_as_a_whole_keeps_the_live_plugins_and_their_tasks(tmp_path: Path) -> None:
    """A reload whose plugin set no tolerant load can accept publishes nothing: the live plugins keep hooks and tasks."""
    _write_import_task_plugin(tmp_path / "plugins" / "import-task")
    other_manifest = tmp_path / "plugins" / "other" / "mindroom.plugin.json"
    other_manifest.parent.mkdir(parents=True)
    other_manifest.write_text('{"name": "other", "skills": []}', encoding="utf-8")
    config = _runtime_bound_config(Config(plugins=["./plugins/import-task", "./plugins/other"]), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True
    hooks_path = (tmp_path / "plugins" / "import-task" / "hooks.py").resolve()

    tasks: list[asyncio.Task[None]] = []
    try:
        await orchestrator.reload_plugins_now(source="command")
        live_task = cached_plugin_module(hooks_path)._BACKGROUND_TASK
        tasks.append(live_task)
        # The other plugin now claims the live plugin's name, which skipping broken plugins cannot resolve.
        other_manifest.write_text('{"name": "import-task", "skills": []}', encoding="utf-8")

        with pytest.raises(plugin_module.PluginValidationError, match="Duplicate plugin manifest names configured"):
            await orchestrator.reload_plugins_now(source="command")
        await asyncio.sleep(0)

        assert not live_task.done()
        assert _live_hook_plugins(orchestrator) == ["import-task"]
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@hook("message:received")
async def _audit(ctx: object) -> None:
    """Be the one default-priority `message:received` hook of a plugin the reload tests write."""
    del ctx


def _write_hook_plugins_config(tmp_path: Path, plugin_paths: list[str], *, role: str = "Helpful") -> None:
    """Write the config file of the plugin reload tests: one agent and ``plugin_paths`` in order."""
    config_data = {
        "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-5-5"}},
        "router": {"model": "default"},
        "agents": {"assistant": {"display_name": "Assistant", "role": role, "model": "default", "rooms": ["lobby"]}},
        "administrators": ["@owner:localhost"],
        "plugins": plugin_paths,
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")


def _live_hook_context(orchestrator: _MultiAgentOrchestrator) -> ConfigReloadedContext:
    """Return a hook context bound to the orchestrator's live hook registry, as a dispatched hook would be."""
    return ConfigReloadedContext(
        event_name="config:reloaded",
        plugin_name="test",
        settings={},
        config=orchestrator.config,
        runtime_paths=orchestrator.runtime_paths,
        logger=MagicMock(),
        correlation_id="reload",
        _hook_registry_state=orchestrator.hook_registry_state,
        _hook_registry_snapshot=orchestrator.hook_registry,
        changed_entities=(),
        added_entities=(),
        removed_entities=(),
        plugin_changes=(),
    )


def _live_hook_plugins(orchestrator: _MultiAgentOrchestrator) -> list[str]:
    return [hook.plugin_name for hook in orchestrator.hook_registry.hooks_for(EVENT_MESSAGE_RECEIVED)]


@pytest.mark.asyncio
async def test_a_hook_stays_active_through_a_config_edit_and_ends_with_a_plugin_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An edit that leaves the plugin entries alone keeps the live hooks; a plugin reload replaces them."""
    write_hook_plugin(tmp_path / "plugins" / "p", "p", [_audit])
    _write_hook_plugins_config(tmp_path, ["./plugins/p"])
    _patch_orchestrator_plugin_update_test_runtime(monkeypatch)
    orchestrator = await initialize_from_config_file(orchestrator_runtime_paths(tmp_path))
    orchestrator.running = True
    context = _live_hook_context(orchestrator)
    _write_hook_plugins_config(tmp_path, ["./plugins/p"], role="Edited")

    assert await orchestrator.config_reload._update_config() is True
    assert orchestrator.config.agents["assistant"].role == "Edited"
    assert context.is_active()
    assert config_plugins(orchestrator.config, orchestrator.runtime_paths).hooks is orchestrator.hook_registry

    await orchestrator.reload_plugins_now(source="test")

    assert not context.is_active()
    assert _live_hook_context(orchestrator).is_active()


@pytest.mark.asyncio
async def test_reordering_plugins_publishes_them_with_their_hooks_in_the_new_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Plugin order orders hooks of equal priority, so reordering plugins is a plugin change that publishes."""
    write_hook_plugin(tmp_path / "plugins" / "p", "p", [_audit])
    write_hook_plugin(tmp_path / "plugins" / "q", "q", [_audit])
    _write_hook_plugins_config(tmp_path, ["./plugins/p", "./plugins/q"])
    _patch_orchestrator_plugin_update_test_runtime(monkeypatch)
    orchestrator = await initialize_from_config_file(orchestrator_runtime_paths(tmp_path))
    assert _live_hook_plugins(orchestrator) == ["p", "q"]
    previous = _live_hook_context(orchestrator)
    _write_hook_plugins_config(tmp_path, ["./plugins/q", "./plugins/p"])

    assert await orchestrator.config_reload._update_config() is True

    assert _live_hook_plugins(orchestrator) == ["q", "p"]
    assert not previous.is_active()
    assert _live_hook_context(orchestrator).is_active()


@pytest.mark.asyncio
async def test_a_reload_publishes_the_healthy_plugin_and_ends_the_task_a_failed_one_started(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A plugin whose tools module starts a task before its hooks module raises is skipped, and its task ends."""
    write_hook_plugin(tmp_path / "plugins" / "healthy", "healthy", [_audit])
    failing_root = tmp_path / "plugins" / "failing"
    failing_root.mkdir(parents=True)
    (failing_root / "mindroom.plugin.json").write_text(
        json.dumps({"name": "failing", "tools_module": "tools.py", "hooks_module": "hooks.py", "skills": []}),
        encoding="utf-8",
    )
    (failing_root / "tools.py").write_text(
        "import asyncio\nimport sys\n"
        "_TASK = asyncio.get_running_loop().create_task(asyncio.Event().wait())\n"
        "sys.modules['mindroom_test_task_probe'].tasks.append(_TASK)\n",
        encoding="utf-8",
    )
    (failing_root / "hooks.py").write_text("", encoding="utf-8")
    probe = ModuleType("mindroom_test_task_probe")
    probe.tasks = []
    monkeypatch.setitem(sys.modules, probe.__name__, probe)
    config = _runtime_bound_config(Config(plugins=["./plugins/healthy", "./plugins/failing"]), tmp_path)
    # The plugin breaks after the config that lists it was accepted.
    (failing_root / "hooks.py").write_text("raise RuntimeError('hooks module failed')\n", encoding="utf-8")
    await asyncio.gather(*probe.tasks, return_exceptions=True)
    probe.tasks.clear()
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True

    try:
        result = await orchestrator.reload_plugins_now(source="command")
        await asyncio.sleep(0)

        assert result.active_plugin_names == ("healthy",)
        assert [plugin.path for plugin in result.skipped] == ["./plugins/failing"]
        assert _live_hook_plugins(orchestrator) == ["healthy"]
        assert len(probe.tasks) == 1
        assert probe.tasks[0].cancelled()
    finally:
        for task in probe.tasks:
            task.cancel()
        await asyncio.gather(*probe.tasks, return_exceptions=True)


_GENERAL_AGENT = {"display_name": "GeneralAgent", "model": "default"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("updated_sections", "account_names", "include_internal_user"),
    [
        pytest.param(
            {"agents": {"general": _GENERAL_AGENT, "writer": {"display_name": "WriterAgent", "model": "default"}}},
            ["writer"],
            False,
            id="new-agent",
        ),
        pytest.param(
            {"agents": {"general": {"display_name": "RenamedGeneralAgent", "model": "default"}}},
            ["general"],
            False,
            id="restarted-agent",
        ),
        pytest.param(
            {"agents": {"general": _GENERAL_AGENT}, "mindroom_user": {"username": "mindroom_user"}},
            [],
            True,
            id="internal-user",
        ),
    ],
)
async def test_update_config_keeps_current_config_when_account_preparation_fails(
    tmp_path: Path,
    updated_sections: dict[str, Any],
    account_names: list[str],
    include_internal_user: bool,
) -> None:
    """A reload publishes nothing until the accounts of started and restarted entities and a changed internal user exist."""
    models = {"default": {"provider": "openai", "id": "test-model"}}
    current_config = _runtime_bound_config(Config(agents={"general": _GENERAL_AGENT}, models=models), tmp_path)
    new_config = _runtime_bound_config(Config(models=models, **updated_sections), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(current_config), config=current_config)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: MagicMock(), "general": MagicMock()})
    orchestrator.running = True
    account_error = PermanentStartupError("configured entities share a Matrix ID")

    with (
        patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(new_config)),
        patch("mindroom.orchestrator.prepare_managed_accounts", new=AsyncMock(side_effect=account_error)) as prepare,
        pytest.raises(PermanentStartupError, match="share a Matrix ID"),
    ):
        await orchestrator.config_reload._update_config()

    assert orchestrator.config is current_config
    prepare.assert_awaited_once_with(
        new_config,
        orchestrator.runtime_paths,
        account_names,
        include_internal_user=include_internal_user,
        update_runtime_state=False,
    )


@pytest.mark.asyncio
async def test_update_config_starts_agent_added_only_by_personal_agent_roster(tmp_path: Path) -> None:
    """A roster entry changes no authored byte, but its generated agent still needs the reload."""
    paths = test_runtime_paths(tmp_path)
    current_config = Config.validate_with_runtime(deepcopy(PERSONAL_AGENTS_BASE), paths)
    _append_personal_agent_roster_entry(paths, personal_agent_entry())
    new_config = Config.validate_with_runtime(deepcopy(PERSONAL_AGENTS_BASE), paths)
    assert current_config.authored_model_dump() == new_config.authored_model_dump()
    orchestrator = _MultiAgentOrchestrator(paths, config=current_config)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: MagicMock()})
    orchestrator.running = True

    with (
        patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(new_config)),
        patch(
            "mindroom.orchestrator.prepare_managed_accounts",
            new=AsyncMock(side_effect=PermanentStartupError("stop")),
        ) as prepare,
        pytest.raises(PermanentStartupError, match="stop"),
    ):
        await orchestrator.config_reload._update_config()

    assert orchestrator.config is current_config
    assert prepare.await_args.args[2] == ["mind_alice_smith"]


@pytest.mark.asyncio
async def test_initialize_binds_nothing_when_entity_account_preparation_fails(tmp_path: Path) -> None:
    """Startup must not bind the journal or build bots before entity accounts validate."""
    config = _runtime_bound_config(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
        ),
        tmp_path,
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    account_error = PermanentStartupError("configured entities share a Matrix ID")

    with (
        patch("mindroom.orchestrator.prepare_managed_accounts", new=AsyncMock(side_effect=account_error)),
        pytest.raises(PermanentStartupError, match="share a Matrix ID"),
    ):
        await orchestrator.initialize()

    assert orchestrator._open_journal is None
    assert orchestrator.agent_bots == {}


@pytest.mark.asyncio
async def test_ready_personal_agent_nudges_router_reconciliation(tmp_path: Path) -> None:
    """A generated agent's first sync lets the router onboard its owner at once instead of after a retry delay."""
    paths = test_runtime_paths(tmp_path)
    _append_personal_agent_roster_entry(paths, personal_agent_entry())
    data = deepcopy(PERSONAL_AGENTS_BASE) | {"agents": {"general": {"display_name": "General"}}}
    orchestrator = _MultiAgentOrchestrator(paths, config=Config.validate_with_runtime(data, paths))
    router = MagicMock(spec=AgentBot)
    install_bots(orchestrator, {ROUTER_AGENT_NAME: router})

    await orchestrator.handle_bot_ready(MagicMock(spec=AgentBot, agent_name="general"))
    router.personal_room_target_ready.assert_not_called()
    await orchestrator.handle_bot_ready(MagicMock(spec=AgentBot, agent_name="mind_alice_smith"))
    router.personal_room_target_ready.assert_called_once_with()


@pytest.mark.asyncio
async def test_update_config_cancels_tasks_for_removed_plugins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Config-driven plugin removal should tear down the old live plugin runtime."""
    hooks_path = _write_plugin_removal_test_files(tmp_path)
    task = asyncio.create_task(asyncio.Event().wait())
    try:
        _write_plugin_removal_test_config(tmp_path, with_plugin=True)
        _patch_orchestrator_plugin_update_test_runtime(monkeypatch)
        orchestrator = await initialize_from_config_file(orchestrator_runtime_paths(tmp_path))

        hooks_module = cached_plugin_module(hooks_path.resolve())
        package_root = hooks_module.__name__.split(".", 1)[0]
        hooks_module._AUTO_POKE_TASK = task

        _write_plugin_removal_test_config(tmp_path, with_plugin=False)
        updated = await orchestrator.config_reload._update_config()
        await asyncio.sleep(0)

        assert updated is True
        assert task.cancelled()
        assert all(
            cached.module.__file__ != str(hooks_path.resolve())
            for cached in plugin_module._MODULE_IMPORT_CACHE.values()
        )
        assert not any(
            module_name == package_root or module_name.startswith(f"{package_root}.") for module_name in sys.modules
        )
        assert not orchestrator.hook_registry.has_hooks(EVENT_MESSAGE_RECEIVED)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_initialize_and_reload_warn_about_foreign_homeserver_authorities(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup and every applied reload must name authorities homed on another homeserver."""
    warning = (
        "Administrators, room invitees, or room admins are on another homeserver; remove them unless you trust them"
    )

    def write_config(*, administrators: list[str], invite_users: list[str]) -> None:
        config_data = {
            "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-5"}},
            "router": {"model": "default"},
            "agents": {"assistant": {"display_name": "Assistant", "model": "default", "rooms": ["lobby"]}},
            "administrators": administrators,
            "room_defaults": {"invite_users": invite_users},
        }
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")

    _patch_orchestrator_plugin_update_test_runtime(monkeypatch)
    write_config(administrators=["@owner:localhost"], invite_users=["@test:m-test-4.mindroom.chat"])
    with capture_logs() as startup_logs:
        orchestrator = await initialize_from_config_file(orchestrator_runtime_paths(tmp_path))

    write_config(administrators=["@owner:localhost", "@admin:remote.example"], invite_users=["@owner:localhost"])
    with capture_logs() as reload_logs:
        updated = await orchestrator.config_reload._update_config()

    assert updated is False
    assert [log["user_ids"] for log in startup_logs if log["event"] == warning] == [["@test:m-test-4.mindroom.chat"]]
    assert [log["user_ids"] for log in reload_logs if log["event"] == warning] == [["@admin:remote.example"]]


@pytest.mark.asyncio
async def test_initialize_and_reload_warn_about_unrestricted_file_access_next_to_worker_code_tools(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Startup and every applied reload must flag path tools that can bypass a worker-isolated shell."""
    warning = (
        "Agent isolates code tools in a worker, but primary-process tools that are not confined "
        "by file_access can read runtime secrets"
    )

    def write_config(agent_name: str) -> None:
        config_data = {
            "models": {"default": {"provider": "anthropic", "id": "claude-sonnet-5"}},
            "router": {"model": "default"},
            "agents": {
                agent_name: {
                    "display_name": agent_name.title(),
                    "model": "default",
                    "rooms": ["lobby"],
                    "tools": ["shell", "gmail"],
                    "worker_tools": ["shell"],
                    "file_access": "unrestricted",
                },
            },
        }
        (tmp_path / "config.yaml").write_text(yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8")

    _patch_orchestrator_plugin_update_test_runtime(monkeypatch)
    write_config("coder")
    with capture_logs() as startup_logs:
        orchestrator = await initialize_from_config_file(orchestrator_runtime_paths(tmp_path))

    write_config("builder")
    with capture_logs() as reload_logs:
        updated = await orchestrator.config_reload._update_config()

    assert updated is True
    assert [log["agent"] for log in startup_logs if log["event"] == warning] == ["coder"]
    assert [log["agent"] for log in reload_logs if log["event"] == warning] == ["builder"]


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
            models={"default": {"provider": "openai", "id": "test-model"}},
            plugins=["./plugins/demo"],
        ),
        tmp_path,
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": {"display_name": "GeneralAgent", "model": "default"}},
            models={"default": {"provider": "openai", "id": "test-model"}},
            plugins=["./plugins/demo", "./plugins/other"],
        ),
        tmp_path,
    )

    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(current_config), config=current_config)
    orchestrator.running = True

    plan = ConfigUpdatePlan(new_config=new_config)
    reload_task: asyncio.Task[PluginReloadResult] | None = None
    try:
        await orchestrator.reload_plugins_now(source="initial")
        assert cached_plugin_module(hooks_path.resolve()).VALUE == 1

        async def start_blocked_reload(*_args: object, **_kwargs: object) -> set[str]:
            nonlocal reload_task
            hooks_path.write_text("VALUE = 2\n", encoding="utf-8")
            reload_task = asyncio.create_task(orchestrator.reload_plugins_now(source="interleaved"))
            await asyncio.sleep(0)
            assert reload_task is not None
            assert not reload_task.done()
            return set()

        with (
            patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(new_config)),
            patch(
                "mindroom.orchestration.config_lifecycle.build_config_update_plan",
                side_effect=lambda **kwargs: replace(plan, plugin_changes=kwargs["plugin_changes"]),
            ),
            # Removal runs after the changed plugins are staged and before they are committed.
            patch.object(orchestrator.fleet, "remove", new=AsyncMock(side_effect=start_blocked_reload)),
            patch.object(orchestrator, "_sync_mcp_manager", new=AsyncMock(return_value=set())),
            patch.object(orchestrator, "_sync_runtime_support_services", new=AsyncMock()),
        ):
            updated = await orchestrator.config_reload._update_config()

        assert updated is False
        assert reload_task is not None
        result = await reload_task

        assert result.active_plugin_names == ("demo", "other")
        assert cached_plugin_module(hooks_path.resolve()).VALUE == 2
    finally:
        if reload_task is not None:
            await asyncio.gather(reload_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_config_update_serializes_manual_plugin_reload_and_mcp_catalog_change(
    tmp_path: Path,
) -> None:
    """Manual plugin reloads and MCP restarts must wait for config application."""
    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1", tools=["mcp_demo"])},
            mcp_servers={"demo": {"transport": "stdio", "command": "npx"}},
        ),
        tmp_path,
    )
    orchestrator = _MultiAgentOrchestrator(runtime_paths_for(config), config=config)
    orchestrator.running = True
    # The catalog change restarts only dependents that have a bot.
    install_bots(orchestrator, {"agent1": MagicMock(spec=AgentBot, running=True)})
    reloaded_config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1", tools=["mcp_demo"])},
            mcp_servers={"demo": {"transport": "stdio", "command": "npx"}},
            defaults={"enable_streaming": False},
        ),
        tmp_path,
    )
    apply_started = asyncio.Event()
    finish_apply = asyncio.Event()

    async def apply_update_plan(_current_config: Config, _plan: ConfigUpdatePlan) -> bool:
        apply_started.set()
        await finish_apply.wait()
        return False

    staged = PluginGeneration()
    with (
        patch.object(orchestrator.config_reload, "load_config", return_value=published_config_load(reloaded_config)),
        patch.object(orchestrator.config_reload, "apply_update_plan", new=apply_update_plan),
        patch("mindroom.orchestrator.prepare_plugins", return_value=staged) as prepare_plugins_mock,
    ):
        config_task = asyncio.create_task(orchestrator.config_reload._update_config())
        await asyncio.wait_for(apply_started.wait(), timeout=1)
        plugin_task = asyncio.create_task(orchestrator.reload_plugins_now(source="test"))
        mcp_task = asyncio.create_task(orchestrator._handle_mcp_catalog_change("demo"))
        await asyncio.sleep(0)

        assert not plugin_task.done()
        assert not mcp_task.done()
        prepare_plugins_mock.assert_not_called()

        # The authoritative under-lock recheck must observe the config current
        # when the queued MCP replacement finally applies.
        replacement_config = _runtime_bound_config(Config(), tmp_path)
        orchestrator.config = replacement_config
        finish_apply.set()
        assert await config_task is False
        assert (await plugin_task).active_plugin_names == ()
        await mcp_task

    prepare_plugins_mock.assert_called_once_with(replacement_config, orchestrator.runtime_paths)


def _reply_request(agent_name: str) -> ResponseRequest:
    """Return one request for ``agent_name`` to answer ``$reply`` in ``!room:localhost``."""
    return ResponseRequest(
        sources=ResponseSources(
            pending_event_ids=("$reply",),
            logical_source_event_ids=("$reply",),
        ),
        thread_history=(),
        prompt="Hello",
        response_envelope=request_envelope(
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            agent_name=agent_name,
        ),
    )


@pytest.mark.asyncio
async def test_queued_config_reload_waits_for_in_flight_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """A queued reload applies its update plan only after the in-flight response finishes."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)

    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    bot = make_test_agent_bot(
        agent_user=mock_agent_users["agent1"],
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    response_started = asyncio.Event()
    release_response = asyncio.Event()

    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = _reply_request(bot.agent_name)

    async def run_response(_target: MessageTarget) -> SourceFate:
        response_started.set()
        await release_response.wait()
        return Owned()

    # The bot's runtime owns the one shared gate that every managed bot admits through and a reload drains.
    orchestrator = bot.orchestrator
    reloaded_config = _runtime_bound_config(
        config.model_copy(update={"defaults": config.defaults.model_copy(update={"enable_streaming": False})}),
        tmp_path,
    )
    load_config_mock = MagicMock(return_value=published_config_load(reloaded_config))
    monkeypatch.setattr(orchestrator.config_reload, "load_config", load_config_mock)
    orchestrator.config_reload.apply_update_plan = AsyncMock(return_value=True)

    response_task = asyncio.create_task(
        runner._run_locked_response_lifecycle(
            request,
            response_kind="ai",
            locked_operation=run_response,
        ),
    )

    try:
        await asyncio.wait_for(response_started.wait(), timeout=1)
        assert bot.in_flight_response_count == 1

        orchestrator.config_reload.request_reload()
        task = orchestrator.config_reload._reload_task
        assert task is not None

        await asyncio.sleep(0.05)
        load_config_mock.assert_called_once()
        orchestrator.config_reload.apply_update_plan.assert_not_awaited()

        release_response.set()
        await asyncio.wait_for(response_task, timeout=1)
        await asyncio.wait_for(task, timeout=1)

        orchestrator.config_reload.apply_update_plan.assert_awaited_once()
    finally:
        release_response.set()
        await asyncio.gather(response_task, return_exceptions=True)
        await orchestrator.config_reload.cancel()


def _entity_edit_config(change: Callable[[dict[str, Any]], object] | None = None) -> Config:
    data: dict[str, Any] = {
        "agents": {
            "helper": {"display_name": "Helper", "tools": [{"shell": {"enable_run_shell_command": False}}]},
            "other": {"display_name": "Other"},
        },
        "teams": {"crew": {"display_name": "Crew", "role": "Coordinate", "agents": ["helper"]}},
        "models": {
            "default": {"provider": "openai", "id": "gpt-6.1-sol"},
            "fast": {"provider": "openai", "id": "gpt-6-luna"},
        },
        "mcp_servers": {"docs": {"transport": "stdio", "command": "npx"}},
    }
    if change is not None:
        change(data)
    return _runtime_bound_config(Config.model_validate(data))


def _entity_edit_plan(
    change: Callable[[dict[str, Any]], object],
    *,
    stopped: AbstractSet[str] = frozenset(),
) -> ConfigUpdatePlan:
    entities = {ROUTER_AGENT_NAME, "helper", "other", "crew"}
    new_config = _entity_edit_config(change)
    return build_config_update_plan(
        current_config=_entity_edit_config(),
        new_config=new_config,
        bots={entity: AsyncMock(running=entity not in stopped) for entity in entities},
    )


def test_every_agent_and_team_field_is_classified_for_config_reload() -> None:
    """A new entity field must be classified, so it cannot silently skip a restart its bot needs."""
    assert set(AgentConfig.model_fields) >= _AGENT_LIVE_FIELDS
    assert set(AgentConfig.model_fields) - _AGENT_LIVE_FIELDS == {"display_name", "accept_invites", "private"}
    assert set(TeamConfig.model_fields) >= _TEAM_LIVE_FIELDS
    assert set(TeamConfig.model_fields) - _TEAM_LIVE_FIELDS == {"display_name", "accept_invites"}


def _update_helper(**fields: object) -> Callable[[dict[str, Any]], object]:
    return lambda data: data["agents"]["helper"].update(fields)


@pytest.mark.parametrize(
    ("change", "entity"),
    [
        pytest.param(_update_helper(instructions=["Be brief."]), "helper", id="instructions"),
        pytest.param(_update_helper(model="fast"), "helper", id="model"),
        pytest.param(_update_helper(tools=["shell", "calculator"]), "helper", id="tools"),
        pytest.param(
            _update_helper(tools=[{"shell": {"enable_run_shell_command": True}}]),
            "helper",
            id="tool-override",
        ),
        pytest.param(_update_helper(participation={"decline_reaction": "👍"}), "helper", id="participation"),
        pytest.param(_update_helper(mid_turn={"judgment": {"provider": "typesafe"}}), "helper", id="mid-turn"),
        pytest.param(_update_helper(contacts={"rules": ["Be brief."]}), "helper", id="contacts"),
        pytest.param(_update_helper(cheap_model="fast"), "helper", id="cheap-model"),
        pytest.param(_update_helper(cheap_model="fast", cold_cache={}), "helper", id="cold-cache"),
        pytest.param(_update_helper(access={"users": ["@alice:localhost"]}), "helper", id="access"),
        pytest.param(lambda data: data["teams"]["crew"].update(mode="collaborate"), "crew", id="team-mode"),
        pytest.param(lambda data: data["teams"]["crew"].update(agents=["helper", "other"]), "crew", id="team-members"),
    ],
)
def test_config_update_plan_applies_live_entity_edits_without_restarting(
    change: Callable[[dict[str, Any]], object],
    entity: str,
) -> None:
    """Edits to fields running bots read live reach them through the unchanged-bot path."""
    plan = _entity_edit_plan(change)

    assert plan.restart == set()
    assert plan.live_updated == {entity}


@pytest.mark.parametrize(
    ("change", "restarted"),
    [
        pytest.param(_update_helper(display_name="Helpful"), {"helper"}, id="display-name"),
        pytest.param(_update_helper(accept_invites=False), {"helper"}, id="accept-invites"),
        pytest.param(lambda data: data["agents"]["other"].update(private={"per": "user"}), {"other"}, id="private"),
        pytest.param(_update_helper(tools=["shell", "desktop"]), {"helper"}, id="desktop"),
        pytest.param(_update_helper(tools=["shell", "mcp_docs"]), {"helper"}, id="mcp-tool"),
        pytest.param(
            lambda data: data.update(defaults={"tools": ["scheduler", "desktop"]}),
            {"helper", "other"},
            id="inherited-desktop",
        ),
        pytest.param(lambda data: data["teams"]["crew"].update(display_name="Crew Two"), {"crew"}, id="team-name"),
        pytest.param(lambda data: data["teams"]["crew"].update(accept_invites=False), {"crew"}, id="team-invites"),
    ],
)
def test_config_update_plan_restarts_only_entities_whose_startup_inputs_changed(
    change: Callable[[dict[str, Any]], object],
    restarted: set[str],
) -> None:
    """Fields a bot reads only when it starts restart that bot and no other."""
    plan = _entity_edit_plan(change)

    assert plan.restart == restarted
    assert plan.live_updated == set()


def _remove_other_and_crew(data: dict[str, Any]) -> None:
    del data["agents"]["other"]
    del data["teams"]["crew"]


def _remove_helper_and_change_docs_server(data: dict[str, Any]) -> None:
    del data["agents"]["helper"]
    data["teams"]["crew"]["agents"] = ["other"]
    data["mcp_servers"]["docs"]["command"] = "uvx"


def _helper_uses_docs(data: dict[str, Any]) -> None:
    data["agents"]["helper"]["tools"] = ["mcp_docs"]


@pytest.mark.parametrize(
    ("change", "removed"),
    [
        pytest.param(_remove_other_and_crew, {"other", "crew"}, id="plain-removal"),
        pytest.param(_remove_helper_and_change_docs_server, {"helper"}, id="removed-mcp-dependent"),
    ],
)
def test_config_update_plan_removes_deleted_entities_without_restarting_them(
    change: Callable[[dict[str, Any]], object],
    removed: set[str],
) -> None:
    """A deleted entity is only removed, so its removal path still finds its bot."""
    entities = {ROUTER_AGENT_NAME, "helper", "other", "crew"}
    current_config = _entity_edit_config(_helper_uses_docs)
    new_config = _entity_edit_config(change)
    plan = build_config_update_plan(
        current_config=current_config,
        new_config=new_config,
        bots={entity: AsyncMock(running=True) for entity in entities},
        plugin_changes=("plugins/changed",),
    )

    assert plan.remove == removed
    assert not plan.restart & removed


def _crew_becomes_agent(data: dict[str, Any]) -> None:
    del data["teams"]["crew"]
    data["agents"]["crew"] = {"display_name": "Crew"}


def _other_becomes_team(data: dict[str, Any]) -> None:
    del data["agents"]["other"]
    data["teams"]["other"] = {"display_name": "Other", "role": "Coordinate", "agents": ["helper"]}


@pytest.mark.parametrize(
    ("change", "entity"),
    [
        pytest.param(_crew_becomes_agent, "crew", id="team-to-agent"),
        pytest.param(_other_becomes_team, "other", id="agent-to-team"),
    ],
)
def test_config_update_plan_rebuilds_an_entity_that_changes_kind_under_the_same_name(
    change: Callable[[dict[str, Any]], object],
    entity: str,
) -> None:
    """The kept account needs the other kind of bot, so the entity restarts instead of keeping its old bot."""
    plan = _entity_edit_plan(change)

    assert entity in plan.restart
    assert entity not in plan.start | plan.remove


@pytest.mark.parametrize(
    ("section", "entity"),
    [pytest.param("teams", "crew", id="team"), pytest.param("agents", "other", id="agent")],
)
def test_config_update_plan_rebuilds_a_bot_left_over_from_a_failed_removal(section: str, entity: str) -> None:
    """Restoring an entity whose removal failed part way rebuilds its half-stopped bot instead of keeping it."""
    removal_config = _entity_edit_config(lambda data: data[section].pop(entity))
    plan = build_config_update_plan(
        current_config=removal_config,
        new_config=_entity_edit_config(),
        bots={name: AsyncMock(running=True) for name in (ROUTER_AGENT_NAME, "helper", "other", "crew")},
    )

    assert entity in plan.restart
    assert entity not in plan.start | plan.remove


@pytest.mark.parametrize(
    ("change", "entity"),
    [
        pytest.param(_update_helper(instructions=["Be brief."]), "helper", id="agent"),
        pytest.param(lambda data: data["teams"]["crew"].update(mode="collaborate"), "crew", id="team"),
    ],
)
def test_config_update_plan_restarts_a_stopped_bot_for_any_edit(
    change: Callable[[dict[str, Any]], object],
    entity: str,
) -> None:
    """An edit to a bot whose startup failed retries that startup, even for a field read live."""
    plan = _entity_edit_plan(change, stopped={entity})

    assert plan.restart == {entity}
    assert plan.live_updated == set()


@pytest.mark.parametrize("prompt_name", ["HIDDEN_TOOL_CALLS_PROMPT", "DELEGATE_BACKGROUND_JOB_INSTRUCTIONS"])
def test_config_update_plan_restarts_running_entities_when_construction_prompts_change(prompt_name: str) -> None:
    """Construction-time root prompt overrides should restart running agents, teams, and router."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Collaborate",
                    agents=["general"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Collaborate",
                    agents=["general"],
                ),
            },
            router=RouterConfig(model="default"),
            prompts={prompt_name: "Custom construction-time prompt."},
        ),
    )

    running_entities = {ROUTER_AGENT_NAME, "general", "team1"}
    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots={entity: AsyncMock() for entity in running_entities},
    )

    assert plan.restart == running_entities


def test_config_update_plan_does_not_restart_for_request_time_prompt_change() -> None:
    """Request-time prompt overrides should not tear down running entities."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Collaborate",
                    agents=["general"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Collaborate",
                    agents=["general"],
                ),
            },
            router=RouterConfig(model="default"),
            prompts={"ROUTER_AGENT_SELECTION_PROMPT_TEMPLATE": "Pick one agent: {agents_info}\n{message}"},
        ),
    )

    running_entities = {ROUTER_AGENT_NAME, "general", "team1"}
    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots={entity: AsyncMock() for entity in running_entities},
    )

    assert plan.restart == set()


def test_config_update_plan_reconciles_room_metadata_without_restarting_bots() -> None:
    """Room display-name edits should reconcile Matrix room state without bot restarts."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", rooms=["lobby"])},
            rooms={"lobby": RoomConfig(display_name="Lobby")},
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", rooms=["lobby"])},
            rooms={"lobby": RoomConfig(display_name="Project Lobby")},
            router=RouterConfig(model="default"),
        ),
    )

    running_entities = {ROUTER_AGENT_NAME, "general"}
    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots={entity: AsyncMock() for entity in running_entities},
    )

    assert plan.restart == set()
    assert plan.room_access_changed is True


@pytest.mark.parametrize(
    ("changes", "requires_drain"),
    [
        pytest.param({"defaults": {"enable_streaming": False}}, False, id="defaults"),
        pytest.param({"router": {"model": "fast"}}, False, id="router-model"),
        pytest.param({"matrix_space": {"name": "Team Space"}}, False, id="space-name"),
        pytest.param({"agents": {"general": {"display_name": "General", "role": "New"}}}, False, id="agent-live"),
        pytest.param({"agents": {"general": {"display_name": "General Agent"}}}, True, id="agent-restart"),
        pytest.param(
            {"agents": {"general": {"display_name": "General", "access": {"users": ["@alice:localhost"]}}}},
            True,
            id="agent-access",
        ),
        pytest.param({"administrators": ["@admin:localhost"]}, True, id="administrators"),
        pytest.param({"authorization": {"aliases": {"@alice:localhost": ["@tg_1:localhost"]}}}, True, id="aliases"),
        pytest.param({"bot_accounts": ["@bridge:localhost"]}, True, id="bot-accounts"),
        pytest.param({"room_defaults": {"invite_users": ["@alice:localhost"]}}, True, id="room-defaults"),
        pytest.param({"router": {"access": {"users": ["@alice:localhost"]}}}, True, id="router-access"),
        pytest.param({"router": {"accept_invites": False}}, True, id="router-invites"),
        pytest.param({"external_trigger_policy": {"enabled": False}}, True, id="trigger-policy"),
    ],
)
def test_config_update_plan_drains_responses_only_for_restarts_or_reply_authorization(
    changes: dict[str, object],
    requires_drain: bool,
) -> None:
    """Edits that restart nothing and leave reply authorization alone may publish during replies."""
    base = {
        "agents": {"general": {"display_name": "General"}},
        "models": {
            "default": {"provider": "openai", "id": "gpt-6.1-sol"},
            "fast": {"provider": "openai", "id": "gpt-6-luna"},
        },
    }
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=_runtime_bound_config(Config.model_validate(base)),
        new_config=_runtime_bound_config(Config.model_validate({**base, **changes})),
        bots={entity: AsyncMock() for entity in running_entities},
    )

    assert plan.requires_response_drain is requires_drain


def test_config_update_plan_restarts_agents_when_tool_output_threshold_changes() -> None:
    """The tool output auto-save threshold is captured when agent and team toolkits are built."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Collaborate",
                    agents=["general"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            teams={
                "team1": TeamConfig(
                    display_name="Team 1",
                    role="Collaborate",
                    agents=["general"],
                ),
            },
            router=RouterConfig(model="default"),
            defaults={"tool_output_auto_save_threshold_bytes": 64 * 1024},
        ),
    )

    running_entities = {ROUTER_AGENT_NAME, "general", "team1"}
    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots={entity: AsyncMock() for entity in running_entities},
    )

    assert plan.restart == {"general", "team1"}


def test_config_update_plan_restarts_old_and_new_call_agents_when_calls_change() -> None:
    """A changed call model or agent selection rebuilds every affected call manager."""
    old_config = _runtime_bound_config(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent"),
                "writer": AgentConfig(display_name="Writer Agent"),
            },
            calls=_calls_for("general", voice="marin"),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent"),
                "writer": AgentConfig(display_name="Writer Agent"),
            },
            calls=_calls_for("writer", voice="cedar"),
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general", "writer"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities),
    )

    assert plan.restart == {"general", "writer"}


def _two_call_agents_config(change: Callable[[dict[str, Any]], object] | None = None) -> Config:
    speech_service = {"provider": "openai_compatible", "model": "local-speech", "host": "http://127.0.0.1:9000"}
    realtime_profile = {
        "backend": "realtime",
        "model": "gpt-realtime",
        "credentials_service": "openai",
        "voice": "marin",
    }
    data: dict[str, Any] = {
        "agents": {
            "general": {"display_name": "General Agent"},
            "writer": {"display_name": "Writer Agent"},
            "helper": {"display_name": "Helper Agent", "instructions": ["Be brief."]},
        },
        "models": {
            "default": {"provider": "openai", "id": "default-model"},
            "call": {"provider": "openai", "id": "call-model"},
        },
        "calls": {
            "enabled": True,
            "profiles": {
                "realtime": realtime_profile,
                "realtime-copy": dict(realtime_profile),
                "cascaded": {"backend": "cascaded", "model": "call", "stt": speech_service, "tts": speech_service},
            },
            "agents": {"general": "realtime", "writer": "cascaded"},
        },
        "router": {"model": "default"},
    }
    if change is not None:
        change(data)
    return _runtime_bound_config(Config.model_validate(data))


@pytest.mark.parametrize(
    ("change", "expected_call_restarts"),
    [
        pytest.param(lambda d: d["agents"]["helper"].update(instructions=["Be thorough."]), set(), id="other-agent"),
        pytest.param(lambda d: d["router"].update(model="call"), set(), id="router-model"),
        pytest.param(lambda d: d.update(administrators=["@admin:example.org"]), set(), id="administrators"),
        pytest.param(
            lambda d: d.update(tool_approval={"rules": [{"match": "read_file", "action": "require_approval"}]}),
            set(),
            id="tool-approval",
        ),
        pytest.param(lambda d: d["models"]["default"].update(id="new-default"), set(), id="agent-model-definition"),
        pytest.param(lambda d: d["calls"]["profiles"]["realtime"].update(voice="cedar"), {"general"}, id="voice"),
        pytest.param(
            lambda d: d["calls"]["profiles"]["realtime"].update(model="gpt-realtime-new"),
            {"general"},
            id="realtime-model",
        ),
        pytest.param(lambda d: d["models"]["call"].update(id="new-call-model"), {"writer"}, id="profile-model"),
        pytest.param(lambda d: d["calls"]["agents"].update(general="cascaded"), {"general"}, id="profile-assignment"),
        pytest.param(
            lambda d: d["calls"]["agents"].update(general="realtime-copy"),
            {"general"},
            id="equal-profile-assignment",
        ),
        pytest.param(
            lambda d: d["calls"].update(livekit_service_url="https://livekit.example.org"),
            {"general", "writer"},
            id="livekit-service-url",
        ),
    ],
)
@pytest.mark.parametrize("in_call", [frozenset(), frozenset({"general"})], ids=["idle", "general-in-call"])
def test_config_update_plan_restarts_call_agents_for_their_call_setup_or_a_call_in_progress(
    change: Callable[[dict[str, Any]], object],
    expected_call_restarts: set[str],
    in_call: frozenset[str],
) -> None:
    """Idle call agents restart only for their own call setup; an agent in a call restarts for any change."""
    running_entities = {ROUTER_AGENT_NAME, "general", "writer", "helper"}

    plan = build_config_update_plan(
        current_config=_two_call_agents_config(),
        new_config=_two_call_agents_config(change),
        bots=_call_bots(running_entities, in_call=in_call),
    )

    assert plan.restart & {"general", "writer"} == expected_call_restarts | in_call


def test_a_knowledge_graph_change_leaves_an_agent_in_a_call_running() -> None:
    """Starting the graphs over does not end a call in progress, which keeps the graphs it joined with."""
    running_entities = {ROUTER_AGENT_NAME, "general", "writer", "helper"}

    plan = build_config_update_plan(
        current_config=_two_call_agents_config(),
        new_config=_two_call_agents_config(lambda d: d.update(knowledge_graph={"generation": 1})),
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == set()


def test_config_update_plan_restarts_call_agents_when_administrators_change() -> None:
    """Active call tool contexts rebuild so administrator changes cannot leave stale policy."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            administrators=["@allowed:example.org"],
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            administrators=["@replacement:example.org"],
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}


def test_config_update_plan_restarts_call_agents_for_captured_policy_changes() -> None:
    """Active call tool closures are rebuilt when their approval policy changes."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            tool_approval={"rules": [{"match": "read_file", "action": "require_approval"}]},
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}


def test_config_update_plan_restarts_call_agent_when_inherited_tools_change() -> None:
    """An active call rebuilds when its effective default-provided tool surface changes."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            defaults={"tools": ["shell"]},
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            defaults={"tools": []},
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}


def test_config_update_plan_restarts_call_agent_when_worker_routing_changes() -> None:
    """An active call rebuilds when inherited worker routing changes for its tools."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            defaults={"tools": ["shell"], "worker_tools": ["shell"]},
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            defaults={"tools": ["shell"], "worker_tools": []},
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}


@pytest.mark.parametrize("backend", ["cascaded", "live"])
def test_config_update_plan_restarts_delegated_call_agent_when_referenced_model_changes(backend: str) -> None:
    """A delegated call agent rebuilds when its named model definition changes."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "call": ModelConfig(provider="openai", id="old-model"),
            },
            calls=_delegated_calls_for("general", model="call", backend=backend),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "call": ModelConfig(provider="openai", id="new-model"),
            },
            calls=_delegated_calls_for("general", model="call", backend=backend),
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities),
    )

    assert plan.restart == {"general"}


@pytest.mark.parametrize("backend", ["cascaded", "live"])
def test_config_update_plan_keeps_call_agents_running_when_only_model_prices_change(backend: str) -> None:
    """Budgets read prices at each refresh, so a price edit must not rebuild an idle call manager."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "call": ModelConfig(provider="openai", id="call-model", pricing=ModelPricing(input=5, output=30)),
            },
            calls=_delegated_calls_for("general", model="call", backend=backend),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            models={
                "default": ModelConfig(provider="openai", id="default-model"),
                "call": ModelConfig(provider="openai", id="call-model", pricing=ModelPricing(input=4, output=20)),
            },
            calls=_delegated_calls_for("general", model="call", backend=backend),
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities),
    )

    assert plan.restart == set()


def test_config_update_plan_restarts_realtime_call_agent_when_agent_model_changes() -> None:
    """An active realtime call rebuilds when the normal agent's model definition changes."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            models={"default": ModelConfig(provider="openai", id="old-model")},
            calls=_calls_for("general"),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            models={"default": ModelConfig(provider="openai", id="new-model")},
            calls=_calls_for("general"),
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}


@pytest.mark.parametrize("backend", ["cascaded", "live"])
def test_config_update_plan_restarts_implicit_delegated_call_agent_when_room_model_changes(backend: str) -> None:
    """An active call's implicit delegated model selection rebuilds when room routing changes."""
    models = {
        "default": ModelConfig(provider="openai", id="default-model"),
        "focused": ModelConfig(provider="openai", id="focused-model"),
        "fast": ModelConfig(provider="openai", id="fast-model"),
    }
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", rooms=["lobby"])},
            models=models,
            room_models={"lobby": "focused"},
            calls=_delegated_calls_for("general", model=None, backend=backend),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", rooms=["lobby"])},
            models=models,
            room_models={"lobby": "fast"},
            calls=_delegated_calls_for("general", model=None, backend=backend),
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}


@pytest.mark.parametrize(
    "encryption_config",
    [
        {"room_defaults": {"encrypted": True}},
        {"rooms": {"lobby": {"encrypted": True}}},
    ],
)
def test_config_update_plan_restarts_call_agents_when_room_encryption_policy_changes(
    encryption_config: dict[str, object],
) -> None:
    """Active call sessions must rebuild when their rooms switch to encrypted media."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", rooms=["lobby"])},
            calls=_calls_for("general"),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", rooms=["lobby"])},
            calls=_calls_for("general"),
            router=RouterConfig(model="default"),
            **encryption_config,
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}


def test_config_update_plan_restarts_call_agent_when_knowledge_definition_changes() -> None:
    """Active call tooling must not keep a captured stale knowledge definition."""
    common = {
        "agents": {
            "general": AgentConfig(
                display_name="General Agent",
                knowledge_bases=["docs"],
            ),
        },
        "calls": _calls_for("general"),
        "router": RouterConfig(model="default"),
    }
    old_config = _runtime_bound_config(
        Config(
            **common,
            knowledge_bases={"docs": KnowledgeBaseConfig(path="./old-docs")},
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            **common,
            knowledge_bases={"docs": KnowledgeBaseConfig(path="./new-docs")},
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}


def test_config_update_plan_restarts_call_agent_when_its_rooms_change() -> None:
    """Replacing a call agent in a call must terminate sessions in rooms it leaves."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", rooms=["lobby"])},
            calls=_calls_for("general"),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent", rooms=["project"])},
            calls=_calls_for("general"),
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities, in_call={"general"}),
    )

    assert plan.restart == {"general"}
    assert plan.reconcile_rooms == {ROUTER_AGENT_NAME}


def test_config_update_plan_reconciles_agent_and_router_room_changes_without_restarts() -> None:
    """Room-list edits should update memberships without replacing Matrix clients."""
    old_config = _runtime_bound_config(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", rooms=["lobby"]),
                "writer": AgentConfig(display_name="Writer Agent", rooms=["writers"]),
            },
            teams={
                "editors": TeamConfig(
                    display_name="Editors",
                    role="Edit documents",
                    agents=["writer"],
                    rooms=["reviews"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent", rooms=["project"]),
                "writer": AgentConfig(display_name="Writer Agent", rooms=["writers"]),
            },
            teams={
                "editors": TeamConfig(
                    display_name="Editors",
                    role="Edit documents",
                    agents=["writer"],
                    rooms=["published"],
                ),
            },
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general", "writer", "editors"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots={entity: AsyncMock() for entity in running_entities},
    )

    assert plan.restart == set()
    assert plan.reconcile_rooms == {ROUTER_AGENT_NAME, "general", "editors"}


def test_config_update_plan_stops_call_agents_when_calls_are_disabled() -> None:
    """Disabling calls restarts former call agents so their managers shut down."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general"),
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            calls=_calls_for("general", enabled=False),
            router=RouterConfig(model="default"),
        ),
    )
    running_entities = {ROUTER_AGENT_NAME, "general"}

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots=_call_bots(running_entities),
    )

    assert plan.restart == {"general"}


def test_config_update_plan_starts_added_entities_without_restarting_them() -> None:
    """An added entity is started, not restarted, so account preparation and hooks see it as added."""
    old_config = _runtime_bound_config(
        Config(
            agents={"general": AgentConfig(display_name="General Agent")},
            router=RouterConfig(model="default"),
        ),
    )
    new_config = _runtime_bound_config(
        Config(
            agents={
                "general": AgentConfig(display_name="General Agent"),
                "writer": AgentConfig(display_name="Writer Agent"),
            },
            router=RouterConfig(model="default"),
        ),
    )

    plan = build_config_update_plan(
        current_config=old_config,
        new_config=new_config,
        bots={ROUTER_AGENT_NAME: AsyncMock(), "general": AsyncMock()},
    )

    assert plan.start == {"writer"}
    assert plan.restart == set()


@pytest.fixture
def mock_agent_users() -> dict[str, AgentMatrixUser]:
    """Create mock agent users."""
    return {
        name: AgentMatrixUser(
            agent_name=name,
            user_id=f"@mindroom_{name}:localhost",
            display_name=display_name,
            password=TEST_PASSWORD,
        )
        for name, display_name in (
            ("agent1", "Agent 1"),
            ("agent2", "Agent 2"),
        )
    }


@pytest.mark.asyncio
async def test_in_flight_response_count_nonzero_during_send_response(
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """in_flight_response_count must be >0 even while the visible send is still awaiting."""
    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    bot = make_test_agent_bot(
        agent_user=mock_agent_users["agent1"],
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()

    send_entered = asyncio.Event()
    release_send = asyncio.Event()

    async def slow_send(*_args: object, **_kwargs: object) -> str:
        send_entered.set()
        await release_send.wait()
        return "$msg"

    send_response = AsyncMock(side_effect=slow_send)
    install_send_response_mock(bot, send_response)

    runner = unwrap_extracted_collaborator(bot._response_runner)
    request = _reply_request(bot.agent_name)

    async def run_response(target: MessageTarget) -> SourceFate:
        await bot._delivery_gateway.send_text(
            SendTextRequest(target=target, response_text="the answer"),
        )
        return Owned()

    task = asyncio.create_task(
        runner._run_locked_response_lifecycle(
            request,
            response_kind="ai",
            locked_operation=run_response,
        ),
    )

    try:
        await asyncio.wait_for(send_entered.wait(), timeout=1)
        assert bot.in_flight_response_count == 1
    finally:
        release_send.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_in_flight_response_count_stays_per_entity_across_bots(
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """One bot's active response must not make a different bot look busy.

    Every bot shares one admission gate, whose count is process-wide. Callers
    like the todo-poke idle check ask whether *this* entity is busy, so the
    per-bot count must stay distinct from the gate total.
    """
    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1"), "agent2": AgentConfig(display_name="Agent 2")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    runtime = ready_runtime(config, runtime_paths_for(config))
    bots = {}
    for agent_name in ("agent1", "agent2"):
        bot = make_test_agent_bot(mock_agent_users[agent_name], orchestrator=runtime)
        bot.client = AsyncMock()
        bots[agent_name] = bot

    shared_gate = runtime.response_admission_gate

    busy, idle = bots["agent1"], bots["agent2"]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def blocked_response(_target: MessageTarget) -> SourceFate:
        entered.set()
        await release.wait()
        return Owned()

    busy_runner = unwrap_extracted_collaborator(busy._response_runner)
    task = asyncio.create_task(
        busy_runner._run_locked_response_lifecycle(
            _reply_request(busy.agent_name),
            response_kind="ai",
            locked_operation=blocked_response,
        ),
    )
    try:
        await asyncio.wait_for(entered.wait(), timeout=1)
        assert busy.in_flight_response_count == 1
        # The shared gate sees the response, so a reload correctly defers...
        assert shared_gate.in_flight_response_count == 1
        # ...but the unrelated bot is still idle.
        assert idle.in_flight_response_count == 0
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)

    assert busy.in_flight_response_count == 0
    assert shared_gate.in_flight_response_count == 0


@pytest.mark.asyncio
async def test_closed_admission_defers_response_until_gate_reopens(
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """A response arriving during config apply should run after the gate reopens."""
    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    bot = make_test_agent_bot(
        agent_user=mock_agent_users["agent1"],
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    runner = unwrap_extracted_collaborator(bot._response_runner)
    admission_gate = bot.orchestrator.response_admission_gate
    response_started = asyncio.Event()

    async def run_response(_target: MessageTarget) -> SourceFate:
        response_started.set()
        return Owned()

    assert admission_gate.close_if_idle()
    task = asyncio.create_task(
        runner._run_locked_response_lifecycle(
            _reply_request(bot.agent_name),
            response_kind="ai",
            locked_operation=run_response,
        ),
    )
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert not response_started.is_set()
        assert bot.in_flight_response_count == 0

        admission_gate.reopen()

        assert await asyncio.wait_for(task, timeout=1) == Owned()
        assert response_started.is_set()
        assert bot.in_flight_response_count == 0
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_replaced_runtime_refuses_deferred_response_without_matrix_io(
    tmp_path: Path,
    mock_agent_users: dict[str, AgentMatrixUser],
) -> None:
    """A replaced runtime should fail its waiter so checkpoint replay owns recovery."""
    config = _runtime_bound_config(
        Config(
            agents={"agent1": AgentConfig(display_name="Agent 1")},
            router=RouterConfig(model="default"),
        ),
        tmp_path,
    )
    bot = make_test_agent_bot(
        agent_user=mock_agent_users["agent1"],
        config=config,
        runtime_paths=runtime_paths_for(config),
    )
    bot.client = AsyncMock()
    send_response = AsyncMock(return_value="$thinking")
    install_send_response_mock(bot, send_response)
    admission_gate = bot.orchestrator.response_admission_gate
    refusal_logger = MagicMock()
    replace_response_runner_deps(bot, logger=refusal_logger)

    # Stand where a config apply stands: admission closed, plan in progress.
    assert admission_gate.close_if_idle()
    task = bot._response_runner.inbox.track_inbox_response(
        bot._response_runner.generate_response(
            _reply_request(bot.agent_name),
        ),
        name="test_reload_admission_race",
        recovery_proof_ready=lambda: False,
        room_id="!room:example.org",
    )
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert bot.in_flight_response_count == 0
        send_response.assert_not_awaited()

        # Replacement shutdown wakes pre-admission waiters without cancelling
        # them, so the callback failure poisons the old sync checkpoint.
        await bot.prepare_for_sync_shutdown(shutdown_intent=SYNC_RESTART_SHUTDOWN)

        # A refusal is an admission failure, not a cancellation: handlers that
        # treat CancelledError as teardown must not mistake it for one.
        assert task.done()
        assert not task.cancelled()
        assert isinstance(task.exception(), ResponseAdmissionRefusedError)
        assert str(task.exception()) == "Runtime replacement is restarting this entity"
        assert bot.in_flight_response_count == 0
        send_response.assert_not_awaited()
        assert any(
            call.args and call.args[0] == "response_deferred_during_replacement"
            for call in refusal_logger.info.call_args_list
        )
        assert any(
            call.args and call.args[0] == "response_refused_after_runtime_replacement"
            for call in refusal_logger.warning.call_args_list
        )
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_shutdown_during_active_drain_cancels_reload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Calling stop() during an active drain must cancel the reload without applying it."""
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._CONFIG_RELOAD_DEBOUNCE_SECONDS", 0.01)
    monkeypatch.setattr("mindroom.orchestration.config_lifecycle._REPLACEMENT_DRAIN_IDLE_POLL_SECONDS", 0.01)

    current_config = _runtime_bound_config(Config(), tmp_path)
    reloaded_config = _runtime_bound_config(Config(defaults={"enable_streaming": False}), tmp_path)
    orchestrator = _MultiAgentOrchestrator(runtime_paths=orchestrator_runtime_paths(tmp_path), config=current_config)
    orchestrator.running = True

    mock_bot = MagicMock(spec=AgentBot)
    mock_bot.pending_response_owner_count = 0
    mock_bot.pending_response_phase_counts = {}
    mock_bot.deferred_stop_phase = None
    mock_bot.deferred_stop_required = False
    mock_bot.stop = AsyncMock()
    orchestrator.fleet.add("agent1", mock_bot)
    # An admitted response that never finishes, so the drain never goes idle.
    assert orchestrator.config_reload.response_admission_gate.admit()
    load_config_mock = MagicMock(return_value=published_config_load(reloaded_config))
    monkeypatch.setattr(orchestrator.config_reload, "load_config", load_config_mock)
    orchestrator.config_reload.apply_update_plan = AsyncMock(return_value=True)
    orchestrator.config_reload.request_reload()
    task = orchestrator.config_reload._reload_task
    assert task is not None

    # Let the drain loop start polling
    await asyncio.sleep(0.05)
    load_config_mock.assert_called_once()
    orchestrator.config_reload.apply_update_plan.assert_not_awaited()

    # Shutdown
    await orchestrator.stop()

    # The reload task should have been cancelled
    assert task.done()
    orchestrator.config_reload.apply_update_plan.assert_not_awaited()
