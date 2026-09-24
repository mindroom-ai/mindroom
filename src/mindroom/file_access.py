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
    from mindroom.config.main import Config
    from mindroom.config.models import FileAccess


@dataclass(frozen=True)
class AuthorizedFile:
    """An existing regular file and the no-follow open that keeps its authorization.

    ``path`` is the canonical file. Descriptor readers open
    ``open_regular_file_within_root(root, relative)``: ``root`` is the caller's
    workspace spelling, so a workspace root replaced by a link is refused, or the
    filesystem anchor in unrestricted mode; ``relative`` is the canonical path below it.
    """

    root: Path
    relative: Path
    path: Path


def agent_file_access(config: Config | None, agent_name: str | None) -> FileAccess:
    """Resolve one agent's file_access; without an agent the configured default applies, without config ``workspace``."""
    if config is None:
        return "workspace"
    return config.resolve_entity(agent_name).file_access


def resolve_agent_file(
    raw_path: str,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
    field_name: str,
) -> AuthorizedFile:
    """Return the authorized file for one model-supplied path, or raise ``ValueError``."""
    try:
        requested = Path(raw_path).expanduser()
    except RuntimeError as exc:
        msg = f"{field_name} '{raw_path}' names a home directory that cannot be determined."
        raise ValueError(msg) from exc
    if file_access == "unrestricted":
        base = workspace_root if workspace_root is not None else Path.cwd()
        candidate = requested if requested.is_absolute() else base / requested
        try:
            resolved = candidate.resolve(strict=True)
        except OSError as exc:
            msg = f"{field_name} '{raw_path}' does not exist or cannot be read: {exc.strerror or exc}"
            raise ValueError(msg) from exc
        root = Path(resolved.anchor)
        canonical_root = root
    else:
        if workspace_root is None:
            msg = f"{field_name} '{raw_path}' requires an agent workspace; file_access is 'workspace'."
            raise ValueError(msg)
        root = workspace_root
        canonical_root = workspace_root.resolve()
        try:
            resolved = resolve_path_within_root(canonical_root, requested, symlinks="internal", strict=True)
        except (ValueError, OSError) as exc:
            msg = f"{field_name} '{raw_path}' must be an existing file inside the agent workspace (file_access is 'workspace'): {exc}"
            raise ValueError(msg) from exc
    if not resolved.is_file():
        msg = f"{field_name} '{raw_path}' is not a regular file."
        raise ValueError(msg)
    return AuthorizedFile(root=root, relative=resolved.relative_to(canonical_root), path=resolved)
