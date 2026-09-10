"""Bounded await-chain diagnostics for stalled Matrix receive loops."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from types import CoroutineType, GeneratorType
from typing import TYPE_CHECKING

from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from typing import Any

logger = get_logger(__name__)

_STALL_REPORT_INTERVAL_SECONDS = 90.0
_MAX_FRAMES = 32
_MAX_STRING_LENGTH = 240


@dataclass(frozen=True, slots=True)
class _SyncTaskSnapshot:
    """Code locations only; opaque awaitables terminate the visible chain."""

    task_name: str
    await_chain: tuple[str, ...]
    await_boundary: str | None
    truncated: bool


def _snapshot_task(task: asyncio.Task[Any]) -> _SyncTaskSnapshot:
    awaited = task.get_coro()
    frames: list[str] = []
    for _ in range(_MAX_FRAMES):
        if isinstance(awaited, CoroutineType):
            frame, awaited = awaited.cr_frame, awaited.cr_await
        elif isinstance(awaited, GeneratorType):
            frame, awaited = awaited.gi_frame, awaited.gi_yieldfrom
        else:
            break
        if frame is not None:
            code = frame.f_code
            location = f"{Path(code.co_filename).name}:{frame.f_lineno}:{code.co_name}"
            frames.append(location[:_MAX_STRING_LENGTH])
    return _SyncTaskSnapshot(
        task_name=task.get_name()[:_MAX_STRING_LENGTH],
        await_chain=tuple(frames),
        await_boundary=type(awaited).__name__[:_MAX_STRING_LENGTH] if awaited is not None else None,
        truncated=isinstance(awaited, (CoroutineType, GeneratorType)),
    )


def _capture_sync_task_snapshots(agent_name: str) -> list[_SyncTaskSnapshot]:
    """Inspect at most four live, exactly named tasks belonging to one agent."""
    names = {
        f"matrix_sync_{agent_name}",
        f"matrix_ingestion_runner_{agent_name}",
        f"matrix_ingestion_pump_{agent_name}",
        f"delivery_recovery_{agent_name}",
    }
    snapshots: list[_SyncTaskSnapshot] = []
    for task in asyncio.all_tasks():
        if task.get_name() in names and not task.done():
            snapshots.append(_snapshot_task(task))
            names.remove(task.get_name())
            if not names:
                break
    return sorted(snapshots, key=lambda snapshot: snapshot.task_name)


@dataclass(slots=True)
class SyncStallDiagnostics:
    """Observe progress independently of watchdog cancellation and health grace."""

    agent_name: str
    last_progress_monotonic: float
    generation: int | None
    _last_report_monotonic: float | None = field(default=None, init=False)

    def observe(self, *, now: float, sync_age: float | None, generation: int | None) -> None:
        """Log no more than once per 90 seconds after 90 seconds without progress."""
        if generation != self.generation:
            self.generation = generation
            if generation is not None:
                self.last_progress_monotonic = now
        if sync_age is not None:
            self.last_progress_monotonic = max(self.last_progress_monotonic, now - sync_age)
        no_progress_seconds = now - self.last_progress_monotonic
        if no_progress_seconds < _STALL_REPORT_INTERVAL_SECONDS or (
            self._last_report_monotonic is not None
            and now - self._last_report_monotonic < _STALL_REPORT_INTERVAL_SECONDS
        ):
            return
        self._last_report_monotonic = now
        logger.warning(
            "matrix_sync_stall_diagnostics",
            agent=self.agent_name[:_MAX_STRING_LENGTH],
            sync_age=sync_age,
            no_progress_seconds=no_progress_seconds,
            generation=generation,
            snapshots=[
                {
                    "task_name": snapshot.task_name,
                    "await_chain": snapshot.await_chain,
                    "await_boundary": snapshot.await_boundary,
                    "truncated": snapshot.truncated,
                }
                for snapshot in _capture_sync_task_snapshots(self.agent_name)
            ],
        )
