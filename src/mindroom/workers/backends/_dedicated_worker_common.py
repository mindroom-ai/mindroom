"""Shared helpers for dedicated worker backends."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

from mindroom.constants import (
    RuntimePaths,
    deserialize_runtime_paths,
    runtime_env_source_path,
    serialize_public_runtime_paths,
)
from mindroom.runtime_env_policy import SANDBOX_RUNTIME_ENV_BY_KEY, SHARED_CREDENTIALS_PATH_ENV
from mindroom.tool_system.worker_routing import (
    resolved_worker_key_scope,
    visible_state_roots_for_worker_key,
    worker_key_agent_name,
)
from mindroom.workers.backend import WorkerBackendError
from mindroom.workspaces import copy_validated_local_file_to_root, validate_local_copy_source_path

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from mindroom.agent_policy import ResolvedAgentPolicy

__all__ = [
    "ScopedVisibleStateRoot",
    "build_backend_config_signature",
    "build_dedicated_worker_runtime_paths",
    "plan_scoped_visible_state_roots",
    "stable_signature_json",
    "validate_dedicated_worker_extra_env",
    "validate_private_user_agent_visibility",
    "validate_unique_worker_visible_paths",
]


_DEDICATED_WORKER_RESERVED_ENV_NAMES = frozenset(
    {
        "HOME",
        "MINDROOM_CONFIG_PATH",
        SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"],
        SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_execution_mode"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"],
        SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"],
        SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"],
        "MINDROOM_STORAGE_PATH",
        SHARED_CREDENTIALS_PATH_ENV,
    },
)
_DEDICATED_WORKER_BLOCKED_FILE_ENV_BASE_NAMES = frozenset(
    {
        "GITHUB_TOKEN",
        "MINDROOM_API_KEY",
        "MINDROOM_LOCAL_CLIENT_SECRET",
    },
)
_SOURCE_PROCESS_FILE_SECRET_SUFFIXES = (
    "_API_KEY_FILE",
    "_CREDENTIAL_FILE",
    "_CREDENTIALS_FILE",
    "_PASSWORD_FILE",
    "_SECRET_FILE",
    "_SERVICE_ACCOUNT_FILE",
    "_TOKEN_FILE",
)
_WORKER_FILE_SECRET_ROOT = Path(".runtime") / "file-secrets"


@dataclass(frozen=True, slots=True)
class ScopedVisibleStateRoot:
    """One durable state root that a dedicated worker may see."""

    local_path: Path
    worker_visible_path: Path


def validate_private_user_agent_visibility(
    *,
    worker_key: str,
    private_agent_names: frozenset[str] | None,
    resolved_agent_policies: dict[str, ResolvedAgentPolicy],
) -> None:
    """Reject stale user-agent visibility snapshots for currently private agents."""
    if resolved_worker_key_scope(worker_key) != "user_agent":
        return
    agent_name = worker_key_agent_name(worker_key)
    if agent_name is None:
        return
    policy = resolved_agent_policies.get(agent_name)
    if policy is None or not policy.is_private or policy.effective_execution_scope != "user_agent":
        return
    if private_agent_names is not None and agent_name in private_agent_names:
        return
    msg = f"user_agent worker key targets a private agent missing from explicit private-agent visibility: {worker_key}"
    raise WorkerBackendError(msg)


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


def _blocked_worker_file_env_names(runtime_paths: RuntimePaths) -> frozenset[str]:
    return frozenset(
        env_name
        for env_name in {*runtime_paths.process_env, *runtime_paths.env_file_values}
        if env_name.endswith("_FILE")
        and env_name.removesuffix("_FILE") in _DEDICATED_WORKER_BLOCKED_FILE_ENV_BASE_NAMES
    )


def _protected_dedicated_worker_env_names(runtime_paths: RuntimePaths) -> frozenset[str]:
    protected_names = set(_blocked_worker_file_env_names(runtime_paths))
    protected_names.update(
        env_name
        for env_name in {*runtime_paths.process_env, *runtime_paths.env_file_values}
        if env_name.endswith("_FILE")
    )
    if runtime_paths.env_value("GOOGLE_APPLICATION_CREDENTIALS"):
        protected_names.add("GOOGLE_APPLICATION_CREDENTIALS")
    return frozenset(protected_names)


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
) -> RuntimePaths:
    """Build worker-visible runtime paths for one dedicated worker."""
    validate_dedicated_worker_extra_env(
        extra_env,
        backend_name=backend_name,
        extra_reserved_names=_protected_dedicated_worker_env_names(runtime_paths),
    )

    public_runtime_paths = deserialize_runtime_paths(serialize_public_runtime_paths(runtime_paths))
    process_env = dict(public_runtime_paths.process_env)
    process_env.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    env_file_values = dict(public_runtime_paths.env_file_values)
    env_file_values.pop("GOOGLE_APPLICATION_CREDENTIALS", None)
    for blocked_env_name in _blocked_worker_file_env_names(runtime_paths):
        process_env.pop(blocked_env_name, None)
        env_file_values.pop(blocked_env_name, None)

    if google_application_credentials := _worker_google_application_credentials_path(
        runtime_paths=runtime_paths,
        backend_name=backend_name,
        dedicated_root=dedicated_root,
        local_dedicated_root=local_dedicated_root,
    ):
        process_env["GOOGLE_APPLICATION_CREDENTIALS"] = google_application_credentials
    _rewrite_worker_file_env_values(
        runtime_paths=runtime_paths,
        backend_name=backend_name,
        dedicated_root=dedicated_root,
        local_dedicated_root=local_dedicated_root,
        source_process_env=runtime_paths.process_env,
        process_env=process_env,
        env_file_values=env_file_values,
    )

    process_env.update(
        {
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_execution_mode"]: "subprocess",
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"]: str(worker_port),
            "MINDROOM_CONFIG_PATH": str(config_path),
            "MINDROOM_STORAGE_PATH": str(dedicated_root),
            SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"]: shared_storage_root,
            SHARED_CREDENTIALS_PATH_ENV: f"{dedicated_root}/.shared_credentials",
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_key"]: worker_key,
            SANDBOX_RUNTIME_ENV_BY_KEY["dedicated_worker_root"]: str(dedicated_root),
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
    resolved_agent_policies: dict[str, ResolvedAgentPolicy] | None = None,
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
    if resolved_agent_policies is not None:
        validate_private_user_agent_visibility(
            worker_key=worker_key,
            private_agent_names=private_agent_names,
            resolved_agent_policies=resolved_agent_policies,
        )

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
) -> str | None:
    """Return a worker-visible ADC path, copying the source into worker state when needed."""
    raw_value = runtime_paths.env_value("GOOGLE_APPLICATION_CREDENTIALS")
    if raw_value is None or not raw_value.strip():
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
    source_process_env: Mapping[str, str],
    process_env: dict[str, str],
    env_file_values: dict[str, str],
) -> None:
    blocked_file_env_names = _blocked_worker_file_env_names(runtime_paths)
    source_process_file_env_names = {
        env_name for env_name in source_process_env if env_name.endswith(_SOURCE_PROCESS_FILE_SECRET_SUFFIXES)
    }
    candidate_env_names = {
        *source_process_file_env_names,
        *process_env,
        *env_file_values,
        *runtime_paths.env_file_values,
    }
    for env_name in sorted(candidate_env_names):
        if not env_name.endswith("_FILE"):
            continue
        if env_name in blocked_file_env_names:
            continue
        process_env.pop(env_name, None)
        env_file_values.pop(env_name, None)
        worker_visible_path = _worker_file_env_path(
            runtime_paths=runtime_paths,
            env_name=env_name,
            backend_name=backend_name,
            dedicated_root=dedicated_root,
            local_dedicated_root=local_dedicated_root,
        )
        if worker_visible_path is None:
            continue
        process_env[env_name] = worker_visible_path


def _worker_file_env_path(
    *,
    runtime_paths: RuntimePaths,
    env_name: str,
    backend_name: str,
    dedicated_root: Path,
    local_dedicated_root: Path,
) -> str | None:
    raw_value = runtime_paths.env_value(env_name)
    if raw_value is None or not raw_value.strip():
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
