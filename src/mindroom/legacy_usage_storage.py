"""One-time import of retained conversation facts into independent usage storage."""

from __future__ import annotations

import json
import sqlite3
from typing import TYPE_CHECKING, cast

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import resolve_session_state_root
from mindroom.legacy_session_storage import decode_persisted_session_json
from mindroom.usage_storage import project_usage, quote_identifier, usage_table_sql, usage_upsert_sql

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from mindroom.constants import RuntimePaths


# LEGACY_COMPAT: Usage derived from disposable conversation rows and session blobs.
# Legacy format: Agno 3 run_data rows and retained Agno 2 single/double-encoded runs blobs.
# Last legacy release: Schema-based recovery; all releases through v2026.9.186 lack the usage table.
# Replacement: Unreleased independent usage snapshots; no historical billing completeness is inferred.
# Handling: Seed once transactionally, prefer current rows, retain unknown dates and content-free gaps.
# Coverage: tests/test_legacy_usage_storage.py.
def migrate_usage_database(path: Path, session_table: str) -> None:
    """Seed an existing database once; publication and all imported records commit together."""
    if not path.is_file():
        return
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, timeout=30)
    try:
        if not _needs_migration(connection, session_table):
            return
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        # Another startup may have finished while this connection waited for its write lock.
        if _needs_migration(connection, session_table):
            connection.execute(usage_table_sql(session_table))
            _seed_usage(connection, session_table)
        connection.commit()
    finally:
        # close rolls back both DDL and rows on interruption, leaving migration retryable.
        connection.close()


def _needs_migration(connection: sqlite3.Connection, session_table: str) -> bool:
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return session_table in tables and f"{session_table}_usage" not in tables


def _seed_usage(connection: sqlite3.Connection, session_table: str) -> None:
    columns = {row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(session_table)})")}
    legacy_column = "runs" if "runs" in columns else "NULL"
    runs_table = f"{session_table}_runs"
    has_runs = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (runs_table,),
    ).fetchone()
    for session_id, blob in connection.execute(
        f"SELECT session_id, {legacy_column} FROM {quote_identifier(session_table)}",  # noqa: S608
    ):
        known_ids: set[str] = set()
        if has_runs:
            for run_id, raw, created_at in connection.execute(
                f"SELECT run_id, run_data, created_at FROM {quote_identifier(runs_table)} WHERE session_id = ?",  # noqa: S608
                (session_id,),
            ):
                known_ids.add(run_id)
                decoded = _decode(raw)
                run = {**decoded, "run_id": run_id, "created_at": created_at} if isinstance(decoded, dict) else None
                _insert_usage(connection, session_table, session_id, run_id, run)
        decoded_blob = _decode(blob)
        # SQL NULL and JSON null were Agno's native empty-history values.
        if decoded_blob is None:
            continue
        for run in decoded_blob if isinstance(decoded_blob, list) else [None]:
            raw_id = cast("dict[str, object]", run).get("run_id") if isinstance(run, dict) else None
            run_id = raw_id if isinstance(raw_id, str) and raw_id else None
            if run_id is not None and run_id in known_ids:
                continue
            if run_id is not None:
                known_ids.add(run_id)
            _insert_usage(connection, session_table, session_id, run_id, run)


def _decode(raw: object) -> object:
    try:
        return decode_persisted_session_json(raw)
    except (RecursionError, TypeError, ValueError):
        return False


def _insert_usage(
    connection: sqlite3.Connection,
    session_table: str,
    session_id: str,
    run_id: str | None,
    run: object,
) -> None:
    payload = json.dumps(project_usage(cast("dict[str, object]", run))) if isinstance(run, dict) else None
    connection.execute(usage_upsert_sql(session_table), (session_id, run_id, payload))


async def migrate_usage_storage(runtime_paths: RuntimePaths) -> None:
    """Finish the startup import before runtime or read-only reporting is admitted."""
    await run_blocking_until_complete(_migrate_stores, runtime_paths)


def _migrate_stores(runtime_paths: RuntimePaths) -> None:
    root = resolve_session_state_root(runtime_paths.storage_root, runtime_paths)
    directories = [*_directories(root / "agents"), *_directories(root / "teams")]
    for scope in _directories(root / "private_instances"):
        directories.extend(_directories(scope))
    for directory in directories:
        session_dir = directory / "sessions"
        path = session_dir / f"{directory.name}.db"
        if not session_dir.is_symlink() and not path.is_symlink():
            migrate_usage_database(path, f"{directory.name}_sessions")


def _directories(root: Path) -> Iterator[Path]:
    if root.is_symlink() or not root.is_dir():
        return
    for child in sorted(root.iterdir()):
        if not child.is_symlink() and child.is_dir():
            yield child
