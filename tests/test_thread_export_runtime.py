"""Thread export runtime coordinator tests."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from mindroom.config.agent import AgentConfig, AgentThreadExportConfig
from mindroom.config.main import Config
from mindroom.orchestration.thread_export_runtime import ThreadExportRuntimeCoordinator
from mindroom.thread_export.models import ThreadExportRoom
from mindroom.thread_export.storage import write_thread_payload
from mindroom.thread_export.workspace_sync import WorkspaceThreadExportRunner
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.asyncio


def _config(tmp_path: Path, *, enabled: bool) -> Config:
    return bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    thread_exports=AgentThreadExportConfig() if enabled else None,
                ),
            },
        ),
        test_runtime_paths(tmp_path),
    )


def _coordinator(config: Config | None, runtime_config: Config) -> ThreadExportRuntimeCoordinator:
    return ThreadExportRuntimeCoordinator(
        runtime_paths=runtime_paths_for(runtime_config),
        config_provider=lambda: config,
        bot_provider=lambda _name: None,
    )


async def _idle_until_stopped(self: WorkspaceThreadExportRunner) -> None:
    """Stand in for ``run``: honour ``stop`` without running any pass."""
    while not self._stopped:
        await self._wakeup.wait()
        self._wakeup.clear()


async def test_sync_starts_runner_and_queues_full_pass_when_an_agent_enables_exports(tmp_path: Path) -> None:
    """Sync starts runner and queues full pass when an agent enables exports."""
    config = _config(tmp_path, enabled=True)
    coordinator = _coordinator(config, config)

    with (
        patch.object(WorkspaceThreadExportRunner, "run", _idle_until_stopped),
        patch.object(WorkspaceThreadExportRunner, "queue_full_pass", autospec=True) as queue_full_pass,
    ):
        await coordinator.sync()
        await coordinator.sync()
        runner = coordinator._runner
        assert runner is not None
        assert coordinator._task is not None
        assert not coordinator._task.done()
        assert queue_full_pass.call_count == 2
        await coordinator.stop()

    assert coordinator._runner is None
    assert coordinator._task is None


async def test_sync_stops_runner_when_no_agent_enables_exports(tmp_path: Path) -> None:
    """Sync stops runner when no agent enables exports."""
    enabled = _config(tmp_path, enabled=True)
    disabled = _config(tmp_path, enabled=False)
    active = enabled
    coordinator = ThreadExportRuntimeCoordinator(
        runtime_paths=runtime_paths_for(enabled),
        config_provider=lambda: active,
        bot_provider=lambda _name: None,
    )

    with patch.object(WorkspaceThreadExportRunner, "run", _idle_until_stopped):
        await coordinator.sync()
        task = coordinator._task
        assert task is not None
        active = disabled
        await coordinator.sync()
        assert coordinator._runner is None
        assert task.done()


async def test_mark_room_activity_is_ignored_while_stopped(tmp_path: Path) -> None:
    """Mark room activity is ignored while stopped."""
    config = _config(tmp_path, enabled=True)
    coordinator = _coordinator(config, config)

    coordinator.mark_room_activity("!room:localhost")

    with (
        patch.object(WorkspaceThreadExportRunner, "run", _idle_until_stopped),
        patch.object(WorkspaceThreadExportRunner, "mark_room_activity", autospec=True) as mark,
    ):
        await coordinator.sync()
        coordinator.mark_room_activity("!room:localhost")
        await coordinator.stop()

    mark.assert_called_once()
    assert mark.call_args.args[1] == "!room:localhost"


async def test_sync_without_config_is_a_no_op(tmp_path: Path) -> None:
    """Sync without config is a no op."""
    coordinator = _coordinator(None, _config(tmp_path, enabled=True))

    await coordinator.sync()

    assert coordinator._runner is None


async def test_reconcile_queues_a_full_pass_only_while_running(tmp_path: Path) -> None:
    """Bots starting after ``sync`` still get their startup pass."""
    config = _config(tmp_path, enabled=True)
    coordinator = _coordinator(config, config)

    coordinator.reconcile()

    with (
        patch.object(WorkspaceThreadExportRunner, "run", _idle_until_stopped),
        patch.object(WorkspaceThreadExportRunner, "queue_full_pass", autospec=True) as queue_full_pass,
    ):
        await coordinator.sync()
        coordinator.reconcile()
        await coordinator.stop()

    assert queue_full_pass.call_count == 2


async def test_stop_cancels_a_pass_in_flight(tmp_path: Path) -> None:
    """Shutdown does not wait for a long export pass to finish."""
    config = _config(tmp_path, enabled=True)
    coordinator = _coordinator(config, config)

    async def _stuck(_self: WorkspaceThreadExportRunner) -> None:
        await asyncio.Event().wait()

    with patch.object(WorkspaceThreadExportRunner, "run", _stuck):
        await coordinator.sync()
        task = coordinator._task
        assert task is not None
        await asyncio.wait_for(coordinator.stop(), timeout=5)

    assert task.cancelled()


async def test_disabling_the_last_agent_clears_its_exports(tmp_path: Path) -> None:
    """Removing the final ``thread_exports`` still cleans up, even though no runner is left to do it."""
    enabled = _config(tmp_path, enabled=True)
    disabled = _config(tmp_path, enabled=False)
    export_dir = runtime_paths_for(enabled).storage_root / "agents" / "code" / "workspace" / "thread_exports"
    room = ThreadExportRoom(key="lobby", room_id="!lobby:localhost", alias="", name="Lobby")
    write_thread_payload(export_dir, room, "$thread:localhost", {"messages": []})
    (thread_file,) = export_dir.rglob("*.yaml")
    active = enabled
    coordinator = ThreadExportRuntimeCoordinator(
        runtime_paths=runtime_paths_for(enabled),
        config_provider=lambda: active,
        bot_provider=lambda _name: None,
    )

    with patch.object(WorkspaceThreadExportRunner, "run", _idle_until_stopped):
        await coordinator.sync()
        active = disabled
        await coordinator.sync()

    assert coordinator._runner is None
    assert not thread_file.exists()
