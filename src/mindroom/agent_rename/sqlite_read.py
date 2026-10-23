"""Read a SQLite database during an agent rename without writing anything next to it."""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import closing, contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def read_only_connection(database: Path) -> Iterator[sqlite3.Connection]:
    """Read ``database`` without writing anything next to it.

    Even a read-only connection to a WAL database creates its ``-wal`` and ``-shm`` files, or updates the ``-shm``.
    MindRoom removes both when it closes cleanly, leaving every commit in the main file, which ``immutable`` reads
    exactly. A ``-wal`` left behind by a running or crashed MindRoom can hold commits the main file lacks, so the
    database and its WAL are copied to a temporary directory and read there; SQLite rebuilds the index from the WAL.
    """
    wal = Path(f"{database}-wal")
    if not wal.exists():
        with closing(sqlite3.connect(f"{database.absolute().as_uri()}?immutable=1", uri=True)) as connection:
            yield connection
        return
    with tempfile.TemporaryDirectory(prefix="mindroom-agent-rename-") as directory:
        copy = Path(directory) / database.name
        shutil.copyfile(database, copy)
        shutil.copyfile(wal, f"{copy}-wal")
        with closing(sqlite3.connect(copy)) as connection:
            yield connection


def quote_identifier(identifier: str) -> str:
    """Quote a table or column name for use in SQL."""
    return '"' + identifier.replace('"', '""') + '"'


def _holds_text(declared_type: str) -> bool:
    """Whether SQLite keeps text stored in a column of this type as text: TEXT affinity, or no type at all."""
    declared = declared_type.upper()
    return not declared or ("INT" not in declared and any(word in declared for word in ("CHAR", "CLOB", "TEXT")))


def text_columns(connection: sqlite3.Connection) -> dict[str, tuple[str, ...]]:
    """Map every ordinary table to its text columns.

    SQLite's own tables are skipped, and so are virtual tables and their shadow tables,
    which only the virtual table's module may write.
    """
    tables = connection.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall()
    virtual = [name for name, sql in tables if (sql or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")]
    columns: dict[str, tuple[str, ...]] = {}
    for name, _ in tables:
        if name.startswith("sqlite_") or any(name == table or name.startswith(f"{table}_") for table in virtual):
            continue
        rows = connection.execute("SELECT name, type FROM pragma_table_info(?)", (name,)).fetchall()
        columns[name] = tuple(column for column, declared_type in rows if _holds_text(declared_type))
    return columns
