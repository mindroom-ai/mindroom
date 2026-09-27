"""Shared helpers for dedicated worker backends."""

from __future__ import annotations

import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, cast

from mindroom.agent_policy import build_agent_policy_seeds, resolve_agent_policy_index
from mindroom.constants import RuntimePaths, deserialize_runtime_paths, serialize_public_runtime_paths
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.private_storage_paths import private_scope_alias_paths
from mindroom.runtime_env_policy import CONTROL_STATE_PATH_ENV, SANDBOX_RUNTIME_ENV_BY_KEY, SHARED_CREDENTIALS_PATH_ENV
from mindroom.tool_system.worker_routing import (
    WORKER_SHARED_CREDENTIALS_DIRNAME,
    private_instance_scope_root_path,
    private_instances_root_path,
    resolved_worker_key_scope,
    shared_storage_root,
    visible_workspace_roots,
    worker_key_agent_name,
)
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping
    from pathlib import Path

    from mindroom.agent_policy import ResolvedAgentPolicy
    from mindroom.tool_system.worker_routing import WorkerScope

__all__ = [
    "ScopedWorkspaceMount",
    "build_backend_config_signature",
    "build_dedicated_worker_runtime_paths",
    "plan_scoped_workspace_mounts",
    "resolve_state_scope_worker_key",
    "resolved_agent_policies_from_config_data",
    "stable_signature_json",
    "validate_dedicated_worker_extra_env",
    "validate_private_user_agent_visibility",
    "validate_unique_worker_visible_paths",
]

logger = get_logger(__name__)

