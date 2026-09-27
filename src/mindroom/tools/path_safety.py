"""Shared path-safety helpers for local file-oriented tools."""

from __future__ import annotations

import os
import stat
from contextlib import suppress
from glob import has_magic
from pathlib import Path

from mindroom.atomic_file import atomic_write_bytes_at, atomic_write_file_at
from mindroom.path_confinement import (
    open_directory_within_root,
    read_regular_file_within_root,
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
    """Read one resolved file through a capped no-follow walk; outside paths need unrestricted access."""
    relative = _relative_below(base_dir, resolved)
    if relative is None:
        return resolved.read_bytes()
    return read_regular_file_within_root(base_dir.resolve(), relative)


def write_resolved_file(base_dir: Path, resolved: Path, payload: bytes) -> None:
    """Publish one resolved file by atomic replacement, keeping an existing file's mode and, where permitted, owner.

    Replacing the entry never writes a hard-linked inode or leaves a partial file.
    """
    relative = _relative_below(base_dir, resolved)
    if relative is None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(payload)
        return
    with open_directory_within_root(base_dir.resolve(), relative.parent, create=True) as directory_fd:
        try:
            existing = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is None or not stat.S_ISREG(existing.st_mode):
            atomic_write_bytes_at(directory_fd, relative.name, payload, file_mode=0o644)
            return
        with atomic_write_file_at(directory_fd, relative.name) as output:
            os.fchmod(output.fileno(), stat.S_IMODE(existing.st_mode))
            # A worker's file stays the worker's: keep its owner, or at least its group, where the primary may.
            for uid in (existing.st_uid, -1):
                with suppress(PermissionError):
                    os.fchown(output.fileno(), uid, existing.st_gid)
                    break
            output.write(payload)


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
