"""Backend-neutral worker lifecycle state transitions.

The dedicated (docker, local) and shared (static) worker backends persist the
same lifecycle fields and apply the same status-transition rules. This module is
the single source of truth for those rules; each backend converts its own
metadata to and from :class:`WorkerLifecycleState` at the edge.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from mindroom.workers.models import WorkerStatus


@dataclass(frozen=True, slots=True)
class WorkerLifecycleState:
    """Backend-neutral lifecycle fields persisted for one worker."""

    created_at: float
    last_used_at: float
    status: WorkerStatus
    last_started_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None


class _MutableLifecycleMetadata(Protocol):
    """Structural view of a backend metadata record carrying lifecycle fields."""

    created_at: float
    last_used_at: float
    status: WorkerStatus
    last_started_at: float | None
    startup_count: int
    failure_count: int
    failure_reason: str | None


def read_lifecycle_state(metadata: _MutableLifecycleMetadata) -> WorkerLifecycleState:
    """Project the lifecycle fields of one backend metadata record."""
    return WorkerLifecycleState(
        created_at=metadata.created_at,
        last_used_at=metadata.last_used_at,
        status=metadata.status,
        last_started_at=metadata.last_started_at,
        startup_count=metadata.startup_count,
        failure_count=metadata.failure_count,
        failure_reason=metadata.failure_reason,
    )


def write_lifecycle_state(metadata: _MutableLifecycleMetadata, state: WorkerLifecycleState) -> None:
    """Write one lifecycle state back onto a backend metadata record in place."""
    metadata.created_at = state.created_at
    metadata.last_used_at = state.last_used_at
    metadata.status = state.status
    metadata.last_started_at = state.last_started_at
    metadata.startup_count = state.startup_count
    metadata.failure_count = state.failure_count
    metadata.failure_reason = state.failure_reason


def initial_worker_lifecycle_state(*, now: float) -> WorkerLifecycleState:
    """Return the initial lifecycle state for a newly created worker."""
    return WorkerLifecycleState(
        created_at=now,
        last_used_at=now,
        status="starting",
    )


def prepare_worker_ensure_lifecycle(
    state: WorkerLifecycleState,
    *,
    now: float,
    should_restart: bool,
) -> WorkerLifecycleState:
    """Return lifecycle fields for one ensure attempt before backend-specific startup IO."""
    return replace(
        state,
        last_used_at=now,
        status="starting" if should_restart else state.status,
        last_started_at=now if should_restart else state.last_started_at,
        startup_count=state.startup_count + int(should_restart),
        failure_reason=None,
    )


def touch_worker_lifecycle(
    state: WorkerLifecycleState,
    *,
    now: float,
) -> WorkerLifecycleState:
    """Refresh last-used state and revive idle workers back to ready."""
    next_status = "ready" if state.status == "idle" else state.status
    return replace(
        state,
        last_used_at=now,
        status=next_status,
        failure_reason=None if next_status != "failed" else state.failure_reason,
    )


def mark_worker_ready(
    state: WorkerLifecycleState,
    *,
    now: float,
) -> WorkerLifecycleState:
    """Return lifecycle fields for one worker that completed startup successfully."""
    return replace(
        state,
        last_used_at=now,
        status="ready",
        failure_reason=None,
    )


def mark_worker_idle(
    state: WorkerLifecycleState,
    *,
    now: float | None = None,
    update_last_used: bool = False,
) -> WorkerLifecycleState:
    """Return lifecycle fields for one worker whose persisted state is being retained."""
    if not update_last_used:
        return replace(state, status="idle", failure_reason=None)
    if now is None:
        msg = "now is required when update_last_used is true."
        raise ValueError(msg)
    return replace(state, last_used_at=now, status="idle", failure_reason=None)


def mark_worker_failed(
    state: WorkerLifecycleState,
    *,
    now: float,
    failure_reason: str,
) -> WorkerLifecycleState:
    """Return lifecycle fields for one worker that failed to start or execute."""
    return replace(
        state,
        last_used_at=now,
        status="failed",
        failure_count=state.failure_count + 1,
        failure_reason=failure_reason,
    )
