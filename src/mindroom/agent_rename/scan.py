"""List what in the storage directory still names the old identity after an agent account rename.

The rename moves what MindRoom looks up by the Matrix ID, but some state stays on purpose: the old crypto store,
the desktop pairing, history records. The scan finds every remaining place that names the old identity,
so the caller can split the ones its own reports already announced from the ones nobody expected.
It checks the names of files and directories, the contents of JSON and YAML files, and every text cell
of the event journal. Needles that are Matrix user IDs match the way the journal rewrite matches them,
so the scan agrees with the rewrite about what still counts as the old ID.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from mindroom.agent_rename.matching import contains_user_id
from mindroom.agent_rename.sqlite_read import quote_identifier, read_only_connection, text_columns
from mindroom.agent_rename.steps import BACKUPS_DIRECTORY_NAME
from mindroom.event_journal.opening import event_journal_sqlite_path

# Directly below the storage root: the Matrix crypto stores stay by design, and the backups hold the old state.
_SKIPPED_ROOT_DIRECTORIES = frozenset({"encryption_keys", BACKUPS_DIRECTORY_NAME})
# Below ``workers/``: package installs and caches never name an identity and can hold thousands of files.
_WORKERS_DIRECTORY = "workers"
_SKIPPED_WORKER_DIRECTORIES = frozenset({"venv", "cache"})
_DATA_SUFFIXES = frozenset({".json", ".yaml", ".yml"})
# History files such as the journal-size records can grow far beyond what is worth reading into memory.
_MAX_DATA_FILE_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class _LeftoverReport:
    """What still names the old identity, as sorted lines ready to print.

    ``expected`` holds the paths the rename itself reported as staying; ``unexpected`` holds every other path,
    ``journal: table.column (N cells)`` line, and note about something the scan could not read.
    Paths are relative to the storage root.
    """

    unexpected: tuple[str, ...]
    expected: tuple[str, ...]


def scan_leftovers(storage_root: Path, *, needles: tuple[str, ...], expected: frozenset[Path]) -> _LeftoverReport:
    """Report every place under ``storage_root`` that still names one of ``needles``.

    A needle that starts with ``@`` and contains ``:`` is a Matrix user ID and matches only as a whole ID,
    the way the journal rewrite matches it; any other needle matches as a plain substring.
    A path is expected when it equals one of ``expected`` or lies inside one; absolute paths under the storage root
    and paths relative to it both work. Journal cells are never expected.
    Symlinks are not followed, and files that cannot be read or are too large are named instead of silently passed.
    """
    wanted = _Needles(tuple(needle for needle in needles if needle))
    found = _scan_files(storage_root, wanted)
    expected_paths = {_relative_to(storage_root, path) for path in expected}
    unexpected = {line for path, line in found if not _is_expected(path, expected_paths)}
    unexpected.update(_scan_journal(storage_root, wanted))
    return _LeftoverReport(
        unexpected=tuple(sorted(unexpected)),
        expected=tuple(sorted({line for path, line in found if _is_expected(path, expected_paths)})),
    )


class _Needles:
    """The strings to look for, split by how each one matches."""

    def __init__(self, needles: tuple[str, ...]) -> None:
        self.user_ids = tuple(needle for needle in needles if needle.startswith("@") and ":" in needle)
        self.substrings = tuple(needle for needle in needles if needle not in self.user_ids)

    def named_in(self, text: str) -> bool:
        """Whether ``text`` names any needle."""
        return any(contains_user_id(text, user_id) for user_id in self.user_ids) or any(
            substring in text for substring in self.substrings
        )


def _relative_to(storage_root: Path, path: Path) -> Path:
    try:
        return path.relative_to(storage_root)
    except ValueError:
        return path


def _is_expected(path: Path, expected: set[Path]) -> bool:
    return path in expected or not expected.isdisjoint(path.parents)


def _is_skipped(relative: Path) -> bool:
    parts = relative.parts
    if len(parts) == 1:
        return parts[0] in _SKIPPED_ROOT_DIRECTORIES
    return parts[0] == _WORKERS_DIRECTORY and parts[-1] in _SKIPPED_WORKER_DIRECTORIES


def _scan_files(storage_root: Path, needles: _Needles) -> list[tuple[Path, str]]:
    """Return ``(relative path, report line)`` for every file or directory that names a needle."""
    found: list[tuple[Path, str]] = []

    def unreadable(error: OSError) -> None:
        relative = _relative_to(storage_root, Path(str(error.filename)))
        found.append((relative, f"{relative.as_posix()}: cannot read ({error.strerror})"))

    for directory, directory_names, file_names in os.walk(storage_root, onerror=unreadable):
        current = _relative_to(storage_root, Path(directory))
        directory_names[:] = [name for name in directory_names if not _is_skipped(current / name)]
        named = [current / name for name in (*directory_names, *file_names) if needles.named_in(name)]
        found.extend((relative, relative.as_posix()) for relative in named)
        for name in file_names:
            path = Path(directory) / name
            if path.suffix.lower() in _DATA_SUFFIXES and not path.is_symlink() and path.is_file():
                line = _scan_data_file(path, current / name, needles)
                if line:
                    found.append((current / name, line))
    return found


def _scan_data_file(path: Path, relative: Path, needles: _Needles) -> str | None:
    """Return the report line for one data file, or ``None`` when it does not name a needle."""
    try:
        if path.stat().st_size > _MAX_DATA_FILE_BYTES:
            return f"{relative.as_posix()}: not scanned, larger than {_MAX_DATA_FILE_BYTES // (1024 * 1024)} MB"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"{relative.as_posix()}: cannot read ({error.strerror})"
    return relative.as_posix() if needles.named_in(text) else None


def _scan_journal(storage_root: Path, needles: _Needles) -> list[str]:
    """Return one ``journal: table.column (N cells)`` line per text column that holds a needle."""
    database = event_journal_sqlite_path(storage_root)
    if not database.exists():
        return []
    try:
        with read_only_connection(database) as connection:
            connection.create_function("contains_user_id", 2, _sql_contains_user_id, deterministic=True)
            return _journal_lines(connection, needles)
    except (sqlite3.Error, OSError) as error:
        return [f"journal: cannot read {_relative_to(storage_root, database).as_posix()}: {error}"]


def _sql_contains_user_id(value: str | bytes | float | None, user_id: str) -> bool:
    return isinstance(value, str) and contains_user_id(value, user_id)


def _journal_lines(connection: sqlite3.Connection, needles: _Needles) -> list[str]:
    parameters = {f"user_id{index}": user_id for index, user_id in enumerate(needles.user_ids)}
    parameters |= {f"substring{index}": substring for index, substring in enumerate(needles.substrings)}
    lines: list[str] = []
    for table, columns in text_columns(connection).items():
        if not columns or not parameters:
            continue
        sums = ", ".join(f"sum({_names_a_needle(quote_identifier(column), needles)})" for column in columns)
        counts = connection.execute(f"SELECT {sums} FROM {quote_identifier(table)}", parameters).fetchone()  # noqa: S608 - quoted schema identifiers
        lines.extend(
            f"journal: {table}.{column} ({count} cell{'' if count == 1 else 's'})"
            for column, count in zip(columns, counts, strict=True)
            if count
        )
    return lines


def _names_a_needle(quoted_column: str, needles: _Needles) -> str:
    """Return a condition true for a text cell that names any needle, using the parameters of ``_journal_lines``."""
    holds = [
        f"(instr({quoted_column}, :user_id{index}) > 0 AND contains_user_id({quoted_column}, :user_id{index}))"
        for index in range(len(needles.user_ids))
    ]
    holds += [f"instr({quoted_column}, :substring{index}) > 0" for index in range(len(needles.substrings))]
    return f"(typeof({quoted_column}) = 'text' AND ({' OR '.join(holds)}))"
