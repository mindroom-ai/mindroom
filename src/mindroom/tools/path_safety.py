"""Shared path-safety helpers for local file-oriented tools."""

from __future__ import annotations

import os
import shutil
import stat
from contextlib import suppress
from glob import has_magic
from pathlib import Path
from typing import TYPE_CHECKING

from mindroom.atomic_file import atomic_write_file_at
from mindroom.path_confinement import (
    open_directory_within_root,
    read_regular_file_within_root,
    resolve_path_within_root,
)

if TYPE_CHECKING:
    from typing import BinaryIO

    from mindroom.config.models import FileAccess

_BASE_DIR_ESCAPE_HINT = "Set the agent's file_access to 'unrestricted' to allow paths outside the workspace."


def _blocked_base_dir_message(path: str, resolved: Path, base_dir: Path) -> str:
    """Explain why a resolved path escaped the configured base directory."""
    return f"Path '{path}' resolves to '{resolved}', which is outside base_dir '{base_dir}'. {_BASE_DIR_ESCAPE_HINT}"


def _moved_base_dir_message(base_dir: Path) -> str:
    """Explain that a toolkit's pinned base dir was moved or replaced, without suggesting weaker access."""
    return f"base_dir '{base_dir}' no longer resolves to itself; it was moved or replaced by a link."


def blocked_file_action_message(action: str, requested_path: str, base_dir: Path) -> str:
    """Explain why a file-tool action was blocked."""
    if not _base_dir_is_current(base_dir):
        return f"Error {action}: {_moved_base_dir_message(base_dir)}"
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


def resolve_tool_base_dir(base_dir: str | Path | None) -> Path:
    """Return a toolkit's canonical base dir, refusing a link or a directory swapped while it was resolved.

    Runtime resolution refused links in the workspace path; pinning the same directory here keeps
    a later swap from becoming the toolkit's root, and every later check refuses a root that moved.
    """
    spelled = Path(base_dir) if base_dir else Path.cwd()
    resolved = spelled.resolve()
    try:
        with open_directory_within_root(spelled) as directory_fd:
            pinned = os.path.samestat(os.fstat(directory_fd), resolved.stat())
    except FileNotFoundError:
        # A base dir that does not exist yet has nothing to pin; later checks still refuse one that moved.
        return resolved
    except OSError as exc:
        msg = f"base_dir '{spelled}' must be a directory reached without a link: {exc.strerror}"
        raise ValueError(msg) from exc
    if not pinned:
        msg = f"base_dir '{spelled}' changed while it was being resolved."
        raise ValueError(msg)
    return resolved


def _base_dir_is_current(base_dir: Path) -> bool:
    """Return whether a toolkit's canonical base dir still resolves to itself, so no link has replaced it."""
    return base_dir.resolve() == base_dir


def is_within_base_dir(path: Path, base_dir: Path) -> bool:
    """Check whether a resolved path stays within base_dir, which must still resolve to itself."""
    if not _base_dir_is_current(base_dir):
        return False
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
        resolved = resolve_path_within_root(base_dir, requested, symlinks="internal")
    except ValueError:
        if not _base_dir_is_current(base_dir):
            raise ValueError(_moved_base_dir_message(base_dir)) from None
        raise ValueError(_blocked_base_dir_message(path, candidate.resolve(), base_dir.resolve())) from None
    # The resolver re-resolves its root, so check against the pinned base dir to refuse a root swapped meanwhile.
    if not resolved.is_relative_to(base_dir):
        raise ValueError(_moved_base_dir_message(base_dir))
    return resolved


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
    """Return ``resolved`` below the pinned base dir, or ``None`` for an unrestricted outside path.

    Workspace-mode paths always lie below the pinned base dir, so they never take the by-path branch;
    callers open them from ``base_dir`` without following a link, so a replaced base dir is refused.
    """
    return resolved.relative_to(base_dir) if resolved.is_relative_to(base_dir) else None


def read_resolved_file(base_dir: Path, resolved: Path) -> bytes:
    """Read one resolved file through a capped no-follow walk; outside paths need unrestricted access."""
    relative = _relative_below(base_dir, resolved)
    if relative is None:
        return resolved.read_bytes()
    return read_regular_file_within_root(base_dir, relative)


def _write_payload(output: BinaryIO, payload: bytes | BinaryIO) -> None:
    """Write bytes, or copy a readable stream in chunks so it is never buffered whole."""
    if isinstance(payload, bytes):
        output.write(payload)
    else:
        shutil.copyfileobj(payload, output)


def write_resolved_file(base_dir: Path, resolved: Path, payload: bytes | BinaryIO) -> None:
    """Publish one resolved file, by atomic replacement below ``base_dir``, keeping its permission bits (not setuid/setgid) and owner where permitted.

    Below ``base_dir``, replacing the entry never writes a hard-linked inode or leaves a partial file;
    an unrestricted path outside it is written in place by path.
    A stream payload is copied in chunks rather than read into memory.
    """
    relative = _relative_below(base_dir, resolved)
    if relative is None:
        resolved.parent.mkdir(parents=True, exist_ok=True)
        with resolved.open("wb") as output:
            _write_payload(output, payload)
        return
    with open_directory_within_root(base_dir, relative.parent, create=True) as directory_fd:
        try:
            existing = os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is None or not stat.S_ISREG(existing.st_mode):
            with atomic_write_file_at(directory_fd, relative.name, file_mode=0o644) as output:
                _write_payload(output, payload)
            return
        with atomic_write_file_at(directory_fd, relative.name) as output:
            os.fchmod(output.fileno(), stat.S_IMODE(existing.st_mode))
            # A worker's file stays the worker's: keep its owner, or at least its group, where the primary may.
            for uid in (existing.st_uid, -1):
                with suppress(OSError):
                    os.fchown(output.fileno(), uid, existing.st_gid)
                    break
            _write_payload(output, payload)


def write_agent_file(
    raw_path: str,
    payload: bytes | BinaryIO,
    *,
    workspace_root: Path | None,
    file_access: FileAccess,
) -> Path:
    """Publish one model-supplied path where the agent's ``file_access`` allows and return the resolved path.

    Relative paths resolve from the workspace; ``workspace`` mode refuses paths outside it and agents without one.
    """
    restrict = file_access == "workspace"
    if restrict and workspace_root is None:
        msg = f"Path '{raw_path}' requires an agent workspace; file_access is 'workspace'."
        raise ValueError(msg)
    base_dir = resolve_tool_base_dir(workspace_root)
    resolved = resolve_base_dir_path(base_dir, raw_path, restrict)
    write_resolved_file(base_dir, resolved, payload)
    return resolved


def remove_resolved_path(base_dir: Path, resolved: Path) -> None:
    """Remove one resolved file or empty directory without following a link below ``base_dir``."""
    relative = _relative_below(base_dir, resolved)
    if relative is None:
        if resolved.is_dir() and not resolved.is_symlink():
            resolved.rmdir()
        else:
            resolved.unlink()
        return
    with open_directory_within_root(base_dir, relative.parent) as directory_fd:
        if stat.S_ISDIR(os.stat(relative.name, dir_fd=directory_fd, follow_symlinks=False).st_mode):
            os.rmdir(relative.name, dir_fd=directory_fd)
        else:
            os.unlink(relative.name, dir_fd=directory_fd)
