"""Retire Agno 2 session run blobs without blocking the runtime process."""

from __future__ import annotations

import asyncio
import json
import multiprocessing
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, NoReturn, cast

from agno.db.sqlite import SqliteDb
from agno.db.utils import build_single_run_row
from sqlalchemy import bindparam, select, text, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mindroom.agent_storage import configure_state_engine_pragmas, create_state_engine
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from sqlalchemy import Table
    from sqlalchemy.orm import Session as OrmSession

logger = get_logger(__name__)
_MAX_CHANGED_BLOB_RETRIES = 2


@dataclass(frozen=True)
class _MigrationResult:
    """Counts from one migration scan."""

    migrated_sessions: int = 0
    failed_sessions: int = 0

    def __add__(self, other: _MigrationResult) -> _MigrationResult:
        """Combine counts from independently migrated databases or tables."""
        return _MigrationResult(
            migrated_sessions=self.migrated_sessions + other.migrated_sessions,
            failed_sessions=self.failed_sessions + other.failed_sessions,
        )


class _MigrationRefusedError(ValueError):
    """A legacy session cannot be represented losslessly as run rows."""


class _LegacyBlobChangedError(RuntimeError):
    """The compatibility blob changed between preparation and the writer transaction."""


def _refuse(reason: str) -> NoReturn:
    raise _MigrationRefusedError(reason)


def _quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _decode_legacy_runs(blob: object) -> list[dict[str, Any]]:
    """Decode both JSON layers written by Agno 2 and require stable run identities."""
    decoded = blob
    try:
        while isinstance(decoded, (str, bytes, bytearray)):
            decoded = json.loads(decoded)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        _refuse("invalid_json")
    if not isinstance(decoded, list) or not all(isinstance(run, dict) for run in decoded):
        _refuse("not_a_list_of_objects")
    runs = cast("list[dict[str, Any]]", decoded)
    run_ids = [run.get("run_id") for run in runs]
    if not all(isinstance(run_id, str) and run_id for run_id in run_ids):
        _refuse("missing_run_id")
    if len(set(run_ids)) != len(run_ids):
        _refuse("duplicate_run_ids")
    return runs


def _legacy_session_tables(db_file: Path) -> list[str]:
    """Return session tables in one SQLite file that still carry legacy run data."""
    connection = sqlite3.connect(f"{db_file.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
    try:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
            if isinstance(row[0], str) and row[0].endswith("sessions")
        ]
        return [
            table
            for table in tables
            if "runs" in {row[1] for row in connection.execute(f"PRAGMA table_info({_quote_identifier(table)})")}
            and connection.execute(
                f"SELECT 1 FROM {_quote_identifier(table)} WHERE runs IS NOT NULL LIMIT 1",  # noqa: S608
            ).fetchone()
            is not None
        ]
    finally:
        connection.close()


def _open_database(db_file: Path, session_table: str) -> SqliteDb:
    """Open one Agno database without changing rollback journaling to WAL."""
    db_path = str(db_file)
    engine = create_state_engine(db_path)
    database = SqliteDb(session_table=session_table, db_file=db_path, db_engine=engine)
    configure_state_engine_pragmas(engine)
    return database


def _legacy_session_row(sess: OrmSession, session_table: str, session_id: str) -> tuple[str | None, object] | None:
    quoted_table = _quote_identifier(session_table)
    row = sess.execute(
        text(f"SELECT user_id, runs FROM {quoted_table} WHERE session_id = :session_id"),  # noqa: S608
        {"session_id": session_id},
    ).one_or_none()
    return None if row is None or row[1] is None else (row[0], row[1])


def _migrate_session_in_transaction(
    sess: OrmSession,
    database: SqliteDb,
    runs_table: Table,
    session_id: str,
    expected_blob: object,
    legacy_runs: list[dict[str, Any]],
) -> bool:
    """Perform the copy and verification while the caller owns the writer transaction."""
    session_row = _legacy_session_row(sess, database.session_table_name, session_id)
    if session_row is None:
        return False
    user_id, blob = session_row
    if blob != expected_blob:
        raise _LegacyBlobChangedError
    legacy_ids = [cast("str", run["run_id"]) for run in legacy_runs]
    legacy_id_set = set(legacy_ids)

    existing_for_session = list(
        sess.execute(
            select(runs_table.c.run_id, runs_table.c.run_index, runs_table.c.created_at)
            .where(runs_table.c.session_id == session_id)
            .order_by(runs_table.c.run_index.asc(), runs_table.c.created_at.asc()),
        ).mappings(),
    )
    existing_ids = {cast("str", row["run_id"]) for row in existing_for_session}
    canonical_ids = [
        *legacy_ids,
        *[cast("str", row["run_id"]) for row in existing_for_session if row["run_id"] not in legacy_id_set],
    ]
    missing_rows = [
        build_single_run_row(run, session_id=session_id, user_id=user_id, run_index=index)
        for index, run in enumerate(legacy_runs)
        if run["run_id"] not in existing_ids
    ]
    if missing_rows:
        sess.execute(
            sqlite_insert(runs_table).on_conflict_do_nothing(index_elements=["run_id"]),
            missing_rows,
        )

    if canonical_ids:
        sess.execute(
            update(runs_table)
            .where(runs_table.c.run_id == bindparam("target_run_id"))
            .values(run_index=bindparam("target_run_index")),
            [{"target_run_id": run_id, "target_run_index": index} for index, run_id in enumerate(canonical_ids)],
        )
    stored_rows = list(
        sess.execute(
            select(runs_table.c.run_id, runs_table.c.run_data)
            .where(runs_table.c.session_id == session_id)
            .order_by(runs_table.c.run_index.asc(), runs_table.c.created_at.asc()),
        ).mappings(),
    )
    stored_ids = [row["run_id"] for row in stored_rows]
    if stored_ids != canonical_ids:
        _refuse("stored_run_order_mismatch")
    stored_by_id = {row["run_id"]: row["run_data"] for row in stored_rows}
    if any(stored_by_id.get(run["run_id"]) != run for run in legacy_runs if run["run_id"] not in existing_ids):
        _refuse("stored_run_content_mismatch")

    quoted_table = _quote_identifier(database.session_table_name)
    sess.execute(
        text(f"UPDATE {quoted_table} SET runs = NULL WHERE session_id = :session_id"),  # noqa: S608
        {"session_id": session_id},
    )
    return True


