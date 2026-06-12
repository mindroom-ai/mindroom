"""Single owner of durable compaction state.

This module is the only code allowed to read or write the three durable
compaction-state locations inside a stored Agno session:

- per-scope control/audit state under ``MINDROOM_COMPACTION_METADATA_KEY``
  (last compaction audit fields, force flag, and compacted-run tombstones)
- per-scope consumed Matrix event ids under ``MINDROOM_MATRIX_HISTORY_METADATA_KEY``
- the pending force-compaction scope keys list inside Agno ``session_state``

It enforces the durable-state half of the compaction invariants
(see ``tests/test_compaction_invariants.py``):

1. Compacted runs never reappear.
   Every compacted run id is recorded as a tombstone in scope state
   (capped to the newest ``_COMPACTED_RUN_ID_RETENTION_LIMIT`` ids), and
   ``prune_reintroduced_runs`` removes resurrected runs — including their
   descendants — before each run uses persisted history.

2. Chunk progress survives interruption.
   ``record_compaction_chunk`` persists one chunk's partial summary, tombstones,
   and run removals in a single session upsert against the freshest stored row,
   so a crash between chunks neither loses the partial summary nor resurrects
   removed runs on restart.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from agno.db.base import SessionType
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.team import TeamSession

from mindroom.constants import (
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.metadata_merge import deep_merge_metadata

if TYPE_CHECKING:
    from collections.abc import Iterable

    from agno.db.base import BaseDb
    from agno.session.agent import AgentSession

_COMPACTION_METADATA_VERSION = 2
_MATRIX_HISTORY_METADATA_VERSION = 1
_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY = "mindroom_pending_compaction_scope_keys"
_COMPACTED_RUN_ID_RETENTION_LIMIT = 1_024


def read_scope_state(session: AgentSession | TeamSession, scope: HistoryScope) -> HistoryScopeState:
    """Return the scoped compaction state for one session and scope."""
    states = _read_scope_states(session)
    return states.get(scope.key) or HistoryScopeState()


def _read_scope_states(session: AgentSession | TeamSession) -> dict[str, HistoryScopeState]:
    """Return all parsed compaction states from session metadata."""
    metadata = session.metadata
    if isinstance(metadata, dict):
        raw_value = metadata.get(MINDROOM_COMPACTION_METADATA_KEY)
        if isinstance(raw_value, dict) and raw_value.get("version") == _COMPACTION_METADATA_VERSION:
            raw_states = raw_value.get("states")
            if isinstance(raw_states, dict):
                parsed_states: dict[str, HistoryScopeState] = {}
                for scope_key, raw_state in raw_states.items():
                    if not isinstance(scope_key, str) or not scope_key or not isinstance(raw_state, dict):
                        continue
                    parsed_states[scope_key] = _parse_state(raw_state)
                return parsed_states
    return {}


def write_scope_state(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> None:
    """Persist compaction control/audit state back into session metadata."""
    states = _read_scope_states(session)
    if _state_is_empty(state):
        states.pop(scope.key, None)
    else:
        states[scope.key] = state

    session_metadata = dict(session.metadata or {})
    serialized_states = {
        scope_key: _state_to_metadata(scope_state)
        for scope_key, scope_state in states.items()
        if not _state_is_empty(scope_state)
    }
    if not serialized_states:
        session_metadata.pop(MINDROOM_COMPACTION_METADATA_KEY, None)
    else:
        session_metadata[MINDROOM_COMPACTION_METADATA_KEY] = {
            "version": _COMPACTION_METADATA_VERSION,
            "states": serialized_states,
        }
    session.metadata = session_metadata


def clear_force_compaction_state(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> HistoryScopeState:
    """Clear the next-run force flag in one session scope."""
    return set_force_compaction_state(session, scope, state, force=False)


def set_force_compaction_state(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
    *,
    force: bool,
) -> HistoryScopeState:
    """Set the next-run force flag in one session scope."""
    next_state = replace(state, force_compact_before_next_run=force)
    write_scope_state(session, scope, next_state)
    return next_state


def add_pending_force_compaction_scope(
    session_state: dict[str, object] | None,
    scope: HistoryScope,
) -> dict[str, object]:
    """Record a next-run compaction request inside Agno session_state."""
    next_session_state = session_state if session_state is not None else {}
    raw_scope_keys = next_session_state.get(_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY)
    scope_keys = (
        [scope_key for scope_key in raw_scope_keys if isinstance(scope_key, str) and scope_key]
        if isinstance(raw_scope_keys, list)
        else []
    )
    if scope.key not in scope_keys:
        scope_keys.append(scope.key)
    next_session_state[_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY] = scope_keys
    return next_session_state


def consume_pending_force_compaction_scope(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> bool:
    """Consume one pending next-run compaction request from Agno session_state."""
    session_data = session.session_data
    if not isinstance(session_data, dict):
        return False
    raw_session_state = session_data.get("session_state")
    if not isinstance(raw_session_state, dict):
        return False
    raw_scope_keys = raw_session_state.get(_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY)
    if not isinstance(raw_scope_keys, list):
        return False

    scope_keys = [scope_key for scope_key in raw_scope_keys if isinstance(scope_key, str) and scope_key]
    if scope.key not in scope_keys:
        return False

    remaining_scope_keys = [scope_key for scope_key in scope_keys if scope_key != scope.key]
    next_session_state = dict(raw_session_state)
    if remaining_scope_keys:
        next_session_state[_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY] = remaining_scope_keys
    else:
        next_session_state.pop(_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY, None)

    next_session_data = dict(session_data)
    if next_session_state:
        next_session_data["session_state"] = next_session_state
    else:
        next_session_data.pop("session_state", None)

    session.session_data = next_session_data or None
    return True


def has_pending_force_compaction_scope(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> bool:
    """Return whether Agno session_state has an unconsumed compaction request."""
    session_data = session.session_data
    if not isinstance(session_data, dict):
        return False
    raw_session_state = session_data.get("session_state")
    if not isinstance(raw_session_state, dict):
        return False
    raw_scope_keys = raw_session_state.get(_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY)
    if not isinstance(raw_scope_keys, list):
        return False
    return scope.key in {scope_key for scope_key in raw_scope_keys if isinstance(scope_key, str) and scope_key}


def read_scope_seen_event_ids(session: AgentSession | TeamSession, scope: HistoryScope) -> set[str]:
    """Return the consumed Matrix event ids for one session scope."""
    seen_event_ids = _read_preserved_scope_seen_event_ids(session, scope)
    for run in session.runs or []:
        if not isinstance(run, (RunOutput, TeamRunOutput)):
            continue
        if _scope_for_run(run) != scope:
            continue
        seen_event_ids.update(_run_seen_event_ids(run))
    return seen_event_ids


def seen_event_ids_for_runs(runs: Iterable[RunOutput | TeamRunOutput]) -> set[str]:
    """Return Matrix event ids already represented by run metadata."""
    seen_event_ids: set[str] = set()
    for run in runs:
        seen_event_ids.update(_run_seen_event_ids(run))
    return seen_event_ids


def _run_seen_event_ids(run: RunOutput | TeamRunOutput) -> set[str]:
    """Return Matrix event ids already represented by one run."""
    metadata = run.metadata
    if not isinstance(metadata, dict):
        return set()
    seen_event_ids: set[str] = set()
    raw_seen_ids = metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    if isinstance(raw_seen_ids, list):
        seen_event_ids.update(event_id for event_id in raw_seen_ids if isinstance(event_id, str) and event_id)
    response_event_id = metadata.get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY)
    if isinstance(response_event_id, str) and response_event_id:
        seen_event_ids.add(response_event_id)
    return seen_event_ids


def update_scope_seen_event_ids(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    event_ids: list[str],
) -> bool:
    """Merge consumed Matrix event ids into one session scope."""
    normalized_event_ids = sorted({event_id for event_id in event_ids if event_id})
    if not normalized_event_ids:
        return False

    states = _read_scope_seen_event_states(session)
    existing_seen_ids = _read_preserved_scope_seen_event_ids(session, scope)
    updated_seen_ids = sorted(existing_seen_ids.union(normalized_event_ids))
    if updated_seen_ids == sorted(existing_seen_ids):
        return False

    states[scope.key] = set(updated_seen_ids)
    _write_scope_seen_event_states(session, states)
    return True


def _metadata_with_merged_seen_event_ids(
    merged_metadata: dict[str, Any] | None,
    *metadata_sources: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Return metadata with Matrix seen-event IDs unioned from all sources."""
    seen_event_states: dict[str, set[str]] = {}
    for metadata in metadata_sources:
        seen_event_states = _merge_scope_seen_event_states(
            seen_event_states,
            _read_scope_seen_event_states_from_metadata(metadata),
        )
    if not seen_event_states:
        return merged_metadata
    return _metadata_with_scope_seen_event_states(merged_metadata, seen_event_states)


