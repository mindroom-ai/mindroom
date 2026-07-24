"""Startup maintenance controller tests."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.startup_maintenance import StartupMaintenanceController


async def _wait_for_controller(controller: StartupMaintenanceController) -> None:
    task = controller.task
    assert task is not None
    await task


async def _wait_until_recheck_sleep(controller: StartupMaintenanceController, ready: asyncio.Event) -> None:
    # mark_runtime_support_ready runs without yielding to the loop, so once the
    # ready event resolves the controller task is already suspended in the
    # recency-guard sleep with the pending recheck recorded.
    await asyncio.wait_for(ready.wait(), timeout=5.0)
    assert controller.recheck_pending is True


def _counting_controller(delay: float) -> tuple[StartupMaintenanceController, dict[str, int], asyncio.Event]:
    counts = {"recover": 0, "setup": 0, "support": 0, "ready": 0}
    ready = asyncio.Event()

    async def recover_stale(_: list[object], __: object, ___: int, ____: set[str]) -> None:
        counts["recover"] += 1

    async def setup_rooms(_: list[object]) -> None:
        counts["setup"] += 1

    async def sync_runtime_support(_: object) -> None:
        counts["support"] += 1

    async def mark_runtime_support_ready() -> None:
        counts["ready"] += 1
        ready.set()

    controller = StartupMaintenanceController(
        recover_stale_streams=recover_stale,
        setup_rooms_and_memberships=setup_rooms,
        sync_runtime_support=sync_runtime_support,
        mark_runtime_support_ready=mark_runtime_support_ready,
        recency_recheck_delay_seconds=delay,
    )
    return controller, counts, ready


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
        scanned_room_ids: set[str],
    ) -> None:
        assert started_bots == bots
        assert recovery_config is config
        assert startup_cutoff_ms == 123456
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
        recency_recheck_delay_seconds=0.0,
    )

    controller.start(bots, config, startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    assert recovery_waves == [
        {"!initial:example.com"},
        {"!joined-during-setup:example.com"},
        {"!initial:example.com", "!joined-during-setup:example.com"},
    ]
    assert call_order == ["recover-1", "setup", "recover-2", "support", "approval_ready", "recover-3"]


@pytest.mark.asyncio
async def test_startup_maintenance_continues_after_failed_recovery_and_room_setup() -> None:
    """Later phases still run after stale recovery and room setup fail."""
    call_order: list[str] = []

    async def recover_stale(_: list[object], __: object, ___: int, ____: set[str]) -> None:
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
        recency_recheck_delay_seconds=0.0,
        recheck_max_attempts=1,
        recheck_retry_delay_seconds=0.0,
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    assert call_order == ["recover", "setup", "recover", "support", "approval_ready", "recover"]


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
        recency_recheck_delay_seconds=0.0,
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
        recover_stale_streams=AsyncMock(),
        setup_rooms_and_memberships=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
        recency_recheck_delay_seconds=0.0,
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
async def test_config_reload_during_recheck_sleep_replays_only_recheck() -> None:
    """A reload landing in the recency-guard sleep must not replay finished phases.

    Config reload re-syncs runtime support itself, so replaying the whole
    maintenance sequence to reach the one pending recheck is pure waste.
    """
    controller, counts, ready = _counting_controller(delay=60.0)

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_until_recheck_sleep(controller, ready)

    assert await controller.cancel() is True
    assert counts == {"recover": 2, "setup": 1, "support": 1, "ready": 1}

    controller.recency_recheck_delay_seconds = 0.0
    controller.restart_after_config_reload(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts == {"recover": 3, "setup": 1, "support": 1, "ready": 1}
    assert controller.recheck_pending is False


@pytest.mark.asyncio
async def test_repeated_reloads_during_recheck_sleep_keep_replaying_only_recheck() -> None:
    """Back-to-back reloads inside the recheck window stay recheck-only."""
    controller, counts, ready = _counting_controller(delay=60.0)

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_until_recheck_sleep(controller, ready)
    assert await controller.cancel() is True

    controller.restart_after_config_reload(config=MagicMock(), running_bots=lambda: [MagicMock()])
    assert await controller.cancel() is True
    assert counts == {"recover": 2, "setup": 1, "support": 1, "ready": 1}

    controller.recency_recheck_delay_seconds = 0.0
    controller.restart_after_config_reload(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts == {"recover": 3, "setup": 1, "support": 1, "ready": 1}
    assert controller.recheck_pending is False


@pytest.mark.asyncio
async def test_fresh_start_after_canceled_recheck_replays_every_phase() -> None:
    """A new startup generation runs full maintenance even with a recheck pending."""
    controller, counts, ready = _counting_controller(delay=60.0)

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_until_recheck_sleep(controller, ready)
    await controller.cancel()
    assert counts == {"recover": 2, "setup": 1, "support": 1, "ready": 1}

    controller.recency_recheck_delay_seconds = 0.0
    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=654321)
    assert controller.recheck_pending is False
    await _wait_for_controller(controller)

    assert counts == {"recover": 5, "setup": 2, "support": 2, "ready": 2}
    assert controller.recheck_pending is False


@pytest.mark.asyncio
async def test_empty_bot_reload_does_not_strand_pending_recheck() -> None:
    """A reload finishing with zero running bots must keep the recheck replayable.

    Previously the empty-bot early return left recheck_pending=True with no
    task, and the next cancel() returned False, so no later reload could ever
    schedule the outstanding recheck.
    """
    controller, counts, ready = _counting_controller(delay=60.0)

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_until_recheck_sleep(controller, ready)
    assert await controller.cancel() is True

    controller.restart_after_config_reload(config=MagicMock(), running_bots=list)
    assert controller.task is None
    assert controller.recheck_pending is True

    assert await controller.cancel() is True

    controller.recency_recheck_delay_seconds = 0.0
    controller.restart_after_config_reload(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts == {"recover": 3, "setup": 1, "support": 1, "ready": 1}
    assert controller.recheck_pending is False


@pytest.mark.asyncio
async def test_bot_start_recovery_resumes_stranded_recheck() -> None:
    """Background bot-start recovery schedules a stranded pending recheck."""
    controller, counts, ready = _counting_controller(delay=60.0)

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_until_recheck_sleep(controller, ready)
    assert await controller.cancel() is True
    controller.restart_after_config_reload(config=MagicMock(), running_bots=list)
    assert controller.task is None

    controller.recency_recheck_delay_seconds = 0.0
    controller.resume_pending_recheck(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts == {"recover": 3, "setup": 1, "support": 1, "ready": 1}
    assert controller.recheck_pending is False

    # Without pending work or with a live task, resume is a no-op.
    controller.resume_pending_recheck(config=MagicMock(), running_bots=lambda: [MagicMock()])
    assert controller.task is not None
    assert counts["recover"] == 3


def _recheck_failure_controller(
    counts: dict[str, int],
    failing_recheck_attempts: set[int],
    *,
    recheck_max_attempts: int,
) -> StartupMaintenanceController:
    """Build a controller whose recheck waves fail on the given attempt numbers."""

    async def recover_stale(_: list[object], __: object, ___: int, ____: set[str]) -> None:
        counts["recover"] += 1
        recheck_attempt = counts["recover"] - 2
        if recheck_attempt in failing_recheck_attempts:
            msg = "transient recheck failure"
            raise RuntimeError(msg)

    return StartupMaintenanceController(
        recover_stale_streams=recover_stale,
        setup_rooms_and_memberships=AsyncMock(),
        sync_runtime_support=AsyncMock(),
        mark_runtime_support_ready=AsyncMock(),
        recency_recheck_delay_seconds=0.0,
        recheck_max_attempts=recheck_max_attempts,
        recheck_retry_delay_seconds=0.0,
    )


@pytest.mark.asyncio
async def test_failed_recheck_retries_automatically_until_success() -> None:
    """A transient recheck failure recovers in the same run without any reload."""
    counts = {"recover": 0}
    controller = _recheck_failure_controller(counts, failing_recheck_attempts={1}, recheck_max_attempts=3)

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    assert counts["recover"] == 4
    assert controller.recheck_pending is False
    assert await controller.cancel() is False


@pytest.mark.asyncio
async def test_exhausted_recheck_debt_resumes_without_prior_cancel() -> None:
    """Bot recovery resumes parked debt while the completed task is still recorded.

    After exhausted bounded retries the finished task object remains stored;
    only a live task may block resume, otherwise the debt stays parked until
    an unrelated cancel() happens to clear it.
    """
    counts = {"recover": 0}
    controller = _recheck_failure_controller(counts, failing_recheck_attempts={1}, recheck_max_attempts=1)

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)
    assert controller.recheck_pending is True
    assert controller.task is not None

    controller.resume_pending_recheck(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts["recover"] == 4
    assert controller.recheck_pending is False


@pytest.mark.asyncio
async def test_exhausted_recheck_retries_stay_pending_and_replay_on_next_reload() -> None:
    """Exhausting bounded retries keeps the debt replayable by a later reload."""
    counts = {"recover": 0}
    controller = _recheck_failure_controller(counts, failing_recheck_attempts={1, 2, 3}, recheck_max_attempts=3)

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    assert counts["recover"] == 5
    assert controller.recheck_pending is True
    assert await controller.cancel() is True

    controller.restart_after_config_reload(config=MagicMock(), running_bots=lambda: [MagicMock()])
    await _wait_for_controller(controller)

    assert counts["recover"] == 6
    assert controller.recheck_pending is False


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
        recency_recheck_delay_seconds=0.0,
    )

    controller.start([MagicMock()], MagicMock(), startup_cutoff_ms=123456)
    await _wait_for_controller(controller)

    mark_runtime_support_ready.assert_not_awaited()
