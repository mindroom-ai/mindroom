"""Config-gated background worker slots owned by the orchestrator."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable


class _BackgroundWorker(Protocol):
    """A long-running loop that stops gracefully on request."""

    def stop(self) -> None:
        """Ask the loop to finish."""

    async def run(self) -> None:
        """Run until stopped."""


@dataclass
class BackgroundWorkerSlot:
    """Run at most one worker, exactly while its feature is enabled."""

    task_name: str
    _worker: _BackgroundWorker | None = field(default=None, init=False)
    _task: asyncio.Task[None] | None = field(default=None, init=False)

    @property
    def running(self) -> bool:
        """Return whether the slot's worker task is alive."""
        return self._task is not None and not self._task.done()

    async def sync(self, *, enabled: bool, factory: Callable[[], _BackgroundWorker]) -> None:
        """Start a worker when enabled and none is alive, or stop the current one when disabled."""
        if not enabled:
            await self.stop()
            return
        if self.running:
            return
        self._worker = factory()
        self._task = asyncio.create_task(self._worker.run(), name=self.task_name)

    async def stop(self) -> None:
        """Stop the worker and wait for its loop to finish."""
        worker, task = self._worker, self._task
        self._worker = None
        self._task = None
        if worker is not None:
            worker.stop()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
