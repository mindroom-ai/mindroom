"""Tests for the scan that lists what still names the old identity after an agent account rename."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from mindroom.agent_rename.scan import _LeftoverReport, scan_leftovers
from mindroom.credentials.manager import scoped_credentials_dir_part
from mindroom.event_journal import EventJournalStore, EventKind, encode_thread_id
from mindroom.event_journal.opening import event_journal_sqlite_path
from tests.agent_rename_helpers import tree_snapshot

OLD = "@old:x"
NEW = "@new:x"
CREDENTIALS_PART = scoped_credentials_dir_part(OLD)
NEEDLES = (OLD, CREDENTIALS_PART)
ROOM = "!room:x"


def _write(root: Path, relative: str, content: str | bytes) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path


def _scan(root: Path, *expected: Path) -> _LeftoverReport:
    return scan_leftovers(root, needles=NEEDLES, expected=frozenset(expected))


def _event(connection: sqlite3.Connection, event_id: str, *, sender: str, source: dict[str, str]) -> None:
    connection.execute(
        "INSERT INTO journal_events (principal_id, event_id, room_id, thread_id, kind, sender, origin_server_ts, "
        "source_json, membership_epoch, state) VALUES (?, ?, ?, ?, ?, ?, 10, ?, 0, 'settled')",
        (f"nova@{NEW}", event_id, ROOM, encode_thread_id(None), EventKind.MESSAGE.value, sender, json.dumps(source)),
    )


async def _journal(root: Path) -> Path:
    """Create a journal with the real schema and return its path."""
    database = event_journal_sqlite_path(root)
    await EventJournalStore.open_sqlite(database).close()
    return database


@pytest.mark.asyncio
async def test_finds_paths_files_and_journal_cells(tmp_path: Path) -> None:
    """Path names, JSON and YAML contents and journal text cells that name the old identity are all listed."""
    (tmp_path / CREDENTIALS_PART).mkdir()
    _write(tmp_path, f"credentials/{CREDENTIALS_PART}.json", "{}")
    _write(tmp_path, "records/owner.json", json.dumps({"owner": OLD}))
    _write(tmp_path, "records/nested/deep/schedule.yaml", f'owner: "{OLD}"\n')
    _write(tmp_path, "config.yml", f"- {OLD}\n")
    _write(tmp_path, "records/clean.json", json.dumps({"owner": NEW}))
    _write(tmp_path, "records/other.yaml", "owner: '@older:x'\n")
    _write(tmp_path, "notes.txt", f"Not a data file, so not read: {OLD}")
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, "$one", sender=OLD, source={"sender": OLD})
        _event(connection, "$two", sender=OLD, source={"sender": NEW})
        _event(connection, "$three", sender=NEW, source={"sender": NEW})

    report = _scan(tmp_path)

    assert report == _LeftoverReport(
        unexpected=(
            "config.yml",
            f"credentials/{CREDENTIALS_PART}.json",
            "journal: journal_events.sender (2 cells)",
            "journal: journal_events.source_json (1 cell)",
            CREDENTIALS_PART,
            "records/nested/deep/schedule.yaml",
            "records/owner.json",
        ),
        expected=(),
    )


def test_expected_paths_are_listed_separately(tmp_path: Path) -> None:
    """A path the rename reports as staying, or anything inside it, is expected; absolute and relative both work."""
    desktop = _write(tmp_path, "desktop_bridge/native_config.json", json.dumps({"controller": {"user_id": OLD}}))
    _write(tmp_path, "kept/a/history.json", json.dumps({"owner": OLD}))
    _write(tmp_path, "kept/b.yaml", f"owner: {OLD}\n")
    _write(tmp_path, "keptover/other.json", json.dumps({"owner": OLD}))
    _write(tmp_path, "stray.json", json.dumps({"owner": OLD}))

    report = _scan(tmp_path, desktop, Path("kept"), tmp_path / "elsewhere" / "stray.json")

    assert report == _LeftoverReport(
        unexpected=("keptover/other.json", "stray.json"),
        expected=("desktop_bridge/native_config.json", "kept/a/history.json", "kept/b.yaml"),
    )


def test_skips_backups_and_encryption_keys(tmp_path: Path) -> None:
    """Backups and the crypto store are not reported, nor are worker virtualenvs and caches."""
    hit = json.dumps({"owner": OLD})
    _write(tmp_path, f"agent-rename-backups/20261019T120000Z-rename-account/000/{CREDENTIALS_PART}/a.json", hit)
    _write(tmp_path, f"encryption_keys/{CREDENTIALS_PART}/store.json", hit)
    _write(tmp_path, "workers/w1/venv/lib/site-packages/pkg/data.json", hit)
    _write(tmp_path, "workers/w1/cache/uv/data.yaml", f"owner: {OLD}\n")
    _write(tmp_path, "workers/w1/workspace/cache/data.json", hit)
    # The same names elsewhere are ordinary directories and are scanned.
    _write(tmp_path, "other/venv/data.json", hit)
    _write(tmp_path, "other/cache/data.json", hit)
    _write(tmp_path, "other/encryption_keys/data.json", hit)
    _write(tmp_path, "other/agent-rename-backups/data.json", hit)
    _write(tmp_path, "workers/w1/workspace/data.json", hit)

    report = _scan(tmp_path)

    assert report.expected == ()
    assert report.unexpected == (
        "other/agent-rename-backups/data.json",
        "other/cache/data.json",
        "other/encryption_keys/data.json",
        "other/venv/data.json",
        "workers/w1/workspace/data.json",
    )


def test_matches_user_ids_only_where_the_journal_rewrite_would(tmp_path: Path) -> None:
    """Other users' IDs that merely start with the old one are not leftovers; punctuation and device suffixes end it."""
    _write(tmp_path, "other-users.json", json.dumps(["@old:x.example", "@old:x:8448", "@old:x-bot:x", "@older:x"]))
    _write(tmp_path, "punctuation.json", json.dumps({"body": f"ask {OLD}.", "device": f"{OLD}:DEVICEID"}))
    (tmp_path / "records" / OLD).mkdir(parents=True)
    (tmp_path / "records" / "@old:x.example").mkdir()

    report = _scan(tmp_path)

    assert report.unexpected == ("punctuation.json", f"records/{OLD}")


