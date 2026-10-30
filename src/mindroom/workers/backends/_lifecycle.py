"""Backend-neutral worker lifecycle state transitions.

The dedicated (docker, kubernetes, local) and shared (static) worker backends persist
the same lifecycle fields and apply the same status-transition rules. This module is
the single source of truth for those rules; each backend keeps one :class:`WorkerLifecycleState`
on its worker record (Kubernetes: converts Deployment annotations to and from it at the edge).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from mindroom.workers.models import WorkerHandle, WorkerStatus

DEFAULT_IDLE_TIMEOUT_SECONDS = 1800.0


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


def worker_handle(
    *,
    worker_id: str,
    worker_key: str,
    lifecycle: WorkerLifecycleState,
    endpoint: str,
    auth_token: str | None,
    status: WorkerStatus,
    backend_name: str,
    debug_metadata: dict[str, str],
) -> WorkerHandle:
    """Build one worker's handle from its lifecycle state and the backend's effective status and endpoint."""
    return WorkerHandle(
        worker_id=worker_id,
        worker_key=worker_key,
        endpoint=endpoint,
        auth_token=auth_token,
        status=status,
        backend_name=backend_name,
        last_used_at=lifecycle.last_used_at,
        created_at=lifecycle.created_at,
        last_started_at=lifecycle.last_started_at,
        startup_count=lifecycle.startup_count,
        failure_count=lifecycle.failure_count,
        failure_reason=lifecycle.failure_reason,
        debug_metadata=debug_metadata,
    )


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
    reapplying: bool = False,
) -> WorkerLifecycleState:
    """Return lifecycle fields for one ensure attempt before backend-specific startup IO.

    A worker reads as starting while it restarts, and while ``reapplying`` its definition without a restart, as
    Kubernetes does for every ensure that misses its ready cache before it waits for the Deployment's rollout.
    """
    return replace(
        state,
        last_used_at=now,
        status="starting" if should_restart or reapplying else state.status,
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


def mark_worker_idle(state: WorkerLifecycleState) -> WorkerLifecycleState:
    """Return lifecycle fields for one worker whose persisted state is being retained."""
    return replace(state, status="idle", failure_reason=None)


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
