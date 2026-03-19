"""Shared helpers for dedicated worker backends."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom.constants import RuntimePaths, runtime_env_source_path, serialize_public_runtime_paths
from mindroom.credentials import SHARED_CREDENTIALS_PATH_ENV
from mindroom.tool_system.worker_routing import resolved_worker_key_scope, visible_state_roots_for_worker_key
from mindroom.workers.backend import WorkerBackendError
from mindroom.workspaces import copy_validated_local_file_to_root, validate_local_copy_source_path

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.workers.models import WorkerHandle, WorkerStatus


_DEDICATED_WORKER_RESERVED_ENV_NAMES = frozenset(
    {
        "HOME",
        "MINDROOM_CONFIG_PATH",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY",
        "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT",
        "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE",
        "MINDROOM_SANDBOX_RUNNER_MODE",
        "MINDROOM_SANDBOX_RUNNER_PORT",
        "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT",
        "MINDROOM_STORAGE_PATH",
        SHARED_CREDENTIALS_PATH_ENV,
    },
)
_WORKER_FILE_SECRET_ROOT = Path(".runtime") / "file-secrets"


@dataclass(frozen=True, slots=True)
class ScopedVisibleStateRoot:
    """One durable state root that a dedicated worker may see."""

    local_path: Path
    worker_visible_path: Path


@dataclass(frozen=True, slots=True)
class DedicatedWorkerLifecycleState:
    """Backend-neutral lifecycle fields persisted for one dedicated worker."""

    created_at: float
    last_used_at: float
    status: WorkerStatus
    last_started_at: float | None = None
    startup_count: int = 0
    failure_count: int = 0
    failure_reason: str | None = None


def initial_dedicated_worker_lifecycle_state(*, now: float) -> DedicatedWorkerLifecycleState:
    """Return the initial lifecycle state for a newly created dedicated worker."""
    return DedicatedWorkerLifecycleState(
        created_at=now,
        last_used_at=now,
        status="starting",
    )


def dedicated_worker_lifecycle_from_handle(
    handle: WorkerHandle | None,
    *,
    now: float,
) -> DedicatedWorkerLifecycleState:
    """Extract lifecycle fields from an existing worker handle or synthesize a new state."""
    if handle is None:
        return initial_dedicated_worker_lifecycle_state(now=now)
    return DedicatedWorkerLifecycleState(
        created_at=handle.created_at,
        last_used_at=handle.last_used_at,
        status=handle.status,
        last_started_at=handle.last_started_at,
        startup_count=handle.startup_count,
        failure_count=handle.failure_count,
        failure_reason=handle.failure_reason,
    )


def prepare_dedicated_worker_ensure_lifecycle(
    state: DedicatedWorkerLifecycleState,
    *,
    now: float,
    should_restart: bool,
    keep_starting_status: bool = False,
) -> DedicatedWorkerLifecycleState:
    """Return lifecycle fields for one ensure attempt before backend-specific startup IO."""
    return replace(
        state,
        last_used_at=now,
        status="starting" if should_restart or keep_starting_status else state.status,
        last_started_at=now if should_restart else state.last_started_at,
        startup_count=state.startup_count + int(should_restart),
        failure_reason=None,
    )


def touch_dedicated_worker_lifecycle(
    state: DedicatedWorkerLifecycleState,
    *,
    now: float,
) -> DedicatedWorkerLifecycleState:
    """Refresh last-used state and revive idle workers back to ready."""
    next_status = "ready" if state.status == "idle" else state.status
    return replace(state, last_used_at=now, status=next_status)


def mark_dedicated_worker_ready(
    state: DedicatedWorkerLifecycleState,
    *,
    now: float,
) -> DedicatedWorkerLifecycleState:
    """Return lifecycle fields for one worker that completed startup successfully."""
    return replace(
        state,
        last_used_at=now,
        status="ready",
        failure_reason=None,
    )


def mark_dedicated_worker_idle(
    state: DedicatedWorkerLifecycleState,
    *,
    now: float | None = None,
    update_last_used: bool = False,
) -> DedicatedWorkerLifecycleState:
    """Return lifecycle fields for one worker whose persisted state is being retained."""
    if not update_last_used:
        return replace(state, status="idle")
    if now is None:
        msg = "now is required when update_last_used is true."
        raise ValueError(msg)
    return replace(state, last_used_at=now, status="idle")


def mark_dedicated_worker_failed(
    state: DedicatedWorkerLifecycleState,
    *,
    now: float,
    failure_reason: str,
) -> DedicatedWorkerLifecycleState:
    """Return lifecycle fields for one worker that failed to start or execute."""
    return replace(
        state,
        last_used_at=now,
        status="failed",
        failure_count=state.failure_count + 1,
        failure_reason=failure_reason,
    )


def stable_signature_json(value: object) -> str:
    """Serialize one cache-signature value with stable JSON ordering."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def validate_dedicated_worker_extra_env(
    extra_env: Mapping[str, str],
    *,
    backend_name: str,
    extra_reserved_names: Iterable[str] = (),
) -> None:
    """Reject extra env that would override backend-owned dedicated-worker variables."""
    reserved_names = _DEDICATED_WORKER_RESERVED_ENV_NAMES.union(extra_reserved_names)
    invalid_names = sorted(name for name in extra_env if name in reserved_names)
    if not invalid_names:
        return
    invalid_names_text = ", ".join(invalid_names)
    msg = f"{backend_name} worker extra env cannot override reserved env vars: {invalid_names_text}"
    raise WorkerBackendError(msg)