def _parse_state(raw_state: dict[str, Any]) -> HistoryScopeState:
    compacted_at = raw_state.get("last_compacted_at")
    summary_model = raw_state.get("last_summary_model")
    compacted_run_count = raw_state.get("last_compacted_run_count")
    compacted_run_ids = raw_state.get("compacted_run_ids")
    force_flag = raw_state.get("force_compact_before_next_run")
    return HistoryScopeState(
        last_compacted_at=compacted_at if isinstance(compacted_at, str) else None,
        last_summary_model=summary_model if isinstance(summary_model, str) else None,
        last_compacted_run_count=compacted_run_count if isinstance(compacted_run_count, int) else None,
        compacted_run_ids=(
            _normalize_compacted_run_ids(compacted_run_ids) if isinstance(compacted_run_ids, list) else ()
        ),
        force_compact_before_next_run=bool(force_flag),
    )


def _state_to_metadata(state: HistoryScopeState) -> dict[str, object]:
    payload: dict[str, object] = {
        "force_compact_before_next_run": state.force_compact_before_next_run,
    }
    if state.last_compacted_at is not None:
        payload["last_compacted_at"] = state.last_compacted_at
    if state.last_summary_model is not None:
        payload["last_summary_model"] = state.last_summary_model
    if state.last_compacted_run_count is not None:
        payload["last_compacted_run_count"] = state.last_compacted_run_count
    if state.compacted_run_ids:
        payload["compacted_run_ids"] = list(_normalize_compacted_run_ids(state.compacted_run_ids))
    return payload


