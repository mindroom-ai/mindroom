"""Detached startup maintenance lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger
from mindroom.matrix.stale_stream_cleanup import STALE_STREAM_RECENCY_GUARD_MS
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

# Streams frozen moments before a fast restart are still inside the recency
# guard when startup recovery scans, so one delayed recheck runs after the
# guard window has provably elapsed.
_DEFAULT_RECENCY_RECHECK_DELAY_SECONDS = STALE_STREAM_RECENCY_GUARD_MS / 1000 + 2.0
# The delayed recheck is the only pass that can see guard-hidden streams, so a
# transient failure retries autonomously a bounded number of times before the
# remaining debt waits for a reload or bot-recovery replay.
_DEFAULT_RECHECK_MAX_ATTEMPTS = 3
_DEFAULT_RECHECK_RETRY_DELAY_SECONDS = 30.0

type _StartupBot = AgentBot | TeamBot
type _SetupRooms = Callable[[list[_StartupBot]], Awaitable[None]]
type _RecoverStaleStreams = Callable[[list[_StartupBot], Config, int, set[str]], Awaitable[None]]
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
    recency_recheck_delay_seconds: float = _DEFAULT_RECENCY_RECHECK_DELAY_SECONDS
    recheck_max_attempts: int = _DEFAULT_RECHECK_MAX_ATTEMPTS
    recheck_retry_delay_seconds: float = _DEFAULT_RECHECK_RETRY_DELAY_SECONDS
    task: asyncio.Task[None] | None = field(default=None, init=False)
    startup_cutoff_ms: int | None = field(default=None, init=False)
    recheck_pending: bool = field(default=False, init=False)

    def start(self, bots: list[_StartupBot], config: Config, *, startup_cutoff_ms: int) -> None:
        """Schedule detached startup maintenance for one startup generation."""
        self.startup_cutoff_ms = startup_cutoff_ms
        self.recheck_pending = False
        self.task = create_logged_task(
            self._run(bots, config, startup_cutoff_ms),
            name="startup_maintenance",
            failure_message="Startup maintenance task failed",
        )

    async def cancel(self) -> bool:
        """Cancel detached startup maintenance and report whether unfinished work was interrupted.

        A pending recency recheck counts as unfinished even when no task is
        alive: a prior replay may have found zero running bots or a failed
        recheck phase, and the pending flag is the only record of that debt.
        """
        task = self.task
        self.task = None
        should_replay = (task is not None and not task.done()) or self.recheck_pending
        await cancel_logged_task(task)
        return should_replay

    def _task_running(self) -> bool:
        """Return whether a live maintenance task is currently scheduled."""
        return self.task is not None and not self.task.done()

    def restart_after_config_reload(
        self,
        *,
        config: Config,
        running_bots: _RunningBots,
    ) -> None:
        """Replay canceled startup maintenance after config reload completes."""
        startup_cutoff_ms = self.startup_cutoff_ms
        if startup_cutoff_ms is None or self._task_running():
            return
        if self.recheck_pending:
            # Every earlier phase already completed before the cancel, and the
            # reload itself re-syncs runtime support, so only the outstanding
            # recency-guard recheck replays.
            self.resume_pending_recheck(config=config, running_bots=running_bots)
            return
        bots = running_bots()
        if not bots:
            return
        self.start(bots, config, startup_cutoff_ms=startup_cutoff_ms)

    def resume_pending_recheck(self, *, config: Config, running_bots: _RunningBots) -> None:
        """Schedule the outstanding recency recheck once running bots exist.

        Called from reload replay and from background bot-start recovery, so a
        reload that completes with zero running bots cannot strand the recheck.
        Only a live task blocks resume: after exhausted bounded retries the
        completed task object remains recorded, and that parked debt must
        stay resumable without an intervening cancel().
        """
        startup_cutoff_ms = self.startup_cutoff_ms
        if not self.recheck_pending or self._task_running() or startup_cutoff_ms is None:
            return
        bots = running_bots()
        if not bots:
            return
        self.task = create_logged_task(
            self._recheck_after_recency_guard(bots, config, startup_cutoff_ms),
            name="startup_maintenance_recheck",
            failure_message="Startup maintenance recheck task failed",
        )

    async def _run(self, bots: list[_StartupBot], config: Config, startup_cutoff_ms: int) -> None:
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
                lambda: self.recover_stale_streams(
                    bots,
                    config,
                    startup_cutoff_ms,
                    scanned_room_ids,
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
                    scanned_room_ids,
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
        self.recheck_pending = True
        await self._recheck_after_recency_guard(bots, config, startup_cutoff_ms)

    async def _recheck_after_recency_guard(
        self,
        bots: list[_StartupBot],
        config: Config,
        startup_cutoff_ms: int,
    ) -> None:
        # Streams interrupted moments before this startup are hidden by the
        # cleanup recency guard on the first scans; rescan every room once the
        # guard window has elapsed so a fast restart cannot freeze them forever.
        await asyncio.sleep(self.recency_recheck_delay_seconds)
        for attempt in range(max(1, self.recheck_max_attempts)):
            if attempt:
                await asyncio.sleep(self.recheck_retry_delay_seconds)
            completed = await self._run_phase(
                "startup_maintenance.stale_stream_recovery.recency_guard_recheck",
                lambda: self.recover_stale_streams(bots, config, startup_cutoff_ms, set()),
                failure_message="Recency-guard stale stream recovery recheck failed",
            )
            if completed:
                self.recheck_pending = False
                return
        # All bounded attempts failed: keep the debt on record so a later
        # reload or bot-recovery replay retries instead of silently
        # recreating permanently frozen streams.

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
