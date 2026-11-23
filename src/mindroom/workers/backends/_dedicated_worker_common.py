"""Shared helpers for dedicated worker backends."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from mindroom.constants import serialize_public_runtime_paths
from mindroom.logging_config import get_logger
from mindroom.path_confinement import open_directory_within_root
from mindroom.private_storage.alias_paths import private_scope_alias_paths
from mindroom.runtime_env_policy import CONTROL_STATE_PATH_ENV, SANDBOX_RUNTIME_ENV_BY_KEY, SHARED_CREDENTIALS_PATH_ENV
from mindroom.shared_directories.store import (
    load_shared_directories,
    shared_directory_access,
    shared_directory_relative_path,
)
from mindroom.tool_system.worker_routing import (
    agent_workspace_relative_path,
    normalize_worker_key_part,
    pinned_worker_kind,
    private_instance_scope_root_path,
    private_instances_root_path,
    resolved_worker_key_scope,
    shared_storage_root,
    visible_workspace_roots,
    worker_key_agent_name,
    worker_log_id,
)
from mindroom.workers.backend import WorkerBackendError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping
    from pathlib import Path

    from mindroom.agent_policy import ResolvedAgentPolicy
    from mindroom.constants import RuntimePaths
    from mindroom.workers.models import WorkerHandle, WorkerLaunchConfig, WorkerSandboxConfig

__all__ = [
    "ScopedWorkspaceMount",
    "SharedDirectoryMount",
    "agent_names_addressed_by_worker_key",
    "build_backend_config_signature",
    "normalize_sandbox_mount_dir",
    "plan_scoped_workspace_mounts",
    "plan_shared_directory_mounts",
    "resolve_sandbox_mount_target",
    "resolve_state_scope_worker_key",
    "retire_pinned_worker",
    "stable_json_digest",
    "stable_signature_json",
    "subagent_worker_expired",
    "tailnet_ssh_marked",
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
    """One existing workspace (or sandboxed subpath) a dedicated worker mounts at its canonical path."""

    local_path: Path
    worker_visible_path: Path
    read_only: bool = False
    ino: int | None = None


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
    msg = f"Worker state scope does not own the requested worker key: {worker_log_id(worker_key)}"
    raise WorkerBackendError(msg)


def subagent_worker_expired(handle: WorkerHandle, idle_timeout_seconds: float, *, now: float) -> bool:
    """Return whether ``handle`` is a subagent's own worker, running or stopped, unused for the idle timeout."""
    return pinned_worker_kind(handle.worker_key) == "subagent" and now - handle.last_used_at >= idle_timeout_seconds


def retire_pinned_worker(retire_locked: Callable[[str], None], worker_key: str) -> bool:
    """Remove a pinned worker under the lock its caller holds; a failure is logged so cleanup goes on."""
    try:
        retire_locked(worker_key)
    except WorkerBackendError:
        logger.warning("pinned_worker_retire_failed", worker=worker_log_id(worker_key), exc_info=True)
        return False
    return True


