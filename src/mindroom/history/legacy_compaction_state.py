"""One-time migration of a conversation database into the summary rows of the conversation log."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, MINDROOM_MATRIX_HISTORY_METADATA_KEY
from mindroom.history.summary_rows_schema import owner_scope_key, summary_rows_schema_sql, summary_rows_table_names
from mindroom.session_storage import agno_compat_sqlite
from mindroom.session_storage.legacy_session_storage import (
    decode_persisted_session_json,
    insert_run_entry,
    materialize_legacy_run_blobs,
    number_unindexed_runs,
    ordered_run_ids,
    renumber_runs,
)
from mindroom.usage.storage import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Collection

    from agno.db.sqlite import SqliteDb
    from sqlalchemy import Connection, Table

# LEGACY_COMPAT: Destructive compaction state without an archive.
# Legacy format: A v2 ``mindroom_compaction`` scope state holding ``compacted_run_ids``
# tombstones and ``last_compacted_at``/``last_summary_model``/``last_compacted_run_count``
# audit fields, and a replayed ``session.summary``, written when compaction deleted the runs it
# summarized, in a database that never got the compaction archive.
# Last legacy release: v2026.9.314; replacement: v2026.9.315 archived compacted runs, now summary rows.
# Handling: ``migrate_to_summary_rows`` runs once per conversation database when storage opens, before
# any response uses it, and is detected by the summary-row tables' absence. The scope owning the
# summary gets a content-free legacy summary row before every run, holding the summary and its
# preserved seen ids, which move out of session metadata; stored copies of tombstoned runs are dropped.
# The retired keys are stripped. Runs deleted by those releases stay lost; ``history/storage.py``
# owns redaction of the summary.
# Coverage: tests/test_legacy_compaction_state.py.
_LEGACY_STATE_KEYS = frozenset(
    {"compacted_run_ids", "last_compacted_at", "last_summary_model", "last_compacted_run_count"},
)
_COMPACTION_STATE_VERSION = 2
_SEEN_STATE_VERSION = 1
_SEEN_STATE_KEYS = frozenset({"seen_event_ids"})


def migrate_to_summary_rows(db: SqliteDb) -> None:
    """Create the summary-row tables once, first giving every stored run a position and adopting older state."""
    session_table = db.session_table_name
    with db.db_engine.connect() as connection:
        if not _needs_summary_rows(connection, session_table):
            return
        # Created before the write lock: Agno creates it on its own connection.
        runs = agno_compat_sqlite.owned_table(db, "runs")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        # Another startup may have finished while this connection waited for its write lock.
        if _needs_summary_rows(connection, session_table):
            materialize_legacy_run_blobs(connection, runs, session_table)
            number_unindexed_runs(connection, runs)
            for statement in summary_rows_schema_sql(session_table):
                connection.exec_driver_sql(statement)
            if f"{session_table}_compactions" in _tables(connection):
                _convert_archive(connection, session_table, runs)
            else:
                _adopt_legacy_sessions(connection, session_table, runs.name)
        connection.commit()


def _needs_summary_rows(connection: Connection, session_table: str) -> bool:
    tables = _tables(connection)
    return session_table in tables and summary_rows_table_names(session_table)[0] not in tables


def _tables(connection: Connection) -> set[str]:
    return {name for (name,) in connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _adopt_legacy_sessions(connection: Connection, session_table: str, runs_table: str) -> None:
    sessions = quote_identifier(session_table)
    summaries = quote_identifier(summary_rows_table_names(session_table)[0])
    rows = connection.exec_driver_sql(
        f"SELECT session_id, session_type, agent_id, team_id, metadata, summary FROM {sessions}",  # noqa: S608
    ).fetchall()
    for session_id, session_type, agent_id, team_id, raw_metadata, raw_summary in rows:
        metadata = _decoded_mapping(raw_metadata)
        scopes = {
            scope_key: tombstones
            for scope_key, state in _scope_states(metadata, MINDROOM_COMPACTION_METADATA_KEY).items()
            if isinstance(state, dict) and (tombstones := _legacy_tombstones(state)) is not None
        }
        seen_states = _scope_states(metadata, MINDROOM_MATRIX_HISTORY_METADATA_KEY)
        summary = _summary_text(_decoded_mapping(raw_summary))
        summary_scope = owner_scope_key(session_type, agent_id, team_id) if summary is not None else None
        if summary_scope is not None:
            scope_seen = json.dumps(_seen_event_ids(seen_states.get(summary_scope)))
            connection.exec_driver_sql(
                f"INSERT INTO {summaries} (session_id, scope_key, after_run_index, summary, legacy, event_ids, "  # noqa: S608
                "seen_event_ids, created_at) VALUES (?, ?, -1, ?, 1, ?, ?, ?)",
                (session_id, summary_scope, summary, scope_seen, scope_seen, int(time.time())),
            )
            scopes.setdefault(summary_scope, ())
        tombstoned = {run_id for ids in scopes.values() for run_id in ids}
        agno_compat_sqlite.delete_run_subtrees(
            connection,
            quote_identifier(runs_table),
            tombstoned,
            session_id=session_id,
        )
        adopted_metadata = _without_adopted_state(metadata, scopes, summary_scope)
        if adopted_metadata != metadata:
            connection.exec_driver_sql(
                f"UPDATE {sessions} SET metadata = ? WHERE session_id = ?",  # noqa: S608
                (json.dumps(adopted_metadata), session_id),
            )


# LEGACY_COMPAT: Compacted runs moved out of the runs table into an archive.
# Legacy format: ``<session_table>_compactions`` generations (cumulative summary, model, ``legacy`` flag,
# captured ``legacy_event_ids``) and ``<session_table>_compacted_runs`` rows holding each folded run's JSON
# and event ids, or a content-free tombstone without ``run_data``; ``session.summary`` cached the newest
# generation's text, and a generation without text ended the summaries before it.
# Last legacy release: v2026.10.273; replacement: the next release keeps every run in the runs table with
# summary rows beside it.
# Handling: ``migrate_to_summary_rows`` runs once per conversation database when storage opens, before any
# response uses it. It drops stored copies of every archived or tombstoned run with their member runs,
# restores the archived runs ahead of the live ones in archive order, renumbers each session, and turns each
# generation into a summary row after its last restored top-level run (legacy generations before every run),
# with the event ids of its archived runs, then drops both archive tables. Replay and redaction candidates
# match what the archive gave after its pre-reply reconcile, and so do seen ids, except that metadata seen ids
# reconcile dropped when the cached summary differed from the newest generation (an interrupted summary
# write) stay; tombstones are not carried over.
# Coverage: tests/test_archive_migration.py::test_ordinary_generations_become_summary_rows_after_their_runs,
# ::test_a_legacy_generation_stays_in_force_before_every_run, ::test_retired_generations_keep_their_runs_behind_the_barrier,
# ::test_stale_copies_of_archived_and_tombstoned_runs_are_dropped, ::test_archived_member_runs_return_with_their_team_run,
# and ::test_redaction_candidates_keep_what_the_archive_matched.
def _convert_archive(connection: Connection, session_table: str, runs: Table) -> None:
    compactions = quote_identifier(f"{session_table}_compactions")
    compacted_runs = quote_identifier(f"{session_table}_compacted_runs")
    archived: dict[str, list[tuple[str, str | None]]] = {}
    for session_id, run_id, run_data in connection.exec_driver_sql(
        f"SELECT session_id, run_id, run_data FROM {compacted_runs} ORDER BY id",  # noqa: S608
    ):
        archived.setdefault(session_id, []).append((run_id, run_data))
    for session_id, entries in archived.items():
        # A stale save or blob residue can hold a copy that reconcile had not pruned yet.
        agno_compat_sqlite.delete_run_subtrees(
            connection,
            quote_identifier(runs.name),
            {run_id for run_id, _ in entries},
            session_id=session_id,
        )
        restored: list[str] = []
        for run_id, run_data in entries:
            if run_data is not None:
                insert_run_entry(connection, runs, json.loads(run_data), session_id=session_id)
                restored.append(run_id)
        restored_ids = set(restored)
        live = [run_id for run_id in ordered_run_ids(connection, runs, session_id) if run_id not in restored_ids]
        renumber_runs(connection, runs, session_id, [*restored, *live])
    _insert_generation_rows(connection, session_table, runs)
    connection.exec_driver_sql(f"DROP TABLE {compacted_runs}")
    connection.exec_driver_sql(f"DROP TABLE {compactions}")


def _insert_generation_rows(connection: Connection, session_table: str, runs: Table) -> None:
    compactions = quote_identifier(f"{session_table}_compactions")
    compacted_runs = quote_identifier(f"{session_table}_compacted_runs")
    summaries = quote_identifier(summary_rows_table_names(session_table)[0])
    positions: dict[tuple[str, str], int] = {}
    generations = connection.exec_driver_sql(
        f"SELECT id, session_id, scope_key, summary, summary_model, legacy, created_at FROM {compactions} ORDER BY id",  # noqa: S608
    ).fetchall()
    for generation_id, session_id, scope_key, summary, summary_model, legacy, created_at in generations:
        newest_run = connection.exec_driver_sql(
            f"SELECT MAX(run.run_index) FROM {compacted_runs} AS archived "  # noqa: S608
            f"JOIN {quote_identifier(runs.name)} AS run "
            "ON run.session_id = archived.session_id AND run.run_id = archived.run_id "
            "WHERE archived.compaction_id = ? AND archived.run_data IS NOT NULL AND run.parent_run_id IS NULL",
            (generation_id,),
        ).scalar()
        if legacy:
            position = -1
        elif newest_run is not None:
            position = newest_run
        else:
            position = positions.get((session_id, scope_key), -1)
        positions[session_id, scope_key] = position
        connection.exec_driver_sql(
            f"INSERT INTO {summaries} (session_id, scope_key, after_run_index, summary, summary_model, legacy, "  # noqa: S608
            "event_ids, seen_event_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                scope_key,
                position,
                summary,
                summary_model,
                legacy,
                json.dumps(_generation_event_ids(connection, session_table, generation_id, "event_ids")),
                json.dumps(_generation_event_ids(connection, session_table, generation_id, "seen_event_ids")),
                created_at,
            ),
        )


def _generation_event_ids(connection: Connection, session_table: str, generation_id: int, column: str) -> list[str]:
    """Return the event ids a generation's archived runs carry in ``column``, plus a legacy generation's own."""
    compactions = quote_identifier(f"{session_table}_compactions")
    compacted_runs = quote_identifier(f"{session_table}_compacted_runs")
    return sorted(
        value
        for (value,) in connection.exec_driver_sql(
            f"SELECT value FROM {compacted_runs} AS archived, json_each(archived.{column}) "  # noqa: S608
            "WHERE archived.compaction_id = ? "
            f"UNION SELECT value FROM {compactions} AS generation, json_each(generation.legacy_event_ids) "
            "WHERE generation.id = ? AND generation.legacy = 1",
            (generation_id, generation_id),
        )
    )


def _legacy_tombstones(raw_state: dict[str, Any]) -> tuple[str, ...] | None:
    """Return a destructive compactor's tombstones, or ``None`` when it did not write this state."""
    if not _LEGACY_STATE_KEYS.intersection(raw_state):
        return None
    raw_run_ids = raw_state.get("compacted_run_ids")
    if not isinstance(raw_run_ids, list):
        return ()
    return tuple(dict.fromkeys(run_id for run_id in raw_run_ids if isinstance(run_id, str) and run_id))


