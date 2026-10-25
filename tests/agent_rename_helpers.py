"""Shared helpers for the agent rename tests."""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from mindroom.agent_rename.steps import BACKUPS_DIRECTORY_NAME

SQLITE_SIDE_FILES = ("-wal", "-shm", "-journal")


def tree_snapshot(root: Path) -> dict[str, bytes | str]:
    """Map every path under ``root`` to its file bytes, ``"dir"``, or ``"symlink:<target>"``."""
    snapshot: dict[str, bytes | str] = {}
    for current, directory_names, file_names in os.walk(root):
        for name in (*directory_names, *file_names):
            path = Path(current) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                snapshot[relative] = f"symlink:{path.readlink()}"
            elif path.is_dir():
                snapshot[relative] = "dir"
            else:
                snapshot[relative] = path.read_bytes()
    return snapshot


def sqlite_rows(database: Path) -> list[str]:
    """Return the rows a reader of ``database`` sees, recovering a copy so the original and its side files stay."""
    with tempfile.TemporaryDirectory() as directory:
        for suffix in ("", *SQLITE_SIDE_FILES):
            if Path(f"{database}{suffix}").exists():
                shutil.copy(f"{database}{suffix}", Path(directory) / f"{database.name}{suffix}")
        with closing(sqlite3.connect(Path(directory) / database.name)) as connection:
            return list(connection.iterdump())


def tree_rows(root: Path) -> dict[str, object]:
    """Map every path under ``root`` outside the backups to its bytes, or its rows for a SQLite database.

    A database's ``-wal`` and ``-journal`` files count as part of its rows, which is what SQLite reads.
    """
    tree: dict[str, object] = {}
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if relative.parts[0] == BACKUPS_DIRECTORY_NAME or path.name.endswith(SQLITE_SIDE_FILES):
            continue
        if path.is_dir():
            tree[relative.as_posix()] = "dir"
        elif path.suffix == ".db":
            tree[relative.as_posix()] = sqlite_rows(path)
        else:
            tree[relative.as_posix()] = path.read_bytes()
    return tree


def sqlite_schema(database: Path) -> set[tuple[str, str, str, str]]:
    """Return every schema entry with its SQL, unquoted and with whitespace collapsed, so only real changes differ."""
    with closing(sqlite3.connect(database)) as connection:
        rows = connection.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    return {(kind, name, table, re.sub(r"\s+", " ", (sql or "").replace('"', ""))) for kind, name, table, sql in rows}
