"""History-state persistence."""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession

from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY, MINDROOM_MATRIX_HISTORY_METADATA_KEY
from mindroom.history.types import HistoryScope, HistoryScopeState

if TYPE_CHECKING:
    from agno.db.base import BaseDb, SessionType

_COMPACTION_METADATA_VERSION = 2
_MATRIX_HISTORY_METADATA_VERSION = 1
_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY = "mindroom_pending_compaction_scope_keys"


def read_scope_state(session: AgentSession | TeamSession, scope: HistoryScope) -> HistoryScopeState:
    """Return the scoped compaction state for one session and scope."""
    states = read_scope_states(session)
    return states.get(scope.key) or HistoryScopeState()


def read_scope_states(session: AgentSession | TeamSession) -> dict[str, HistoryScopeState]:
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
    states = read_scope_states(session)
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
    cleared_state = replace(state, force_compact_before_next_run=False)
    write_scope_state(session, scope, cleared_state)
    return cleared_state


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


def strip_transient_enrichment_from_session(
    storage: BaseDb,
    *,
    session_id: str,
    session_type: SessionType,
    response_run_id: str | None = None,
    memory_prompt: str,
) -> bool:
    """Restore the persisted current user turn after transient model context was used."""
    session = storage.get_session(session_id, session_type)
    if not isinstance(session, AgentSession | TeamSession) or not session.runs:
        return False

    runs = list(reversed(session.runs))
    has_run_ids = any(isinstance(run, RunOutput | TeamRunOutput) and run.run_id for run in runs)
    for run in runs:
        if not isinstance(run, RunOutput | TeamRunOutput) or not run.messages:
            continue
        if response_run_id is not None and has_run_ids and run.run_id != response_run_id:
            continue
        for message in reversed(run.messages):
            if message.role != "user":
                continue
            if message.content == memory_prompt:
                return False
            message.content = memory_prompt
            storage.upsert_session(session)
            return True
    return False


def read_scope_seen_event_ids(session: AgentSession | TeamSession, scope: HistoryScope) -> set[str]:
    """Return the consumed Matrix event ids for one session scope."""
    seen_event_ids = _read_preserved_scope_seen_event_ids(session, scope)
    for run in session.runs or []:
        if not isinstance(run, (RunOutput, TeamRunOutput)):
            continue
        if _scope_for_run(run) != scope:
            continue
        metadata = run.metadata
        if not isinstance(metadata, dict):
            continue
        raw_seen_ids = metadata.get("matrix_seen_event_ids")
        if isinstance(raw_seen_ids, list):
            seen_event_ids.update(event_id for event_id in raw_seen_ids if isinstance(event_id, str) and event_id)
        response_event_id = metadata.get("matrix_response_event_id")
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


def _parse_state(raw_state: dict[str, Any]) -> HistoryScopeState:
    compacted_at = raw_state.get("last_compacted_at")
    summary_model = raw_state.get("last_summary_model")
    compacted_run_count = raw_state.get("last_compacted_run_count")
    force_flag = raw_state.get("force_compact_before_next_run")
    return HistoryScopeState(
        last_compacted_at=compacted_at if isinstance(compacted_at, str) else None,
        last_summary_model=summary_model if isinstance(summary_model, str) else None,
        last_compacted_run_count=compacted_run_count if isinstance(compacted_run_count, int) else None,
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
    return payload


def _state_is_empty(state: HistoryScopeState) -> bool:
    return (
        state.last_compacted_at is None
        and state.last_summary_model is None
        and state.last_compacted_run_count is None
        and not state.force_compact_before_next_run
    )


def _read_preserved_scope_seen_event_ids(session: AgentSession | TeamSession, scope: HistoryScope) -> set[str]:
    return set(_read_scope_seen_event_states(session).get(scope.key, set()))


def _read_scope_seen_event_states(session: AgentSession | TeamSession) -> dict[str, set[str]]:
    metadata = session.metadata
    if not isinstance(metadata, dict):
        return {}

    raw_value = metadata.get(MINDROOM_MATRIX_HISTORY_METADATA_KEY)
    if not isinstance(raw_value, dict):
        return {}

    if raw_value.get("version") != _MATRIX_HISTORY_METADATA_VERSION:
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
    session_metadata = dict(session.metadata or {})
    serialized_states = {
        scope_key: {"seen_event_ids": sorted(event_ids)} for scope_key, event_ids in sorted(states.items()) if event_ids
    }
    if serialized_states:
        session_metadata[MINDROOM_MATRIX_HISTORY_METADATA_KEY] = {
            "version": _MATRIX_HISTORY_METADATA_VERSION,
            "states": serialized_states,
        }
    else:
        session_metadata.pop(MINDROOM_MATRIX_HISTORY_METADATA_KEY, None)
    session.metadata = session_metadata


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
