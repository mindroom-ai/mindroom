"""The backup a conversation database gets before its one-way conversion to storage v2.

Worker code can write session directories, so the copy reaches every file through descriptors of the pinned
sessions directory and follows no link planted there.
"""

from __future__ import annotations

import os
import shutil
from contextlib import suppress
from typing import TYPE_CHECKING

from mindroom.path_confinement import open_directory_within_root, open_regular_file_at

if TYPE_CHECKING:
    import sqlite3
    from contextlib import ExitStack
    from pathlib import Path

BACKUP_SUFFIX = ".pre-storage-v2"
_TEMPORARY_SUFFIX = ".tmp"


class ConversionSpaceError(RuntimeError):
    """A database's directory lacks the free space its conversion needs; names the database and the bytes."""


# LEGACY_COMPAT: Agno-layout conversation databases kept as a backup before their conversion.
# Legacy format: A conversation database without `<s>_log_meta`, in the Agno 3 run-row layout, or the Agno 2 layout
# the conversion's Agno-era steps still read.
# Last legacy release: the last release before storage v2; replacement: unreleased, storage v2's `<s>_log_*` tables.
# Handling: Before the conversion writes anything, copy the file byte for byte to `<name>.db.pre-storage-v2` beside
# it, so a downgrade can restore it; MindRoom never deletes the backup.
# Coverage: tests/session_storage/test_legacy_agno3_backup.py.
def write_conversion_backup(connection: sqlite3.Connection, database: Path, descriptors: ExitStack) -> Path:
    """Copy ``database`` to ``<name>.db.pre-storage-v2`` beside it, while ``connection`` holds its write lock.

    Free space must be at least twice the file's size. The copy reads and writes only through descriptors of the
    pinned directory, never following a link, and replaces an earlier attempt's temporary file.

    ``descriptors`` takes the database file's own descriptor. SQLite's locks are POSIX record locks, which a
    process loses on every descriptor of the file as soon as it closes any one of them, so the caller must close
    ``descriptors`` only after it has closed ``connection``.
    """
    if not connection.in_transaction:
        msg = f"The conversion backup of {database} needs the database's write lock"
        raise RuntimeError(msg)
    backup_name = f"{database.name}{BACKUP_SUFFIX}"
    temporary_name = f"{backup_name}{_TEMPORARY_SUFFIX}"
    with open_directory_within_root(database.parent) as directory:
        # A crashed attempt's file is unlinked by name, so a link planted there loses only itself.
        with suppress(FileNotFoundError):
            os.unlink(temporary_name, dir_fd=directory)
        source = open_regular_file_at(directory, database.name)
        descriptors.callback(os.close, source)
        _require_free_space(directory, database, 2 * os.fstat(source).st_size)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        with (
            os.fdopen(source, "rb", closefd=False) as reader,
            os.fdopen(open_regular_file_at(directory, temporary_name, flags), "wb") as writer,
        ):
            shutil.copyfileobj(reader, writer)
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temporary_name, backup_name, src_dir_fd=directory, dst_dir_fd=directory)
        os.fsync(directory)
    return database.with_name(backup_name)


def _require_free_space(directory: int, database: Path, needed: int) -> None:
    status = os.statvfs(directory)
    available = status.f_bavail * status.f_frsize
    if available < needed:
        msg = (
            f"Converting {database} to storage v2 needs {needed} bytes free in its directory, "
            f"twice the database's size, but only {available} are free"
        )
        raise ConversionSpaceError(msg)
