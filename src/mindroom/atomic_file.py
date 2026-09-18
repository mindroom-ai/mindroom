"""Atomic file publication relative to an already-open directory."""

from __future__ import annotations

import os
from contextlib import suppress
from uuid import uuid4


def atomic_write_bytes_at(
    directory_fd: int,
    filename: str,
    payload: bytes,
    *,
    file_mode: int | None = None,
    temp_prefix: str = ".mindroom-",
) -> None:
    """Replace one validated filename, keeping publication and cleanup in the same directory.

    The caller owns the directory descriptor and validates the single-component filename
    and any custom temporary prefix.
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
            temp_file.write(payload)
            temp_file.flush()
            if file_mode is not None:
                os.fchmod(temp_file.fileno(), file_mode)
            os.fsync(temp_file.fileno())
        os.replace(temp_name, filename, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        with suppress(OSError):
            os.fsync(directory_fd)
    finally:
        if temp_fd >= 0:
            os.close(temp_fd)
        with suppress(FileNotFoundError):
            os.unlink(temp_name, dir_fd=directory_fd)
