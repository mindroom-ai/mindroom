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
        scanned_room_ids: set[str],
    ) -> None:
        assert started_bots == bots
        assert recovery_config is config
        newly_joined_room_ids = joined_room_ids - scanned_room_ids
        scanned_room_ids.update(newly_joined_room_ids)
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

    controller.start(bots, config)
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

    async def recover_stale(_: list[object], __: object, ___: set[str]) -> None:
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

    controller.start([MagicMock()], MagicMock())
    await _wait_for_controller(controller)

    assert call_order == ["recover", "setup", "recover", "support", "approval_ready"]


@pytest.mark.asyncio
async def test_startup_maintenance_cancel_reports_unfinished_and_replays_with_running_bots() -> None:
    """Canceling unfinished maintenance reports replay and reuses fresh running bots."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def setup_rooms(_: list[object]) -> None:
        started.set()
        await release.wait()

    controller = StartupMaintenanceController(
        recover_stale_streams=AsyncMock(),
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )

    controller.start([MagicMock()], MagicMock())
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

    start.assert_called_once_with([running_bot], replay_config)
    release.set()


@pytest.mark.asyncio
async def test_startup_maintenance_cancel_completed_task_returns_false() -> None:
    """Canceling completed maintenance does not request replay."""
    controller = StartupMaintenanceController(
        recover_stale_streams=AsyncMock(),
        setup_rooms_and_memberships=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )

    controller.start([MagicMock()], MagicMock())
    await _wait_for_controller(controller)

    should_replay = await controller.cancel()

    assert should_replay is False
    with patch.object(controller, "start") as start:
        if should_replay:
            controller.restart_after_config_reload(config=MagicMock(), running_bots=lambda: [MagicMock()])
    start.assert_not_called()


def _counting_controller() -> tuple[StartupMaintenanceController, dict[str, int]]:
    counts = {"recover": 0, "setup": 0, "support": 0, "ready": 0}

    async def recover_stale(_: list[object], __: object, ___: set[str]) -> None:
        counts["recover"] += 1

    async def setup_rooms(_: list[object]) -> None:
        counts["setup"] += 1

    async def sync_runtime_support(_: object) -> None:
        counts["support"] += 1

    async def mark_runtime_support_ready() -> None:
        counts["ready"] += 1

    controller = StartupMaintenanceController(
        recover_stale_streams=recover_stale,
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )
    return controller, counts


@pytest.mark.asyncio
async def test_config_reload_replays_full_maintenance_sequence() -> None:
    """A reload that cancels an unfinished run replays the whole sequence."""
    controller, counts = _counting_controller()
    release = asyncio.Event()
    setup_started = asyncio.Event()

    async def blocking_setup(_: list[object]) -> None:
        counts["setup"] += 1
        setup_started.set()
        await release.wait()

    controller.setup_rooms_and_memberships = blocking_setup

    controller.start([MagicMock()], MagicMock())
    await asyncio.wait_for(setup_started.wait(), timeout=5.0)
    assert await controller.cancel() is True

    release.set()

    async def instant_setup(_: list[object]) -> None:
        counts["setup"] += 1

    controller.setup_rooms_and_memberships = instant_setup
    controller.restart_after_config_reload(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts["support"] == 1
    assert counts["ready"] == 1
    assert controller.replay_pending is False


@pytest.mark.asyncio
async def test_empty_bot_reload_keeps_full_maintenance_debt_replayable() -> None:
    """A cancel during the main phases plus an empty-bot reload must not lose the sequence.

    A reload can cancel before any phase completes and then finish with zero
    running bots; the full-maintenance debt flag is the only record that
    initial recovery, room setup, and runtime support are still owed.
    """
    counts = {"recover": 0, "setup": 0, "support": 0, "ready": 0}
    setup_started = asyncio.Event()
    release_setup = asyncio.Event()

    async def recover_stale(_: list[object], __: object, ___: set[str]) -> None:
        counts["recover"] += 1

    async def setup_rooms(_: list[object]) -> None:
        counts["setup"] += 1
        setup_started.set()
        await release_setup.wait()

    async def sync_runtime_support(_: object) -> None:
        counts["support"] += 1

    async def mark_runtime_support_ready() -> None:
        counts["ready"] += 1

    controller = StartupMaintenanceController(
        recover_stale_streams=recover_stale,
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
    )

    controller.start([MagicMock()], MagicMock())
    await asyncio.wait_for(setup_started.wait(), timeout=5.0)
    assert await controller.cancel() is True

    controller.restart_after_config_reload(config=MagicMock(), running_bots=list)
    assert controller.task is None
    assert controller.replay_pending is True

    # Debt survives a later cancel with no live task.
    assert await controller.cancel() is True

    release_setup.set()
    controller.resume_pending_maintenance(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts["support"] == 1
    assert counts["ready"] == 1
    assert controller.replay_pending is False


@pytest.mark.asyncio
async def test_bot_start_recovery_resumes_stranded_full_maintenance() -> None:
    """Background bot-start recovery schedules stranded full-maintenance debt."""
    controller, counts = _counting_controller()
    release = asyncio.Event()
    setup_started = asyncio.Event()

    async def blocking_setup(_: list[object]) -> None:
        counts["setup"] += 1
        setup_started.set()
        await release.wait()

    controller.setup_rooms_and_memberships = blocking_setup
    controller.start([MagicMock()], MagicMock())
    await asyncio.wait_for(setup_started.wait(), timeout=5.0)
    assert await controller.cancel() is True

    controller.restart_after_config_reload(config=MagicMock(), running_bots=list)
    assert controller.task is None
    assert controller.replay_pending is True

    release.set()

    async def instant_setup(_: list[object]) -> None:
        counts["setup"] += 1

    controller.setup_rooms_and_memberships = instant_setup
    controller.resume_pending_maintenance(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts["support"] == 1
    assert controller.replay_pending is False

    # Without pending debt or with a live task, resume is a no-op.
    resumed_task = controller.task
    controller.resume_pending_maintenance(config=MagicMock(), running_bots=lambda: [MagicMock()])
    assert controller.task is resumed_task


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

    controller.start([MagicMock()], MagicMock())
    await _wait_for_controller(controller)

    mark_runtime_support_ready.assert_not_awaited()