@pytest.mark.asyncio
async def test_journal_cells_follow_the_same_rule(tmp_path: Path) -> None:
    """A journal cell naming a longer user ID is not counted, and non-text columns are ignored."""
    database = await _journal(tmp_path)
    with closing(sqlite3.connect(database)) as connection, connection:
        _event(connection, "$one", sender="@old:x.example", source={"sender": "@older:x"})
        _event(connection, "$two", sender=NEW, source={"note": f"via {CREDENTIALS_PART}"})

    report = _scan(tmp_path)

    assert report.unexpected == ("journal: journal_events.source_json (1 cell)",)


def test_large_data_files_are_reported_not_read(tmp_path: Path) -> None:
    """A data file over 5 MB is named instead of read, so the operator knows it was not checked."""
    big = tmp_path / "history" / "big.json"
    big.parent.mkdir()
    with big.open("wb") as handle:
        handle.truncate(5 * 1024 * 1024 + 1)

    assert _scan(tmp_path).unexpected == ("history/big.json: not scanned, larger than 5 MB",)


def test_symlinks_are_never_followed(tmp_path: Path) -> None:
    """A link to something outside the storage root is neither walked nor read."""
    outside = tmp_path / "outside"
    _write(outside, "dir/data.json", json.dumps({"owner": OLD}))
    _write(outside, "file.json", json.dumps({"owner": OLD}))
    root = tmp_path / "storage"
    root.mkdir()
    (root / "linked-dir").symlink_to(outside / "dir", target_is_directory=True)
    (root / "linked-file.json").symlink_to(outside / "file.json")

    assert _scan(root) == _LeftoverReport(unexpected=(), expected=())


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")
def test_unreadable_files_and_directories_are_named(tmp_path: Path) -> None:
    """What the scan cannot open is listed, so a clean report never hides a place it could not look."""
    locked_file = _write(tmp_path, "locked.json", "{}")
    locked_directory = tmp_path / "locked"
    _write(locked_directory, "inside.json", "{}")
    locked_file.chmod(0)
    locked_directory.chmod(0)
    try:
        report = _scan(tmp_path)
    finally:
        locked_file.chmod(0o600)
        locked_directory.chmod(0o700)

    assert report.expected == ()
    assert report.unexpected == (
        "locked.json: cannot read (Permission denied)",
        "locked: cannot read (Permission denied)",
    )


@pytest.mark.asyncio
async def test_journal_is_read_without_writing_next_to_it(tmp_path: Path) -> None:
    """Rows only a leftover WAL holds are seen, and the live files stay byte-identical."""
    database = await _journal(tmp_path)
    connection = sqlite3.connect(database)
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    with connection:
        _event(connection, "$only-in-wal", sender=OLD, source={"sender": NEW})
    saved = {suffix: Path(f"{database}{suffix}").read_bytes() for suffix in ("", "-wal")}
    connection.close()
    for suffix, content in saved.items():
        Path(f"{database}{suffix}").write_bytes(content)
    before = tree_snapshot(tmp_path)

    assert _scan(tmp_path).unexpected == ("journal: journal_events.sender (1 cell)",)
    assert tree_snapshot(tmp_path) == before


def test_unreadable_journal_is_reported(tmp_path: Path) -> None:
    """A journal SQLite cannot read is named instead of silently counted as clean."""
    database = event_journal_sqlite_path(tmp_path)
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not a database")

    (line,) = _scan(tmp_path).unexpected

    assert line.startswith("journal: cannot read tracking/event_journal.db: ")