def validate_private_user_agent_visibility(
    *,
    worker_key: str,
    private_agent_names: frozenset[str] | None,
    resolved_agent_policies: Mapping[str, ResolvedAgentPolicy],
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
    msg = f"user_agent worker key targets a private agent missing from explicit private-agent visibility: {worker_log_id(worker_key)}"
    raise WorkerBackendError(msg)


def stable_signature_json(value: object) -> str:
    """Serialize one cache-signature value with stable JSON ordering."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def stable_json_digest(value: object) -> str:
    """Return the SHA-256 hex digest of one value's stable JSON serialization."""
    return hashlib.sha256(stable_signature_json(value).encode("utf-8")).hexdigest()


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


def _scoped_workspaces(
    *,
    worker_key: str,
    storage_root: Path,
    private_agent_names: frozenset[str] | None,
    resolved_agent_policies: Mapping[str, ResolvedAgentPolicy],
) -> tuple[Path, ...]:
    scope = resolved_worker_key_scope(worker_key)
    if scope is None:
        msg = f"Unsupported worker key for scoped storage mounts: {worker_log_id(worker_key)}"
        raise WorkerBackendError(msg)

    if scope == "user_agent" and private_agent_names is None:
        msg = f"user_agent workers require explicit private-agent visibility: {worker_log_id(worker_key)}"
        raise WorkerBackendError(msg)
    validate_private_user_agent_visibility(
        worker_key=worker_key,
        private_agent_names=private_agent_names,
        resolved_agent_policies=resolved_agent_policies,
    )
    workspaces = visible_workspace_roots(
        storage_root,
        worker_key,
        resolved_agent_policies,
        private_agent_names=private_agent_names or frozenset(),
    )
    if not workspaces and scope != "user":
        msg = f"Unsupported worker key for scoped storage mounts: {worker_log_id(worker_key)}"
        raise WorkerBackendError(msg)
    return workspaces


def normalize_sandbox_mount_dir(mount_dir: str) -> str:
    """Validate and normalize one workspace-relative sandbox ``mount_dir``."""
    if "\x00" in mount_dir:
        msg = "mount_dir must not contain NUL bytes."
        raise ValueError(msg)
    return agent_workspace_relative_path(mount_dir).as_posix()


def resolve_sandbox_mount_target(
    *,
    storage_root: Path,
    workspaces: tuple[Path, ...],
    mount_dir: str,
    validate: bool = True,
) -> tuple[Path, int | None]:
    """Resolve one sandbox ``mount_dir`` relative to ``storage_root`` and optionally pin its inode."""
    if len(workspaces) != 1:
        msg = "Sandbox mount_dir requires a single agent workspace."
        raise ValueError(msg)
    normalized = normalize_sandbox_mount_dir(mount_dir)
    workspace_rel = workspaces[0].relative_to(storage_root)
    target_rel = workspace_rel if normalized == "." else workspace_rel / normalized
    if not validate:
        return target_rel, None
    with open_directory_within_root(storage_root, target_rel, create=False) as fd:
        return target_rel, os.fstat(fd).st_ino


def _plan_sandbox_workspace_mounts(
    *,
    worker_key: str,
    storage_root: Path,
    worker_visible_shared_storage_root: Path,
    workspaces: tuple[Path, ...],
    sandbox: WorkerSandboxConfig,
) -> tuple[ScopedWorkspaceMount, ...]:
    if sandbox.mount_dir is None:
        return ()
    try:
        target_rel, ino = resolve_sandbox_mount_target(
            storage_root=storage_root,
            workspaces=workspaces,
            mount_dir=sandbox.mount_dir,
            validate=True,
        )
    except ValueError as exc:
        msg = f"Invalid sandbox mount_dir for {worker_log_id(worker_key)}: {exc}"
        raise WorkerBackendError(msg) from exc
    except OSError as exc:
        msg = f"Sandbox mount_dir '{sandbox.mount_dir}' must exist as a real directory without symlinks."
        raise WorkerBackendError(msg) from exc
    return (
        ScopedWorkspaceMount(
            storage_root / target_rel,
            worker_visible_shared_storage_root / target_rel,
            read_only=sandbox.mount_read_only,
            ino=ino,
        ),
    )


def plan_scoped_workspace_mounts(
    *,
    worker_key: str,
    local_shared_storage_root: Path,
    worker_visible_shared_storage_root: Path,
    private_agent_names: frozenset[str] | None,
    resolved_agent_policies: Mapping[str, ResolvedAgentPolicy],
    create_shared: bool = False,
    sandbox: WorkerSandboxConfig | None = None,
) -> tuple[ScopedWorkspaceMount, ...]:
    """Return the workspaces one dedicated worker mounts, at their canonical paths.

    A workspace is mounted only when it is a real directory reached without links
    from the storage root, because container runtimes create missing sources as
    root. ``create_shared`` first creates missing shared workspaces that way; private
    workspaces appear only after the primary writes their scope's identity record.
    Private mounts repeat at the verified legacy spellings of their scope.
    When ``sandbox`` is set, either no workspace or only one validated directory is mounted.
    """
    storage_root = shared_storage_root(local_shared_storage_root)
    workspaces = _scoped_workspaces(
        worker_key=worker_key,
        storage_root=storage_root,
        private_agent_names=private_agent_names,
        resolved_agent_policies=resolved_agent_policies,
    )
    if sandbox is not None:
        return _plan_sandbox_workspace_mounts(
            worker_key=worker_key,
            storage_root=storage_root,
            worker_visible_shared_storage_root=worker_visible_shared_storage_root,
            workspaces=workspaces,
            sandbox=sandbox,
        )
    private_root = private_instances_root_path(storage_root)
    canonical_scope = private_instance_scope_root_path(storage_root, worker_key)
    mounts: list[ScopedWorkspaceMount] = []
    for workspace in workspaces:
        relative_path = workspace.relative_to(storage_root)
        # A private scope's directory is named after its requester, so logs show the path below it.
        logged_workspace = str(
            workspace.relative_to(canonical_scope) if workspace.is_relative_to(canonical_scope) else relative_path,
        )
        create = create_shared and not workspace.is_relative_to(private_root)
        try:
            with open_directory_within_root(storage_root, relative_path, create=create):
                mounts.append(ScopedWorkspaceMount(workspace, worker_visible_shared_storage_root / relative_path))
        except FileNotFoundError:
            logger.info(
                "Not mounting a workspace that does not exist yet",
                worker=worker_log_id(worker_key),
                workspace=logged_workspace,
            )
        except OSError as exc:
            if create:
                msg = f"Worker workspace must be a real directory below the storage root: {relative_path}"
                raise WorkerBackendError(msg) from exc
            logger.warning(
                "Not mounting a workspace reached through a link",
                worker=worker_log_id(worker_key),
                workspace=logged_workspace,
            )
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


def agent_names_addressed_by_worker_key(
    worker_key: str,
    resolved_agent_policies: Mapping[str, ResolvedAgentPolicy],
) -> tuple[str, ...]:
    """Return the configured agents whose tool calls one worker key serves."""
    scope = resolved_worker_key_scope(worker_key)
    if scope == "user":
        return tuple(
            agent_name
            for agent_name, policy in resolved_agent_policies.items()
            if policy.effective_execution_scope == "user"
        )

    encoded_agent_name = worker_key_agent_name(worker_key)
    if encoded_agent_name is None:
        return ()
    if scope == "user_agent":
        matches = tuple(
            agent_name
            for agent_name in resolved_agent_policies
            if normalize_worker_key_part(agent_name) == encoded_agent_name
        )
    else:
        expected_scope = None if scope == "unscoped" else scope
        matches = tuple(
            agent_name
            for agent_name, policy in resolved_agent_policies.items()
            if policy.effective_execution_scope == expected_scope
            and normalize_worker_key_part(agent_name) == encoded_agent_name
        )
    if len(matches) > 1:
        msg = f"Worker '{worker_log_id(worker_key)}' has an ambiguous normalized agent name."
        raise WorkerBackendError(msg)
    return matches


def tailnet_ssh_marked(worker_key: str, launch: WorkerLaunchConfig, state_scope_worker_key: str | None) -> bool:
    """Mark dedicated workers serving an opted-in agent, not transient pinned workers."""
    return resolve_state_scope_worker_key(worker_key, state_scope_worker_key) == worker_key and any(
        launch.config.agents[name].tailnet_ssh
        for name in agent_names_addressed_by_worker_key(worker_key, launch.agent_policies)
    )


@dataclass(frozen=True, slots=True)
class SharedDirectoryMount:
    """One shared directory a dedicated worker mounts at its canonical path."""

    local_path: Path
    worker_visible_path: Path
    read_only: bool


def plan_shared_directory_mounts(
    *,
    worker_key: str,
    runtime_paths: RuntimePaths,
    worker_visible_shared_storage_root: Path,
    resolved_agent_policies: Mapping[str, ResolvedAgentPolicy],
) -> tuple[SharedDirectoryMount, ...]:
    """Return the shared directories one dedicated worker mounts.

    A directory mounts read-write when its owner is an agent the worker serves and
    read-only when only a member is. Private agents neither own nor join one, and a
    directory is mounted only when it is a real directory reached without links,
    because container runtimes follow links in mount sources.
    """
    served = agent_names_addressed_by_worker_key(worker_key, resolved_agent_policies)

    def is_shareable(agent_name: str) -> bool:
        policy = resolved_agent_policies.get(agent_name)
        return policy is not None and not policy.is_private

    storage_root = runtime_paths.storage_root
    mounts: list[SharedDirectoryMount] = []
    for directory in load_shared_directories(runtime_paths):
        writable = shared_directory_access(directory, served, is_shareable)
        if writable is None:
            continue
        relative_path = shared_directory_relative_path(directory.owner, directory.name)
        try:
            with open_directory_within_root(storage_root, relative_path):
                pass
        except OSError:
            logger.warning(
                "Not mounting a shared directory that is missing or reached through a link",
                worker=worker_log_id(worker_key),
                directory=f"{directory.owner}/{directory.name}",
            )
            continue
        mounts.append(
            SharedDirectoryMount(
                local_path=storage_root / relative_path,
                worker_visible_path=worker_visible_shared_storage_root / relative_path,
                read_only=not writable,
            ),
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
    msg = f"Duplicate {duplicate_label} generated for worker key: {worker_log_id(worker_key)}"
    raise WorkerBackendError(msg)
