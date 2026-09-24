"""Resolve model-supplied file paths under an agent's ``file_access`` setting.

``workspace`` confines paths to the agent workspace; ``unrestricted`` allows any
regular file the process can read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.path_confinement import resolve_path_within_root

if TYPE_CHECKING:
    from mindroom.config.models import FileAccess


@dataclass(frozen=True)
class AuthorizedFile:
    """An existing regular file and the root that authorized it; open it relative to ``root``."""

    root: Path
    path: Path


def resolve_agent_file(
    raw_path: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
    field_name: str,
) -> AuthorizedFile:
    """Return the authorized file for one model-supplied path, or raise ``ValueError``."""
    requested = Path(raw_path).expanduser()
    if file_access == "unrestricted":
        base = workspace_root if workspace_root is not None else Path.cwd()
        candidate = requested if requested.is_absolute() else base / requested
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            msg = f"{field_name} '{raw_path}' does not exist or cannot be read: {exc.strerror or exc}"
            raise ValueError(msg) from exc
        root = Path(resolved.anchor)
    else:
        if workspace_root is None:
            msg = f"{field_name} '{raw_path}' requires an agent workspace; file_access is 'workspace'."
            raise ValueError(msg)
        root = workspace_root.resolve()
        try:
            resolved = resolve_path_within_root(root, requested, symlinks="internal", strict=True)
        except (ValueError, OSError) as exc:
            msg = f"{field_name} '{raw_path}' must be an existing file inside the agent workspace (file_access is 'workspace'): {exc}"
            raise ValueError(msg) from exc
    if not resolved.is_file():
        msg = f"{field_name} '{raw_path}' is not a regular file."
        raise ValueError(msg)
    return AuthorizedFile(root=root, path=resolved)
