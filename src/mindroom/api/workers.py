"""Worker lifecycle and observability endpoints for the primary runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request

from mindroom.api import config_lifecycle
from mindroom.api.worker_responses import (
    SandboxWorkerCleanupResponse,
    SandboxWorkerListResponse,
    SandboxWorkerResponse,
    serialize_sandbox_worker_response,
)
from mindroom.workers.runtime import lease_configured_primary_worker_manager

if TYPE_CHECKING:
    from mindroom.workers.runtime import PrimaryWorkerManagerLease


__all__ = [
    "SandboxWorkerCleanupResponse",
    "SandboxWorkerListResponse",
    "SandboxWorkerResponse",
    "cleanup_idle_workers",
    "list_workers",
    "router",
]

router = APIRouter(prefix="/api/workers", tags=["workers"])


def _worker_manager_lease(request: Request) -> PrimaryWorkerManagerLease:
    runtime_config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    lease = lease_configured_primary_worker_manager(
        runtime_paths,
        runtime_config=runtime_config,
    )
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


@router.post("/cleanup", response_model=SandboxWorkerCleanupResponse)
async def cleanup_idle_workers(request: Request) -> SandboxWorkerCleanupResponse:
    """Run one idle-worker cleanup pass on the configured backend."""
    with _worker_manager_lease(request) as worker_manager:
        touch_live_workers = config_lifecycle.app_state(request.app).script_worker_keepalive
        if touch_live_workers is not None:
            touch_live_workers(worker_manager)
        cleaned_workers = [
            serialize_sandbox_worker_response(worker) for worker in worker_manager.cleanup_idle_workers()
        ]
    return SandboxWorkerCleanupResponse(
        idle_timeout_seconds=worker_manager.idle_timeout_seconds,
        cleaned_workers=cleaned_workers,
    )
