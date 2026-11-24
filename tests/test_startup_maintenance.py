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
async def test_startup_maintenance_continues_after_failed_room_setup() -> None:
    """Runtime support still runs after room setup fails."""
    call_order: list[str] = []

    async def setup_rooms(_: list[object]) -> None:
        call_order.append("setup")
        msg = "room setup failed"
        raise RuntimeError(msg)

    async def sync_runtime_support(_: object) -> None:
        call_order.append("support")

    async def mark_runtime_support_ready() -> None:
        call_order.append("approval_ready")

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start([MagicMock()], MagicMock())
    await _wait_for_controller(controller)

    assert call_order == ["setup", "support", "approval_ready"]


@pytest.mark.asyncio
async def test_paused_startup_maintenance_replays_interrupted_work_with_running_bots() -> None:
    """A replacement that interrupts maintenance replays it with the bots running afterwards."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def setup_rooms(_: list[object]) -> None:
        started.set()
        await release.wait()

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )
    controller.start([MagicMock()], MagicMock())
    await asyncio.wait_for(started.wait(), timeout=1.0)
    running_bot = MagicMock()
    replay_config = MagicMock()

    with patch.object(controller, "start") as start:
        async with controller.paused(config_provider=lambda: replay_config, running_bots=lambda: [running_bot]):
            assert controller.task is None
            start.assert_not_called()

    start.assert_called_once_with([running_bot], replay_config)
    release.set()


@pytest.mark.asyncio
async def test_paused_startup_maintenance_does_not_replay_finished_work() -> None:
    """Pausing finished maintenance starts nothing afterwards."""
    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )
    controller.start([MagicMock()], MagicMock())
    await _wait_for_controller(controller)

    with patch.object(controller, "start") as start:
        async with controller.paused(config_provider=MagicMock, running_bots=lambda: [MagicMock()]):
            pass

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
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start([MagicMock()], MagicMock())
    await _wait_for_controller(controller)

    mark_runtime_support_ready.assert_not_awaited()
