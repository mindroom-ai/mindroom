"""Per-binding background refresh ownership for knowledge snapshots."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from mindroom.knowledge.refresh_runner import refresh_knowledge_binding
from mindroom.knowledge.registry import KnowledgeSnapshotKey, resolve_snapshot_key
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
class PerBindingKnowledgeRefreshOwner:
    """Own fire-and-forget refresh tasks without global manager coordination."""

    _tasks: dict[KnowledgeSnapshotKey, asyncio.Task[None]] = field(default_factory=dict, init=False)

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
            key = resolve_snapshot_key(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except Exception:
            return False
        task = self._tasks.get(key)
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

    def _schedule(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
    ) -> None:
        try:
            key = resolve_snapshot_key(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except Exception:
            logger.exception("Could not resolve knowledge binding for refresh", base_id=base_id)
            return

        task = self._tasks.get(key)
        if task is not None and not task.done():
            return

        captured_config = config.model_copy(deep=True)
        captured_runtime_paths = runtime_paths
        captured_execution_identity = execution_identity
        task = asyncio.create_task(
            self._run_refresh(
                key,
                config=captured_config,
                runtime_paths=captured_runtime_paths,
                execution_identity=captured_execution_identity,
            ),
            name=f"knowledge_refresh:{base_id}",
        )
        self._tasks[key] = task
        task.add_done_callback(lambda completed, *, scheduled_key=key: self._handle_done(scheduled_key, completed))

    def _handle_done(self, key: KnowledgeSnapshotKey, task: asyncio.Task[None]) -> None:
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Background knowledge refresh failed", base_id=key.base_id)

    async def _run_refresh(
        self,
        key: KnowledgeSnapshotKey,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
    ) -> None:
        await refresh_knowledge_binding(
            key.base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
        )


class StandaloneKnowledgeRefreshOwner(PerBindingKnowledgeRefreshOwner):
    """API/runtime-owned per-binding refresh scheduler."""


class OrchestratorKnowledgeRefreshOwner(PerBindingKnowledgeRefreshOwner):
    """Orchestrator-owned per-binding refresh scheduler."""
