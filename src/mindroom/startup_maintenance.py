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

logger = get_logger(__name__)

type _StartupBot = AgentBot | TeamBot
type _SetupRooms = Callable[[list[_StartupBot]], Awaitable[None]]
type _RecoverStaleStreams = Callable[[list[_StartupBot], Config, set[str]], Awaitable[None]]
type _SyncRuntimeSupport = Callable[[Config], Awaitable[None]]
type _MarkRuntimeSupportReady = Callable[[], Awaitable[None]]
type _RunningBots = Callable[[], list[_StartupBot]]


@dataclass
class StartupMaintenanceController:
    """Own detached post-sync startup maintenance task lifecycle."""

    recover_stale_streams: _RecoverStaleStreams
    setup_rooms_and_memberships: _SetupRooms
    sync_runtime_support: _SyncRuntimeSupport
    mark_runtime_support_ready: _MarkRuntimeSupportReady
    task: asyncio.Task[None] | None = field(default=None, init=False)
    _started: bool = field(default=False, init=False)
    # A reload can cancel maintenance before any phase completes and then
    # finish with zero running bots; this records that the full sequence is
    # still owed so a later reload or bot-recovery replay reruns it.
    replay_pending: bool = field(default=False, init=False)
    _config_reload_suspended: bool = field(default=False, init=False)

    def start(self, bots: list[_StartupBot], config: Config) -> None:
        """Schedule detached startup maintenance for one startup generation."""
        self._started = True
        self.replay_pending = False
        self.task = create_logged_task(
            self._run(bots, config),
            name="startup_maintenance",
            failure_message="Startup maintenance task failed",
        )

    async def cancel(self) -> bool:
        """Cancel detached startup maintenance and report whether unfinished work was interrupted.

        Parked debt counts as unfinished even when no task is alive: a prior
        replay may have found zero running bots, and the pending flag is the
        only record of that debt.
        """
        task = self.task
        self.task = None
        should_replay = (task is not None and not task.done()) or self.replay_pending
        await cancel_logged_task(task)
        return should_replay

    async def suspend_for_config_reload(self) -> bool:
        """Cancel maintenance and fence background recovery until reload finalization."""
        self._config_reload_suspended = True
        return await self.cancel()

    def _task_running(self) -> bool:
        """Return whether a live maintenance task is currently scheduled."""
        return self.task is not None and not self.task.done()

    def restart_after_config_reload(
        self,
        *,
        config: Config,
        running_bots: _RunningBots,
        replay: bool = True,
    ) -> None:
        """Replay canceled startup maintenance after config reload completes."""
        self._config_reload_suspended = False
        if not replay or not self._started or self._task_running():
            return
        # The cancel interrupted the full sequence, so it is owed. Record the
        # debt before attempting resume: a reload that finishes with zero
        # running bots must stay replayable by a later reload or by background
        # bot recovery.
        self.replay_pending = True
        self.resume_pending_maintenance(config=config, running_bots=running_bots)

    def resume_pending_maintenance(self, *, config: Config, running_bots: _RunningBots) -> None:
        """Resume parked full-maintenance debt once running bots exist.

        Called from reload replay and from background bot-start recovery. Only
        a live task blocks resume; parked debt must stay resumable without an
        intervening cancel().
        """
        if self._config_reload_suspended or self._task_running() or not self.replay_pending:
            return
        bots = running_bots()
        if not bots:
            return
        self.start(bots, config)

    async def _run(self, bots: list[_StartupBot], config: Config) -> None:
        scanned_room_ids: set[str] = set()
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
                lambda: self.recover_stale_streams(bots, config, scanned_room_ids),
                failure_message="Initial startup stale stream recovery failed",
            )
            await room_setup_task
            await self._run_phase(
                "startup_maintenance.stale_stream_recovery.joined_room_delta",
                lambda: self.recover_stale_streams(bots, config, scanned_room_ids),
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
