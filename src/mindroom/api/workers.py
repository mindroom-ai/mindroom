"""Worker lifecycle and observability endpoints for the primary runtime."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from mindroom.api import config_lifecycle
from mindroom.api.computers import active_computer_worker_keys
from mindroom.api.worker_responses import (
    SandboxWorkerCleanupResponse,
    SandboxWorkerListResponse,
    SandboxWorkerResponse,
    serialize_sandbox_worker_response,
)
from mindroom.workers.backend import maintain_workers
from mindroom.workers.in_flight import touch_workers_in_flight
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from collections.abc import Callable

    from mindroom.workers.backend import WorkerBackend
    from mindroom.workers.models import WorkerMaintenanceResult
    from mindroom.workers.runtime import PrimaryWorkerManagerLease


__all__ = [
    "SandboxWorkerCleanupResponse",
    "SandboxWorkerListResponse",
    "SandboxWorkerResponse",
    "cleanup_idle_workers",
    "list_workers",
    "maintain_leased_workers",
    "router",
]

router = APIRouter(prefix="/api/workers", tags=["workers"])


def _worker_manager_lease(request: Request) -> PrimaryWorkerManagerLease:
    lease = lease_configured_primary_worker_manager(config_lifecycle.api_runtime_paths(request))
    if lease is None:
        raise HTTPException(status_code=503, detail="Worker backend is not configured.")
    return lease


@router.get("", response_model=SandboxWorkerListResponse)
async def list_workers(request: Request, include_idle: bool = True) -> SandboxWorkerListResponse:
    """List known workers from the configured primary-runtime backend."""
    with _worker_manager_lease(request) as worker_manager:
        workers = [
            serialize_sandbox_worker_response(worker)
            for worker in worker_manager.list_workers(include_idle=include_idle)
        ]
    return SandboxWorkerListResponse(workers=workers)


def maintain_leased_workers(
    manager: WorkerBackend,
    *,
    touch_live_workers: Callable[[WorkerBackend], None],
    computer_worker_keys: frozenset[str],
) -> WorkerMaintenanceResult:
    """Mark every worker still in use as active, then run the backend's maintenance pass on a leased manager.

    The pass cleans idle workers and, on Kubernetes, reconciles drifted pod templates.
    """
    touch_live_workers(manager)
    for worker_key in computer_worker_keys:
        manager.touch_worker(worker_key)
    touch_workers_in_flight(manager)
    return maintain_workers(manager)


@router.post("/cleanup", response_model=SandboxWorkerCleanupResponse)
async def cleanup_idle_workers(request: Request) -> SandboxWorkerCleanupResponse:
    """Run one worker maintenance pass on the configured backend, like the background cleanup does."""
    touch_live_workers = config_lifecycle.app_state(request.app).runtime.script_worker_keepalive
    # Computer sessions live on the app's event loop, so their keys are read here before the pass leaves it.
    computer_worker_keys = active_computer_worker_keys(request.app)

    def run_pass() -> SandboxWorkerCleanupResponse:
        with _worker_manager_lease(request) as worker_manager:
            maintenance = maintain_leased_workers(
                worker_manager,
                touch_live_workers=touch_live_workers,
                computer_worker_keys=computer_worker_keys,
            )
        return SandboxWorkerCleanupResponse(
            idle_timeout_seconds=worker_manager.idle_timeout_seconds,
            cleaned_workers=[serialize_sandbox_worker_response(worker) for worker in maintenance.cleaned],
        )

    return await asyncio.to_thread(run_pass)
