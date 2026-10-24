"""Run data older Agno releases stored: Agno 2 run blobs, JSON columns encoded twice, and rows without a position."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

from mindroom.usage.storage import quote_identifier

if TYPE_CHECKING:
    import sqlite3

    from mindroom.session_storage.schema import TableNames


# LEGACY_COMPAT: Agno 2 session JSON columns encoded twice.
# Legacy format: Agno 2 wrote `session_data`, `metadata`, and the `runs` blob as the JSON text of their JSON
# (a JSON string holding the value); a row keeps that form until a later release rewrites the column.
# Last legacy release: v2026.9.11 (agno 2.6.12); replacement: v2026.9.12 (agno 3.0.5) wrote each column's JSON once.
# Handling: Readers decode a column once and again when the first decode yields a string; owned writers
# encode once, so a column the owned store rewrites is single-encoded from then on.
# Coverage: tests/session_storage/test_legacy_agno2_runs.py::test_blob_runs_become_rows_when_the_store_opens,
# tests/session_storage/test_store.py::test_double_encoded_session_columns_load_and_are_rewritten_once,
# tests/test_legacy_compaction_state.py::test_opening_storage_reads_double_encoded_legacy_metadata, and
# tests/test_usage_stats.py::test_daily_models_read_real_agno_2_history.
def decode_session_json(raw_value: object) -> object:
    """Strictly decode a single- or double-encoded JSON column value."""
    if raw_value is None:
        return None
    if not isinstance(raw_value, (str, bytes, bytearray)):
        raise TypeError
    decoded = json.loads(raw_value)
    return json.loads(decoded) if isinstance(decoded, str) else decoded


# LEGACY_COMPAT: Agno 2 session history stored in JSON run blobs.
# Legacy format: Agno 2 sessions stored run history in a single- or double-encoded JSON `runs` blob on the session
# row, which Agno 3 merged into every read ahead of the run rows; the column exists only on files Agno 2 created.
# Last legacy release: v2026.9.11 (agno 2.6.12); replacement: v2026.9.12 (agno 3.0.5) wrote first-class run rows.
# Handling: When the open-time summary-row migration finds a sessions table that still has the column, every blob
# entry with a string run id that has no row yet becomes a run row, the session's rows are renumbered in the order
# Agno's merge read them, and the blob is cleared, all in one write transaction, so no read has to merge a blob.
# Entries without a string run id are dropped, as no read could address them.
# Coverage: tests/session_storage/test_legacy_agno2_runs.py and
# tests/session_storage/test_factories.py::test_an_agno_2_database_is_migrated_once_and_then_keeps_its_log_order.
def has_blob_runs(connection: sqlite3.Connection, names: TableNames) -> bool:
    """Return whether the sessions table may still hold Agno 2 run blobs: whether it still has their column."""
    return "runs" in {row[1] for row in connection.execute(f"PRAGMA table_info({quote_identifier(names.sessions)})")}


def move_blob_runs_to_rows(connection: sqlite3.Connection, names: TableNames) -> None:
    """Turn the runs Agno 2 blobs hold into run rows, in the caller's write transaction."""
    sessions = quote_identifier(names.sessions)
    blobs = connection.execute(f"SELECT session_id, runs FROM {sessions} WHERE runs IS NOT NULL").fetchall()  # noqa: S608
    for session_id, blob in blobs:
        stored = ordered_run_ids(connection, names, session_id)
        legacy = _legacy_entries(blob)
        for entry in legacy.values():
            insert_run_entry(connection, names, entry, session_id=session_id)
        # Agno's merge read blob entries first, table copies substituted, then table-only rows.
        renumber_runs(connection, names, session_id, [*legacy, *(run_id for run_id in stored if run_id not in legacy)])
        connection.execute(f"UPDATE {sessions} SET runs = NULL WHERE session_id = ?", (session_id,))  # noqa: S608


