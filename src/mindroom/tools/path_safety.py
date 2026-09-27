"""Shared path-safety helpers for local file-oriented tools."""

from __future__ import annotations

import os
import stat
from glob import has_magic
from pathlib import Path

from mindroom.path_confinement import (
    open_directory_within_root,
    open_regular_file_within_root,
    resolve_path_within_root,
)

_BASE_DIR_ESCAPE_HINT = "Set the agent's file_access to 'unrestricted' to allow paths outside the workspace."


def _blocked_base_dir_message(path: str, resolved: Path, base_dir: Path) -> str:
    """Explain why a resolved path escaped the configured base directory."""
    return f"Path '{path}' resolves to '{resolved}', which is outside base_dir '{base_dir}'. {_BASE_DIR_ESCAPE_HINT}"


def blocked_file_action_message(action: str, requested_path: str, base_dir: Path) -> str:
    """Explain why a file-tool action was blocked."""
    return f"Error {action}: path '{requested_path}' is outside base_dir '{base_dir}'. {_BASE_DIR_ESCAPE_HINT}"


def blocked_git_metadata_message(action: str, requested_path: str) -> str:
    """Explain why a file-tool write into Git metadata was blocked."""
    return f"Error {action}: path '{requested_path}' is inside Git metadata ('.git'), which file tools may not modify."


def format_path_for_output(path: str | Path, base_dir: Path) -> str:
    """Prefer base-dir-relative output, falling back to absolute paths outside the base dir."""
    try:
        return str(Path(path).relative_to(base_dir))
    except ValueError:
        return str(path)


def is_within_base_dir(path: Path, base_dir: Path) -> bool:
    """Check whether a resolved path stays within base_dir."""
    try:
        resolve_path_within_root(base_dir, path.resolve(), symlinks="internal")
    except (OSError, ValueError):
        return False
    return True


def resolve_base_dir_path(base_dir: Path, path: str, restrict_to_base_dir: bool = True) -> Path:
    """Resolve a path relative to base_dir, optionally preventing traversal."""
    requested = Path(path)
    candidate = requested if requested.is_absolute() else base_dir / requested
    if not restrict_to_base_dir:
        return candidate.resolve()

    try:
        return resolve_path_within_root(base_dir, requested, symlinks="internal")
    except ValueError:
        raise ValueError(_blocked_base_dir_message(path, candidate.resolve(), base_dir.resolve())) from None


def split_search_pattern(base_dir: Path, pattern: str) -> tuple[Path, str]:
    """Resolve the concrete search root ahead of the first glob component."""
    pattern_path = Path(pattern)
    if pattern_path.is_absolute():
        search_root = Path(pattern_path.anchor)
        parts = list(pattern_path.relative_to(search_root).parts)
    else:
        search_root = base_dir
        parts = list(pattern_path.parts)

    first_glob_index = next((index for index, part in enumerate(parts) if has_magic(part)), len(parts))
    static_parts = parts[:first_glob_index]
    glob_parts = parts[first_glob_index:]
    if not glob_parts and static_parts:
        glob_parts = [static_parts.pop()]

    resolved_root = search_root.joinpath(*static_parts).resolve()
    resolved_pattern = str(Path(*glob_parts)) if glob_parts else "."
    return resolved_root, resolved_pattern


def _relative_below(base_dir: Path, resolved: Path) -> Path | None:
    """Return ``resolved`` below the canonical base dir, or ``None`` for an unrestricted outside path."""
    canonical_base = base_dir.resolve()
    return resolved.relative_to(canonical_base) if resolved.is_relative_to(canonical_base) else None


def read_resolved_file(base_dir: Path, resolved: Path) -> bytes:
    """Read one resolved file, by a no-follow walk from ``base_dir`` when it lies inside it.

    Worker code can write the workspace, so a file checked by resolution and
    then swapped for a link or FIFO is refused instead of followed. A path
    outside ``base_dir`` only resolves under unrestricted file access, the
    operator's full-trust choice, and is read by path.
    """
    relative = _relative_below(base_dir, resolved)
    if relative is None:
        return resolved.read_bytes()
    with (
        open_regular_file_within_root(base_dir.resolve(), relative) as descriptor,
        os.fdopen(descriptor, "rb", closefd=False) as file,
    ):
        return file.read()


def write_resolved_file(base_dir: Path, resolved: Path, payload: bytes) -> None:
    """Create or overwrite one resolved file in place without following a link below ``base_dir``.

    Parents are created by the same no-follow walk, and an existing file keeps
    its mode and inode like an ordinary write.
    """
    relative = _relative_below(base_dir, resolved)
    if relative is None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(payload)
        return
    with open_directory_within_root(base_dir.resolve(), relative.parent, create=True) as directory_fd:
        descriptor = os.open(
            relative.name,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            0o666,
            dir_fd=directory_fd,
        )
    with os.fdopen(descriptor, "wb") as file:
        if not stat.S_ISREG(os.fstat(file.fileno()).st_mode):
            msg = f"Not a regular file: {resolved}"
            raise OSError(msg)
        file.truncate(0)
        file.write(payload)


def remove_resolved_path(base_dir: Path, resolved: Path) -> None:
    """Remove one resolved file or empty directory without following a link below ``base_dir``."""
    relative = _relative_below(base_dir, resolved)
    if relative is None:
        if resolved.is_dir() and not resolved.is_symlink():
            resolved.rmdir()
        else:
            resolved.unlink()
        return
    with open_directory_within_root(base_dir.resolve(), relative.parent) as directory_fd:
        if stat.S_ISDIR(os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False).st_mode):
            os.rmdir(relative.name, dir_fd=directory_fd)
        else:
            os.unlink(relative.name, dir_fd=directory_fd)
