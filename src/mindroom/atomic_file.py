"""Atomic file publication relative to an already-open directory."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING
from uuid import uuid4

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import BinaryIO


def atomic_write_bytes_at(
    directory_fd: int,
    filename: str,
    payload: bytes,
    *,
    file_mode: int | None = None,
    temp_prefix: str = ".mindroom-",
) -> None:
    """Publish bytes through the shared descriptor-bound atomic transaction."""
    with atomic_write_file_at(directory_fd, filename, file_mode=file_mode, temp_prefix=temp_prefix) as output:
        output.write(payload)


@contextmanager
def atomic_write_file_at(
    directory_fd: int,
    filename: str,
    *,
    file_mode: int | None = None,
    temp_prefix: str = ".mindroom-",
    replace: bool = True,
) -> Iterator[BinaryIO]:
    """Stream a file, publishing it only when the caller finishes successfully.

    The caller owns the directory descriptor and validates the single-component filename
    and any custom temporary prefix. Without ``replace``, publishing raises ``FileExistsError``
    when anything, even a link leading nowhere, already has the name.
    """
    temp_name = f"{temp_prefix}{uuid4().hex}.tmp"
    temp_fd = os.open(
        temp_name,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
        0o600,
        dir_fd=directory_fd,
    )
    try:
        with os.fdopen(temp_fd, mode="wb") as temp_file:
            temp_fd = -1
            yield temp_file
            temp_file.flush()
            if file_mode is not None:
                os.fchmod(temp_file.fileno(), file_mode)
            os.fsync(temp_file.fileno())
        if replace:
            os.replace(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        else:
            # Never through a link swapped in for the temporary file meanwhile.
            os.link(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd, follow_symlinks=False)
        with suppress(OSError):
            os.fsync(directory_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=directory_fd)


def existing_file_mode(directory_fd: int, name: str) -> int | None:
    """Return a regular entry's permission bits so a rewrite keeps them."""
    try:
        file_stat = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return stat.S_IMODE(file_stat.st_mode) & 0o777 if stat.S_ISREG(file_stat.st_mode) else None
