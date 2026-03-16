"""Sandbox runner worker preparation and lease helpers."""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException
from loguru import logger

from mindroom.api import sandbox_exec
from mindroom.tool_system import sandbox_proxy
from mindroom.tool_system.worker_routing import visible_agent_state_roots_for_worker_key, worker_dir_name
from mindroom.workers.backend import WorkerBackendError
from mindroom.workers.backends.local import (
    LocalWorkerStatePaths,
    ensure_local_worker_state_locked,
    get_local_worker_manager,
    local_worker_state_paths_for_root,
    local_worker_state_paths_from_handle,
)
from mindroom.workers.models import WorkerHandle, WorkerSpec

if TYPE_CHECKING:
    from mindroom.constants import RuntimePaths

MAX_LEASE_TTL_SECONDS = 3600
DEFAULT_LEASE_TTL_SECONDS = 60


@dataclass
class CredentialLease:
    """In-memory lease for short-lived credential overrides."""

    lease_id: str
    tool_name: str
    function_name: str
    credential_overrides: dict[str, Any]
    expires_at: float
    uses_remaining: int


# NOTE: In-process dict — leases are not shared across multiple uvicorn workers.
# The sandbox runner must be deployed with a single worker for lease correctness.
_LEASES_BY_ID: dict[str, CredentialLease] = {}
_LEASES_LOCK = threading.Lock()


@dataclass(frozen=True)
class PreparedWorkerRequest:
    """Prepared worker state reused across validation and dispatch."""

    handle: WorkerHandle
    paths: LocalWorkerStatePaths
    runtime_overrides: dict[str, object]


class WorkerRequestPreparationError(ValueError):
    """Raised when one worker-backed execute request cannot be prepared."""


def bounded_ttl_seconds(raw_ttl_seconds: int) -> int:
    """Clamp a requested lease TTL to the supported range."""
    return max(1, min(MAX_LEASE_TTL_SECONDS, raw_ttl_seconds))


def bounded_max_uses(raw_max_uses: int) -> int:
    """Clamp a requested lease usage count to the supported range."""
    return max(1, min(10, raw_max_uses))


def cleanup_expired_leases(now: float) -> None:
    """Remove expired in-memory credential leases."""
    expired_ids = [lease_id for lease_id, lease in _LEASES_BY_ID.items() if lease.expires_at <= now]
    for lease_id in expired_ids:
        _LEASES_BY_ID.pop(lease_id, None)


def create_credential_lease(
    *,
    tool_name: str,
    function_name: str,
    credential_overrides: dict[str, Any],
    ttl_seconds: int,
    max_uses: int,
) -> CredentialLease:
    """Create and store one bounded short-lived credential lease."""
    now = time.time()
    lease = CredentialLease(
        lease_id=secrets.token_urlsafe(24),
        tool_name=tool_name,
        function_name=function_name,
        credential_overrides=dict(credential_overrides),
        expires_at=now + bounded_ttl_seconds(ttl_seconds),
        uses_remaining=bounded_max_uses(max_uses),
    )
    with _LEASES_LOCK:
        cleanup_expired_leases(now)
        _LEASES_BY_ID[lease.lease_id] = lease
    return lease


def consume_credential_lease(
    lease_id: str,
    *,
    tool_name: str,
    function_name: str,
) -> dict[str, object]:
    """Consume one lease use and return its credential overrides."""
    now = time.time()
    with _LEASES_LOCK:
        cleanup_expired_leases(now)
        lease = _LEASES_BY_ID.get(lease_id)
        if lease is None:
            raise HTTPException(status_code=400, detail="Credential lease is invalid or expired.")
        if lease.tool_name != tool_name or lease.function_name != function_name:
            raise HTTPException(status_code=400, detail="Credential lease does not match tool/function.")

        lease.uses_remaining -= 1
        if lease.uses_remaining <= 0:
            _LEASES_BY_ID.pop(lease_id, None)

    return dict(lease.credential_overrides)


def prepare_worker(
    worker_key: str,
    runtime_paths: RuntimePaths,
    *,
    runner_token: str | None = None,
) -> WorkerHandle:
    """Ensure a worker is ready and return its handle."""
    dedicated_worker_key = sandbox_exec.runner_dedicated_worker_key(runtime_paths)
    if dedicated_worker_key is not None:
        if worker_key != dedicated_worker_key:
            msg = f"Dedicated sandbox worker is pinned to '{dedicated_worker_key}' but received '{worker_key}'."
            raise WorkerBackendError(msg)
        dedicated_root = sandbox_exec.runner_dedicated_worker_root(runtime_paths)
        if dedicated_root is None:
            msg = "Dedicated sandbox worker requires a configured worker root."
            raise WorkerBackendError(msg)
        paths = local_worker_state_paths_for_root(dedicated_root)
        try:
            ensure_local_worker_state_locked(worker_key, paths)
        except Exception as exc:
            failure_reason = f"Failed to initialize dedicated worker '{worker_key}': {exc}"
            raise WorkerBackendError(failure_reason) from exc
        now = time.time()
        return WorkerHandle(
            worker_id=worker_dir_name(worker_key),
            worker_key=worker_key,
            endpoint="/api/sandbox-runner/execute",
            auth_token=runner_token or sandbox_proxy.sandbox_proxy_config(runtime_paths).proxy_token,
            status="ready",
            backend_name="dedicated_sandbox_runner",
            last_used_at=now,
            created_at=now,
            last_started_at=now,
            startup_count=1,
            debug_metadata={
                "state_root": str(paths.root),
                "api_root": "/api/sandbox-runner",
            },
        )
    return get_local_worker_manager(runtime_paths).ensure_worker(WorkerSpec(worker_key))