def _state_is_empty(state: HistoryScopeState) -> bool:
    return (
        state.last_compacted_at is None
        and state.last_summary_model is None
        and state.last_compacted_run_count is None
        and not state.compacted_run_ids
        and not state.force_compact_before_next_run
    )


def _normalize_compacted_run_ids(run_ids: Iterable[object]) -> tuple[str, ...]:
    """Deduplicate tombstones preserving order and keep only the newest ids."""
    compacted_run_ids: list[str] = []
    seen_run_ids: set[str] = set()
    for run_id in run_ids:
        if not isinstance(run_id, str) or not run_id or run_id in seen_run_ids:
            continue
        seen_run_ids.add(run_id)
        compacted_run_ids.append(run_id)
    return tuple(compacted_run_ids[-_COMPACTED_RUN_ID_RETENTION_LIMIT:])


def compacted_run_ids_with(state: HistoryScopeState, run_ids: Iterable[str]) -> tuple[str, ...]:
    """Return the state tombstone list extended with newly compacted run ids."""
    return _normalize_compacted_run_ids([*state.compacted_run_ids, *run_ids])


def remove_runs_by_id(
    runs: Iterable[RunOutput | TeamRunOutput],
    compacted_run_ids: Iterable[str],
) -> list[RunOutput | TeamRunOutput]:
    """Return runs with the compacted run ids, and all their descendants, removed."""
    remove_ids = {run_id for run_id in compacted_run_ids if run_id}
    if not remove_ids:
        return list(runs)

    run_list = list(runs)
    children_by_parent: dict[str, list[str]] = {}
    for run in run_list:
        parent_run_id = run.parent_run_id
        run_id = run.run_id
        if isinstance(parent_run_id, str) and parent_run_id and isinstance(run_id, str) and run_id:
            children_by_parent.setdefault(parent_run_id, []).append(run_id)

    stack = list(remove_ids)
    while stack:
        run_id = stack.pop()
        for child_run_id in children_by_parent.get(run_id, []):
            if child_run_id not in remove_ids:
                remove_ids.add(child_run_id)
                stack.append(child_run_id)

    return [
        run
        for run in run_list
        if not (
            (isinstance(run.run_id, str) and run.run_id in remove_ids)
            or (isinstance(run.parent_run_id, str) and run.parent_run_id in remove_ids)
        )
    ]


