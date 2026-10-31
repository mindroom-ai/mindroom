"""The conversion's step that gives an Agno-layout database without summary rows its summary rows."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from mindroom.session_storage.legacy_agno2_runs import (
    has_blob_column,
    insert_run_entry,
    move_blob_runs_to_rows,
    number_unindexed_runs,
    ordered_run_ids,
    renumber_runs,
)
from mindroom.session_storage.legacy_agno3_schema import (
    agno_runs_sql,
    agno_summary_rows_sql,
    delete_run_subtrees_in_transaction,
)
from mindroom.session_storage.legacy_scope_metadata import (
    LEGACY_COMPACTION_METADATA_KEY,
    LEGACY_MATRIX_HISTORY_METADATA_KEY,
    legacy_seen_event_ids,
    scope_states,
)
from mindroom.session_storage.schema import quote_identifier, table_names
from mindroom.session_storage.store import decoded_mapping

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Collection

    from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames

# LEGACY_COMPAT: Destructive compaction state without an archive.
# Legacy format: A v2 ``mindroom_compaction`` scope state holding ``compacted_run_ids``
# tombstones and ``last_compacted_at``/``last_summary_model``/``last_compacted_run_count``
# audit fields, and a replayed ``session.summary``, written when compaction deleted the runs it
# summarized, in a database that never got the compaction archive.
# Last legacy release: v2026.9.314; replacement: v2026.9.315 archived compacted runs, now summary rows.
# Handling: ``adopt_summary_rows`` runs in a database's conversion to storage v2, before any response uses it,
# when the summary-row tables are absent. The scope owning the summary gets a content-free legacy summary row
# before every run, holding the summary and its preserved seen ids, which move out of session metadata; stored
# copies of tombstoned runs are dropped, with their member rows but for one whose run id is not text, which the
# conversion leaves out. The retired keys are stripped. Runs deleted by those releases stay lost;
# ``history/storage.py`` owns redaction of the summary.
# Coverage: tests/session_storage/test_legacy_compaction_state.py, including
# ::test_a_member_row_whose_id_is_not_text_below_a_tombstoned_run_does_not_stop_the_conversion.
_LEGACY_STATE_KEYS = frozenset(
    {"compacted_run_ids", "last_compacted_at", "last_summary_model", "last_compacted_run_count"},
)
_SEEN_STATE_KEYS = frozenset({"seen_event_ids"})


def adopt_summary_rows(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    """Give a database without summary rows its summary-row tables in the caller's write transaction.

    Every stored run first gets a position, and older compaction state becomes summary rows.
    """
    if _needs_summary_rows(connection, names):
        _create_summary_rows(connection, names)


def adoption_copy_bytes(connection: sqlite3.Connection, names: AgnoTableNames) -> int:
    """Return the bytes of run data ``adopt_summary_rows`` copies into run rows: Agno 2 blobs and archived runs.

    The copies leave the pages of the data they move free, and the conversion's later inserts reuse those pages in the
    same transaction, so SQLite journals them: such a conversion needs about this much more free space.
    """
    if not _needs_summary_rows(connection, names):
        return 0
    copied = 0
    if has_blob_column(connection, names):
        copied += _octets(connection, names.sessions, "runs")
    if f"{names.sessions}_compactions" in table_names(connection):
        copied += _octets(connection, f"{names.sessions}_compacted_runs", "run_data")
    return copied


def _octets(connection: sqlite3.Connection, table: str, column: str) -> int:
    # The length of a BLOB cast counts bytes on every SQLite release; ``octet_length`` needs SQLite 3.43.
    (octets,) = connection.execute(
        f"SELECT coalesce(sum(length(CAST({column} AS BLOB))), 0) FROM {quote_identifier(table)}",  # noqa: S608
    ).fetchone()
    return octets


def _create_summary_rows(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    for statement in agno_runs_sql(names):
        connection.execute(statement)
    if has_blob_column(connection, names):
        move_blob_runs_to_rows(connection, names)
    number_unindexed_runs(connection, names)
    for statement in agno_summary_rows_sql(names):
        connection.execute(statement)
    if f"{names.sessions}_compactions" in table_names(connection):
        _convert_archive(connection, names)
    else:
        _adopt_legacy_sessions(connection, names)


def _needs_summary_rows(connection: sqlite3.Connection, names: AgnoTableNames) -> bool:
    tables = table_names(connection)
    return names.sessions in tables and names.summaries not in tables


def _owner_scope_key(session_type: object, agent_id: object, team_id: object) -> str | None:
    """Return the key of the scope that owns an Agno session row and its summary, if the row names one."""
    if session_type == "team" and isinstance(team_id, str) and team_id:
        return f"team:{team_id}"
    if isinstance(agent_id, str) and agent_id:
        return f"agent:{agent_id}"
    return None


def _adopt_legacy_sessions(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    sessions = quote_identifier(names.sessions)
    summaries = quote_identifier(names.summaries)
    rows = connection.execute(
        f"SELECT session_id, session_type, agent_id, team_id, metadata, summary FROM {sessions}",  # noqa: S608
    ).fetchall()
    for session_id, session_type, agent_id, team_id, raw_metadata, raw_summary in rows:
        metadata = decoded_mapping(raw_metadata) or {}
        scopes = {
            scope_key: tombstones
            for scope_key, state in scope_states(metadata, LEGACY_COMPACTION_METADATA_KEY).items()
            if isinstance(state, dict) and (tombstones := _legacy_tombstones(state)) is not None
        }
        seen_states = scope_states(metadata, LEGACY_MATRIX_HISTORY_METADATA_KEY)
        summary = _summary_text(decoded_mapping(raw_summary) or {})
        summary_scope = _owner_scope_key(session_type, agent_id, team_id) if summary is not None else None
        if summary_scope is not None:
            scope_seen = json.dumps(legacy_seen_event_ids(seen_states.get(summary_scope)))
            connection.execute(
                f"INSERT INTO {summaries} (session_id, scope_key, after_run_index, summary, legacy, event_ids, "  # noqa: S608
                "seen_event_ids, created_at) VALUES (?, ?, -1, ?, 1, ?, ?, ?)",
                (session_id, summary_scope, summary, scope_seen, scope_seen, int(time.time())),
            )
            scopes.setdefault(summary_scope, ())
        tombstoned = {run_id for ids in scopes.values() for run_id in ids}
        delete_run_subtrees_in_transaction(connection, names, tombstoned, session_id=session_id)
        adopted_metadata = _without_adopted_state(metadata, scopes, summary_scope)
        if adopted_metadata != metadata:
            connection.execute(
                f"UPDATE {sessions} SET metadata = ? WHERE session_id = ?",  # noqa: S608
                (json.dumps(adopted_metadata), session_id),
            )


# LEGACY_COMPAT: Compacted runs moved out of the runs table into an archive.
# Legacy format: ``<session_table>_compactions`` generations (cumulative summary, model, ``legacy`` flag,
# captured ``legacy_event_ids``) and ``<session_table>_compacted_runs`` rows holding each folded run's JSON
# and event ids, or a content-free tombstone without ``run_data``; ``session.summary`` cached the newest
# generation's text, and a generation without text ended the summaries before it.
# Last legacy release: v2026.10.340; replacement: v2026.10.341 keeps every run in the runs table with
# summary rows beside it.
# Handling: ``adopt_summary_rows`` runs in a database's conversion to storage v2, before any response uses it. It
# drops stored copies of every archived or tombstoned run with their member runs, restores the archived runs
# ahead of the live ones in archive order, renumbers each session, and turns each
# generation into a summary row after its last restored top-level run (legacy generations before every run),
# with the event ids of its archived runs, then drops both archive tables. Replay, redaction candidates, and
# seen ids match what the archive gave after its pre-reply reconcile; tombstones are not carried over. Worker
# code can write the archive, so an archived run that is not a JSON object does not return, a row naming no
# stored session or a run by an id that is not text stays out, as does a
# stale copy's member row whose run id is not text, event ids count only as text entries of valid JSON, and text that is
# not UTF-8 reads with replacement characters, as everywhere in the conversion; none fails the conversion.
# Coverage: tests/test_archive_migration.py::test_ordinary_generations_become_summary_rows_after_their_runs,
# ::test_a_legacy_generation_stays_in_force_before_every_run, ::test_retired_generations_keep_their_runs_behind_the_barrier,
# ::test_stale_copies_of_archived_and_tombstoned_runs_are_dropped, ::test_archived_member_runs_return_with_their_team_run,
# ::test_a_generation_of_several_runs_sorts_after_its_last_top_level_run_with_their_event_ids_once,
# ::test_redaction_candidates_keep_what_the_archive_matched, ::test_an_archived_run_that_does_not_decode_stays_out,
# ::test_an_archived_run_with_unusable_values_returns_without_them,
# ::test_event_ids_the_archive_holds_in_odd_shapes_count_only_as_text,
# ::test_archive_text_that_is_not_utf8_reads_with_replacement_characters,
# ::test_archive_rows_naming_no_stored_session_or_a_run_by_an_id_that_is_not_text_stay_out, and
# ::test_a_member_row_whose_id_is_not_text_below_an_archived_run_does_not_stop_the_conversion.
def _convert_archive(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    compactions = quote_identifier(f"{names.sessions}_compactions")
    compacted_runs = quote_identifier(f"{names.sessions}_compacted_runs")
    archived: dict[str, list[tuple[str, str | None]]] = {}
    # Worker code can write the archive: a row naming no stored session, or a run by an id that is not text, stays out.
    for session_id, run_id, run_data in connection.execute(
        f"SELECT session_id, run_id, run_data FROM {compacted_runs} WHERE typeof(run_id) = 'text' "  # noqa: S608
        f"AND session_id IN (SELECT session_id FROM {quote_identifier(names.sessions)}) ORDER BY id",
    ):
        archived.setdefault(session_id, []).append((run_id, run_data))
    for session_id, entries in archived.items():
        # A stale save or blob residue can hold a copy that reconcile had not pruned yet.
        delete_run_subtrees_in_transaction(connection, names, {run_id for run_id, _ in entries}, session_id=session_id)
        restored: list[str] = []
        for run_id, run_data in entries:
            # A tombstone holds no run, and an archived run that is not a JSON object, which worker code can leave,
            # cannot return; the backup keeps both. The row's id wins over any the run's JSON holds.
            if (entry := decoded_mapping(run_data)) is not None:
                insert_run_entry(connection, names, {**entry, "run_id": run_id}, session_id=session_id)
                restored.append(run_id)
        restored_ids = set(restored)
        live = [run_id for run_id in ordered_run_ids(connection, names, session_id) if run_id not in restored_ids]
        renumber_runs(connection, names, session_id, [*restored, *live])
    _insert_generation_rows(connection, names)
    connection.execute(f"DROP TABLE {compacted_runs}")
    connection.execute(f"DROP TABLE {compactions}")


def _insert_generation_rows(connection: sqlite3.Connection, names: AgnoTableNames) -> None:
    compactions = quote_identifier(f"{names.sessions}_compactions")
    compacted_runs = quote_identifier(f"{names.sessions}_compacted_runs")
    summaries = quote_identifier(names.summaries)
    positions: dict[tuple[str, str], int] = {}
    generations = connection.execute(
        f"SELECT id, session_id, scope_key, summary, summary_model, legacy, created_at FROM {compactions} "  # noqa: S608
        f"WHERE session_id IN (SELECT session_id FROM {quote_identifier(names.sessions)}) ORDER BY id",
    ).fetchall()
    for generation_id, session_id, scope_key, summary, summary_model, legacy, created_at in generations:
        (newest_run,) = connection.execute(
            f"SELECT MAX(run.run_index) FROM {compacted_runs} AS archived "  # noqa: S608
            f"JOIN {quote_identifier(names.runs)} AS run "
            "ON run.session_id = archived.session_id AND run.run_id = archived.run_id "
            "WHERE archived.compaction_id = ? AND archived.run_data IS NOT NULL AND run.parent_run_id IS NULL",
            (generation_id,),
        ).fetchone()
        if legacy:
            position = -1
        elif newest_run is not None:
            position = newest_run
        else:
            position = positions.get((session_id, scope_key), -1)
        positions[session_id, scope_key] = position
        connection.execute(
            f"INSERT INTO {summaries} (session_id, scope_key, after_run_index, summary, summary_model, legacy, "  # noqa: S608
            "event_ids, seen_event_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                scope_key,
                position,
                summary,
                summary_model,
                legacy,
                json.dumps(_generation_event_ids(connection, names, generation_id, "event_ids")),
                json.dumps(_generation_event_ids(connection, names, generation_id, "seen_event_ids")),
                created_at,
            ),
        )


def _generation_event_ids(
    connection: sqlite3.Connection,
    names: AgnoTableNames,
    generation_id: int,
    column: str,
) -> list[str]:
    """Return the event ids a generation's archived runs carry in ``column``, plus a legacy generation's own.

    Only text entries of valid JSON count: worker code can write the archive, and no writer stored any other.
    """
    compactions = quote_identifier(f"{names.sessions}_compactions")
    compacted_runs = quote_identifier(f"{names.sessions}_compacted_runs")
    return sorted(
        value
        for (value,) in connection.execute(
            f"SELECT item.value FROM {compacted_runs} AS archived, "  # noqa: S608
            f"json_each({_json_or_empty(f'archived.{column}')}) AS item "
            "WHERE archived.compaction_id = ? AND item.type = 'text' "
            f"UNION SELECT item.value FROM {compactions} AS generation, "
            f"json_each({_json_or_empty('generation.legacy_event_ids')}) AS item "
            "WHERE generation.id = ? AND generation.legacy = 1 AND item.type = 'text'",
            (generation_id, generation_id),
        )
    )


def _json_or_empty(column: str) -> str:
    """Return SQL for ``column``'s JSON, or an empty array when it holds none, so ``json_each`` never fails on it."""
    return f"CASE WHEN json_valid({column}) THEN {column} ELSE '[]' END"


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
        (LEGACY_COMPACTION_METADATA_KEY, _LEGACY_STATE_KEYS, scope_keys),
        (LEGACY_MATRIX_HISTORY_METADATA_KEY, _SEEN_STATE_KEYS, () if summary_scope is None else (summary_scope,)),
    ):
        states = scope_states(metadata, metadata_key)
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


def _summary_text(summary: dict[str, Any]) -> str | None:
    text = summary.get("summary")
    return text if isinstance(text, str) and text.strip() else None
