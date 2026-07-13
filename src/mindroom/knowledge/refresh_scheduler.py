"""Best-effort background refresh scheduling for published knowledge indexes."""

from __future__ import annotations

import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from mindroom.background_tasks import create_background_task
from mindroom.embedder_health import (
    EmbedderHealthRecorder,
    capture_embedder_health_recorder,
    check_embedder_health,
    embedder_in_use,
    extract_classified_embedder_detail,
    get_embedder_failure,
)
from mindroom.knowledge.refresh_runner import (
    is_refresh_active,
    mark_refresh_active,
    mark_refresh_inactive,
    refresh_knowledge_binding,
    refresh_knowledge_binding_in_subprocess,
)
from mindroom.knowledge.registry import (
    KnowledgeRefreshTarget,
    load_published_index_state,
    published_index_metadata_path,
    resolve_published_index_key,
    resolve_refresh_target,
)
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from mindroom.config.main import Config
    from mindroom.constants import RuntimePaths
    from mindroom.knowledge.refresh_runner import KnowledgeRefreshResult
    from mindroom.tool_system.worker_routing import ToolExecutionIdentity


logger = get_logger(__name__)
_DEFAULT_MAX_CONCURRENT_REFRESHES = 1
_MAX_CONCURRENT_REFRESHES_ENV = "MINDROOM_KNOWLEDGE_REFRESH_CONCURRENCY"


def _default_max_concurrent_refreshes() -> int:
    raw_value = os.getenv(_MAX_CONCURRENT_REFRESHES_ENV)
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


@dataclass(slots=True)
class KnowledgeRefreshScheduler:
    """Deduplicate per-binding refreshes and limit global background refresh concurrency."""

    max_concurrent_refreshes: int = field(default_factory=_default_max_concurrent_refreshes)
    _tasks: dict[KnowledgeRefreshTarget, asyncio.Task[None]] = field(default_factory=dict, init=False)
    _pending: dict[KnowledgeRefreshTarget, _ScheduledRefresh] = field(default_factory=dict, init=False)
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
        """Return whether one resolved knowledge binding is refreshing."""
        try:
            key = resolve_refresh_target(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except ValueError:
            return False
        return is_refresh_active(key)

    async def refresh_now(
        self,
        base_id: str,
        *,
        config: Config,
        runtime_paths: RuntimePaths,
        execution_identity: ToolExecutionIdentity | None = None,
        force_reindex: bool = False,
    ) -> KnowledgeRefreshResult:
        """Run a refresh immediately and wait for it."""
        with suppress(ValueError):
            key = resolve_refresh_target(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
            self._pending.pop(key, None)
        return await refresh_knowledge_binding(
            base_id,
            config=config,
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            force_reindex=force_reindex,
        )

    async def shutdown(self) -> None:
        """Cancel in-flight background refresh tasks."""
        self._shutting_down = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
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
    ) -> None:
        if self._shutting_down:
            logger.debug("Skipping knowledge refresh schedule after shutdown", base_id=base_id)
            return
        try:
            key = resolve_refresh_target(
                base_id,
                config=config,
                runtime_paths=runtime_paths,
                execution_identity=execution_identity,
                create=False,
            )
        except ValueError:
            logger.exception("Could not resolve knowledge binding for refresh", base_id=base_id)
            return

        request = _ScheduledRefresh(
            base_id=base_id,
            config=config.model_copy(deep=True),
            runtime_paths=runtime_paths,
            execution_identity=execution_identity,
            health_recorder=capture_embedder_health_recorder(),
        )
        if key in self._tasks:
            self._pending[key] = request
            return
        self._start_task(key, request)

    def _start_task(self, key: KnowledgeRefreshTarget, request: _ScheduledRefresh) -> None:
        loop = _running_loop_for_schedule(key.base_id)
        if loop is None:
            return
        mark_refresh_active(key)
        task = loop.create_task(self._run_refresh(key, request), name=f"knowledge_refresh:{key.base_id}")
        self._tasks[key] = task

        task.add_done_callback(lambda completed, *, scheduled_key=key: self._handle_done(scheduled_key, completed))

    def _handle_done(self, key: KnowledgeRefreshTarget, task: asyncio.Task[None]) -> None:
        mark_refresh_inactive(key)
        if self._tasks.get(key) is task:
            self._tasks.pop(key, None)
        cancelled = False
        try:
            task.result()
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            logger.exception("Background knowledge refresh failed", base_id=key.base_id)
        if cancelled or self._shutting_down:
            self._pending.pop(key, None)
            return
        pending_request = self._pending.pop(key, None)
        if pending_request is not None:
            self._start_task(key, pending_request)

    async def _run_refresh(self, key: KnowledgeRefreshTarget, request: _ScheduledRefresh) -> None:
        async with self._refresh_semaphore():
            try:
                await refresh_knowledge_binding_in_subprocess(
                    key.base_id,
                    config=request.config,
                    runtime_paths=request.runtime_paths,
                    execution_identity=request.execution_identity,
                )
            except Exception:
                _schedule_embedder_probe_after_refresh(key, request, refresh_raised=True)
                raise
        _schedule_embedder_probe_after_refresh(key, request, refresh_raised=False)

    def _refresh_semaphore(self) -> asyncio.Semaphore:
        if self._refresh_slots is None:
            self._refresh_slots = asyncio.Semaphore(max(self.max_concurrent_refreshes, 1))
        return self._refresh_slots


def _schedule_embedder_probe_after_refresh(
    key: KnowledgeRefreshTarget,
    request: _ScheduledRefresh,
    *,
    refresh_raised: bool,
) -> None:
    """Fire-and-forget an embedder health probe when a refresh failed.

    Subprocess refreshes never touch the main-process embedder, so passive
    health recording alone would miss key rotation without query traffic. The
    probe only records health state; the persisted last_error stays untouched,
    so distinct reader/Git/chunking errors are preserved verbatim.
    """
    if not embedder_in_use(request.config):
        return
    create_background_task(
        _check_embedder_after_refresh(request, refresh_raised=refresh_raised),
        name=f"embedder_refresh_health_check:{key.base_id}",
    )


async def _check_embedder_after_refresh(request: _ScheduledRefresh, *, refresh_raised: bool) -> None:
    if not request.health_recorder.is_current():
        return
    persisted_embedder_failure = await asyncio.to_thread(_persisted_refresh_embedder_failure, request)
    if refresh_raised or persisted_embedder_failure:
        if not persisted_embedder_failure:
            return
        reason = "knowledge_refresh_failed"
    else:
        if get_embedder_failure() is None:
            return
        reason = "knowledge_refresh_recovery"
    await check_embedder_health(
        request.config,
        request.runtime_paths,
        reason=reason,
        health_recorder=request.health_recorder,
    )


def _persisted_refresh_embedder_failure(request: _ScheduledRefresh) -> bool:
    """Return whether persisted refresh state proves an embedder failure."""
    try:
        key = resolve_published_index_key(
            request.base_id,
            config=request.config,
            runtime_paths=request.runtime_paths,
            execution_identity=request.execution_identity,
            create=False,
        )
    except ValueError:
        return False
    state = load_published_index_state(published_index_metadata_path(key))
    return state is not None and extract_classified_embedder_detail(state.last_error) is not None


def _running_loop_for_schedule(base_id: str) -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        logger.warning("Skipping knowledge refresh schedule without a running event loop", base_id=base_id)
        return None
