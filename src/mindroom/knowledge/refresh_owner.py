"""Per-binding background refresh ownership for knowledge snapshots."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from mindroom.knowledge.refresh_runner import (
    is_refresh_active,
    mark_refresh_active,
    mark_refresh_inactive,
    refresh_knowledge_binding,
)
from mindroom.knowledge.registry import KnowledgeRefreshKey, resolve_refresh_key
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)


class KnowledgeRefreshOwner(Protocol):
    """Owns background refresh tasks keyed by resolved knowledge binding."""

    def schedule_refresh(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Schedule a background refresh for one resolved knowledge binding."""
        ...

    def schedule_initial_load(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Schedule the first background load for one resolved knowledge binding."""
        ...

    def is_refreshing(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> bool:
        """Return whether one resolved knowledge binding is refreshing."""
        ...


@dataclass(slots=True)
class _ScheduledRefresh:
    """Captured inputs for one refresh task."""

    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None


@dataclass(slots=True)
class PerBindingKnowledgeRefreshOwner:
    """Own fire-and-forget refresh tasks without global manager coordination."""

    _tasks: dict[KnowledgeRefreshKey, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _pending: dict[KnowledgeRefreshKey, _ScheduledRefresh] = field(default_factory=dict, init=False)
    _shutting_down: bool = field(default=False, init=False)

    def schedule_refresh(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Schedule a background refresh for one resolved knowledge binding."""
        self._schedule(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )

    def schedule_initial_load(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Schedule the first background load for one resolved knowledge binding."""
        self._schedule(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )

    def is_refreshing(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> bool:
        """Return whether one resolved knowledge binding is already refreshing."""
        try:
            key = resolve_refresh_key(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except Exception:
            return False
        return is_refresh_active(key)

    async def shutdown(self) -> None:
        """Cancel any in-flight background refresh tasks."""
        self._shutting_down = True
        self._pending.clear()
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError, Exception):
                await task

    def _schedule(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
    ) -> None:
        if self._shutting_down:
            logger.debug("Skipping knowledge refresh schedule after shutdown", base_id=base_id)
            return
        try:
            key = resolve_refresh_key(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except Exception:
            logger.exception("Could not resolve knowledge binding for refresh", base_id=base_id)
            return

        request = _ScheduledRefresh(
            base_id=base_id,
            config=config.model_copy(deep=True),
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )
        task = self._tasks.get(key)
        if task is not None:
            self._pending[key] = request
            return

        self._start_task(key, request)

    def _start_task(self, key: KnowledgeRefreshKey, request: _ScheduledRefresh) -> None:
        mark_refresh_active(key)
        task = asyncio.create_task(
            self._run_refresh(key, request=request),
            name=f"knowledge_refresh:{request.base_id}",
        )
        self._tasks[key] = task
        task.add_done_callback(lambda completed, *, scheduled_key=key: self._handle_done(scheduled_key, completed))

    def _handle_done(self, key: KnowledgeRefreshKey, task: asyncio.Task[None]) -> None:
        mark_refresh_inactive(key)
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Background knowledge refresh failed", base_id=key.base_id)
        if self._shutting_down:
            return
        pending = self._pending.pop(key, None)
        if pending is not None:
            self._start_task(key, pending)

    async def _run_refresh(
        self,
        key: KnowledgeRefreshKey,
        *,
        request: _ScheduledRefresh,
    ) -> None:
        await refresh_knowledge_binding(
            key.base_id,
            config=request.config,
            runtime_paths=request.runtime_paths,
            execution_identity=request.execution_identity,
        )


StandaloneKnowledgeRefreshOwner = PerBindingKnowledgeRefreshOwner
OrchestratorKnowledgeRefreshOwner = PerBindingKnowledgeRefreshOwner
