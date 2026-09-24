"""Shared symlink and Git-metadata path policy, and descriptor-relative access below caller-authorized roots.

Resolution checks a pathname at one instant; it does not authorize a later open.
Use the descriptor helpers for local I/O that must reject links swapped after
validation. Roots are trusted caller inputs, not discovered or authorized here.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator


def is_git_metadata_path(path: Path) -> bool:
    """Return whether a path is a ``.git`` entry or lies beneath one.

    MindRoom runs Git in knowledge checkouts that may sit inside agent
    workspaces, and Git trusts ``.git`` contents, so agent-facing writers refuse
    these paths. That is defense in depth: code-execution tools and tools that
    accept arbitrary output paths can still write there, so the Git commands
    themselves must not trust a checkout's config.
    """
    return any(part.casefold() == ".git" for part in path.parts)


def resolve_path_within_root(
    root: Path,
    path: str | Path,
    *,
    symlinks: Literal["internal", "reject", "preserve_leaf"],
    strict: bool = False,
) -> Path:
    """Resolve a path under a trusted root using an explicit descendant-link policy.

    ``internal`` permits links whose resolved target stays inside the root.
    ``reject`` rejects all descendant links, including internal ones.
    ``preserve_leaf`` rejects linked parents but leaves the final entry untouched
    for unlink/atomic replacement, never for a subsequent following open.
    ``strict`` requires resolved components to exist; a preserved leaf is never
    resolved or checked for existence. Caller wrappers own additional input
    syntax restrictions and user-facing error messages.
    """
    lexical_root = root.expanduser()
    canonical_root = lexical_root.resolve(strict=strict)
    requested = Path(path)
    message = "Path must stay within its authorized root."
    if symlinks not in {"internal", "reject", "preserve_leaf"}:
        policy_error = f"Unknown symlink policy: {symlinks}"
        raise ValueError(policy_error)
    if symlinks == "preserve_leaf" and (requested.is_absolute() or ".." in requested.parts):
        raise ValueError(message)
    if symlinks != "internal":
        relative = requested.relative_to(canonical_root) if requested.is_absolute() else requested
        current = canonical_root
        checked_parts = relative.parts[:-1] if symlinks == "preserve_leaf" else relative.parts
        for part in checked_parts:
            current /= part
            if current.is_symlink():
                raise ValueError(message)
    candidate = canonical_root / requested
    if symlinks == "preserve_leaf" and requested.parts:
        resolved = candidate.parent.resolve(strict=strict) / candidate.name
    else:
        resolved = candidate.resolve(strict=strict)
        # Python versions that suppress ELOOP during non-strict resolve must
        # still reject loops, including when reached through a dangling path.
        if not strict:
            with suppress(FileNotFoundError, NotADirectoryError):
                resolved.stat()
    if not resolved.is_relative_to(canonical_root):
        raise ValueError(message)
    return resolved


def _relative_parts(path: str | Path) -> tuple[str, ...]:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts:
        message = "Descriptor paths must be relative and must not contain '..'."
        raise ValueError(message)
    return relative.parts


@contextmanager
def open_directory_within_root(
    root: Path | int,
    relative_path: str | Path = Path(),
    *,
    create: bool = False,
    mode: int = 0o777,
) -> Iterator[int]:
    """Pin a directory through a no-follow walk; close owned descriptors on exit.

    A supplied root descriptor is borrowed, never closed. A supplied root path
    must already be trusted, with trusted ancestors; its final entry cannot be
    a symlink. Directory creation is relative to each pinned parent. Symlink
    swaps cannot redirect traversal; arbitrary directory renames and hard links
    require the caller's storage ownership/isolation policy.
    """
    parts = _relative_parts(relative_path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.dup(root) if isinstance(root, int) else os.open(root, flags)
    try:
        for part in parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, mode=mode, dir_fd=directory)
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


@contextmanager
def open_regular_file_within_root(
    root: Path | int,
    relative_path: str | Path,
) -> Iterator[int]:
    """Open a regular file for reading without following links or blocking on a FIFO.

    Pass canonical relative paths from the resolver to allow internal links;
    pass lexical relative paths to reject them. Writes use the directory helper
    with descriptor-relative publication instead.
    """
    parts = _relative_parts(relative_path)
    if not parts:
        message = "Path must name a regular file."
        raise ValueError(message)
    with open_directory_within_root(root, Path(*parts[:-1])) as directory:
        descriptor = os.open(
            parts[-1],
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=directory,
        )
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                message = "Path must name a regular file."
                raise ValueError(message)
            yield descriptor
        finally:
            os.close(descriptor)
