"""The conversion's step that seeds the usage ledger of an Agno-layout database from the history it holds."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, cast

from mindroom.session_storage.legacy_agno2_runs import decode_session_json, has_blob_column
from mindroom.session_storage.schema import quote_identifier, table_names, usage_table_sql, usage_upsert_sql
from mindroom.usage.storage import project_usage

if TYPE_CHECKING:
    import sqlite3

    from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames


# LEGACY_COMPAT: Usage derived from disposable conversation rows and session blobs.
# Legacy format: Agno 3 run_data rows and retained Agno 2 single/double-encoded runs blobs.
# Last legacy release: v2026.9.190, the last verified native writer without an independent usage table.
# Replacement: v2026.9.191 introduced independent usage snapshots; no historical billing completeness is inferred.
# Handling: Detect by schema in a database's conversion to storage v2; seed in its transaction, prefer current rows,
# retain unknown dates and content-free gaps.
# Coverage: tests/session_storage/test_legacy_usage_ledger.py.
def seed_usage_ledger(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    """Seed the usage ledger of a database that has none in the caller's write transaction."""
    if _needs_migration(connection, names):
        _seed_usage(connection, names)


def _needs_migration(connection: sqlite3.Connection, names: AgnoTableNames) -> bool:
    tables = table_names(connection)
    return names.sessions in tables and names.usage not in tables


def _seed_usage(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    connection.execute(usage_table_sql(names))
    legacy_column = "runs" if has_blob_column(connection, names) else "NULL"
    has_runs = names.runs in table_names(connection)
    for session_id, blob in connection.execute(
        f"SELECT session_id, {legacy_column} FROM {quote_identifier(names.sessions)}",  # noqa: S608
    ):
        known_ids: set[str] = set()
        if has_runs:
            for run_id, raw, created_at in connection.execute(
                f"SELECT run_id, run_data, created_at FROM {quote_identifier(names.runs)} WHERE session_id = ?",  # noqa: S608
                (session_id,),
            ):
                known_ids.add(run_id)
                decoded = _decode(raw)
                run = {**decoded, "run_id": run_id, "created_at": created_at} if isinstance(decoded, dict) else None
                _insert_usage(connection, names, session_id, run_id, run)
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
            _insert_usage(connection, names, session_id, run_id, run)


def _decode(raw: object) -> object:
    try:
        return decode_session_json(raw)
    except (RecursionError, TypeError, ValueError):
        return False


def _insert_usage(
    connection: sqlite3.Connection,
    names: AgnoTableNames,
    session_id: str,
    run_id: str | None,
    run: object,
) -> None:
    payload = json.dumps(project_usage(cast("dict[str, object]", run))) if isinstance(run, dict) else None
    connection.execute(usage_upsert_sql(names), (session_id, run_id, payload))
