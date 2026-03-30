"""Scoped compaction-state persistence and legacy migration."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary

from mindroom.constants import MINDROOM_COMPACTION_METADATA_KEY
from mindroom.history.types import CompactionState, HistoryScope
from mindroom.logging_config import get_logger

logger = get_logger(__name__)

_COMPACTION_METADATA_VERSION = 2


def read_scope_state(session: AgentSession, scope: HistoryScope) -> CompactionState:
    """Return the scoped compaction state for one session and scope."""
    return read_scope_states(session).get(scope.key, CompactionState())


def read_scope_states(session: AgentSession) -> dict[str, CompactionState]:
    """Return all scoped compaction states parsed from session metadata."""
    metadata = session.metadata
    if not isinstance(metadata, dict):
        return {}

    raw_value = metadata.get(MINDROOM_COMPACTION_METADATA_KEY)
    if not isinstance(raw_value, dict):
        return {}

    version = raw_value.get("version")
    if version == _COMPACTION_METADATA_VERSION:
        raw_states = raw_value.get("states")
        if not isinstance(raw_states, dict):
            return {}
        parsed: dict[str, CompactionState] = {}
        for scope_key, raw_state in raw_states.items():
            if not isinstance(scope_key, str) or not isinstance(raw_state, dict):
                continue
            parsed[scope_key] = _parse_state(raw_state)
        return parsed

    return _migrate_legacy_scope_states(session=session, legacy_metadata=raw_value)


def write_scope_state(
    session: AgentSession,
    scope: HistoryScope,
    state: CompactionState,
) -> None:
    """Persist one scoped compaction state back into session metadata."""
    states = read_scope_states(session)
    if _state_is_empty(state):
        states.pop(scope.key, None)
    else:
        states[scope.key] = state

    session_metadata = dict(session.metadata or {})
    session_metadata[MINDROOM_COMPACTION_METADATA_KEY] = {
        "version": _COMPACTION_METADATA_VERSION,
        "states": {scope_key: _state_to_metadata(scope_state) for scope_key, scope_state in sorted(states.items())},
    }
    session.metadata = session_metadata
    # A single global Agno session summary cannot represent mixed scopes.
    session.summary = None


def _parse_state(raw_state: dict[str, Any]) -> CompactionState:
    summary = raw_state.get("summary")
    last_compacted_run_id = raw_state.get("last_compacted_run_id")
    compacted_at = raw_state.get("compacted_at")
    summary_model = raw_state.get("summary_model")
    force_flag = raw_state.get("force_compact_before_next_run")
    return CompactionState(
        summary=summary if isinstance(summary, str) and summary.strip() else None,
        last_compacted_run_id=last_compacted_run_id if isinstance(last_compacted_run_id, str) else None,
        compacted_at=compacted_at if isinstance(compacted_at, str) else None,
        summary_model=summary_model if isinstance(summary_model, str) else None,
        force_compact_before_next_run=bool(force_flag),
    )


def _state_to_metadata(state: CompactionState) -> dict[str, object]:
    payload: dict[str, object] = {
        "force_compact_before_next_run": state.force_compact_before_next_run,
    }
    if state.summary is not None:
        payload["summary"] = state.summary
    if state.last_compacted_run_id is not None:
        payload["last_compacted_run_id"] = state.last_compacted_run_id
    if state.compacted_at is not None:
        payload["compacted_at"] = state.compacted_at
    if state.summary_model is not None:
        payload["summary_model"] = state.summary_model
    return payload


def _state_is_empty(state: CompactionState) -> bool:
    return (
        state.summary is None
        and state.last_compacted_run_id is None
        and state.compacted_at is None
        and state.summary_model is None
        and not state.force_compact_before_next_run
    )


def _migrate_legacy_scope_states(
    *,
    session: AgentSession,
    legacy_metadata: dict[str, Any],
) -> dict[str, CompactionState]:
    """Best-effort migration from the legacy session-global compaction state."""
    summary = session.summary
    if not isinstance(summary, SessionSummary) or not summary.summary.strip():
        return {}

    last_compacted_run_id = legacy_metadata.get("last_compacted_run_id")
    if not isinstance(last_compacted_run_id, str) or not last_compacted_run_id:
        return {}

    inferred_scope = _infer_legacy_scope(session)
    if inferred_scope is None:
        logger.info(
            "Ignoring legacy mixed-scope compaction state",
            session_id=session.session_id,
        )
        return {}

    compacted_at = legacy_metadata.get("compacted_at")
    summary_model = legacy_metadata.get("summary_model")
    migrated_state = CompactionState(
        summary=summary.summary,
        last_compacted_run_id=last_compacted_run_id,
        compacted_at=compacted_at if isinstance(compacted_at, str) else None,
        summary_model=summary_model if isinstance(summary_model, str) else None,
        force_compact_before_next_run=False,
    )
    return {inferred_scope.key: migrated_state}


def _infer_legacy_scope(session: AgentSession) -> HistoryScope | None:
    completed_runs = _completed_top_level_runs(session)
    shared_scope = _shared_scope(completed_runs)
    if shared_scope is not None:
        return shared_scope
    if isinstance(session.team_id, str) and session.team_id:
        return HistoryScope(kind="team", scope_id=session.team_id)
    if isinstance(session.agent_id, str) and session.agent_id:
        return HistoryScope(kind="agent", scope_id=session.agent_id)
    return None


def _completed_top_level_runs(session: AgentSession) -> list[RunOutput | TeamRunOutput]:
    skip_statuses = {RunStatus.paused, RunStatus.cancelled, RunStatus.error}
    return [
        run
        for run in session.runs or []
        if isinstance(run, (RunOutput, TeamRunOutput)) and run.parent_run_id is None and run.status not in skip_statuses
    ]


def _shared_scope(runs: Sequence[RunOutput | TeamRunOutput]) -> HistoryScope | None:
    if not runs:
        return None
    first_scope = _scope_for_run(runs[0])
    if first_scope is None:
        return None
    if any(_scope_for_run(run) != first_scope for run in runs[1:]):
        return None
    return first_scope


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
