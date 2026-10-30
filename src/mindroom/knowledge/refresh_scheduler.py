"""The runtime's one knowledge refresh scheduler: which bases are refreshing, and how many children run at once.

Each process has one scheduler, shared by the bots, the API, and file memory, so its task map is the only answer to
"is this base refreshing" and its limit bounds every refresh child the process starts.
"""

from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from contextvars import Context
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from mindroom.embeddings.embedder_health import (
    EmbedderHealthRecorder,
    capture_embedder_health_recorder,
)
from mindroom.knowledge.registry import (
    KnowledgeRefreshTarget,
    resolve_refresh_target,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Hashable

    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_runner import KnowledgeRefreshResult
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)
_DEFAULT_MAX_CONCURRENT_REFRESHES = 1
_MAX_CONCURRENT_REFRESHES_ENV = "MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY"
#: On-access refreshes remembered for their cooldown; private knowledge has one target per requester.
_MAX_SCHEDULED_COOLDOWNS = 512


def _max_concurrent_refreshes(runtime_paths: RuntimePaths) -> int:
    raw_value = runtime_paths.env_value(_MAX_CONCURRENT_REFRESHES_ENV)
    if raw_value is None:
        return _DEFAULT_MAX_CONCURRENT_REFRESHES
    try:
        value = int(raw_value)
    except ValueError as exc:
        msg = f"{_MAX_CONCURRENT_REFRESHES_ENV} must be an integer, got {raw_value!r}"
        raise ValueError(msg) from exc
    if value < 1:
        msg = f"{_MAX_CONCURRENT_REFRESHES_ENV} must be at least 1, got {value}"
        raise ValueError(msg)
    return value


@dataclass(frozen=True, slots=True)
class _ScheduledRefresh:
    base_id: str
    config: Config
    runtime_paths: RuntimePaths
    execution_identity: ToolExecutionIdentity | None
    health_recorder: EmbedderHealthRecorder
    force_reindex: bool = False
    #: Callers of ``refresh_now`` waiting for the refresh that serves this request.
    waiters: tuple[asyncio.Future[KnowledgeRefreshResult], ...] = ()

    def absorbing(self, earlier: _ScheduledRefresh) -> _ScheduledRefresh:
        """Return this request, standing in for an earlier one still queued: a forced rebuild covers a plain refresh."""
        return replace(
            self,
            force_reindex=self.force_reindex or earlier.force_reindex,
            waiters=(*earlier.waiters, *self.waiters),
        )