def normalize_request_worker_key(
    worker_key: str | None,
    runtime_paths: RuntimePaths,
) -> str | None:
    """Fill in the pinned worker key for dedicated worker pods when omitted."""
    return worker_key or sandbox_exec.runner_dedicated_worker_key(runtime_paths)


def resolve_worker_base_dir(
    paths: LocalWorkerStatePaths,
    storage_root: Path,
    worker_key: str,
    requested_base_dir: object | None,
) -> Path:
    """Resolve the effective base_dir inside shared storage or the worker root."""
    shared_root = storage_root.resolve()
    if requested_base_dir is None:
        return paths.workspace.resolve()
    if not isinstance(requested_base_dir, str):
        msg = "base_dir must be a string path."
        raise TypeError(msg)

    visible_agent_roots = visible_agent_state_roots_for_worker_key(storage_root, worker_key)
    raw_path = Path(requested_base_dir).expanduser()
    if raw_path.is_absolute():
        candidate = raw_path.resolve()
    elif visible_agent_roots:
        candidate = (shared_root / raw_path).resolve()
    else:
        msg = f"base_dir requires a resolved worker key with visible agent roots: {worker_key}"
        raise ValueError(msg)

    allowed_roots = (paths.root.resolve(), *visible_agent_roots)
    if not any(candidate.is_relative_to(root) for root in allowed_roots):
        msg = f"base_dir must stay inside the allowed agent roots or worker root: {requested_base_dir}"
        raise ValueError(msg)

    return candidate


def ready_runtime_overrides(runtime_overrides: dict[str, object] | None) -> dict[str, object] | None:
    """Materialize runtime override paths before tool execution."""
    if runtime_overrides is None:
        return None

    base_dir = runtime_overrides.get("base_dir")
    if isinstance(base_dir, Path):
        base_dir.mkdir(parents=True, exist_ok=True)
    return runtime_overrides


def prepare_worker_request(
    *,
    worker_key: str | None,
    tool_init_overrides: dict[str, object],
    runtime_paths: RuntimePaths,
    runner_token: str | None = None,
) -> PreparedWorkerRequest:
    """Prepare one worker-backed request for execution."""
    if worker_key is None:
        msg = "worker_key is required for worker-backed sandbox execution."
        raise WorkerRequestPreparationError(msg)

    try:
        worker_handle = prepare_worker(worker_key, runtime_paths, runner_token=runner_token)
    except WorkerBackendError as exc:
        logger.opt(exception=True).warning("Sandbox worker initialization failed", worker_key=worker_key)
        raise WorkerRequestPreparationError(str(exc)) from exc

    paths = local_worker_state_paths_from_handle(worker_handle)
    try:
        runtime_overrides = {
            "base_dir": resolve_worker_base_dir(
                paths,
                sandbox_exec.runner_storage_root(runtime_paths),
                worker_key,
                tool_init_overrides.get("base_dir"),
            ),
        }
    except (TypeError, ValueError) as exc:
        raise WorkerRequestPreparationError(str(exc)) from exc

    return PreparedWorkerRequest(
        handle=worker_handle,
        paths=paths,
        runtime_overrides=runtime_overrides,
    )


def resolve_prepared_worker_request(
    *,
    worker_key: str | None,
    tool_init_overrides: dict[str, object],
    runtime_paths: RuntimePaths,
    prepared_worker: PreparedWorkerRequest | None,
    runner_token: str | None = None,
) -> PreparedWorkerRequest | None:
    """Reuse or prepare worker state for one request."""
    if worker_key is None:
        return None
    return prepared_worker or prepare_worker_request(
        worker_key=worker_key,
        tool_init_overrides=tool_init_overrides,
        runtime_paths=runtime_paths,
        runner_token=runner_token,
    )


def record_worker_failure(
    worker_key: str | None,
    error: str,
    runtime_paths: RuntimePaths,
) -> None:
    """Record one subprocess failure against the local worker manager."""
    if worker_key is not None and not sandbox_exec.runner_uses_dedicated_worker(runtime_paths):
        get_local_worker_manager(runtime_paths).record_failure(worker_key, error)
