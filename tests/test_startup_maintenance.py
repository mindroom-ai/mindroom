"""Startup maintenance controller tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.startup_maintenance import StartupMaintenanceController


async def _wait_for_controller(controller: StartupMaintenanceController) -> None:
    task = controller.task
    assert task is not None
    await task


@pytest.mark.asyncio
async def test_startup_maintenance_runs_phases_in_order_and_passes_cutoff() -> None:
    """Maintenance phases run in order and pass cutoff into stale cleanup."""
    call_order: list[str] = []
    bots = [MagicMock()]
    config = MagicMock()
    interrupted_threads = [MagicMock()]

    async def setup_rooms(started_bots: list[object]) -> None:
        assert started_bots == bots
        call_order.append("setup")

    async def cleanup_stale(started_bots: list[object], cleanup_config: object, startup_cutoff_ms: int) -> list[object]:
        assert started_bots == bots
        assert cleanup_config is config
        assert startup_cutoff_ms == 123456
        call_order.append("cleanup")
        return interrupted_threads

    async def auto_resume(cleaned_threads: list[object], resume_config: object) -> None:
        assert cleaned_threads == interrupted_threads
        assert resume_config is config
        call_order.append("resume")

    async def sync_runtime_support(sync_config: object) -> None:
        assert sync_config is config
        call_order.append("support")

    async def mark_runtime_support_ready() -> None:
        call_order.append("approval_ready")

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=setup_rooms,
        cleanup_stale_streams=cleanup_stale,
        auto_resume=auto_resume,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start(bots, config, startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    assert call_order == ["setup", "cleanup", "resume", "support", "approval_ready"]


@pytest.mark.asyncio
async def test_startup_maintenance_continues_after_failed_room_setup() -> None:
    """Later phases still run after room setup fails."""
    call_order: list[str] = []

    async def setup_rooms(_: list[object]) -> None:
        call_order.append("setup")
        msg = "room setup failed"
        raise RuntimeError(msg)

    async def cleanup_stale(_: list[object], __: object, ___: int) -> list[object]:
        call_order.append("cleanup")
        return []

    async def auto_resume(_: list[object], __: object) -> None:
        call_order.append("resume")

    async def sync_runtime_support(_: object) -> None:
        call_order.append("support")

    async def mark_runtime_support_ready() -> None:
        call_order.append("approval_ready")

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=setup_rooms,
        cleanup_stale_streams=cleanup_stale,
        auto_resume=auto_resume,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    assert call_order == ["setup", "cleanup", "resume", "support", "approval_ready"]


@pytest.mark.asyncio
async def test_startup_maintenance_cancel_reports_unfinished_and_replays_with_running_bots() -> None:
    """Canceling unfinished maintenance reports replay and reuses fresh running bots."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def setup_rooms(_: list[object]) -> None:
        started.set()
        await release.wait()

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=setup_rooms,
        cleanup_stale_streams=AsyncMock(return_value=[]),
        auto_resume=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await asyncio.wait_for(started.wait(), timeout=1.0)

    should_replay = await controller.cancel()

    assert should_replay is True

    running_bot = MagicMock()

    def running_bots() -> list[object]:
        return [running_bot]

    replay_config = MagicMock()
    with patch.object(controller, "start") as start:
        controller.restart_after_config_reload(
            config=replay_config,
            running_bots=running_bots,
        )

    start.assert_called_once_with([running_bot], replay_config, startup_cutoff_ms=123456)
    release.set()


@pytest.mark.asyncio
async def test_startup_maintenance_cancel_completed_task_returns_false() -> None:
    """Canceling completed maintenance does not request replay."""
    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=AsyncMock(),
        cleanup_stale_streams=AsyncMock(return_value=[]),
        auto_resume=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    should_replay = await controller.cancel()

    assert should_replay is False
    with patch.object(controller, "start") as start:
        if should_replay:
            controller.restart_after_config_reload(config=MagicMock(), running_bots=lambda: [MagicMock()])
    start.assert_not_called()


@pytest.mark.asyncio
async def test_startup_maintenance_runtime_support_failure_skips_approval_ready_marker() -> None:
    """Runtime-support failure prevents approval cleanup ready marker."""
    mark_runtime_support_ready = AsyncMock()

    async def sync_runtime_support(_: object) -> None:
        msg = "support failed"
        raise RuntimeError(msg)

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=AsyncMock(),
        cleanup_stale_streams=AsyncMock(return_value=[]),
        auto_resume=AsyncMock(),
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    mark_runtime_support_ready.assert_not_awaited()