@dataclass(slots=True)
class KnowledgeRefreshScheduler:
    """Run at most one refresh per base, queue one more behind it, and bound concurrent refresh children."""

    max_concurrent_refreshes: int = _DEFAULT_MAX_CONCURRENT_REFRESHES
    _tasks: dict[KnowledgeRefreshTarget, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _pending: dict[KnowledgeRefreshTarget, _ScheduledRefresh] = field(default_factory=dict, init=False)
    _scheduled_at: dict[Hashable, float] = field(default_factory=dict, init=False)
    _shutting_down: bool = field(default=False, init=False)
    _refresh_slots: asyncio.Semaphore | None = field(default=None, init=False)

    def schedule_refresh(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
    ) -> None:
        """Schedule a background refresh for one resolved knowledge binding."""
        self._schedule(base_id, config=config, runtime_paths=runtime_paths, execution_identity=execution_identity)

    def schedule_refresh_after_cooldown(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None,
        cooldown_key: Hashable,
        cooldown_seconds: float,
    ) -> bool:
        """Schedule a refresh unless one with ``cooldown_key`` was scheduled within ``cooldown_seconds``."""
        now = time.monotonic()
        last_scheduled_at = self._scheduled_at.get(cooldown_key)
        if last_scheduled_at is not None and now - last_scheduled_at < cooldown_seconds:
            return False
        self._scheduled_at[cooldown_key] = now
        for stale in sorted(self._scheduled_at, key=self._scheduled_at.__getitem__)[:-_MAX_SCHEDULED_COOLDOWNS]:
            del self._scheduled_at[stale]
        self._schedule(base_id, config=config, runtime_paths=runtime_paths, execution_identity=execution_identity)
        return True

    def is_refreshing(self, target: KnowledgeRefreshTarget) -> bool:
        """Return whether a refresh of ``target`` is running or waiting for a slot."""
        return target in self._tasks

    async def refresh_now(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
        force_reindex: bool = False,
    ) -> KnowledgeRefreshResult:
        """Schedule a refresh and wait for it, behind any refresh of the same base already running.

        The refresh is shared with background requests for the base and keeps running if this caller stops waiting.
        """
        if self._shutting_down:
            msg = "Knowledge refresh scheduler is shutting down"
            raise RuntimeError(msg)
        waiter: asyncio.Future[KnowledgeRefreshResult] = asyncio.get_running_loop().create_future()
        self._schedule(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            force_reindex=force_reindex,
            waiter=waiter,
        )
        return await waiter

    async def shutdown(self) -> None:
        """Cancel in-flight background refresh tasks; requests made after it, as during the runtime's exit, are skipped."""
        self._shutting_down = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for request in self._pending.values():
            _cancel_waiters(request)
        self._pending.clear()
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
        force_reindex: bool = False,
        waiter: asyncio.Future[KnowledgeRefreshResult] | None = None,
    ) -> None:
        if self._shutting_down:
            logger.debug("Skipping knowledge refresh schedule after shutdown", base_id=base_id)
            return
        try:
            target = resolve_refresh_target(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except ValueError as exc:
            logger.exception("Could not resolve knowledge binding for refresh", base_id=base_id)
            if waiter is not None:
                waiter.set_exception(exc)
            return

        request = _ScheduledRefresh(
            base_id=base_id,
            # Config reloads replace the object, so retaining this generation
            # avoids a CPU-heavy deep copy on the event loop.
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            health_recorder=capture_embedder_health_recorder(),
            force_reindex=force_reindex,
            waiters=() if waiter is None else (waiter,),
        )
        if target in self._tasks:
            earlier = self._pending.get(target)
            self._pending[target] = request if earlier is None else request.absorbing(earlier)
            return
        self._start_task(target, request)

    def _start_task(self, target: KnowledgeRefreshTarget, request: _ScheduledRefresh) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("Skipping knowledge refresh schedule without a running event loop", base_id=target.base_id)
            _cancel_waiters(request)
            return
        # A tool call can schedule a refresh that outlives it, and a turn's contextvars hold its Agent and tools.
        context = Context()
        task = loop.create_task(
            self._run_refresh(request),
            name=f"knowledge_refresh:{target.base_id}",
            context=context,
        )
        self._tasks[target] = task
        task.add_done_callback(
            lambda completed, *, scheduled_target=target, scheduled=request: self._handle_done(
                scheduled_target,
                scheduled,
                completed,
            ),
            context=context,
        )

    def _handle_done(
        self,
        target: KnowledgeRefreshTarget,
        request: _ScheduledRefresh,
        task: asyncio.Task[None],
    ) -> None:
        if self._tasks.get(target) is task:
            self._tasks.pop(target, None)
        if task.cancelled():
            # Also a task cancelled before it first ran, whose waiters nothing else would answer.
            _cancel_waiters(request)
            return
        if (error := task.exception()) is not None:
            logger.error("Background knowledge refresh failed", base_id=target.base_id, exc_info=error)
        pending_request = self._pending.pop(target, None)
        if pending_request is not None and not self._shutting_down:
            self._start_task(target, pending_request)

    async def _run_refresh(self, request: _ScheduledRefresh) -> None:
        from mindroom.knowledge.refresh_runner import refresh_knowledge_binding_in_subprocess  # noqa: PLC0415

        try:
            async with self._refresh_semaphore():
                result = await refresh_knowledge_binding_in_subprocess(
                    request.base_id,
                    config=request.config,
                    runtime_paths=request.runtime_paths,
                    execution_identity=request.execution_identity,
                    force_reindex=request.force_reindex,
                    health_recorder=request.health_recorder,
                )
        except Exception as exc:
            answered = [waiter for waiter in request.waiters if not waiter.done()]
            for waiter in answered:
                waiter.set_exception(exc)
            if answered:
                return
            raise
        for waiter in request.waiters:
            if not waiter.done():
                waiter.set_result(result)

    def _refresh_semaphore(self) -> asyncio.Semaphore:
        if self._refresh_slots is None:
            self._refresh_slots = asyncio.Semaphore(max(self.max_concurrent_refreshes, 1))
        return self._refresh_slots


def _cancel_waiters(request: _ScheduledRefresh) -> None:
    for waiter in request.waiters:
        waiter.cancel()


_runtime_scheduler: KnowledgeRefreshScheduler | None = None


def runtime_refresh_scheduler(runtime_paths: RuntimePaths) -> KnowledgeRefreshScheduler:
    """Return this process's one refresh scheduler, created with its runtime's refresh limit; a bad value raises.

    The bots, the API, and file memory all schedule through it, so one limit bounds every refresh child.
    """
    global _runtime_scheduler
    if _runtime_scheduler is None:
        _runtime_scheduler = KnowledgeRefreshScheduler(
            max_concurrent_refreshes=_max_concurrent_refreshes(runtime_paths),
        )
    return _runtime_scheduler
