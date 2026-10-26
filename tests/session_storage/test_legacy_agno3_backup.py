"""The backup each conversation database gets before its conversion to storage v2."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
from contextlib import ExitStack, closing
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest

from mindroom.constants import resolve_runtime_paths
from mindroom.debug_report import _session_databases
from mindroom.session_storage.legacy_agno3_backup import (
    BACKUP_SUFFIX,
    ConversionSpaceError,
    write_conversion_backup,
)
from mindroom.session_storage.legacy_agno3_conversion import migrate_conversation_storage
from mindroom.session_storage.store import connect_conversation_database
from tests.conftest import create_agno_2_sessions_db
from tests.session_storage.goldens import GoldenStore, golden_stores

if TYPE_CHECKING:
    from collections.abc import Callable

_PROC_FDS = Path("/proc/self/fd")
_TAKE_WRITE_LOCK = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    connection.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError as error:
    print(error)
else:
    print("locked")
"""


def _database(tmp_path: Path) -> Path:
    return golden_stores()[0].restore(tmp_path)


def _backup_name(database: Path) -> Path:
    return database.with_name(f"{database.name}{BACKUP_SUFFIX}")


def _temporary_name(database: Path) -> Path:
    return database.with_name(f"{database.name}{BACKUP_SUFFIX}.tmp")


