"""One-time migration of compaction state written before compacted runs were archived."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import TYPE_CHECKING, Any, cast

from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, MINDROOM_MATRIX_HISTORY_METADATA_KEY
from mindroom.history.archive_schema import archive_schema_sql, archive_table_names
from mindroom.history.types import HistoryScope
from mindroom.legacy_session_storage import decode_persisted_session_json
from mindroom.usage_storage import quote_identifier

if TYPE_CHECKING:
    from collections.abc import Collection
    from pathlib import Path

# LEGACY_COMPAT: Destructive compaction state without an archive.
# Legacy format: A v2 ``mindroom_compaction`` scope state holding ``compacted_run_ids``
# tombstones and ``last_compacted_at``/``last_summary_model``/``last_compacted_run_count``
# audit fields, and a replayed ``session.summary``, written when compaction deleted the runs it
# summarized.
# Last legacy release: v2026.9.314; replacement: the next release archives compacted runs
# in ``<session_table>_compactions`` and ``<session_table>_compacted_runs``.
# Handling: ``migrate_compaction_database`` runs once per conversation database when storage
# opens, before any response uses it, and is detected by the archive tables' absence. It records
# each such scope as a content-free legacy generation holding its tombstone rows; the scope owning
# the summary also gets the summary and its preserved seen ids, which move out of session metadata.
# The retired keys are stripped.
# Runs deleted by those releases stay lost; ``history/storage.py`` owns redaction of the summary.
# Coverage: tests/test_legacy_compaction_state.py.
_LEGACY_STATE_KEYS = frozenset(
    {"compacted_run_ids", "last_compacted_at", "last_summary_model", "last_compacted_run_count"},
)
_COMPACTION_STATE_VERSION = 2
_SEEN_STATE_VERSION = 1
_SEEN_STATE_KEYS = frozenset({"seen_event_ids"})


def migrate_compaction_database(path: Path, session_table: str) -> None:
    """Create the archive once, adopting every scope the destructive compactor left behind."""
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
            for statement in archive_schema_sql(session_table):
                connection.execute(statement)
            _adopt_legacy_sessions(connection, session_table)
        connection.commit()
    finally:
        # close rolls back both DDL and rows on interruption, leaving migration retryable.
        connection.close()


def _needs_migration(connection: sqlite3.Connection, session_table: str) -> bool:
    tables = {name for (name,) in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    return session_table in tables and archive_table_names(session_table)[0] not in tables


def _adopt_legacy_sessions(connection: sqlite3.Connection, session_table: str) -> None:
    sessions = quote_identifier(session_table)
    compactions, compacted_runs = (quote_identifier(name) for name in archive_table_names(session_table))
    rows = connection.execute(
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
            scopes.setdefault(summary_scope, ())
        for scope_key, tombstones in scopes.items():
            compaction_id = connection.execute(
                f"INSERT INTO {compactions} "  # noqa: S608
                "(session_id, scope_key, summary, legacy, legacy_event_ids, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                (
                    session_id,
                    scope_key,
                    summary if scope_key == summary_scope else None,
                    json.dumps(_seen_event_ids(seen_states.get(scope_key)) if scope_key == summary_scope else []),
                    int(time.time()),
                ),
            ).lastrowid
            connection.executemany(
                f"INSERT INTO {compacted_runs} (compaction_id, session_id, run_id, event_ids) "  # noqa: S608
                "VALUES (?, ?, ?, '[]') ON CONFLICT(session_id, run_id) DO NOTHING",
                [(compaction_id, session_id, run_id) for run_id in tombstones],
            )
        adopted_metadata = _without_adopted_state(metadata, scopes, summary_scope)
        if adopted_metadata != metadata:
            connection.execute(
                f"UPDATE {sessions} SET metadata = ? WHERE session_id = ?",  # noqa: S608
                (json.dumps(adopted_metadata), session_id),
            )


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
