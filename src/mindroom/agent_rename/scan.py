"""List what in the storage directory still names the old identity after an agent rename.

The rename moves what MindRoom looks up by the Matrix ID or the agent's config name, but some state stays on purpose:
the old crypto store, the desktop pairing, history records. The scan finds every remaining place that names the old
identity, so the caller can split the ones its own reports already announced from the ones nobody expected.
It checks the names of files and directories, the contents of JSON and YAML files, and every text cell
of the event journal. Needles that are Matrix user IDs match the way the journal rewrite matches them,
so the scan agrees with the rewrite about what still counts as the old ID.
An agent's config name matches only as a whole: a path component, a string value or key, or a journal cell that is
one of the spellings MindRoom writes the name in, so ``code_team`` never counts as ``code``.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from mindroom import yaml_io
from mindroom.agent_rename.matching import contains_user_id
from mindroom.agent_rename.sqlite_read import quote_identifier, read_only_connection, text_columns
from mindroom.agent_rename.steps import BACKUPS_DIRECTORY_NAME
from mindroom.credentials.manager import scoped_credentials_dir_part
from mindroom.event_journal.opening import event_journal_sqlite_path
from mindroom.tool_system.worker_routing import agent_state_root_path, shared_storage_root, worker_key_agent_name

if TYPE_CHECKING:
    from collections.abc import Iterator

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


def scan_leftovers(
    storage_root: Path,
    *,
    needles: tuple[str, ...],
    expected: frozenset[Path],
    names: tuple[str, ...] = (),
) -> _LeftoverReport:
    """Report every place under ``storage_root`` that still names one of ``needles`` or ``names``.

    A needle that starts with ``@`` and contains ``:`` is a Matrix user ID and matches only as a whole ID,
    the way the journal rewrite matches it; any other needle matches as a plain substring.
    A name is an agent's config name and matches only as one of its whole spellings (see ``_names_agent``)
    in a path component, in a string value or key of a JSON or YAML file, or in a journal cell.
    A path is expected when it equals one of ``expected`` or lies inside one; absolute paths under the storage root
    and paths relative to it both work. Journal cells are never expected, and neither are dangling symlinks whose
    text points into the state root ``agents/<name>`` of a name, through which MindRoom would create that root again.
    Symlinks are not followed, and files that cannot be read or are too large are named instead of silently passed,
    and so are JSON and YAML files that do not parse although their text holds a name.
    """
    wanted = _Needles(tuple(needle for needle in needles if needle), tuple(name for name in names if name))
    found, dangling = _scan_files(storage_root, wanted)
    expected_paths = {_relative_to(storage_root, path) for path in expected}
    unexpected = {line for path, line in found if not _is_expected(path, expected_paths)}
    unexpected.update(dangling)
    unexpected.update(_scan_journal(storage_root, wanted))
    return _LeftoverReport(
        unexpected=tuple(sorted(unexpected)),
        expected=tuple(sorted({line for path, line in found if _is_expected(path, expected_paths)})),
    )


class _Needles:
    """The strings to look for, split by how each one matches."""

    def __init__(self, needles: tuple[str, ...], names: tuple[str, ...]) -> None:
        self.user_ids = tuple(needle for needle in needles if needle.startswith("@") and ":" in needle)
        self.substrings = tuple(needle for needle in needles if needle not in self.user_ids)
        self.names = names

    def named_in(self, text: str) -> bool:
        """Whether ``text`` names any needle."""
        return any(contains_user_id(text, user_id) for user_id in self.user_ids) or any(
            substring in text for substring in self.substrings
        )

    def is_name_spelling(self, text: str) -> bool:
        """Whether the whole of ``text`` is a spelling of any name."""
        return any(_names_agent(text, name) for name in self.names)

    def may_hold_name(self, text: str) -> bool:
        """Whether ``text`` holds a part that every spelling of some name contains, so a value in it can be one."""
        return any(part in text for name in self.names for part in _spelling_parts(name))


@cache
def _spellings(name: str) -> re.Pattern[str]:
    """Return the pattern that matches the whole of any text MindRoom writes the agent ``name`` into."""
    escaped = re.escape(name)
    # The spellings below copy names that the modules owning each store keep private: history/team_scope.py's team
    # scope ids and their requester suffix, tool_system/worker_routing.py's worker keys and worker_dir_name,
    # knowledge/file_memory_knowledge.py's index names, and the legacy files that agent_rename/indexes.py lists.
    return re.compile(
        rf"""
          (?:agent_|agent:|model:)?{escaped}
        | {escaped}(?:\.(?:db|png)|\.db-(?:wal|shm|journal)|\.json(?:\.lock)?)
        | {escaped}_responded\.json.*
        | {escaped}@.*
        | agents/{escaped}
        | dispatch_obligations-{escaped}-.*
        | (?:file_memory_)?agent_{escaped}_[0-9a-f]{{16}}(?:_[0-9a-f]{{8}})?
        | [A-Za-z0-9._@+-]*_{escaped}-[0-9a-f]{{16}}
        | {re.escape(scoped_credentials_dir_part(name))}
        | team_(?:[^+]*\+)*{escaped}(?:\+[^+]*)*(?:_requester_[0-9a-f]{{12}})?
        """,
        re.VERBOSE | re.DOTALL,
    )


@cache
def _spelling_parts(name: str) -> tuple[str, ...]:
    """Return texts of which each spelling of ``name`` contains at least one.

    Every spelling holds the name itself, except the OAuth directory, which trims ``_`` from the ends of the name and
    cuts it to 80 characters.
    MindRoom writes names into JSON and YAML unescaped, since agent names hold only letters, digits and ``_``.
    """
    return (name, scoped_credentials_dir_part(name))


def _names_agent(text: str, name: str) -> bool:
    """Whether ``text`` is one of the whole spellings of the agent ``name``, never a longer name that starts with it.

    The spellings are the name itself and its composed forms (``agent_<name>``, ``agent:<name>``, ``model:<name>``),
    the files named after it, a principal ``<name>@<user ID>``, the state root key ``agents/<name>``,
    a team scope id with the name among its members, and the worker keys, worker directories and OAuth directories
    that embed it.
    """
    return _spellings(name).fullmatch(text) is not None or worker_key_agent_name(text) == name


def _relative_to(storage_root: Path, path: Path) -> Path:
    try:
        return path.relative_to(storage_root)
    except ValueError:
        return path


def _is_expected(path: Path, expected: set[Path]) -> bool:
    return path in expected or not expected.isdisjoint(path.parents)


def is_skipped(relative: Path) -> bool:
    """Whether the walk of the storage root leaves out ``relative``, a path relative to the storage root."""
    parts = relative.parts
    if len(parts) == 1:
        return parts[0] in _SKIPPED_ROOT_DIRECTORIES
    return parts[0] == _WORKERS_DIRECTORY and parts[-1] in _SKIPPED_WORKER_DIRECTORIES


def _scan_files(storage_root: Path, needles: _Needles) -> tuple[list[tuple[Path, str]], list[str]]:
    """Return ``(relative path, report line)`` for every path that names a needle, and the dangling-link lines.

    A dangling-link line names a symlink whose target no longer exists and whose text points into the state root
    ``agents/<name>`` of a name.
    """
    found: list[tuple[Path, str]] = []
    dangling: list[str] = []
    resolved_root = shared_storage_root(storage_root)
    name_roots = tuple(agent_state_root_path(storage_root, name) for name in needles.names)

    def unreadable(error: OSError) -> None:
        relative = _relative_to(storage_root, Path(str(error.filename)))
        found.append((relative, f"{relative.as_posix()}: cannot read ({error.strerror})"))

    for directory, directory_names, file_names in os.walk(storage_root, onerror=unreadable):
        current = _relative_to(storage_root, Path(directory))
        directory_names[:] = [name for name in directory_names if not is_skipped(current / name)]
        named = [
            current / name
            for name in (*directory_names, *file_names)
            if needles.named_in(name) or needles.is_name_spelling(name)
        ]
        found.extend((relative, relative.as_posix()) for relative in named)
        for name in file_names:
            path = Path(directory) / name
            if path.is_symlink():
                # Dangling links are listed among the files; the text is read from the real directory holding it.
                text = os.readlink(path)  # noqa: PTH115 - the report shows the text as written
                target = Path(os.path.normpath(resolved_root / current / text))
                if not path.exists() and any(target.is_relative_to(root) for root in name_roots):
                    dangling.append(f"{(current / name).as_posix()}: dangling symlink to {text}")
            elif path.suffix.lower() in _DATA_SUFFIXES and path.is_file():
                line = _scan_data_file(path, current / name, needles)
                if line:
                    found.append((current / name, line))
    return found, dangling


def _scan_data_file(path: Path, relative: Path, needles: _Needles) -> str | None:
    """Return the report line for one data file, or ``None`` when it does not name a needle or a name."""
    try:
        if path.stat().st_size > _MAX_DATA_FILE_BYTES:
            return f"{relative.as_posix()}: not scanned, larger than {_MAX_DATA_FILE_BYTES // (1024 * 1024)} MB"
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as error:
        return f"{relative.as_posix()}: cannot read ({error.strerror})"
    if needles.named_in(text):
        return relative.as_posix()
    # Only a file whose text holds a name is parsed, so data files that never name the agent, such as a workspace's
    # tsconfig.json with comments, are not listed as unscanned.
    if not needles.may_hold_name(text):
        return None
    try:
        document = _parse(text, path.suffix.lower())
    except (ValueError, RecursionError, yaml.YAMLError):
        return f"{relative.as_posix()}: not scanned for names, it is not valid JSON or YAML"
    return relative.as_posix() if any(needles.is_name_spelling(string) for string in _strings(document)) else None


def _parse(text: str, suffix: str) -> object:
    """Parse a JSON or YAML file; a file without content holds no values."""
    if not text.strip():
        return None
    return json.loads(text) if suffix == ".json" else yaml_io.safe_load_without_aliases(text)


def _strings(document: object) -> Iterator[str]:
    """Yield every string in a parsed document, at any depth: its values and its mapping keys.

    Keys count because MindRoom keys records by an agent: ``agent_<name>`` accounts in ``matrix_state.yaml``,
    ``model:<name>`` fields of thread records, and the ``<name>`` and ``agents/<name>`` keys of auto-flush progress.
    """
    pending = [document]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _scan_journal(storage_root: Path, needles: _Needles) -> list[str]:
    """Return one ``journal: table.column (N cells)`` line per text column that holds a needle."""
    database = event_journal_sqlite_path(storage_root)
    if not database.exists():
        return []
    try:
        with read_only_connection(database) as connection:
            connection.create_function("contains_user_id", 2, _sql_contains_user_id, deterministic=True)
            connection.create_function("names_agent", 2, _sql_names_agent, deterministic=True)
            return _journal_lines(connection, needles)
    except (sqlite3.Error, OSError) as error:
        return [f"journal: cannot read {_relative_to(storage_root, database).as_posix()}: {error}"]


def _sql_contains_user_id(value: str | bytes | float | None, user_id: str) -> bool:
    return isinstance(value, str) and contains_user_id(value, user_id)


def _sql_names_agent(value: str | bytes | float | None, name: str) -> bool:
    return isinstance(value, str) and _names_agent(value, name)


def _journal_lines(connection: sqlite3.Connection, needles: _Needles) -> list[str]:
    parameters = {f"user_id{index}": user_id for index, user_id in enumerate(needles.user_ids)}
    parameters |= {f"substring{index}": substring for index, substring in enumerate(needles.substrings)}
    parameters |= {f"name{index}": name for index, name in enumerate(needles.names)}
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
    holds += [
        f"(instr({quoted_column}, :name{index}) > 0 AND names_agent({quoted_column}, :name{index}))"
        for index in range(len(needles.names))
    ]
    return f"(typeof({quoted_column}) = 'text' AND ({' OR '.join(holds)}))"
