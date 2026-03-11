"""Worker lifecycle and observability endpoints for the primary runtime."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import mindroom.tool_system.sandbox_proxy as sandbox_proxy_module
from mindroom.workers.runtime import get_primary_worker_manager, primary_worker_backend_available

if TYPE_CHECKING:
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


def _worker_manager() -> WorkerManager:
    if not primary_worker_backend_available(
        proxy_url=sandbox_proxy_module._PROXY_URL,
        proxy_token=sandbox_proxy_module._PROXY_TOKEN,
    ):
        raise HTTPException(status_code=503, detail="Worker backend is not configured.")
    return get_primary_worker_manager(
        proxy_url=sandbox_proxy_module._PROXY_URL,
        proxy_token=sandbox_proxy_module._PROXY_TOKEN,
    )


@router.get("", response_model=WorkerListResponse)
async def list_workers(include_idle: bool = True) -> WorkerListResponse:
    """List known workers from the configured primary-runtime backend."""
    worker_manager = _worker_manager()
    workers = [_serialize_worker(worker) for worker in worker_manager.list_workers(include_idle=include_idle)]
    return WorkerListResponse(workers=workers)


@router.post("/cleanup", response_model=WorkerCleanupResponse)
async def cleanup_idle_workers() -> WorkerCleanupResponse:
    """Run one idle-worker cleanup pass on the configured backend."""
    worker_manager = _worker_manager()
    cleaned_workers = [_serialize_worker(worker) for worker in worker_manager.cleanup_idle_workers()]
    return WorkerCleanupResponse(
        idle_timeout_seconds=worker_manager.idle_timeout_seconds,
        cleaned_workers=cleaned_workers,
    )
