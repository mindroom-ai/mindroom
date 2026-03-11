"""Backend-neutral worker models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

WorkerStatus = Literal["starting", "ready", "idle", "failed"]


@dataclass(frozen=True, slots=True)
class WorkerSpec:
    """Stable worker request resolved from worker-routing semantics."""

    worker_key: str


@dataclass(frozen=True, slots=True)
class WorkerHandle:
    """Generic worker handle used by the execution layer."""

    worker_id: str
    worker_key: str
    endpoint: str
    auth_token: str | None
    status: WorkerStatus
    backend_name: str
    last_used_at: float
    created_at: float
    last_started_at: float | None = None
    expires_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None
    debug_metadata: dict[str, str] = field(default_factory=dict)


def worker_api_endpoint(handle: WorkerHandle, operation: Literal["execute", "leases", "workers", "cleanup"]) -> str:
    """Return the API endpoint for one worker operation."""
    api_root = handle.debug_metadata.get("api_root")
    if api_root is None:
        api_root = handle.endpoint.removesuffix("/execute").rstrip("/")

    if operation == "execute":
        return handle.endpoint
    if operation == "cleanup":
        return f"{api_root}/workers/cleanup"
    return f"{api_root}/{operation}"
