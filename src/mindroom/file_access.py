"""Resolve model-supplied file paths under an agent's ``file_access`` setting.

``workspace`` confines paths to the agent workspace; ``unrestricted`` allows any
regular file the process can read.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, BinaryIO

from mindroom.path_confinement import open_regular_file_within_root, resolve_path_within_root

if TYPE_CHECKING:
    from collections.abc import Iterator

    from mindroom.config.main import Config
    from mindroom.config.models import FileAccess


@dataclass(frozen=True)
class AuthorizedFile:
    """An existing regular file whose authorization only survives a no-follow open below ``root``.

    ``root`` is the caller's workspace spelling, so a workspace root replaced by a link
    is refused, or the filesystem anchor in unrestricted mode; ``relative`` is the
    canonical path below it. Read the file through :meth:`open`. The object carries no
    reopenable full path on purpose: ``display_path`` is for messages and receipts only.
    """

    root: Path
    relative: Path
    display_path: str

    @property
    def name(self) -> str:
        """Return the file name for staging copies, MIME guessing, and default titles."""
        return self.relative.name

    @contextmanager
    def open(self) -> Iterator[BinaryIO]:
        """Open the file below its authorizing root without following links or blocking on a FIFO."""
        with (
            open_regular_file_within_root(self.root, self.relative) as descriptor,
            os.fdopen(descriptor, "rb", closefd=False) as file,
        ):
            yield file


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
        # Python 3.12 raises RuntimeError for symlink loops; 3.13 raises OSError.
        except (OSError, RuntimeError) as exc:
            detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            msg = f"{field_name} '{raw_path}' does not exist or cannot be read: {detail}"
            raise ValueError(msg) from exc
        root = Path(resolved.anchor)
        canonical_root = root
    else:
        if workspace_root is None:
            msg = f"{field_name} '{raw_path}' requires an agent workspace; file_access is 'workspace'."
            raise ValueError(msg)
        root = workspace_root
        try:
            canonical_root = workspace_root.resolve()
            resolved = resolve_path_within_root(canonical_root, requested, symlinks="internal", strict=True)
        except (ValueError, OSError, RuntimeError) as exc:
            msg = f"{field_name} '{raw_path}' must be an existing file inside the agent workspace (file_access is 'workspace'): {exc}"
            raise ValueError(msg) from exc
    if not resolved.is_file():
        msg = f"{field_name} '{raw_path}' is not a regular file."
        raise ValueError(msg)
    return AuthorizedFile(root=root, relative=resolved.relative_to(canonical_root), display_path=str(resolved))
