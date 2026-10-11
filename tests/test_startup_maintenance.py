"""Startup maintenance controller tests."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from structlog.testing import capture_logs

from mindroom import startup_maintenance
from mindroom.startup_maintenance import StartupMaintenanceController, _freeze_startup_heap


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
async def test_startup_maintenance_cancel_reports_unfinished_and_replays_with_running_bots() -> None:
    """Canceling unfinished maintenance reports replay and reuses fresh running bots."""
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

    should_replay = await controller.cancel()

    assert should_replay is True

    running_bot = MagicMock()

    def running_bots() -> list[object]:
        return [running_bot]

    replay_config = MagicMock()
    with patch.object(controller, "start") as start:
        controller.restart_after_runtime_replacement(
            config=replay_config,
            running_bots=running_bots,
        )

    start.assert_called_once_with([running_bot], replay_config)
    release.set()


@pytest.mark.asyncio
async def test_startup_maintenance_cancel_completed_task_returns_false() -> None:
    """Canceling completed maintenance does not request replay."""
    controller = StartupMaintenanceController(
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
            controller.restart_after_runtime_replacement(config=MagicMock(), running_bots=lambda: [MagicMock()])
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


def test_freeze_startup_heap_collects_then_freezes_and_logs_the_frozen_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Garbage is collected before the survivors are frozen, and one record reports both."""
    gc_calls: list[str] = []
    fake_gc = SimpleNamespace(
        collect=lambda: gc_calls.append("collect"),
        freeze=lambda: gc_calls.append("freeze"),
        get_freeze_count=lambda: 747_738,
    )
    monkeypatch.setattr(startup_maintenance, "gc", fake_gc)

    with capture_logs() as logs:
        _freeze_startup_heap()

    assert gc_calls == ["collect", "freeze"]
    [record] = [entry for entry in logs if entry["event"] == "startup_heap_frozen"]
    assert record["log_level"] == "info"
    assert record["frozen_objects"] == 747_738
    assert isinstance(record["collect_ms"], float)
    assert record["collect_ms"] >= 0


@pytest.mark.asyncio
async def test_startup_maintenance_freezes_the_heap_once_after_both_phases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The freeze is the last step and still runs when a phase failed."""
    call_order: list[str] = []
    monkeypatch.setattr(startup_maintenance, "_freeze_startup_heap", lambda: call_order.append("freeze"))

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

    assert call_order == ["setup", "support", "approval_ready", "freeze"]


@pytest.mark.asyncio
async def test_canceled_startup_maintenance_does_not_freeze_the_heap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Interrupted maintenance leaves the freeze to its replay."""
    freezes: list[None] = []
    monkeypatch.setattr(startup_maintenance, "_freeze_startup_heap", lambda: freezes.append(None))
    started = asyncio.Event()

    async def setup_rooms(_: list[object]) -> None:
        started.set()
        await asyncio.Event().wait()

    controller = StartupMaintenanceController(
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
    )

    controller.start([MagicMock()], MagicMock())
    await asyncio.wait_for(started.wait(), timeout=1.0)

    assert await controller.cancel() is True
    assert freezes == []