def _write_backup(database: Path) -> Path:
    """Back ``database`` up the way the conversion does: under its write lock, after the first read."""
    with ExitStack() as descriptors, closing(connect_conversation_database(database)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        backup = write_conversion_backup(database, descriptors)
        connection.rollback()
    return backup


def _other_process_takes_write_lock(database: Path) -> str:
    completed = subprocess.run(
        [sys.executable, "-I", "-c", _TAKE_WRITE_LOCK, str(database)],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


def _descriptors_of(path: Path) -> list[str]:
    target = path.resolve()
    descriptors = []
    for entry in _PROC_FDS.iterdir():
        try:
            if entry.readlink() == target:
                descriptors.append(entry.name)
        except OSError:
            continue
    return descriptors


@pytest.mark.parametrize("golden", golden_stores(), ids=lambda golden: golden.label)
def test_a_backup_is_a_byte_identical_copy_beside_its_database(tmp_path: Path, golden: GoldenStore) -> None:
    """Every golden database's backup holds its bytes, and the database itself is left as it was."""
    database = golden.restore(tmp_path)
    original = database.read_bytes()

    backup = _write_backup(database)

    assert backup == _backup_name(database)
    assert backup.read_bytes() == original
    assert database.read_bytes() == original
    assert sorted(path.name for path in database.parent.iterdir()) == sorted([database.name, backup.name])


def _plant_stale_file(name: Path, _target: Path) -> None:
    name.write_bytes(b"an earlier attempt's partial copy" * 4096)


def _plant_link(name: Path, target: Path) -> None:
    name.symlink_to(target)


def _plant_dangling_link(name: Path, target: Path) -> None:
    name.symlink_to(target.with_name("missing"))


def _plant_hard_link(name: Path, target: Path) -> None:
    os.link(target, name)


@pytest.mark.parametrize("entry_name", [_backup_name, _temporary_name], ids=["backup", "temporary"])
@pytest.mark.parametrize(
    "plant",
    [_plant_stale_file, _plant_link, _plant_dangling_link, _plant_hard_link],
    ids=["stale-file", "link", "dangling-link", "hard-link"],
)
def test_an_entry_planted_at_a_backup_file_name_is_replaced_without_touching_its_target(
    tmp_path: Path,
    entry_name: Callable[[Path], Path],
    plant: Callable[[Path, Path], None],
) -> None:
    """Worker code can write the sessions directory, so whatever sits at either name loses only its own entry."""
    database = _database(tmp_path)
    target = tmp_path / "primary-file"
    target.write_bytes(b"the primary's own file")
    plant(entry_name(database), target)

    backup = _write_backup(database)

    assert not backup.is_symlink()
    assert backup.stat().st_nlink == 1
    assert backup.read_bytes() == database.read_bytes()
    assert not os.path.lexists(_temporary_name(database))
    assert target.read_bytes() == b"the primary's own file"
    assert target.stat().st_nlink == 1
    assert not target.with_name("missing").exists()


def test_a_file_planted_at_the_temporary_name_after_its_removal_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The temporary file is created exclusively, so a file planted after the cleanup is never written through."""
    database = _database(tmp_path)
    temporary = _temporary_name(database)
    real_unlink = os.unlink

    def unlink_then_plant(path: str, *, dir_fd: int | None = None) -> None:
        try:
            real_unlink(path, dir_fd=dir_fd)
        finally:
            if path == temporary.name:
                temporary.write_bytes(b"planted")

    monkeypatch.setattr(os, "unlink", unlink_then_plant)
    with pytest.raises(FileExistsError):
        _write_backup(database)

    assert temporary.read_bytes() == b"planted"
    assert not os.path.lexists(_backup_name(database))


def test_a_temporary_file_hard_linked_after_its_creation_is_refused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A temporary file that gains another link before the copy is refused, so no bytes reach the other name."""
    database = _database(tmp_path)
    temporary = _temporary_name(database)
    planted = tmp_path / "planted"
    real_open = os.open

    def open_then_link(path: str, flags: int, mode: int = 0o777, *, dir_fd: int | None = None) -> int:
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if path == temporary.name:
            os.link(temporary, planted)
        return descriptor

    monkeypatch.setattr(os, "open", open_then_link)
    with pytest.raises(ValueError, match="another hard link"):
        _write_backup(database)

    assert planted.read_bytes() == b""
    assert not os.path.lexists(_backup_name(database))


@pytest.mark.parametrize("link", ["database", "directory"])
def test_a_linked_database_or_sessions_directory_is_not_backed_up(tmp_path: Path, link: str) -> None:
    """The copy follows no link for the database or its directory, so it never reads a file the link names."""
    elsewhere = _database(tmp_path / "elsewhere")
    database = tmp_path / "state" / "agents" / "code" / "sessions" / "code.db"
    if link == "database":
        database.parent.mkdir(parents=True)
        database.symlink_to(elsewhere)
    else:
        database.parent.parent.mkdir(parents=True)
        database.parent.symlink_to(elsewhere.parent, target_is_directory=True)

    with pytest.raises(OSError, match=r"Too many levels of symbolic links|Not a directory"):
        _write_backup(database)

    assert sorted(path.name for path in elsewhere.parent.iterdir()) == [elsewhere.name]


def test_an_earlier_attempts_backup_does_not_count_against_the_free_space(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry after a rolled-back attempt needs the same free space as the first attempt, beside that attempt's copy."""
    database = _database(tmp_path)
    needed = 5 * database.stat().st_size // 2
    _backup_name(database).write_bytes(database.read_bytes())

    def free_space(_directory: int) -> SimpleNamespace:
        # The disk had ``needed`` bytes free before the earlier attempt wrote its copy.
        leftover = _backup_name(database).stat().st_size if _backup_name(database).is_file() else 0
        return SimpleNamespace(f_bavail=needed - leftover, f_frsize=1)

    monkeypatch.setattr(os, "statvfs", free_space)

    assert _write_backup(database).read_bytes() == database.read_bytes()


def test_a_leftover_temporary_file_is_replaced(tmp_path: Path) -> None:
    """A crashed attempt's temporary file, even one larger than the database, is replaced by a fresh copy."""
    database = _database(tmp_path)
    _temporary_name(database).write_bytes(b"x" * (2 * database.stat().st_size))

    backup = _write_backup(database)

    assert backup.read_bytes() == database.read_bytes()
    assert not os.path.lexists(_temporary_name(database))


@pytest.mark.parametrize(("spare", "written"), [(-1, False), (0, True)])
def test_the_backup_needs_two_and_a_half_times_the_database_size_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    spare: int,
    written: bool,
) -> None:
    """Below two and a half times the file's size, the error names the database and the bytes, and nothing is written."""
    database = _database(tmp_path)
    needed = 5 * database.stat().st_size // 2
    monkeypatch.setattr(os, "statvfs", lambda _directory: SimpleNamespace(f_bavail=needed + spare, f_frsize=1))

    if written:
        assert _write_backup(database).read_bytes() == database.read_bytes()
        return
    with pytest.raises(ConversionSpaceError) as raised:
        _write_backup(database)
    assert str(database) in str(raised.value)
    assert f"needs {needed} bytes" in str(raised.value)
    assert f"only {needed + spare} are free" in str(raised.value)
    assert sorted(path.name for path in database.parent.iterdir()) == [database.name]


def test_free_space_counts_blocks_available_to_the_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocks reserved for the superuser do not count, and blocks count at the fragment size."""
    database = _database(tmp_path)
    needed = 5 * database.stat().st_size // 2
    monkeypatch.setattr(
        os,
        "statvfs",
        lambda _directory: SimpleNamespace(f_bavail=needed // 4096 - 1, f_bfree=needed, f_frsize=4096, f_bsize=8192),
    )

    with pytest.raises(ConversionSpaceError):
        _write_backup(database)


def test_the_whole_copy_and_its_rename_are_synced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every copied byte is on disk before the rename, and the rename is on disk before the backup is reported."""
    # Small pages leave the whole file in the copy's write buffer, so only a flush before the sync writes it.
    database = tmp_path / "sessions" / "small.db"
    database.parent.mkdir()
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript("PRAGMA page_size = 512; CREATE TABLE small (value TEXT);")
    real_fsync = os.fsync
    synced: list[tuple[int, int, int | None]] = []

    def recording_fsync(descriptor: int) -> None:
        real_fsync(descriptor)
        status = os.fstat(descriptor)
        synced.append((status.st_dev, status.st_ino, status.st_size if stat.S_ISREG(status.st_mode) else None))

    monkeypatch.setattr(os, "fsync", recording_fsync)
    backup = _write_backup(database)

    backup_status, directory_status = backup.stat(), database.parent.stat()
    assert synced == [
        (backup_status.st_dev, backup_status.st_ino, database.stat().st_size),
        (directory_status.st_dev, directory_status.st_ino, None),
    ]


def test_the_write_lock_outlives_the_backup(tmp_path: Path) -> None:
    """Closing a descriptor of the database would drop SQLite's locks, so the copy's closes when the caller says."""
    database = _database(tmp_path)

    with ExitStack() as descriptors:
        with closing(connect_conversation_database(database)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            write_conversion_backup(database, descriptors)

            assert _other_process_takes_write_lock(database) == "database is locked"

            connection.rollback()
        if _PROC_FDS.is_dir():
            assert len(_descriptors_of(database)) == 1

    assert _other_process_takes_write_lock(database) == "locked"
    if _PROC_FDS.is_dir():
        assert _descriptors_of(database) == []


@pytest.mark.asyncio
async def test_neither_the_startup_sweep_nor_the_debug_report_takes_a_backup_for_a_database(tmp_path: Path) -> None:
    """The backup name does not end in ``.db``, so it is never converted or reported as a conversation database."""
    paths = resolve_runtime_paths(config_path=tmp_path / "config.yaml", storage_path=tmp_path / "state", process_env={})
    root = paths.storage_root
    database = create_agno_2_sessions_db(root / "agents" / "code" / "sessions" / "code.db")
    backup = _write_backup(database)
    backup_bytes = backup.read_bytes()

    await migrate_conversation_storage(paths)

    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT count(*) FROM code_sessions_usage").fetchone() == (3,)
    assert backup.read_bytes() == backup_bytes
    assert _session_databases(root) == [database]