def build_backend_config_signature(
    *,
    prefix_parts: tuple[str, ...],
    runtime_paths: RuntimePaths,
    json_values: tuple[object, ...] = (),
    suffix_parts: tuple[str, ...] = (),
) -> tuple[str, ...]:
    """Assemble one backend config cache signature with a shared runtime segment."""
    return (
        *prefix_parts,
        stable_signature_json(serialize_public_runtime_paths(runtime_paths)),
        *(stable_signature_json(value) for value in json_values),
        *suffix_parts,
    )


def build_dedicated_worker_runtime_paths(
    *,
    runtime_paths: RuntimePaths,
    backend_name: str,
    worker_key: str,
    config_path: Path,
    dedicated_root: Path,
    local_dedicated_root: Path,
    worker_port: int,
    shared_storage_root: str,
    extra_env: Mapping[str, str],
    required_existing_storage_root: Path | None = None,
) -> RuntimePaths:
    """Build worker-visible runtime paths for one dedicated worker."""
    validate_dedicated_worker_extra_env(extra_env, backend_name=backend_name)

    process_env = dict(runtime_paths.process_env)
    process_env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    env_file_values = dict(runtime_paths.env_file_values)
    env_file_values.pop("GOOGLE_APPLICATION_CREDENTIALS", None)

    if google_application_credentials := _worker_google_application_credentials_path(
        runtime_paths=runtime_paths,
        backend_name=backend_name,
        dedicated_root=dedicated_root,
        local_dedicated_root=local_dedicated_root,
        required_existing_storage_root=required_existing_storage_root,
    ):
        process_env["GOOGLE_APPLICATION_CREDENTIALS"] = google_application_credentials
    _rewrite_worker_file_env_values(
        runtime_paths=runtime_paths,
        backend_name=backend_name,
        dedicated_root=dedicated_root,
        local_dedicated_root=local_dedicated_root,
        process_env=process_env,
        env_file_values=env_file_values,
        required_existing_storage_root=required_existing_storage_root,
    )

    process_env.update(
        {
            "MINDROOM_SANDBOX_RUNNER_MODE": "true",
            "MINDROOM_SANDBOX_RUNNER_EXECUTION_MODE": "subprocess",
            "MINDROOM_SANDBOX_RUNNER_PORT": str(worker_port),
            "MINDROOM_CONFIG_PATH": str(config_path),
            "MINDROOM_STORAGE_PATH": str(dedicated_root),
            "MINDROOM_SANDBOX_SHARED_STORAGE_ROOT": shared_storage_root,
            SHARED_CREDENTIALS_PATH_ENV: f"{dedicated_root}/.shared_credentials",
            "MINDROOM_SANDBOX_DEDICATED_WORKER_KEY": worker_key,
            "MINDROOM_SANDBOX_DEDICATED_WORKER_ROOT": str(dedicated_root),
        },
    )
    process_env.update(extra_env)

    return RuntimePaths(
        config_path=config_path,
        config_dir=config_path.parent,
        env_path=config_path.parent / ".env",
        storage_root=dedicated_root.resolve(),
        process_env=MappingProxyType(process_env),
        env_file_values=MappingProxyType(env_file_values),
    )


def plan_scoped_visible_state_roots(
    *,
    worker_key: str,
    local_shared_storage_root: Path,
    worker_visible_shared_storage_root: Path,
    private_agent_names: frozenset[str] | None,
    allow_unknown_worker_key: bool,
) -> tuple[ScopedVisibleStateRoot, ...]:
    """Return the durable state roots a dedicated worker may mount by default."""
    scope = resolved_worker_key_scope(worker_key)
    if scope is None:
        if allow_unknown_worker_key:
            return ()
        msg = f"Unsupported worker key for scoped storage mounts: {worker_key}"
        raise WorkerBackendError(msg)

    if scope == "user_agent" and private_agent_names is None:
        msg = f"user_agent workers require explicit private-agent visibility: {worker_key}"
        raise WorkerBackendError(msg)

    effective_private_agent_names = private_agent_names or frozenset()
    worker_visible_roots = visible_state_roots_for_worker_key(
        worker_visible_shared_storage_root,
        worker_key,
        private_agent_names=effective_private_agent_names,
    )
    local_roots = visible_state_roots_for_worker_key(
        local_shared_storage_root,
        worker_key,
        private_agent_names=effective_private_agent_names,
    )
    if not worker_visible_roots or len(worker_visible_roots) != len(local_roots):
        msg = f"Unsupported worker key for scoped storage mounts: {worker_key}"
        raise WorkerBackendError(msg)

    for local_root in local_roots:
        local_root.mkdir(parents=True, exist_ok=True)

    return tuple(
        ScopedVisibleStateRoot(
            local_path=local_root,
            worker_visible_path=worker_visible_root,
        )
        for local_root, worker_visible_root in zip(local_roots, worker_visible_roots, strict=True)
    )


