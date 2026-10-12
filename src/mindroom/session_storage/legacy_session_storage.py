"""Compatibility for older session payloads: Agno 2 run blobs, run rows without a position, and retired run metadata."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from agno.db.utils import build_single_run_row
from agno.run.team import TeamRunOutput
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from mindroom.usage.storage import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agno.run.agent import RunOutput
    from sqlalchemy import Connection, Table


# LEGACY_COMPAT: Agno 2 session history stored in JSON run blobs.
# Legacy format: Agno 2 sessions stored run history in single- or double-encoded JSON `runs` blobs,
# which Agno 3 merges into every read ahead of run rows.
# Last legacy release: v2026.9.11; replacement: v2026.9.12 wrote first-class Agno 3 run rows.
# Handling: The open-time summary-row migration turns every blob entry with a string run id that has
# no row yet into a run row, renumbers the session's rows in the order Agno's merge read them, and
# clears the blob, so every run has a position in the conversation log and no read merges a blob.
# Coverage: tests/test_agent_storage_runs.py::test_agno_2_blob_runs_become_rows_once,
# tests/test_agent_storage_runs.py::test_blob_runs_keep_the_order_agno_read_them_in, and
# tests/test_agent_storage_runs.py::test_blob_entries_without_a_string_run_id_are_dropped.
def materialize_legacy_run_blobs(connection: Connection, runs: Table, session_table: str) -> None:
    """Turn the runs Agno 2 blobs still hold into run rows, in the caller's write transaction."""
    sessions = quote_identifier(session_table)
    columns = {row[1] for row in connection.exec_driver_sql(f"PRAGMA table_info({sessions})")}
    if "runs" not in columns:
        return
    blobs = connection.exec_driver_sql(
        f"SELECT session_id, runs FROM {sessions} WHERE runs IS NOT NULL",  # noqa: S608
    ).fetchall()
    for session_id, blob in blobs:
        stored = ordered_run_ids(connection, runs, session_id)
        legacy = _legacy_entries(blob)
        for run_id, entry in legacy.items():
            if run_id not in stored:
                insert_run_entry(connection, runs, entry, session_id=session_id)
        # Agno's merge read blob entries first, table copies substituted, then table-only rows.
        renumber_runs(connection, runs, session_id, [*legacy, *(run_id for run_id in stored if run_id not in legacy)])
        connection.exec_driver_sql(
            f"UPDATE {sessions} SET runs = NULL WHERE session_id = ?",  # noqa: S608
            (session_id,),
        )


def ordered_run_ids(connection: Connection, runs: Table, session_id: str) -> list[str]:
    """Return one session's run rows in the order Agno reads them, rows without an index first."""
    return list(
        connection.execute(
            select(runs.c.run_id)
            .where(runs.c.session_id == session_id)
            .order_by(runs.c.run_index, runs.c.created_at, runs.c.run_id),
        ).scalars(),
    )


def insert_run_entry(connection: Connection, runs: Table, entry: dict[str, Any], *, session_id: str) -> None:
    """Write one serialized run as a run row without an index, unless a row already has its id."""
    row = build_single_run_row(entry, session_id=session_id, user_id=entry.get("user_id"), run_index=None)
    connection.execute(insert(runs).values(**row).on_conflict_do_nothing(index_elements=["run_id"]))


def renumber_runs(connection: Connection, runs: Table, session_id: str, run_ids: list[str]) -> None:
    """Give one session's run rows consecutive indexes in the order of ``run_ids``."""
    for run_index, run_id in enumerate(run_ids):
        connection.execute(
            runs.update().where(runs.c.session_id == session_id, runs.c.run_id == run_id).values(run_index=run_index),
        )


def _legacy_entries(blob: object) -> dict[str, dict[str, Any]]:
    """Return the blob's run entries a read could address by id, keyed by run id in stored order."""
    try:
        decoded = decode_persisted_session_json(blob)
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


def decode_persisted_session_json(raw_value: object) -> object:
    """Strictly decode the single- or double-encoded JSON used by retained session payloads."""
    if raw_value is None:
        return None
    if not isinstance(raw_value, (str, bytes, bytearray)):
        raise TypeError
    decoded = json.loads(raw_value)
    return json.loads(decoded) if isinstance(decoded, str) else decoded


# LEGACY_COMPAT: Run metadata carrying a copy of every model-visible tool schema.
# Legacy format: Run rows whose `metadata` holds a `tools_schema` list of tool definitions, written with every run.
# Last legacy release: v2026.10.281; replacement: the unreleased run metadata omits tool schemas.
# Handling: Each loaded run, including member responses embedded in a paused team run, drops the key, so cached and
# replayed runs stop holding the schemas; rows written by current releases omit it.
# Coverage: tests/test_agent_storage_runs.py::test_loaded_runs_drop_their_stored_tool_schemas,
# tests/test_agent_storage_runs.py::test_loaded_team_runs_drop_tool_schemas_of_embedded_member_responses.
def drop_run_tool_schemas(runs: Iterable[RunOutput | TeamRunOutput]) -> None:
    """Drop the per-run tool schema copy that older releases stored in run metadata."""
    for run in runs:
        if run.metadata:
            run.metadata.pop("tools_schema", None)
        if isinstance(run, TeamRunOutput):
            drop_run_tool_schemas(run.member_responses)
