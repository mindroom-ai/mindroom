"""The conversion's backup and the downgrade it allows, as the operator procedure of spec v4 section 5 runs them.

Every storage golden, legacy input, and recorded end state converts with a backup holding its original bytes, and
restoring that backup gives the original file back, which the older release reads as it read it before, and which the
next start converts again the same way. An older release started on a converted file without restoring the backups
finds its required sessions columns missing and moves the sessions directory aside, backups included.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from mindroom.session_storage import preflight
from mindroom.session_storage.legacy_agno3_backup import BACKUP_SUFFIX
from mindroom.session_storage.preflight import session_storage_preflight
from mindroom.session_storage.schema import TableNames
from tests.storage_oracle.record import FIXTURES, GOLDEN_DUMPS, SEQUENCES, observe_store, sequence_id, storage_name_of

if TYPE_CHECKING:
    from pathlib import Path

_DUMPS = {
    **{f"goldens/{dump.stem}": dump for dump in GOLDEN_DUMPS},
    **{f"legacy/{dump.stem}": dump for dump in sorted((FIXTURES / "legacy").glob("*.sql"))},
    **{sequence_id(path): path.with_suffix(".sql") for path in SEQUENCES},
}
# The sessions columns the last release before storage v2 required of an existing database, frozen from its
# ``session_storage/schema.py`` (#3022's head, 59f7c9c56); its preflight archives a sessions directory lacking any.
_OLDER_RELEASE_SESSION_COLUMNS = frozenset(
    {
        "session_id",
        "session_type",
        "agent_id",
        "team_id",
        "workflow_id",
        "user_id",
        "session_data",
        "agent_data",
        "team_data",
        "workflow_data",
        "metadata",
        "summary",
        "created_at",
        "updated_at",
    },
)


def _restored(dump: Path, root: Path) -> tuple[TableNames, Path]:
    """Write ``dump`` to its database file under ``root``, as the older release left it."""
    text = dump.read_text(encoding="utf-8")
    names = TableNames(storage_name_of(text))
    database = names.database_path(root)
    database.parent.mkdir(parents=True)
    with closing(sqlite3.connect(database)) as connection:
        connection.executescript(text)
    return names, database


def _backup(database: Path) -> Path:
    return database.with_name(f"{database.name}{BACKUP_SUFFIX}")


@pytest.mark.parametrize("name", sorted(_DUMPS))
def test_restoring_the_backup_gives_the_original_which_converts_again_the_same_way(name: str, tmp_path: Path) -> None:
    """The backup holds the original's bytes; put back, it is the original file, and the next open converts it alike."""
    names, database = _restored(_DUMPS[name], tmp_path)
    original = database.read_bytes()
    converted = observe_store(names.storage_name, tmp_path)
    assert _backup(database).read_bytes() == original

    # The downgrade: replace the database with its backup, and delete any journal beside it.
    _backup(database).replace(database)
    database.with_name(f"{database.name}-journal").unlink(missing_ok=True)

    assert database.read_bytes() == original
    assert observe_store(names.storage_name, tmp_path) == converted
    assert _backup(database).read_bytes() == original


@pytest.mark.parametrize("dump", GOLDEN_DUMPS, ids=lambda dump: dump.stem)
def test_an_older_release_archives_a_converted_sessions_directory_with_its_backup(
    dump: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The older release's preflight moves the converted directory aside; moving the backup back recovers it."""
    names, database = _restored(dump, tmp_path)
    original = database.read_bytes()
    observe_store(names.storage_name, tmp_path)
    monkeypatch.setattr(preflight, "_REQUIRED_SESSION_COLUMNS", _OLDER_RELEASE_SESSION_COLUMNS)

    with session_storage_preflight(tmp_path, names, timeout_seconds=1):
        pass

    [archive] = tmp_path.glob("sessions.incompatible-*")
    assert sorted(path.name for path in archive.iterdir()) == sorted([database.name, _backup(database).name])
    assert not database.exists()
    assert (archive / _backup(database).name).read_bytes() == original

    # The recovery: move the backup back from the archive; the older release then keeps the directory.
    (archive / _backup(database).name).replace(database)
    with session_storage_preflight(tmp_path, names, timeout_seconds=1):
        pass

    assert list(tmp_path.glob("sessions.incompatible-*")) == [archive]
    assert database.read_bytes() == original
