"""Detached startup maintenance lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.orchestration.runtime import (
    cancel_logged_task,
    create_logged_task,
    log_startup_phase_finished,
    log_startup_phase_started,
)

if TYPE_CHECKING:
    from mindroom.bot import AgentBot, TeamBot
    from mindroom.config.main import Config
    from mindroom.matrix.stale_stream_cleanup import InterruptedThread

logger = get_logger(__name__)

type _StartupBot = AgentBot | TeamBot
type _SetupRooms = Callable[[list[_StartupBot]], Awaitable[None]]
type _CleanupStaleStreams = Callable[[list[_StartupBot], Config, int], Awaitable[list[InterruptedThread]]]
type _AutoResume = Callable[[list[InterruptedThread], Config], Awaitable[None]]
type _SyncRuntimeSupport = Callable[[Config], Awaitable[None]]
type _MarkRuntimeSupportReady = Callable[[], Awaitable[None]]
type _RunningBots = Callable[[], list[_StartupBot]]


@dataclass
class StartupMaintenanceController:
    """Own detached post-sync startup maintenance task lifecycle."""

    setup_rooms_and_memberships: _SetupRooms
    cleanup_stale_streams: _CleanupStaleStreams
    auto_resume: _AutoResume
    sync_runtime_support: _SyncRuntimeSupport
    mark_runtime_support_ready: _MarkRuntimeSupportReady
    task: asyncio.Task[None] | None = field(default=None, init=False)
    startup_cutoff_ms: int | None = field(default=None, init=False)

    def start(self, bots: list[_StartupBot], config: Config, *, startup_cutoff_ms: int) -> None:
        """Schedule detached startup maintenance for one startup generation."""
        self.startup_cutoff_ms = startup_cutoff_ms
        self.task = create_logged_task(
            self._run(bots, config, startup_cutoff_ms),
            name="startup_maintenance",
            failure_message="Startup maintenance task failed",
        )

    async def cancel(self) -> bool:
        """Cancel detached startup maintenance and report whether unfinished work was interrupted."""
        task = self.task
        self.task = None
        should_replay = task is not None and not task.done()
        await cancel_logged_task(task)
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
        self.start(bots, config, startup_cutoff_ms=self.startup_cutoff_ms)

    async def _run(self, bots: list[_StartupBot], config: Config, startup_cutoff_ms: int) -> None:
        await self._run_phase(
            "startup_maintenance.rooms_and_memberships",
            lambda: self.setup_rooms_and_memberships(bots),
            failure_message="Startup room and membership maintenance failed",
        )

        async def cleanup_and_resume() -> None:
            interrupted_threads = await self.cleanup_stale_streams(bots, config, startup_cutoff_ms)
            await self.auto_resume(interrupted_threads, config)

        await self._run_phase(
            "startup_maintenance.stale_stream_cleanup",
            cleanup_and_resume,
            failure_message="Startup stale stream maintenance failed",
        )
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
