"""Shared helpers for dedicated worker backend runtime and mount planning."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom.constants import RuntimePaths, runtime_env_source_path
from mindroom.credentials import SHARED_CREDENTIALS_PATH_ENV
from mindroom.tool_system.worker_routing import resolved_worker_key_scope, visible_state_roots_for_worker_key
from mindroom.workers.backend import WorkerBackendError
from mindroom.workspaces import copy_validated_local_file_to_root, validate_local_copy_source_path

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping


@dataclass(frozen=True, slots=True)
class ScopedVisibleStateRoot:
    """One durable state root that a dedicated worker may see."""

    local_path: Path
    worker_visible_path: Path


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
