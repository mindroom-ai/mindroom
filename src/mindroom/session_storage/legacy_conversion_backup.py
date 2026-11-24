"""The backup a conversation database gets before a one-way conversion of its layout.

Worker code can write session directories, so the copy reaches every file through descriptors of the pinned
sessions directory and follows no link planted there.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from contextlib import suppress
from typing import TYPE_CHECKING

from mindroom.path_confinement import open_directory_within_root, open_regular_file_at

if TYPE_CHECKING:
    from contextlib import ExitStack
    from pathlib import Path

_TEMPORARY_SUFFIX = ".tmp"


class ConversionSpaceError(RuntimeError):
    """A database's directory lacks the free space its conversion needs; names the database and the bytes."""


def leave_write_ahead_log(connection: sqlite3.Connection, path: Path) -> None:
    """Return a file another tool put in write-ahead-log mode to the rollback journal, checkpointing its log.

    MindRoom never sets the mode. Leaving it before a conversion moves every commit into the file before the backup
    copies it, and no checkpoint can change the file under the conversion's lock.
    """
    try:
        mode = connection.execute("PRAGMA journal_mode = DELETE").fetchone()
    except sqlite3.OperationalError as error:
        mode = error
    if mode != ("delete",):
        msg = f"Converting {path} needs its write-ahead log checkpointed, which failed: {mode}"
        raise RuntimeError(msg)


# LEGACY_COMPAT: Conversation databases kept as a backup before a one-way conversion of their layout.
# Legacy format: A conversation database without `<s>_log_meta`, in the Agno 3 run-row layout or the Agno 2 layout the
# conversion's Agno-era steps still read, and one whose meta row records a layout version below 4.
# Last legacy release: the Agno layout, v2026.10.608, replaced by the next release's storage v2 `<s>_log_*` tables;
# layout version 3, v2026.10.676, replaced by the next release's layout version 4.
# Handling: Before a conversion writes anything, copy the file byte for byte beside it, to `<name>.db.pre-storage-v2`
# before storage v2 and to `<name>.db.pre-storage-v4` before layout version 4, so a downgrade can restore it; a retry
# replaces an earlier attempt's backup, and MindRoom never deletes the backup of a converted database.
# Coverage: tests/session_storage/test_legacy_conversion_backup.py and
# tests/session_storage/test_legacy_session_columns.py::test_a_converted_database_converts_once.
def write_conversion_backup(
    database: Path,
    descriptors: ExitStack,
    *,
    suffix: str,
    size_factor: float,
    copied_bytes: int,
) -> None:
    """Copy ``database`` to ``<name>.db<suffix>`` beside it, called while the conversion holds its write lock.

    Free space must be at least ``size_factor`` times the file's size, the copy and what the conversion writes beside
    it, plus ``copied_bytes``, older data the conversion copies inside its transaction before it converts. The copy
    reads and writes only through descriptors of the pinned directory, never following a link, and replaces an
    earlier attempt's files.

    ``descriptors`` takes the database file's own descriptor. SQLite's locks are POSIX record locks, which a
    process loses on every descriptor of the file as soon as it closes any one of them, so the caller must close
    ``descriptors`` only after it has closed its connection.
    """
    backup_name = f"{database.name}{suffix}"
    temporary_name = f"{backup_name}{_TEMPORARY_SUFFIX}"
    with open_directory_within_root(database.parent) as directory:
        # Under the write lock the database is unconverted, since a failed or crashed attempt rolled back, so that
        # attempt's backup holds nothing the database lacks and must not count against this attempt's free space. Its
        # files are unlinked by name, so a link planted there loses only itself.
        for name in (temporary_name, backup_name):
            with suppress(FileNotFoundError):
                os.unlink(name, dir_fd=directory)
        source = open_regular_file_at(directory, database.name)
        descriptors.callback(os.close, source)
        _require_free_space(directory, database, int(size_factor * os.fstat(source).st_size), size_factor, copied_bytes)
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


def _require_free_space(directory: int, database: Path, base: int, size_factor: float, copied: int) -> None:
    needed = base + copied
    status = os.statvfs(directory)
    available = status.f_bavail * status.f_frsize
    if available < needed:
        copies = f" plus the {copied} bytes of older run data it copies first" if copied else ""
        msg = (
            f"Converting {database} needs {needed} bytes free in its directory, "
            f"{size_factor:g} times the database's size{copies}, but only {available} are free"
        )
        raise ConversionSpaceError(msg)
