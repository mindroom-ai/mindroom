"""Validation and path resolution for named-worker sandbox ``mount_dir`` targets."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from mindroom.path_confinement import open_directory_within_root
from mindroom.tool_system.worker_routing import agent_workspace_relative_path

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "normalize_sandbox_mount_dir",
    "resolve_sandbox_mount_target",
]


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
