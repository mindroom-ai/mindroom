"""Detached startup maintenance lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
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
    from collections.abc import AsyncIterator

    from mindroom.bot import AgentBot
    from mindroom.config.main import Config

logger = get_logger(__name__)

type _SetupRooms = Callable[[list[AgentBot]], Awaitable[None]]
type _SyncRuntimeSupport = Callable[[Config], Awaitable[None]]
type _MarkRuntimeSupportReady = Callable[[], Awaitable[None]]
type _RunningBots = Callable[[], list[AgentBot]]


@dataclass
class StartupMaintenanceController:
    """Own detached post-sync startup maintenance task lifecycle."""

    setup_rooms_and_memberships: _SetupRooms
    sync_runtime_support: _SyncRuntimeSupport
    mark_runtime_support_ready: _MarkRuntimeSupportReady
    task: asyncio.Task[None] | None = field(default=None, init=False)

    def start(self, bots: list[AgentBot], config: Config) -> None:
        """Schedule detached startup maintenance for one startup generation."""
        self.task = create_logged_task(
            self._run(bots, config),
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

    @asynccontextmanager
    async def paused(
        self,
        *,
        config_provider: Callable[[], Config | None],
        running_bots: _RunningBots,
    ) -> AsyncIterator[None]:
        """Cancel maintenance across a runtime replacement, then replay it with the bots running after it.

        Maintenance holds the bots it started with, so a replacement that
        interrupted it replays it with the replacements, even when the
        replacement itself failed.
        """
        interrupted = await self.cancel()
        try:
            yield
        finally:
            config = config_provider()
            if interrupted and config is not None and self.task is None:
                bots = running_bots()
                if bots:
                    self.start(bots, config)

    async def _run(self, bots: list[AgentBot], config: Config) -> None:
        await self._run_phase(
            "startup_maintenance.rooms_and_memberships",
            lambda: self.setup_rooms_and_memberships(bots),
            failure_message="Startup room and membership maintenance failed",
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
