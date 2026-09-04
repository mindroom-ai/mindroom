"""Native workspace thread-export runtime binding for the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.thread_export.workspace_sync import (
    WorkspaceThreadExportDeps,
    WorkspaceThreadExportRunner,
    enabled_thread_export_agents,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.thread_export.workspace_sync import ThreadExportBot


@dataclass
class ThreadExportRuntimeCoordinator:
    """Own the workspace thread-export runner lifecycle."""

    runtime_paths: RuntimePaths
    config_provider: Callable[[], Config | None]
    bot_provider: Callable[[str], ThreadExportBot | None]
    _runner: WorkspaceThreadExportRunner | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    async def sync(self) -> None:
        """Start or stop the runner from the active config, and reconcile every workspace once."""
        config = self.config_provider()
        if config is None or not enabled_thread_export_agents(config):
            await self.stop()
            return
        if self._runner is None or self._task is None or self._task.done():
            runner = WorkspaceThreadExportRunner(
                WorkspaceThreadExportDeps(
                    runtime_paths=self.runtime_paths,
                    config_provider=self.config_provider,
                    bot_provider=self.bot_provider,
                ),
            )
            self._runner = runner
            self._task = asyncio.create_task(runner.run(), name="thread_export_workspace_sync")
        self._runner.queue_full_pass()

    async def stop(self) -> None:
        """Stop the runner, abandoning a pass in flight; every write it makes is atomic."""
        runner = self._runner
        task = self._task
        self._runner = None
        self._task = None
        if runner is not None:
            runner.stop()
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    def reconcile(self) -> None:
        """Queue one full pass, for when bots just started and can now be read."""
        if self._runner is not None:
            self._runner.queue_full_pass()

    def mark_room_activity(self, room_id: str) -> None:
        """Queue one room for re-export when exports are enabled."""
        if self._runner is not None:
            self._runner.mark_room_activity(room_id)
