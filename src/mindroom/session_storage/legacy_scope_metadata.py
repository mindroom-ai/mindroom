"""The storage step that gives each session row its owner's history state in one metadata value."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, cast

from mindroom.constants import MINDROOM_HISTORY_METADATA_KEY
from mindroom.session_storage.schema import quote_identifier
from mindroom.session_storage.store import database_owner, decoded_mapping

if TYPE_CHECKING:
    import sqlite3

    from mindroom.session_storage.schema import TableNames

# The session metadata keys that held each scope's history state, and the versions their readers accepted.
LEGACY_COMPACTION_METADATA_KEY = "mindroom_compaction"
LEGACY_MATRIX_HISTORY_METADATA_KEY = "mindroom_matrix_history"
_VERSIONS = {LEGACY_COMPACTION_METADATA_KEY: 2, LEGACY_MATRIX_HISTORY_METADATA_KEY: 1}


# LEGACY_COMPAT: History state kept per scope in session metadata.
# Legacy format: A session row's metadata held ``mindroom_compaction`` ``{"version": 2, "states": {scope_key:
# {"force_compact_before_next_run": true}}}`` and ``mindroom_matrix_history`` ``{"version": 1, "states": {scope_key:
# {"seen_event_ids": [...]}}}``, keyed by scope, although every reader read only the database owner's entry.
# Last legacy release: v2026.10.725; replacement: the next release, one ``mindroom_history`` value holding the owner's
# ``force_compact_before_next_run`` and ``seen_event_ids``.
# Handling: ``flatten_history_metadata`` runs in the step to layout version 7, after every older step, so the
# Agno conversion's adoption of other scopes' tombstones has already read them. The owner's force flag and seen ids move
# into the new value, merged with one the conversion's force-request fold already wrote; other scopes' entries, which no
# release read, and values in a version no reader accepted are dropped with both keys. A row without either key keeps
# its metadata bytes.
# Coverage: tests/session_storage/test_legacy_scope_metadata.py::test_every_recorded_session_keeps_its_owner_state_in_one_value,
# tests/session_storage/test_legacy_scope_metadata.py::test_other_scopes_and_unread_versions_are_dropped_and_a_newer_flag_is_kept,
# tests/session_storage/test_legacy_scope_metadata.py::test_a_converted_database_converts_once.
def flatten_history_metadata(connection: sqlite3.Connection, names: TableNames) -> None:
    """Move the owner's per-scope history state into one metadata value, in the caller's write transaction."""
    owner_key = database_owner(connection, names).key
    sessions = quote_identifier(names.sessions)
    for session_id, raw_metadata in connection.execute(
        f"SELECT session_id, metadata FROM {sessions}",  # noqa: S608
    ).fetchall():
        metadata = decoded_mapping(raw_metadata)
        if metadata is not None and _VERSIONS.keys() & metadata.keys():
            connection.execute(
                f"UPDATE {sessions} SET metadata = ? WHERE session_id = ?",  # noqa: S608
                (json.dumps(_flattened_history_metadata(metadata, owner_key)), session_id),
            )


def _flattened_history_metadata(metadata: dict[str, Any], owner_key: str) -> dict[str, Any]:
    """Return session ``metadata`` with the per-scope history state of ``owner_key`` moved into one value."""
    state = {**_owner_state(metadata, owner_key), **_current_state(metadata)}
    flattened = {key: value for key, value in metadata.items() if key not in _VERSIONS}
    if state:
        flattened[MINDROOM_HISTORY_METADATA_KEY] = state
    return flattened


def _owner_state(metadata: dict[str, Any], owner_key: str) -> dict[str, Any]:
    """Return the owner's entries of both legacy maps as one state, each field only when it is set."""
    state: dict[str, Any] = {}
    compaction = scope_states(metadata, LEGACY_COMPACTION_METADATA_KEY).get(owner_key)
    if isinstance(compaction, dict) and compaction.get("force_compact_before_next_run") is True:
        state["force_compact_before_next_run"] = True
    if seen := legacy_seen_event_ids(scope_states(metadata, LEGACY_MATRIX_HISTORY_METADATA_KEY).get(owner_key)):
        state["seen_event_ids"] = seen
    return state


def scope_states(metadata: dict[str, Any], key: str) -> dict[str, Any]:
    """Return one legacy per-scope map of session ``metadata`` by scope key, or nothing in a version no reader read."""
    value = metadata.get(key)
    if not isinstance(value, dict) or value.get("version") != _VERSIONS[key]:
        return {}
    states = value.get("states")
    return states if isinstance(states, dict) else {}


def legacy_seen_event_ids(raw_state: object) -> list[str]:
    """Return the seen event ids one scope's legacy state preserved, sorted, without duplicates or non-ids."""
    raw_event_ids = cast("dict[str, Any]", raw_state).get("seen_event_ids") if isinstance(raw_state, dict) else None
    if not isinstance(raw_event_ids, list):
        return []
    return sorted({event_id for event_id in raw_event_ids if isinstance(event_id, str) and event_id})


def _current_state(metadata: dict[str, Any]) -> dict[str, Any]:
    value = metadata.get(MINDROOM_HISTORY_METADATA_KEY)
    return value if isinstance(value, dict) else {}