def validate_unique_worker_visible_paths(
    paths: Iterable[str | Path],
    *,
    worker_key: str,
    duplicate_label: str,
) -> None:
    """Fail closed when one mount plan maps multiple sources to the same target."""
    normalized_paths = [str(path) for path in paths]
    if len(normalized_paths) == len(set(normalized_paths)):
        return
    msg = f"Duplicate {duplicate_label} generated for worker key: {worker_key}"
    raise WorkerBackendError(msg)


def _worker_google_application_credentials_path(
    *,
    runtime_paths: RuntimePaths,
    backend_name: str,
    dedicated_root: Path,
    local_dedicated_root: Path,
    required_existing_storage_root: Path | None,
) -> str | None:
    """Return a worker-visible ADC path, copying the source into worker state when needed."""
    raw_value = runtime_paths.env_value("GOOGLE_APPLICATION_CREDENTIALS")
    if raw_value is None or not raw_value.strip():
        return None
    if required_existing_storage_root is not None and not required_existing_storage_root.exists():
        return None

    source_path = runtime_env_source_path(runtime_paths, "GOOGLE_APPLICATION_CREDENTIALS")
    if source_path is None or (not source_path.exists() and not source_path.is_symlink()):
        return None

    field_name = f"{backend_name} worker GOOGLE_APPLICATION_CREDENTIALS"
    try:
        resolved_source_path = validate_local_copy_source_path(source_path, field_name=field_name)
    except ValueError as exc:
        raise WorkerBackendError(str(exc)) from exc
    if not resolved_source_path.is_file():
        return None

    runtime_dir = local_dedicated_root / ".runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    try:
        copy_validated_local_file_to_root(
            resolved_source_path,
            destination_root=local_dedicated_root,
            destination_relative_path=Path(".runtime") / resolved_source_path.name,
            destination_field_name=f"{field_name} destination",
            destination_root_label="worker state root",
            mode=0o600,
        )
    except ValueError as exc:
        raise WorkerBackendError(str(exc)) from exc
    return str(dedicated_root / ".runtime" / resolved_source_path.name)


def _rewrite_worker_file_env_values(
    *,
    runtime_paths: RuntimePaths,
    backend_name: str,
    dedicated_root: Path,
    local_dedicated_root: Path,
    process_env: dict[str, str],
    env_file_values: dict[str, str],
    required_existing_storage_root: Path | None,
) -> None:
    for env_name in sorted({*process_env, *env_file_values}):
        if not env_name.endswith("_FILE"):
            continue
        worker_visible_path = _worker_file_env_path(
            runtime_paths=runtime_paths,
            env_name=env_name,
            backend_name=backend_name,
            dedicated_root=dedicated_root,
            local_dedicated_root=local_dedicated_root,
            required_existing_storage_root=required_existing_storage_root,
        )
        if worker_visible_path is None:
            continue
        process_env[env_name] = worker_visible_path
        env_file_values.pop(env_name, None)


def _worker_file_env_path(
    *,
    runtime_paths: RuntimePaths,
    env_name: str,
    backend_name: str,
    dedicated_root: Path,
    local_dedicated_root: Path,
    required_existing_storage_root: Path | None,
) -> str | None:
    raw_value = runtime_paths.env_value(env_name)
    if raw_value is None or not raw_value.strip():
        return None
    if required_existing_storage_root is not None and not required_existing_storage_root.exists():
        return None

    source_path = runtime_env_source_path(runtime_paths, env_name)
    if source_path is None or (not source_path.exists() and not source_path.is_symlink()):
        return None

    field_name = f"{backend_name} worker {env_name}"
    try:
        resolved_source_path = validate_local_copy_source_path(source_path, field_name=field_name)
    except ValueError as exc:
        raise WorkerBackendError(str(exc)) from exc
    if not resolved_source_path.is_file():
        return None

    destination_relative_path = _WORKER_FILE_SECRET_ROOT / env_name / resolved_source_path.name
    try:
        copy_validated_local_file_to_root(
            resolved_source_path,
            destination_root=local_dedicated_root,
            destination_relative_path=destination_relative_path,
            destination_field_name=f"{field_name} destination",
            destination_root_label="worker state root",
            mode=0o600,
        )
    except ValueError as exc:
        raise WorkerBackendError(str(exc)) from exc
    return str(dedicated_root / destination_relative_path)