def _attempt_migrate_session(
    database: SqliteDb,
    runs_table: Table,
    session_id: str,
    expected_blob: object,
    legacy_runs: list[dict[str, Any]],
) -> bool:
    """Commit prepared runs only when the source blob is still current."""
    with database.Session() as sess:
        try:
            sess.execute(text("BEGIN IMMEDIATE"))
            migrated = _migrate_session_in_transaction(
                sess,
                database,
                runs_table,
                session_id,
                expected_blob,
                legacy_runs,
            )
            sess.commit()
        except BaseException:
            sess.rollback()
            raise
    return migrated


def _migrate_session(
    database: SqliteDb,
    runs_table: Table,
    session_id: str,
) -> bool:
    """Prepare outside the writer lock, retrying if a current write changes the blob."""
    for _ in range(_MAX_CHANGED_BLOB_RETRIES):
        with database.Session() as sess:
            session_row = _legacy_session_row(sess, database.session_table_name, session_id)
        if session_row is None:
            return False
        _, blob = session_row
        legacy_runs = _decode_legacy_runs(blob)
        try:
            return _attempt_migrate_session(database, runs_table, session_id, blob, legacy_runs)
        except _LegacyBlobChangedError:
            continue
    _refuse("legacy_blob_kept_changing")


def _migrate_table(db_file: Path, session_table: str) -> _MigrationResult:
    """Migrate each legacy session independently so one bad row cannot block the rest."""
    database = _open_database(db_file, session_table)
    result = _MigrationResult()
    try:
        runs_table = database._get_table(table_type="runs", create_table_if_not_found=True)
        if runs_table is None:
            return result
        quoted_table = _quote_identifier(session_table)
        with database.Session() as sess:
            session_ids = list(
                sess.execute(
                    text(f"SELECT session_id FROM {quoted_table} WHERE runs IS NOT NULL ORDER BY session_id"),  # noqa: S608
                ).scalars(),
            )
        for session_id in session_ids:
            try:
                migrated = _migrate_session(database, runs_table, session_id)
            except Exception as exc:
                logger.warning(
                    "legacy_session_migration_skipped",
                    db_file=str(db_file),
                    session_table=session_table,
                    session_id=session_id,
                    error=str(exc),
                )
                result += _MigrationResult(failed_sessions=1)
            else:
                result += _MigrationResult(migrated_sessions=int(migrated))
        if result.failed_sessions == 0:
            database.upsert_schema_version(session_table, "3.0.0")
    finally:
        database.close()
    return result


def _migrate_database(db_file: Path) -> _MigrationResult:
    """Migrate every legacy session table in one SQLite database."""
    result = _MigrationResult()
    for session_table in _legacy_session_tables(db_file):
        result += _migrate_table(db_file, session_table)
    return result


def _migrate_storage_root(storage_root: str) -> None:
    """Child-process entry point: scan and migrate databases one at a time."""
    result = _MigrationResult()
    failed_databases = 0
    for db_file in sorted(Path(storage_root).rglob("sessions/*.db")):
        try:
            result += _migrate_database(db_file)
        except Exception:
            failed_databases += 1
            logger.warning("legacy_session_database_migration_failed", db_file=str(db_file), exc_info=True)
    logger.info(
        "legacy_session_migration_finished",
        migrated_sessions=result.migrated_sessions,
        failed_sessions=result.failed_sessions,
        failed_databases=failed_databases,
    )


async def _run_legacy_session_migration(storage_root: Path) -> None:
    """Run the legacy scan outside this process and stop it with the runtime."""
    context = multiprocessing.get_context("spawn")
    process = context.Process(
        target=_migrate_storage_root,
        args=(str(storage_root),),
        name="legacy-session-migration",
        daemon=True,
    )
    process.start()
    try:
        await asyncio.to_thread(process.join)
    except asyncio.CancelledError:
        if process.is_alive():
            process.terminate()
        await asyncio.shield(asyncio.to_thread(process.join))
        raise
    if process.exitcode != 0:
        msg = f"Legacy session migration process exited with status {process.exitcode}"
        raise RuntimeError(msg)


async def run_legacy_session_migration_after_ready(ready: asyncio.Event, storage_root: Path) -> None:
    """Wait for runtime availability, then run best-effort legacy migration."""
    await ready.wait()
    try:
        await _run_legacy_session_migration(storage_root)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("legacy_session_migration_process_failed", exc_info=True)