_DEDICATED_WORKER_RESERVED_ENV_NAMES = frozenset(
    {
        "HOME",
        CONTROL_STATE_PATH_ENV,
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


@dataclass(frozen=True, slots=True)
class ScopedWorkspaceMount:
    """One existing workspace a dedicated worker mounts writable at its canonical path."""

    local_path: Path
    worker_visible_path: Path


def resolve_state_scope_worker_key(worker_key: str, state_scope_worker_key: str | None) -> str:
    """Resolve a narrower run key to the durable worker scope it is allowed to mount."""
    if state_scope_worker_key is None or state_scope_worker_key == worker_key:
        return worker_key
    worker_parts = worker_key.split(":")
    state_scope_parts = state_scope_worker_key.split(":")
    if (
        len(worker_parts) == len(state_scope_parts) + 1
        and worker_parts[:-2] == state_scope_parts[:-1]
        and worker_parts[-1] == state_scope_parts[-1]
    ):
        return state_scope_worker_key
    msg = f"Worker state scope does not own the requested worker key: {worker_key}"
    raise WorkerBackendError(msg)


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


def _protected_dedicated_worker_env_names(runtime_paths: RuntimePaths) -> frozenset[str]:
    protected_names = {
        env_name
        for env_name in {*runtime_paths.process_env, *runtime_paths.env_file_values}
        if env_name.endswith("_FILE")
    }
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


def _default_worker_scope_from_config_data(config_data: Mapping[str, object]) -> WorkerScope | None:
    raw_defaults = config_data.get("defaults")
    if not isinstance(raw_defaults, dict):
        return None
    raw_worker_scope = cast("dict[str, object]", raw_defaults).get("worker_scope")
    if isinstance(raw_worker_scope, str) and raw_worker_scope in {
        "shared",
        "user",
        "user_agent",
    }:
        return cast("WorkerScope", raw_worker_scope)
    return None


def resolved_agent_policies_from_config_data(
    config_data: Mapping[str, object],
) -> dict[str, ResolvedAgentPolicy]:
    """Resolve worker isolation policy from raw config data."""
    raw_agents = config_data.get("agents")
    if not isinstance(raw_agents, dict):
        return {}

    agent_mappings = {
        agent_name: cast("dict[str, object]", raw_agent)
        for agent_name, raw_agent in raw_agents.items()
        if isinstance(agent_name, str) and isinstance(raw_agent, dict)
    }
    if not agent_mappings:
        return {}

    seeds = build_agent_policy_seeds(
        agent_mappings,
        default_worker_scope=_default_worker_scope_from_config_data(config_data),
    )
    return resolve_agent_policy_index(seeds).policies


def build_dedicated_worker_runtime_paths(
    *,
    runtime_paths: RuntimePaths,
    backend_name: str,
    worker_key: str,
    config_path: Path,
    dedicated_root: Path,
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
    env_file_values = dict(public_runtime_paths.env_file_values)

    process_env.update(
        {
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_mode"]: "true",
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_execution_mode"]: "forkserver",
            SANDBOX_RUNTIME_ENV_BY_KEY["runner_port"]: str(worker_port),
            "MINDROOM_CONFIG_PATH": str(config_path),
            "MINDROOM_STORAGE_PATH": str(dedicated_root),
            SANDBOX_RUNTIME_ENV_BY_KEY["shared_storage_root"]: shared_storage_root,
            SHARED_CREDENTIALS_PATH_ENV: f"{dedicated_root}/{WORKER_SHARED_CREDENTIALS_DIRNAME}",
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


def _scoped_workspaces(
    *,
    worker_key: str,
    storage_root: Path,
    private_agent_names: frozenset[str] | None,
    resolved_agent_policies: dict[str, ResolvedAgentPolicy] | None,
) -> tuple[Path, ...]:
    scope = resolved_worker_key_scope(worker_key)
    if scope is None:
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
    try:
        workspaces = visible_workspace_roots(
            storage_root,
            worker_key,
            resolved_agent_policies or {},
            private_agent_names=private_agent_names or frozenset(),
        )
    except ValueError as exc:
        raise WorkerBackendError(str(exc)) from exc
    if not workspaces and scope != "user":
        msg = f"Unsupported worker key for scoped storage mounts: {worker_key}"
        raise WorkerBackendError(msg)
    return workspaces


def plan_scoped_workspace_mounts(
    *,
    worker_key: str,
    local_shared_storage_root: Path,
    worker_visible_shared_storage_root: Path,
    private_agent_names: frozenset[str] | None,
    resolved_agent_policies: dict[str, ResolvedAgentPolicy] | None = None,
    create_shared: bool = False,
) -> tuple[ScopedWorkspaceMount, ...]:
    """Return the workspaces one dedicated worker mounts writable, at their canonical paths.

    A workspace is mounted only when it is a real directory reached without links
    from the storage root, because container runtimes create missing sources as
    root. ``create_shared`` first creates missing shared workspaces that way; private
    workspaces appear only after the primary writes their scope's identity record.
    Private mounts repeat at the verified legacy spellings of their scope.
    """
    storage_root = shared_storage_root(local_shared_storage_root)
    private_root = private_instances_root_path(storage_root)
    mounts: list[ScopedWorkspaceMount] = []
    for workspace in _scoped_workspaces(
        worker_key=worker_key,
        storage_root=storage_root,
        private_agent_names=private_agent_names,
        resolved_agent_policies=resolved_agent_policies,
    ):
        relative_path = workspace.relative_to(storage_root)
        create = create_shared and not workspace.is_relative_to(private_root)
        try:
            with open_directory_within_root(storage_root, relative_path, create=create):
                mounts.append(ScopedWorkspaceMount(workspace, worker_visible_shared_storage_root / relative_path))
        except FileNotFoundError:
            logger.info(
                "Not mounting a workspace that does not exist yet",
                worker_key=worker_key,
                workspace=str(relative_path),
            )
        except OSError as exc:
            if create:
                msg = f"Worker workspace must be a real directory below the storage root: {relative_path}"
                raise WorkerBackendError(msg) from exc
            logger.warning(
                "Not mounting a workspace reached through a link",
                worker_key=worker_key,
                workspace=str(relative_path),
            )
    canonical_scope = private_instance_scope_root_path(storage_root, worker_key)
    private_mounts = [mount for mount in mounts if mount.local_path.is_relative_to(canonical_scope)]
    if private_mounts:
        try:
            aliases = private_scope_alias_paths(storage_root, worker_key)
        except ValueError as exc:
            raise WorkerBackendError(str(exc)) from exc
        mounts.extend(
            ScopedWorkspaceMount(
                mount.local_path,
                worker_visible_shared_storage_root
                / alias.relative_to(storage_root)
                / mount.local_path.relative_to(canonical_scope),
            )
            for alias in aliases
            for mount in private_mounts
        )
    return tuple(mounts)


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