def _without_adopted_state(
    metadata: dict[str, Any],
    scope_keys: Collection[str],
    summary_scope: str | None,
) -> dict[str, Any]:
    """Return metadata without the adopted scopes' retired keys and the summary scope's seen ids."""
    adopted = dict(metadata)
    for metadata_key, retired_keys, scopes in (
        (MINDROOM_COMPACTION_METADATA_KEY, _LEGACY_STATE_KEYS, scope_keys),
        (MINDROOM_MATRIX_HISTORY_METADATA_KEY, _SEEN_STATE_KEYS, () if summary_scope is None else (summary_scope,)),
    ):
        states = _scope_states(metadata, metadata_key)
        if not states:
            continue
        kept = {
            scope_key: (
                {name: value for name, value in state.items() if name not in retired_keys}
                if scope_key in scopes and isinstance(state, dict)
                else state
            )
            for scope_key, state in states.items()
        }
        kept = {scope_key: state for scope_key, state in kept.items() if state != {}}
        if kept:
            adopted[metadata_key] = {**metadata[metadata_key], "states": kept}
        else:
            adopted.pop(metadata_key)
    return adopted


def _scope_states(metadata: dict[str, Any], metadata_key: str) -> dict[str, Any]:
    """Return one versioned per-scope metadata mapping, or nothing when its version is not legacy."""
    version = _COMPACTION_STATE_VERSION if metadata_key == MINDROOM_COMPACTION_METADATA_KEY else _SEEN_STATE_VERSION
    value = metadata.get(metadata_key)
    if not isinstance(value, dict) or value.get("version") != version:
        return {}
    states = value.get("states")
    return states if isinstance(states, dict) else {}


def _seen_event_ids(raw_state: dict[str, Any] | None) -> list[str]:
    raw_event_ids = raw_state.get("seen_event_ids") if isinstance(raw_state, dict) else None
    if not isinstance(raw_event_ids, list):
        return []
    return sorted({event_id for event_id in raw_event_ids if isinstance(event_id, str) and event_id})


def _summary_text(summary: dict[str, Any]) -> str | None:
    text = summary.get("summary")
    return text if isinstance(text, str) and text.strip() else None


def _decoded_mapping(raw_value: object) -> dict[str, Any]:
    """Decode one JSON object column; unreadable or non-object values read as empty."""
    try:
        value = decode_persisted_session_json(raw_value)
    except (TypeError, ValueError):
        return {}
    return cast("dict[str, Any]", value) if isinstance(value, dict) else {}
