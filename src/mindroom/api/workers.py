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
from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config
from mindroom.workers.runtime import (
    get_primary_worker_manager,
    primary_worker_backend_available,
    primary_worker_backend_name,
    serialized_kubernetes_worker_validation_snapshot,
)

if TYPE_CHECKING:
    from mindroom.workers.manager import WorkerManager


__all__ = [
    "SandboxWorkerCleanupResponse",
    "SandboxWorkerListResponse",
    "SandboxWorkerResponse",
    "cleanup_idle_workers",
    "list_workers",
    "router",
]

router = APIRouter(prefix="/api/workers", tags=["workers"])


def _worker_manager(request: Request) -> WorkerManager:
    runtime_config, runtime_paths = config_lifecycle.read_committed_runtime_config(request)
    proxy_config = sandbox_proxy_config(runtime_paths)
    if not primary_worker_backend_available(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
    ):
        raise HTTPException(status_code=503, detail="Worker backend is not configured.")
    kubernetes_tool_validation_snapshot: dict[str, dict[str, object]] | None = None
    if primary_worker_backend_name(runtime_paths) == "kubernetes":
        kubernetes_tool_validation_snapshot = serialized_kubernetes_worker_validation_snapshot(
            runtime_paths,
            runtime_config=runtime_config,
        )
    return get_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=runtime_paths.storage_root,
        kubernetes_tool_validation_snapshot=kubernetes_tool_validation_snapshot,
        worker_grantable_credentials=runtime_config.get_worker_grantable_credentials(),
    )


@router.get("", response_model=SandboxWorkerListResponse)
async def list_workers(request: Request, include_idle: bool = True) -> SandboxWorkerListResponse:
    """List known workers from the configured primary-runtime backend."""
    worker_manager = _worker_manager(request)
    workers = [
        serialize_sandbox_worker_response(worker) for worker in worker_manager.list_workers(include_idle=include_idle)
    ]
    return SandboxWorkerListResponse(workers=workers)


@router.post("/cleanup", response_model=SandboxWorkerCleanupResponse)
async def cleanup_idle_workers(request: Request) -> SandboxWorkerCleanupResponse:
    """Run one idle-worker cleanup pass on the configured backend."""
    worker_manager = _worker_manager(request)
    cleaned_workers = [serialize_sandbox_worker_response(worker) for worker in worker_manager.cleanup_idle_workers()]
    return SandboxWorkerCleanupResponse(
        idle_timeout_seconds=worker_manager.idle_timeout_seconds,
        cleaned_workers=cleaned_workers,
    )
