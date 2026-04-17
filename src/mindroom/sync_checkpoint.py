"""Coordinate safe Matrix sync checkpoint persistence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import create_background_task

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class SyncCheckpointCoordinator:
    """Persist sync tokens only after tracked startup-catchup ingress work settles.

    Intentionally narrow scope:
    only sync-delivered text/media ingress claims should register tasks here.
    Reactions, redactions, invites, and already-started replies are handled by
    their own restart flows and must not delay checkpoint persistence.
    """

    agent_name: str
    persist_sync_token: Callable[[str], None]
    owner: object | None = None
    _pending_event_tasks: set[asyncio.Task[Any]] = field(default_factory=set, init=False, repr=False)
    _pending_token: str | None = field(default=None, init=False, repr=False)
    _flush_task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @property
    def flush_task(self) -> asyncio.Task[None] | None:
        """Return the current deferred flush task when one exists."""
        return self._flush_task

    def register_event_task(self, task: asyncio.Task[Any]) -> None:
        """Track one startup-catchup ingress task until it settles."""
        self._pending_event_tasks.add(task)
        task.add_done_callback(self._pending_event_tasks.discard)

    def note_sync_token(self, token: str) -> None:
        """Queue persistence for the newest observed sync token."""
        self._pending_token = token
        existing_task = self._flush_task
        if existing_task is not None and not existing_task.done():
            return
        self._flush_task = create_background_task(
            self._flush_after_pending_event_tasks(),
            name=f"sync_checkpoint_flush_{self.agent_name}",
            owner=self.owner,
        )

    async def flush_for_shutdown(self, current_token: str | None) -> None:
        """Persist the latest token after tracked event work during shutdown."""
        if not isinstance(current_token, str) or not current_token:
            return
        if self._pending_token is None:
            self._pending_token = current_token
        flush_task = self._flush_task
        if flush_task is None or flush_task.done():
            await self._flush_after_pending_event_tasks()
            return
        await asyncio.gather(flush_task, return_exceptions=True)

    def _discard_completed_event_tasks(self) -> None:
        """Drop completed sync event tasks while preserving callback registrations."""
        completed_tasks = {task for task in self._pending_event_tasks if task.done()}
        if completed_tasks:
            self._pending_event_tasks.difference_update(completed_tasks)

    async def _flush_after_pending_event_tasks(self) -> None:
        """Persist the newest pending sync token once tracked event work has settled."""
        current_task = asyncio.current_task()
        try:
            while True:
                # Let callback wrappers register tasks spawned from the same sync response first.
                await asyncio.sleep(0)
                self._discard_completed_event_tasks()
                while self._pending_event_tasks:
                    await asyncio.gather(*tuple(self._pending_event_tasks), return_exceptions=True)
                    self._discard_completed_event_tasks()
                token = self._pending_token
                if not isinstance(token, str) or not token:
                    return
                self.persist_sync_token(token)
                await asyncio.sleep(0)
                self._discard_completed_event_tasks()
                if token == self._pending_token and not self._pending_event_tasks:
                    self._pending_token = None
                    return
        finally:
            if self._flush_task is current_task:
                self._flush_task = None
