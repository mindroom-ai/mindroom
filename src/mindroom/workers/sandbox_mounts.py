"""Shared path normalization and validation for sandboxed worker ``mount_dir`` mounts."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom.path_confinement import open_directory_within_root
from mindroom.tool_system.worker_routing import (
    agent_workspace_relative_path,
    shared_storage_root,
    visible_workspace_roots,
)
from mindroom.workers.models import WorkerSandboxConfig

if TYPE_CHECKING:
    from pathlib import Path

    from mindroom.config.main import Config

__all__ = [
    "normalize_sandbox_mount_dir",
    "parse_sandbox_config",
    "resolve_sandbox_mount_target",
    "resolve_worker_sandbox_mount",
    "sandbox_mount_dir_runtime_class_refusal",
]


def sandbox_mount_dir_runtime_class_refusal(runtime_class_name: str) -> str:
    """Return the refusal message when sandbox ``mount_dir`` is requested under a RuntimeClass."""
    return (
        f"mount_dir is not supported when workers.kubernetes.runtimeClassName is set ({runtime_class_name!r}) "
        "because runtimes such as gVisor do not preserve host inode numbers for mount verification; "
        "use sandbox=true without mount_dir."
    )


def normalize_sandbox_mount_dir(mount_dir: str) -> str:
    """Validate and normalize one workspace-relative sandbox ``mount_dir``."""
    if "\x00" in mount_dir:
        msg = "mount_dir must not contain NUL bytes."
        raise ValueError(msg)
    relative = agent_workspace_relative_path(mount_dir)
    if not relative.parts:
        msg = "mount_dir must name a subdirectory inside the agent workspace, not the workspace root."
        raise ValueError(msg)
    if any(part.startswith(".") for part in relative.parts):
        msg = "mount_dir cannot target hidden or dot-prefixed directories such as .mindroom, .config, or .git."
        raise ValueError(msg)
    return relative.as_posix()


def parse_sandbox_config(
    sandbox: object,
    mount_dir: object,
    mount_read_only: object,
    *,
    runtime_class_name: str | None = None,
) -> tuple[WorkerSandboxConfig | None, bool]:
    """Validate and normalize ``sandbox``/``mount_dir``/``mount_read_only`` options without touching disk."""
    if sandbox is not None and not isinstance(sandbox, bool):
        msg = "sandbox must be a boolean."
        raise ValueError(msg)
    if mount_dir is not None and not isinstance(mount_dir, str):
        msg = "mount_dir must be a workspace-relative directory path string."
        raise ValueError(msg)
    if mount_read_only is not None and not isinstance(mount_read_only, bool):
        msg = "mount_read_only must be a boolean."
        raise ValueError(msg)
    if sandbox is False and mount_dir is not None:
        msg = "mount_dir always runs in sandbox mode; omit sandbox or set sandbox=true."
        raise ValueError(msg)
    if mount_read_only is True and mount_dir is None:
        msg = "mount_read_only requires mount_dir."
        raise ValueError(msg)
    normalized_mount_dir: str | None = None
    if mount_dir is not None:
        try:
            normalized_mount_dir = normalize_sandbox_mount_dir(mount_dir)
        except ValueError as exc:
            msg = f"Invalid mount_dir: {exc}"
            raise ValueError(msg) from exc
        if runtime_class_name is not None:
            raise ValueError(sandbox_mount_dir_runtime_class_refusal(runtime_class_name))
    sandbox_specified = sandbox is not None or mount_dir is not None or mount_read_only is not None
    sandbox_config = (
        WorkerSandboxConfig(mount_dir=normalized_mount_dir, mount_read_only=bool(mount_read_only))
        if sandbox is True or normalized_mount_dir is not None
        else None
    )
    return sandbox_config, sandbox_specified


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
    target_rel = workspace_rel / normalized
    if not validate:
        return target_rel, None
    with open_directory_within_root(storage_root, target_rel, create=False) as fd:
        return target_rel, os.fstat(fd).st_ino


def resolve_worker_sandbox_mount(
    *,
    storage_root: Path,
    worker_key: str,
    private_agent_names: frozenset[str] | None,
    config: Config | None,
    sandbox: WorkerSandboxConfig,
    validate: bool = True,
) -> Path | None:
    """Return the storage-relative mount path for ``sandbox`` (None for empty scratch), validating on disk."""
    if sandbox.mount_dir is None:
        return None
    root = shared_storage_root(storage_root)
    policies = config.get_agent_policies() if config is not None else {}
    workspaces = visible_workspace_roots(
        root,
        worker_key,
        policies,
        private_agent_names=private_agent_names or frozenset(),
    )
    try:
        target_rel, _ = resolve_sandbox_mount_target(
            storage_root=root,
            workspaces=workspaces,
            mount_dir=sandbox.mount_dir,
            validate=validate,
        )
    except FileNotFoundError as exc:
        msg = f"Sandbox mount_dir '{sandbox.mount_dir}' does not exist in the agent workspace."
        raise ValueError(msg) from exc
    except OSError as exc:
        msg = f"Sandbox mount_dir '{sandbox.mount_dir}' must be a real directory without symlinks."
        raise ValueError(msg) from exc
    return target_rel