def prune_reintroduced_runs(
    session: AgentSession | TeamSession,
    state: HistoryScopeState,
) -> bool:
    """Remove runs that a stale session write resurrected after compaction (invariant 1)."""
    if not state.compacted_run_ids:
        return False
    runs = session.runs or []
    pruned_runs = remove_runs_by_id(runs, state.compacted_run_ids)
    if len(pruned_runs) == len(runs):
        return False
    session.runs = pruned_runs
    return True


def latest_persisted_session(
    storage: BaseDb,
    session: AgentSession | TeamSession,
) -> AgentSession | TeamSession:
    """Return the freshest stored row for one session, or the given session when unavailable."""
    session_type = SessionType.TEAM if isinstance(session, TeamSession) else SessionType.AGENT
    latest_session = storage.get_session(session_id=session.session_id, session_type=session_type)
    return latest_session if isinstance(latest_session, type(session)) else session


def adopt_session_fields(
    session: AgentSession | TeamSession,
    source: AgentSession | TeamSession,
) -> None:
    """Sync one in-memory session's durable fields from another loaded row."""
    session.metadata = source.metadata
    session.runs = source.runs
    session.summary = source.summary


def record_compaction_chunk(
    *,
    storage: BaseDb,
    persisted_session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
    scope: HistoryScope,
    compacted_run_ids: Iterable[str],
    sync_remaining_runs: bool = False,
) -> None:
    """Durably persist one compaction chunk before the next chunk is attempted (invariant 2).

    One upsert against the freshest stored row carries the partial summary, the
    merged scope metadata, the compacted-run tombstones, and the run removals,
    so an interruption between chunks can neither lose progress nor resurrect
    already-compacted runs.
    """
    chunk_run_ids = [run_id for run_id in compacted_run_ids if run_id]
    working_state = read_scope_state(working_session, scope)
    write_scope_state(
        working_session,
        scope,
        replace(working_state, compacted_run_ids=compacted_run_ids_with(working_state, chunk_run_ids)),
    )

    target_session = latest_persisted_session(storage, persisted_session)
    preexisting_tombstones = read_scope_state(target_session, scope).compacted_run_ids
    target_session.summary = working_session.summary
    target_session.metadata = _metadata_with_merged_seen_event_ids(
        deep_merge_metadata(target_session.metadata, working_session.metadata),
        target_session.metadata,
        working_session.metadata,
    )
    target_state = read_scope_state(target_session, scope)
    # The three-way union is deliberate, not redundant: preexisting_tombstones was
    # captured before the metadata merge so tombstones present only on the stored row
    # survive, target_state carries the post-merge ids, and the explicit chunk_run_ids
    # guard against the generic metadata merge dropping the compaction key entirely.
    # _normalize_compacted_run_ids dedupes.
    write_scope_state(
        target_session,
        scope,
        replace(
            target_state,
            compacted_run_ids=_normalize_compacted_run_ids(
                [*preexisting_tombstones, *target_state.compacted_run_ids, *chunk_run_ids],
            ),
        ),
    )
    target_session.runs = remove_runs_by_id(target_session.runs or [], chunk_run_ids)
    if sync_remaining_runs:
        target_session.runs = _sync_remaining_runs_from_working(
            target_session.runs or [],
            working_session.runs or [],
        )
    storage.upsert_session(target_session)
    adopt_session_fields(persisted_session, target_session)


def _sync_remaining_runs_from_working(
    target_runs: list[RunOutput | TeamRunOutput],
    working_runs: list[RunOutput | TeamRunOutput],
) -> list[RunOutput | TeamRunOutput]:
    working_by_id = {run.run_id: run for run in working_runs if isinstance(run.run_id, str) and run.run_id}
    synced_runs: list[RunOutput | TeamRunOutput] = []
    for run in target_runs:
        run_id = run.run_id
        if isinstance(run_id, str) and run_id in working_by_id:
            synced_runs.append(deepcopy(working_by_id[run_id]))
        else:
            synced_runs.append(run)
    return synced_runs


