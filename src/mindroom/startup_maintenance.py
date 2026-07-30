"""Detached startup maintenance lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.stale_stream_cleanup import StaleStreamRecoveryState
from mindroom.orchestration.runtime import (
    cancel_logged_task,
    create_logged_task,
    log_startup_phase_finished,
    log_startup_phase_started,
)

if TYPE_CHECKING:
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config

logger = get_logger(__name__)

type _StartupBot = AgentBot | TeamBot
type _SetupRooms = Callable[[list[_StartupBot]], Awaitable[None]]
type _RecoverStaleStreams = Callable[
    [list[_StartupBot], Config, int, StaleStreamRecoveryState],
    Awaitable[None],
]
type _SyncRuntimeSupport = Callable[[Config], Awaitable[None]]
type _MarkRuntimeSupportReady = Callable[[], Awaitable[None]]
type _RunningBots = Callable[[], list[_StartupBot]]
type _ReadyRecovery = Callable[[], Awaitable[None]]


@dataclass
class StartupMaintenanceController:
    """Own detached post-sync startup maintenance task lifecycle."""

    recover_stale_streams: _RecoverStaleStreams
    setup_rooms_and_memberships: _SetupRooms
    sync_runtime_support: _SyncRuntimeSupport
    mark_runtime_support_ready: _MarkRuntimeSupportReady
    task: asyncio.Task[None] | None = field(default=None, init=False)
    _tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False, repr=False)
    startup_cutoff_ms: int | None = field(default=None, init=False)
    recovery_state: StaleStreamRecoveryState = field(
        default_factory=StaleStreamRecoveryState,
        init=False,
    )

    def start(self, bots: list[_StartupBot], config: Config, *, startup_cutoff_ms: int) -> None:
        """Schedule detached startup maintenance for one startup generation."""
        self.startup_cutoff_ms = startup_cutoff_ms
        self.recovery_state = StaleStreamRecoveryState()
        self._schedule(bots, config, startup_cutoff_ms)

    def _schedule(self, bots: list[_StartupBot], config: Config, startup_cutoff_ms: int) -> None:
        """Schedule maintenance while preserving the current generation state."""
        self._track_task(
            create_logged_task(
                self._run(bots, config, startup_cutoff_ms),
                name="startup_maintenance",
                failure_message="Startup maintenance task failed",
            ),
        )

    def _track_task(self, task: asyncio.Task[None]) -> None:
        """Track every task represented by the serialized maintenance chain."""
        self.task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def schedule_ready_recovery(self, recovery: _ReadyRecovery) -> None:
        """Queue one bot-ready recovery after current startup maintenance."""
        previous_task = self.task
        self._track_task(
            create_logged_task(
                self._run_ready_recovery(previous_task, recovery),
                name="startup_ready_recovery",
                failure_message="Bot-ready restart recovery task failed",
            ),
        )

    async def _run_ready_recovery(
        self,
        previous_task: asyncio.Task[None] | None,
        recovery: _ReadyRecovery,
    ) -> None:
        """Serialize a bot-ready recovery behind prior maintenance work."""
        if previous_task is not None:
            await previous_task
        await self._run_phase(
            "startup_maintenance.stale_stream_recovery.bot_ready",
            recovery,
            failure_message="Bot-ready stale stream recovery failed",
        )

    async def cancel(self) -> bool:
        """Cancel all detached maintenance and report whether unfinished work was interrupted."""
        tasks = set(self._tasks)
        if self.task is not None:
            tasks.add(self.task)
        self.task = None
        should_replay = any(not task.done() for task in tasks)
        await asyncio.gather(*(cancel_logged_task(task) for task in tasks))
        self._tasks.difference_update(tasks)
        return should_replay

    def restart_after_config_reload(
        self,
        *,
        config: Config,
        running_bots: _RunningBots,
    ) -> None:
        """Replay canceled startup maintenance after config reload completes."""
        if self.startup_cutoff_ms is None or self.task is not None:
            return
        bots = running_bots()
        if not bots:
            return
        self._schedule(bots, config, self.startup_cutoff_ms)

    async def _run(self, bots: list[_StartupBot], config: Config, startup_cutoff_ms: int) -> None:
        room_setup_task = asyncio.create_task(
            self._run_phase(
                "startup_maintenance.rooms_and_memberships",
                lambda: self.setup_rooms_and_memberships(bots),
                failure_message="Startup room and membership maintenance failed",
            ),
            name="startup_rooms_and_memberships",
        )
        try:
            await self._run_phase(
                "startup_maintenance.stale_stream_recovery.initial",
                lambda: self.recover_stale_streams(
                    bots,
                    config,
                    startup_cutoff_ms,
                    self.recovery_state,
                ),
                failure_message="Initial startup stale stream recovery failed",
            )
            await room_setup_task
            await self._run_phase(
                "startup_maintenance.stale_stream_recovery.joined_room_delta",
                lambda: self.recover_stale_streams(
                    bots,
                    config,
                    startup_cutoff_ms,
                    self.recovery_state,
                ),
                failure_message="Joined-room delta stale stream recovery failed",
            )
        finally:
            if not room_setup_task.done():
                room_setup_task.cancel()
                await asyncio.gather(room_setup_task, return_exceptions=True)
        runtime_support_ready = await self._run_phase(
            "startup_maintenance.runtime_support",
            lambda: self.sync_runtime_support(config),
            failure_message="Startup runtime support maintenance failed",
        )
        if runtime_support_ready:
            await self.mark_runtime_support_ready()

    async def _run_phase(
        self,
        phase: str,
        operation: Callable[[], Awaitable[None]],
        *,
        failure_message: str,
    ) -> bool:
        phase_started = log_startup_phase_started(phase)
        try:
            await operation()
        except asyncio.CancelledError:
            log_startup_phase_finished(phase, phase_started, status="cancelled")
            raise
        except Exception:
            log_startup_phase_finished(phase, phase_started, status="failed")
            logger.warning(failure_message, exc_info=True)
            return False
        log_startup_phase_finished(phase, phase_started)
        return True
