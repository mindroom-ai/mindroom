"""Shared worker observability response models."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from mindroom.workers.models import WorkerHandle


__all__ = [
    "WorkerCleanupResponse",
    "WorkerListResponse",
    "WorkerResponse",
    "serialize_worker_response",
]


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


def serialize_worker_response(worker: WorkerHandle) -> WorkerResponse:
    """Serialize a WorkerHandle without exposing its auth token."""
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
