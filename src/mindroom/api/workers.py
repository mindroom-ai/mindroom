"""Worker lifecycle and observability endpoints for the primary runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from mindroom.tool_system.sandbox_proxy import sandbox_proxy_config
from mindroom.workers.runtime import get_primary_worker_manager, primary_worker_backend_available

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths
    from mindroom.workers.manager import WorkerManager
    from mindroom.workers.models import WorkerHandle


class WorkerResponse(BaseModel):
    """Serialized worker metadata for API responses."""

    worker_id: str
    worker_key: str
    endpoint: str
    status: str
    backend_name: str
    last_used_at: float
    created_at: float
    last_started_at: float | None = None
    expires_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None
    debug_metadata: dict[str, str] = Field(default_factory=dict)


class WorkerListResponse(BaseModel):
    """List of known workers."""

    workers: list[WorkerResponse]


class WorkerCleanupResponse(BaseModel):
    """Result of one cleanup pass."""

    idle_timeout_seconds: float
    cleaned_workers: list[WorkerResponse]


router = APIRouter(prefix="/api/workers", tags=["workers"])


def _serialize_worker(worker: WorkerHandle) -> WorkerResponse:
    return WorkerResponse(
        worker_id=worker.worker_id,
        worker_key=worker.worker_key,
        endpoint=worker.endpoint,
        status=worker.status,
        backend_name=worker.backend_name,
        last_used_at=worker.last_used_at,
        created_at=worker.created_at,
        last_started_at=worker.last_started_at,
        expires_at=worker.expires_at,
        startup_count=worker.startup_count,
        failure_count=worker.failure_count,
        failure_reason=worker.failure_reason,
        debug_metadata=worker.debug_metadata,
    )


def _request_runtime_paths(request: Request) -> RuntimePaths:
    from mindroom.api.main import api_runtime_paths  # noqa: PLC0415

    return api_runtime_paths(request)


def _worker_manager(request: Request) -> WorkerManager:
    runtime_paths = _request_runtime_paths(request)
    proxy_config = sandbox_proxy_config(runtime_paths)
    if not primary_worker_backend_available(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
    ):
        raise HTTPException(status_code=503, detail="Worker backend is not configured.")
    return get_primary_worker_manager(
        runtime_paths,
        proxy_url=proxy_config.proxy_url,
        proxy_token=proxy_config.proxy_token,
        storage_root=runtime_paths.storage_root,
    )


@router.get("", response_model=WorkerListResponse)
async def list_workers(request: Request, include_idle: bool = True) -> WorkerListResponse:
    """List known workers from the configured primary-runtime backend."""
    worker_manager = _worker_manager(request)
    workers = [_serialize_worker(worker) for worker in worker_manager.list_workers(include_idle=include_idle)]
    return WorkerListResponse(workers=workers)


@router.post("/cleanup", response_model=WorkerCleanupResponse)
async def cleanup_idle_workers(request: Request) -> WorkerCleanupResponse:
    """Run one idle-worker cleanup pass on the configured backend."""
    worker_manager = _worker_manager(request)
    cleaned_workers = [_serialize_worker(worker) for worker in worker_manager.cleanup_idle_workers()]
    return WorkerCleanupResponse(
        idle_timeout_seconds=worker_manager.idle_timeout_seconds,
        cleaned_workers=cleaned_workers,
    )
