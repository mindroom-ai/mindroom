"""Background refresh ownership for shared knowledge bases."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from mindroom.knowledge.shared_managers import ensure_shared_knowledge_manager
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.orchestrator import MultiAgentOrchestrator


logger = get_logger(__name__)


def _create_logged_task(
    coro: Coroutine[object, object, None],
    *,
    name: str,
    failure_message: str,
) -> asyncio.Task[None]:
    """Create a detached task that logs failures on completion."""
    task = asyncio.create_task(coro, name=name)

    def _log_failure(completed: asyncio.Task[None]) -> None:
        try:
            completed.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception(failure_message)

    task.add_done_callback(_log_failure)
    return task


class KnowledgeRefreshOwner(Protocol):
    """Owns background refresh of shared knowledge bases."""

    def schedule_refresh(self, base_id: str) -> None:
        """Schedule a background refresh for one shared knowledge base."""
        ...

    def schedule_initial_load(self, base_id: str) -> None:
        """Schedule the first background load for one shared knowledge base."""
        ...

    def is_refreshing(self, base_id: str) -> bool:
        """Return whether one shared knowledge base is already refreshing."""
        ...


@dataclass(slots=True)
class StandaloneKnowledgeRefreshOwner:
    """Own shared-knowledge refresh in API-only runtimes with no orchestrator.

    API-only ``/v1`` mode only performs the initial shared-base load at startup.
    Restart the API process to pick up later shared-knowledge changes.
    Matrix/orchestrator-managed runtimes keep ongoing background refresh enabled.
    """

    load_config: Callable[[], tuple[Config | None, RuntimePaths]]
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False)

    def schedule_refresh(self, base_id: str) -> None:
        """Schedule a background refresh for one shared knowledge base."""
        self._schedule(base_id)

    def schedule_initial_load(self, base_id: str) -> None:
        """Schedule the first background load for one shared knowledge base."""
        self._schedule(base_id)

    def is_refreshing(self, base_id: str) -> bool:
        """Return whether one shared knowledge base is already refreshing."""
        task = self._tasks.get(base_id)
        return task is not None and not task.done()

    async def shutdown(self) -> None:
        """Cancel any in-flight background refresh tasks."""
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    def _schedule(self, base_id: str) -> None:
        if self.is_refreshing(base_id):
            return
        task = _create_logged_task(
            self._run_refresh(base_id),
            name=f"standalone_knowledge_refresh:{base_id}",
            failure_message="Standalone knowledge refresh failed",
        )
        self._tasks[base_id] = task
        task.add_done_callback(
            lambda completed, *, refreshed_base_id=base_id: self._clear_task(refreshed_base_id, completed),
        )

    def _clear_task(self, base_id: str, task: asyncio.Task[None]) -> None:
        if self._tasks.get(base_id) is task:
            self._tasks.pop(base_id, None)

    async def _run_refresh(self, base_id: str) -> None:
        config, runtime_paths = self.load_config()
        if config is None or base_id not in config.knowledge_bases:
            return
        await ensure_shared_knowledge_manager(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            start_watchers=False,
            reindex_on_create=False,
            reconcile_existing_runtime=True,
        )


@dataclass(slots=True)
class OrchestratorKnowledgeRefreshOwner:
    """Own shared-knowledge refresh via the running orchestrator."""

    orchestrator: MultiAgentOrchestrator

    def schedule_refresh(self, base_id: str) -> None:
        """Schedule a background refresh for one shared knowledge base."""
        self._schedule(base_id)

    def schedule_initial_load(self, base_id: str) -> None:
        """Schedule the first background load for one shared knowledge base."""
        self._schedule(base_id)

    def is_refreshing(self, base_id: str) -> bool:
        """Return whether one shared knowledge base is already refreshing."""
        task = self.orchestrator._knowledge_base_refresh_tasks.get(base_id)
        return task is not None and not task.done()

    def _schedule(self, base_id: str) -> None:
        config = self.orchestrator.config
        if config is None or base_id not in config.knowledge_bases or self.is_refreshing(base_id):
            return
        self.orchestrator._schedule_knowledge_base_refresh(
            base_id,
            config,
            start_watcher=self.orchestrator.running,
        )
