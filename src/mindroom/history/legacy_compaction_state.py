"""One-time migration of a conversation database into the summary rows of the conversation log."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any, cast

from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, MINDROOM_MATRIX_HISTORY_METADATA_KEY
from mindroom.history.summary_rows_schema import summary_rows_schema_sql, summary_rows_table_names
from mindroom.history.types import HistoryScope
from mindroom.session_storage import agno_compat_sqlite
from mindroom.session_storage.legacy_session_storage import decode_persisted_session_json, materialize_legacy_run_blobs
from mindroom.usage.storage import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Collection

    from agno.db.sqlite import SqliteDb
    from sqlalchemy import Connection

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
            for statement in summary_rows_schema_sql(session_table):
                connection.exec_driver_sql(statement)
            if f"{session_table}_compactions" not in _tables(connection):
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
        summary_scope = _owner_scope_key(session_type, agent_id, team_id) if summary is not None else None
        if summary_scope is not None:
            scope_seen = json.dumps(_seen_event_ids(seen_states.get(summary_scope)))
            connection.exec_driver_sql(
                f"INSERT INTO {summaries} (session_id, scope_key, after_run_index, summary, legacy, event_ids, "  # noqa: S608
                "seen_event_ids, created_at) VALUES (?, ?, -1, ?, 1, ?, ?, ?)",
                (session_id, summary_scope, summary, scope_seen, scope_seen, int(time.time())),
            )
            scopes.setdefault(summary_scope, ())
        tombstoned = {run_id for ids in scopes.values() for run_id in ids}
        _drop_tombstoned_runs(connection, runs_table, session_id, tombstoned)
        adopted_metadata = _without_adopted_state(metadata, scopes, summary_scope)
        if adopted_metadata != metadata:
            connection.exec_driver_sql(
                f"UPDATE {sessions} SET metadata = ? WHERE session_id = ?",  # noqa: S608
                (json.dumps(adopted_metadata), session_id),
            )


def _drop_tombstoned_runs(connection: Connection, runs_table: str, session_id: str, run_ids: set[str]) -> None:
    """Drop stored copies of runs the destructive compactor deleted, with their member runs."""
    runs = quote_identifier(runs_table)
    frontier = sorted(run_ids)
    while frontier:
        placeholders = ", ".join("?" for _ in frontier)
        children = [
            run_id
            for (run_id,) in connection.exec_driver_sql(
                f"SELECT run_id FROM {runs} WHERE session_id = ? AND parent_run_id IN ({placeholders})",  # noqa: S608
                (session_id, *frontier),
            )
        ]
        connection.exec_driver_sql(
            f"DELETE FROM {runs} WHERE session_id = ? AND run_id IN ({placeholders})",  # noqa: S608
            (session_id, *frontier),
        )
        frontier = children


def _legacy_tombstones(raw_state: dict[str, Any]) -> tuple[str, ...] | None:
    """Return a destructive compactor's tombstones, or ``None`` when it did not write this state."""
    if not _LEGACY_STATE_KEYS.intersection(raw_state):
        return None
    raw_run_ids = raw_state.get("compacted_run_ids")
    if not isinstance(raw_run_ids, list):
        return ()
    return tuple(dict.fromkeys(run_id for run_id in raw_run_ids if isinstance(run_id, str) and run_id))


def _owner_scope_key(session_type: object, agent_id: object, team_id: object) -> str | None:
    """Return the key of the scope that owns a session row, which also owns its replayed summary."""
    if session_type == "team" and isinstance(team_id, str) and team_id:
        return HistoryScope(kind="team", scope_id=team_id).key
    if isinstance(agent_id, str) and agent_id:
        return HistoryScope(kind="agent", scope_id=agent_id).key
    return None


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