def _read_preserved_scope_seen_event_ids(session: AgentSession | TeamSession, scope: HistoryScope) -> set[str]:
    return set(_read_scope_seen_event_states(session).get(scope.key, set()))


def _read_scope_seen_event_states(session: AgentSession | TeamSession) -> dict[str, set[str]]:
    return _read_scope_seen_event_states_from_metadata(session.metadata)


def _read_scope_seen_event_states_from_metadata(metadata: dict[str, Any] | None) -> dict[str, set[str]]:
    if not isinstance(metadata, dict):
        return {}

    raw_value = _valid_matrix_history_metadata(metadata)
    if raw_value is None:
        return {}

    raw_states = raw_value.get("states")
    if not isinstance(raw_states, dict):
        return {}

    parsed: dict[str, set[str]] = {}
    for scope_key, raw_state in raw_states.items():
        if not isinstance(scope_key, str) or not isinstance(raw_state, dict):
            continue
        raw_seen_ids = raw_state.get("seen_event_ids")
        if not isinstance(raw_seen_ids, list):
            continue
        parsed[scope_key] = {event_id for event_id in raw_seen_ids if isinstance(event_id, str) and event_id}
    return parsed


def _write_scope_seen_event_states(session: AgentSession | TeamSession, states: dict[str, set[str]]) -> None:
    session.metadata = _metadata_with_scope_seen_event_states(session.metadata, states) or {}


def _metadata_with_scope_seen_event_states(
    metadata: dict[str, Any] | None,
    states: dict[str, set[str]],
) -> dict[str, Any] | None:
    session_metadata = dict(metadata or {})
    serialized_states = {
        scope_key: _state_with_seen_event_ids(session_metadata, scope_key, event_ids)
        for scope_key, event_ids in sorted(states.items())
        if event_ids
    }
    if serialized_states:
        raw_value = _valid_matrix_history_metadata(session_metadata)
        matrix_history = dict(raw_value) if raw_value is not None else {}
        raw_states = matrix_history.get("states")
        next_states = dict(raw_states) if isinstance(raw_states, dict) else {}
        next_states.update(serialized_states)
        matrix_history["version"] = _MATRIX_HISTORY_METADATA_VERSION
        matrix_history["states"] = next_states
        session_metadata[MINDROOM_MATRIX_HISTORY_METADATA_KEY] = matrix_history
    else:
        session_metadata.pop(MINDROOM_MATRIX_HISTORY_METADATA_KEY, None)
    return session_metadata


def _state_with_seen_event_ids(
    metadata: dict[str, Any],
    scope_key: str,
    event_ids: set[str],
) -> dict[str, Any]:
    raw_value = _valid_matrix_history_metadata(metadata)
    raw_states = raw_value.get("states") if raw_value is not None else None
    raw_state = raw_states.get(scope_key) if isinstance(raw_states, dict) else None
    state = dict(raw_state) if isinstance(raw_state, dict) else {}
    state["seen_event_ids"] = sorted(event_ids)
    return state


def _valid_matrix_history_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw_value = metadata.get(MINDROOM_MATRIX_HISTORY_METADATA_KEY)
    if not isinstance(raw_value, dict):
        return None
    if raw_value.get("version") != _MATRIX_HISTORY_METADATA_VERSION:
        return None
    return raw_value


def _merge_scope_seen_event_states(
    base_states: dict[str, set[str]],
    extra_states: dict[str, set[str]],
) -> dict[str, set[str]]:
    merged = {scope_key: set(event_ids) for scope_key, event_ids in base_states.items()}
    for scope_key, event_ids in extra_states.items():
        merged.setdefault(scope_key, set()).update(event_ids)
    return merged


def _scope_for_run(run: RunOutput | TeamRunOutput) -> HistoryScope | None:
    if isinstance(run, TeamRunOutput):
        team_id = run.team_id
        if isinstance(team_id, str) and team_id:
            return HistoryScope(kind="team", scope_id=team_id)
        return None
    agent_id = run.agent_id
    if isinstance(agent_id, str) and agent_id:
        return HistoryScope(kind="agent", scope_id=agent_id)
    return None
