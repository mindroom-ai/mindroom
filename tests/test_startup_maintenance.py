"""Startup maintenance controller tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.matrix.stale_stream_cleanup import StaleStreamRecoveryState
from mindroom.startup_maintenance import StartupMaintenanceController


async def _wait_for_controller(controller: StartupMaintenanceController) -> None:
    task = controller.task
    assert task is not None
    await task


@pytest.mark.asyncio
async def test_startup_maintenance_scans_rooms_joined_during_concurrent_setup() -> None:
    """Maintenance should overlap setup with recovery and then scan newly joined rooms."""
    call_order: list[str] = []
    bots = [MagicMock()]
    config = MagicMock()
    joined_room_ids = {"!initial:example.com"}
    recovery_waves: list[set[str]] = []
    initial_rooms_discovered = asyncio.Event()
    room_setup_finished = asyncio.Event()

    async def recover_stale(
        started_bots: list[object],
        recovery_config: object,
        startup_cutoff_ms: int,
        recovery_state: StaleStreamRecoveryState,
    ) -> None:
        assert started_bots == bots
        assert recovery_config is config
        assert startup_cutoff_ms == 123456
        newly_joined_room_ids = joined_room_ids - recovery_state.scanned_room_ids
        recovery_state.scanned_room_ids.update(newly_joined_room_ids)
        recovery_waves.append(newly_joined_room_ids)
        call_order.append(f"recover-{len(recovery_waves)}")
        if len(recovery_waves) == 1:
            initial_rooms_discovered.set()
            await room_setup_finished.wait()

    async def setup_rooms(started_bots: list[object]) -> None:
        assert started_bots == bots
        await initial_rooms_discovered.wait()
        call_order.append("setup")
        joined_room_ids.add("!joined-during-setup:example.com")
        room_setup_finished.set()

    async def sync_runtime_support(sync_config: object) -> None:
        assert sync_config is config
        call_order.append("support")

    async def mark_runtime_support_ready() -> None:
        call_order.append("approval_ready")

    controller = StartupMaintenanceController(
        recover_stale_streams=recover_stale,
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start(bots, config, startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    assert recovery_waves == [
        {"!initial:example.com"},
        {"!joined-during-setup:example.com"},
    ]
    assert call_order == ["recover-1", "setup", "recover-2", "support", "approval_ready"]


@pytest.mark.asyncio
async def test_startup_maintenance_continues_after_failed_recovery_and_room_setup() -> None:
    """Later phases still run after stale recovery and room setup fail."""
    call_order: list[str] = []

    async def recover_stale(
        _: list[object],
        __: object,
        ___: int,
        ____: StaleStreamRecoveryState,
    ) -> None:
        call_order.append("recover")
        msg = "recovery failed"
        raise RuntimeError(msg)

    async def setup_rooms(_: list[object]) -> None:
        call_order.append("setup")
        msg = "room setup failed"
        raise RuntimeError(msg)

    async def sync_runtime_support(_: object) -> None:
        call_order.append("support")

    async def mark_runtime_support_ready() -> None:
        call_order.append("approval_ready")

    controller = StartupMaintenanceController(
        recover_stale_streams=recover_stale,
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    assert call_order == ["recover", "setup", "recover", "support", "approval_ready"]


@pytest.mark.asyncio
async def test_startup_maintenance_cancel_reports_unfinished_and_replays_with_running_bots() -> None:
    """Canceled replay keeps exact retry targets and reuses fresh running bots."""
    started = asyncio.Event()
    release = asyncio.Event()
    recover_stale = AsyncMock()

    async def setup_rooms(_: list[object]) -> None:
        started.set()
        await release.wait()

    controller = StartupMaintenanceController(
        recover_stale_streams=recover_stale,
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await asyncio.wait_for(started.wait(), timeout=1.0)
    recovery_state = controller.recovery_state
    recovery_state.retry_target_event_ids.add("$retry")

    should_replay = await controller.cancel()

    assert should_replay is True

    running_bot = MagicMock()

    def running_bots() -> list[object]:
        return [running_bot]

    release.set()
    replay_config = MagicMock()
    controller.restart_after_config_reload(
        config=replay_config,
        running_bots=running_bots,
    )
    await _wait_for_controller(controller)

    assert controller.recovery_state is recovery_state
    assert controller.recovery_state.retry_target_event_ids == {"$retry"}
    assert recover_stale.await_args_list[-1].args[:3] == ([running_bot], replay_config, 123456)


@pytest.mark.asyncio
async def test_ready_recovery_is_detached_and_serialized() -> None:
    """Ready callbacks enqueue recovery without overlapping maintenance sweeps."""
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    second_started = asyncio.Event()

    async def first_recovery() -> None:
        first_started.set()
        await release_first.wait()

    async def second_recovery() -> None:
        second_started.set()

    controller = StartupMaintenanceController(
        recover_stale_streams=AsyncMock(),
        setup_rooms_and_memberships=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )

    controller.schedule_ready_recovery(first_recovery)
    controller.schedule_ready_recovery(second_recovery)
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert not second_started.is_set()

    release_first.set()
    assert controller.task is not None
    await asyncio.wait_for(controller.task, timeout=1.0)

    assert second_started.is_set()


@pytest.mark.asyncio
async def test_cancel_stops_current_recovery_when_queued_recovery_has_not_started() -> None:
    """Canceling the queue must stop work hidden behind its newest task."""
    first_started = asyncio.Event()
    first_cancelled = asyncio.Event()
    release_first = asyncio.Event()

    async def first_recovery() -> None:
        first_started.set()
        try:
            await release_first.wait()
        finally:
            first_cancelled.set()

    controller = StartupMaintenanceController(
        recover_stale_streams=AsyncMock(),
        setup_rooms_and_memberships=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )

    controller.schedule_ready_recovery(first_recovery)
    first_task = controller.task
    assert first_task is not None
    await asyncio.wait_for(first_started.wait(), timeout=1.0)
    controller.schedule_ready_recovery(AsyncMock())

    try:
        should_replay = await controller.cancel()
        assert should_replay is True
        assert first_cancelled.is_set()
        assert first_task.done()
    finally:
        release_first.set()
        if not first_task.done():
            first_task.cancel()
        await asyncio.gather(first_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_startup_maintenance_cancel_completed_task_returns_false() -> None:
    """Canceling completed maintenance does not request replay."""
    controller = StartupMaintenanceController(
        recover_stale_streams=AsyncMock(),
        setup_rooms_and_memberships=AsyncMock(),
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
        recover_stale_streams=AsyncMock(),
        setup_rooms_and_memberships=AsyncMock(),
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    mark_runtime_support_ready.assert_not_awaited()