# LEGACY_COMPAT: Run rows without a position.
# Legacy format: Run rows whose ``run_index`` is ``NULL``, which Agno 3 writers before its ``upsert_run`` index backfill
# stored when a run arrived without one; Agno reads them first in a session.
# Last legacy release: unversioned external input; no MindRoom release is known to write them, and the current
# insertion rules give every new row an index.
# Handling: The open-time summary-row migration renumbers each session holding such a row in the order a read returned
# its rows, so every run has a place in the conversation log before any response loads it.
# Coverage: tests/session_storage/test_legacy_agno2_runs.py::test_rows_without_an_index_are_numbered_in_read_order,
# tests/test_legacy_compaction_state.py::test_opening_storage_numbers_rows_an_old_writer_stored_without_an_index, and
# tests/test_archive_migration.py::test_rows_without_an_index_keep_their_place.
def number_unindexed_runs(connection: sqlite3.Connection, names: TableNames) -> None:
    """Give every session that holds a row without an index consecutive indexes in its read order."""
    unindexed = connection.execute(
        f"SELECT DISTINCT session_id FROM {quote_identifier(names.runs)} WHERE run_index IS NULL",  # noqa: S608
    ).fetchall()
    for (session_id,) in unindexed:
        renumber_runs(connection, names, session_id, ordered_run_ids(connection, names, session_id))


def ordered_run_ids(connection: sqlite3.Connection, names: TableNames, session_id: str) -> list[str]:
    """Return one session's run rows in the order Agno read them, rows without an index first."""
    return [
        run_id
        for (run_id,) in connection.execute(
            f"SELECT run_id FROM {quote_identifier(names.runs)} WHERE session_id = ? "  # noqa: S608
            "ORDER BY run_index, created_at, run_id",
            (session_id,),
        )
    ]


def renumber_runs(connection: sqlite3.Connection, names: TableNames, session_id: str, run_ids: list[str]) -> None:
    """Give one session's run rows consecutive indexes in the order of ``run_ids``."""
    connection.executemany(
        f"UPDATE {quote_identifier(names.runs)} SET run_index = ? WHERE session_id = ? AND run_id = ?",  # noqa: S608
        [(run_index, session_id, run_id) for run_index, run_id in enumerate(run_ids)],
    )


def insert_run_entry(
    connection: sqlite3.Connection,
    names: TableNames,
    entry: dict[str, Any],
    *,
    session_id: str,
) -> None:
    """Write one serialized run as a run row without an index, with the columns Agno 3 derived from a run dict.

    A run that names an agent is an agent run and any other a team run; MindRoom stored no other kind of run.
    A row that already has the run's id is kept.
    """
    run_type = "agent" if entry.get("agent_id") or entry.get("agent_name") else "team"
    now = int(time.time())
    connection.execute(
        f"INSERT INTO {quote_identifier(names.runs)} (run_id, session_id, run_type, agent_id, team_id, workflow_id, "  # noqa: S608
        "user_id, parent_run_id, status, run_index, run_data, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?) ON CONFLICT(run_id) DO NOTHING",
        (
            entry["run_id"],
            session_id,
            run_type,
            entry.get("agent_id"),
            entry.get("team_id"),
            entry.get("workflow_id"),
            entry.get("user_id"),
            entry.get("parent_run_id"),
            entry.get("status"),
            json.dumps(entry),
            entry.get("created_at") or now,
            now,
        ),
    )


def _legacy_entries(blob: object) -> dict[str, dict[str, Any]]:
    """Return the blob's run entries a read could address by id, keyed by run id in stored order."""
    try:
        decoded = decode_session_json(blob)
    except (TypeError, ValueError):
        return {}
    entries: dict[str, dict[str, Any]] = {}
    for entry in decoded if isinstance(decoded, list) else []:
        if not isinstance(entry, dict):
            continue
        run = cast("dict[str, Any]", entry)
        run_id = run.get("run_id")
        if isinstance(run_id, str) and run_id:
            entries.setdefault(run_id, run)
    return entries


__all__ = [
    "decode_session_json",
    "has_blob_runs",
    "insert_run_entry",
    "move_blob_runs_to_rows",
    "number_unindexed_runs",
    "ordered_run_ids",
    "renumber_runs",
]
