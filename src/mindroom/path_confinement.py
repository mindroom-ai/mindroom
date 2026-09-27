"""Shared symlink and Git-metadata path policy, and descriptor-relative access below caller-authorized roots.

Resolution checks a pathname at one instant; it does not authorize a later open.
Use the descriptor helpers for local I/O that must reject links swapped after
validation. Roots are trusted caller inputs, not discovered or authorized here.

Sandbox workers write agent workspaces, so the primary treats workspace content
as untrusted: it reaches its own files there by walking from the workspace root
with these helpers, which open every component without following links, open
files non-blocking so a planted FIFO cannot stall it, and cap what they read.
"""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from mindroom.atomic_file import atomic_write_bytes_at

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
    a symlink. Directory creation is relative to each pinned parent, which is
    synced after it gains a new entry. Symlink swaps cannot redirect traversal;
    arbitrary directory renames and hard links require the caller's storage
    ownership/isolation policy.
    """
    parts = _relative_parts(relative_path)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.dup(root) if isinstance(root, int) else os.open(root, flags)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, mode=mode, dir_fd=directory)
                except FileExistsError:
                    pass
                else:
                    with suppress(OSError):
                        os.fsync(directory)
            child = os.open(part, flags, dir_fd=directory)
            os.close(directory)
            directory = child
        yield directory
    finally:
        os.close(directory)


def open_regular_file_at(directory_fd: int, name: str, flags: int = os.O_RDONLY, mode: int = 0o600) -> int:
    """Open one entry of a pinned directory as a regular file; the caller closes the returned descriptor."""
    descriptor = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, mode, dir_fd=directory_fd)
    if stat.S_ISREG(os.fstat(descriptor).st_mode):
        return descriptor
    os.close(descriptor)
    message = "Path must name a regular file."
    raise ValueError(message)


@contextmanager
def open_regular_file_within_root(
    root: Path | int,
    relative_path: str | Path,
) -> Iterator[int]:
    """Open a regular file for reading without following links or blocking on a FIFO.

    Pass canonical relative paths from the resolver to allow internal links;
    pass lexical relative paths to reject them.
    """
    parts = _relative_parts(relative_path)
    if not parts:
        message = "Path must name a regular file."
        raise ValueError(message)
    with open_directory_within_root(root, Path(*parts[:-1])) as directory:
        descriptor = open_regular_file_at(directory, parts[-1])
    try:
        yield descriptor
    finally:
        os.close(descriptor)


def read_regular_file_within_root(
    root: Path | int,
    relative_path: str | Path,
    *,
    max_bytes: int = 64 << 20,
    truncate: bool = False,
) -> bytes:
    """Read one regular file through a no-follow walk; a file above ``max_bytes`` is refused, or cut when ``truncate``."""
    with open_regular_file_within_root(root, relative_path) as descriptor:
        if not truncate and os.fstat(descriptor).st_size > max_bytes:
            message = f"File exceeds its size limit: {relative_path}"
            raise ValueError(message)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0 and (chunk := os.read(descriptor, min(remaining, 1 << 16))):
            chunks.append(chunk)
            remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > max_bytes and not truncate:
        message = f"File exceeds its size limit: {relative_path}"
        raise ValueError(message)
    return payload[:max_bytes]


def write_file_within_root(
    root: Path,
    relative_path: str | Path,
    payload: bytes,
    *,
    file_mode: int = 0o600,
    dir_mode: int = 0o777,
    exclusive: bool = False,
) -> None:
    """Publish one file below a trusted root, creating missing directories through a no-follow walk.

    The file replaces any entry atomically; with ``exclusive`` it is created only
    when nothing exists at its name, raising ``FileExistsError`` otherwise.
    """
    parts = _relative_parts(relative_path)
    root.mkdir(parents=True, exist_ok=True)
    with open_directory_within_root(root, Path(*parts[:-1]), create=True, mode=dir_mode) as directory:
        if not exclusive:
            atomic_write_bytes_at(directory, parts[-1], payload, file_mode=file_mode)
            return
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with os.fdopen(open_regular_file_at(directory, parts[-1], flags, file_mode), "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
