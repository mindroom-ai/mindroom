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


@pytest.mark.asyncio
async def test_startup_maintenance_runs_orphaned_bot_cleanup_first_and_best_effort() -> None:
    """Orphaned-bot cleanup runs before room setup (so blocked retries never starve it) and fails best-effort."""
    call_order: list[str] = []
    setup_started = asyncio.Event()
    release_setup = asyncio.Event()

    async def setup_rooms(_: list[object]) -> None:
        call_order.append("setup")
        setup_started.set()
        await release_setup.wait()

    async def sync_runtime_support(_: object) -> None:
        call_order.append("support")

    async def mark_runtime_support_ready() -> None:
        call_order.append("approval_ready")

    async def cleanup_orphaned_bots(_: object) -> None:
        call_order.append("orphaned_bots")
        msg = "cleanup boom"
        raise RuntimeError(msg)

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
        cleanup_orphaned_bots=cleanup_orphaned_bots,
    )

    controller.start([MagicMock()], MagicMock())
    try:
        await asyncio.wait_for(setup_started.wait(), timeout=1.0)
        assert call_order == ["orphaned_bots", "setup"]
    finally:
        release_setup.set()
    await _wait_for_controller(controller)

    assert call_order == ["orphaned_bots", "setup", "support", "approval_ready"]


@pytest.mark.asyncio
async def test_paused_startup_maintenance_reports_interruption_during_orphaned_bot_cleanup() -> None:
    """Cancelling while orphaned-bot cleanup is in flight yields True and replays maintenance."""
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cleanup_orphaned_bots(_: object) -> None:
        cleanup_started.set()
        await release_cleanup.wait()

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
        cleanup_orphaned_bots=cleanup_orphaned_bots,
    )
    controller.start([MagicMock()], MagicMock())
    await asyncio.wait_for(cleanup_started.wait(), timeout=1.0)

    with patch.object(controller, "start") as start:
        async with controller.paused(config_provider=MagicMock, running_bots=lambda: [MagicMock()]) as interrupted:
            assert interrupted is True

    start.assert_called_once()
    release_cleanup.set()


@pytest.mark.asyncio
async def test_startup_maintenance_waits_for_first_syncs_before_cleanup_and_replays_on_pause() -> None:
    """First-sync gating forwards a shared deadline, runs before cleanup, and replays with a fresh deadline."""
    call_order: list[str] = []
    seen_deadlines: list[float | None] = []
    wait_started = asyncio.Event()
    release_wait = asyncio.Event()

    async def wait_for_first_syncs(_: list[object], deadline: float | None) -> None:
        seen_deadlines.append(deadline)
        call_order.append("wait_for_first_syncs")
        wait_started.set()
        await release_wait.wait()

    async def cleanup_orphaned_bots(_: object) -> None:
        call_order.append("orphaned_bots")

    async def setup_rooms(_: list[object]) -> None:
        call_order.append("setup")

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
        cleanup_orphaned_bots=cleanup_orphaned_bots,
        wait_for_first_syncs=wait_for_first_syncs,
    )
    controller.start([MagicMock()], MagicMock(), first_sync_deadline=123.0)
    await asyncio.wait_for(wait_started.wait(), timeout=1.0)
    assert call_order == ["wait_for_first_syncs"]
    assert seen_deadlines == [123.0]

    replay_bot = MagicMock()
    replay_config = MagicMock()
    with patch.object(controller, "start") as replay_start:
        async with controller.paused(
            config_provider=lambda: replay_config,
            running_bots=lambda: [replay_bot],
        ) as interrupted:
            assert interrupted is True

    replay_start.assert_called_once_with([replay_bot], replay_config)
    assert call_order == ["wait_for_first_syncs"]

    release_wait.set()
    controller.start([MagicMock()], MagicMock())
    await _wait_for_controller(controller)
    assert call_order == ["wait_for_first_syncs", "wait_for_first_syncs", "orphaned_bots", "setup"]
    assert seen_deadlines == [123.0, None]
