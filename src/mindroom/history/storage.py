"""Single owner of durable compaction state.

This module is the only code allowed to read or write the durable
compaction-state locations of a stored Agno session:

- the compaction archive (``history/archive.py``): summary generations and the
  runs each compaction chunk moved out of the live runs table
- ``session.summary``, a cache of the scope's latest generation summary that
  Agno replays
- per-scope control state under ``MINDROOM_COMPACTION_METADATA_KEY`` (force flag)
- per-scope consumed Matrix event ids under ``MINDROOM_MATRIX_HISTORY_METADATA_KEY``
  that no stored run carries (team consumption)
- the pending force-compaction scope keys list inside Agno ``session_state``

Nothing else the archive already knows is stored: the event ids compacted history
represents are derived from the archive when seen ids are read.

It enforces the durable-state half of the compaction invariants
(see ``tests/test_compaction_invariants.py`` and ``tests/test_compaction_fuzz.py``):

1. Compaction loses nothing.
   ``archive_compaction_chunk`` moves one chunk's runs, member runs included,
   into the archive in the same transaction that deletes their live rows.

2. Compacted runs never reappear in replay.
   ``reconcile_compaction_state`` deletes live runs that are archived, for
   example after a stale write resurrected one, before each run uses history.

3. The replayed summary is the latest generation's.
   The archive transaction commits before ``session.summary`` is written;
   ``reconcile_compaction_state`` refreshes the cached summary that an
   interruption or a stale session write left behind.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any, Literal

from agno.db.base import SessionType
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from agno.session.team import TeamSession

from mindroom.agent_storage import (
    replace_runs,
    runs_without,
    save_compaction_usage,
    save_runs,
)
from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_IDS_METADATA_KEY,
    MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY,
    MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY,
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.history import archive
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.history_run_visibility import is_model_history_visible_run
from mindroom.legacy_revision_replay import summary_depends_on_source
from mindroom.logging_config import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.models.response import ModelResponse
    from agno.run.agent import RunOutput


_COMPACTION_METADATA_VERSION = 2
_MATRIX_HISTORY_METADATA_VERSION = 1
_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY = "mindroom_pending_compaction_scope_keys"
logger = get_logger(__name__)


async def record_summary_usage(
    model: Model,
    response: ModelResponse,
    *,
    storage: BaseDb,
    session_id: str,
    requester_id: str | None,
) -> None:
    """Record returned counters even when the summary is rejected or later cancelled."""
    if response.response_usage is None:
        return
    await run_blocking_until_complete(
        partial(
            save_compaction_usage,
            storage,
            session_id=session_id,
            requester_id=requester_id,
            model_provider=model.get_provider(),
            model=model.id,
            metrics=response.response_usage.to_dict(),
        ),
    )


def new_scope_session(*, session_id: str, scope_id: str, is_team: bool) -> AgentSession | TeamSession:
    """Return one empty session row for a scope with no stored session yet."""
    created_at = int(datetime.now(UTC).timestamp())
    if is_team:
        return TeamSession(
            session_id=session_id,
            team_id=scope_id,
            metadata={},
            runs=[],
            created_at=created_at,
            updated_at=created_at,
        )
    return AgentSession(
        session_id=session_id,
        agent_id=scope_id,
        metadata={},
        runs=[],
        created_at=created_at,
        updated_at=created_at,
    )


def read_scope_state(session: AgentSession | TeamSession, scope: HistoryScope) -> HistoryScopeState:
    """Return the scoped compaction state for one session and scope."""
    raw_state = _read_raw_scope_states(session).get(scope.key)
    return _parse_state(raw_state) if raw_state is not None else HistoryScopeState()


def _read_raw_scope_states(session: AgentSession | TeamSession) -> dict[str, dict[str, Any]]:
    """Return every scope's stored compaction-state mapping from session metadata."""
    metadata = session.metadata
    if isinstance(metadata, dict):
        raw_value = metadata.get(MINDROOM_COMPACTION_METADATA_KEY)
        if isinstance(raw_value, dict) and raw_value.get("version") == _COMPACTION_METADATA_VERSION:
            raw_states = raw_value.get("states")
            if isinstance(raw_states, dict):
                return {
                    scope_key: raw_state
                    for scope_key, raw_state in raw_states.items()
                    if isinstance(scope_key, str) and scope_key and isinstance(raw_state, dict)
                }
    return {}


def _write_scope_state(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> None:
    """Persist one scope's compaction control state back into session metadata.

    Other scopes' stored mappings are kept verbatim, so state this release does
    not model survives until that scope's own reconciliation adopts it.
    """
    raw_states = _read_raw_scope_states(session)
    if _state_is_empty(state):
        raw_states.pop(scope.key, None)
    else:
        raw_states[scope.key] = _state_to_metadata(state)

    session_metadata = dict(session.metadata or {})
    if not raw_states:
        session_metadata.pop(MINDROOM_COMPACTION_METADATA_KEY, None)
    else:
        session_metadata[MINDROOM_COMPACTION_METADATA_KEY] = {
            "version": _COMPACTION_METADATA_VERSION,
            "states": raw_states,
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
    _write_scope_state(session, scope, next_state)
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


def read_scope_seen_event_ids(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> set[str]:
    """Return the Matrix event ids one scope's replayed history already represents.

    Live runs carry their own ids, the archive supplies those of compacted history,
    and session metadata adds ids no stored run carries.
    """
    seen_event_ids = _read_preserved_scope_seen_event_ids(session, scope)
    seen_event_ids |= archive.compacted_event_ids(storage, session_id=session.session_id, scope_key=scope.key)
    for run in session.runs or []:
        if not is_model_history_visible_run(run):
            continue
        if _scope_for_run(run) != scope:
            continue
        seen_event_ids.update(_run_seen_event_ids(run))
    return seen_event_ids


def _run_event_ids(run: RunOutput | TeamRunOutput) -> set[str]:
    """Return every Matrix event id one run consumed or answered, as redaction matches them."""
    if not is_model_history_visible_run(run):
        return set()
    event_ids = _run_seen_event_ids(run)
    metadata = run.metadata
    if isinstance(metadata, dict):
        event_id = metadata.get(MATRIX_EVENT_ID_METADATA_KEY)
        if isinstance(event_id, str) and event_id:
            event_ids.add(event_id)
        for key in (MATRIX_SOURCE_EVENT_IDS_METADATA_KEY, MATRIX_TURN_DISCOVERY_EVENT_IDS_METADATA_KEY):
            raw_ids = metadata.get(key)
            if isinstance(raw_ids, list):
                event_ids.update(candidate for candidate in raw_ids if isinstance(candidate, str) and candidate)
    return event_ids


def _run_seen_event_ids(run: RunOutput | TeamRunOutput) -> set[str]:
    """Return Matrix event ids already represented by one run."""
    metadata = run.metadata
    if not isinstance(metadata, dict):
        return set()
    seen_event_ids: set[str] = set()
    raw_seen_ids = metadata.get(MATRIX_SEEN_EVENT_IDS_METADATA_KEY)
    if isinstance(raw_seen_ids, list):
        seen_event_ids.update(event_id for event_id in raw_seen_ids if isinstance(event_id, str) and event_id)
    raw_revisions = metadata.get(MATRIX_SOURCE_EVENT_REVISIONS_METADATA_KEY)
    if isinstance(raw_revisions, dict):
        seen_event_ids.update(
            revision[1]
            for revision in raw_revisions.values()
            if isinstance(revision, list | tuple) and len(revision) == 2 and isinstance(revision[1], str)
        )
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
    existing_seen_ids = _read_preserved_scope_seen_event_ids(session, scope)
    updated_seen_ids = existing_seen_ids.union(event_id for event_id in event_ids if event_id)
    if updated_seen_ids == existing_seen_ids:
        return False
    _replace_scope_seen_event_ids(session, scope, updated_seen_ids)
    return True


def remove_redacted_event_from_compaction(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    *,
    event_id: str,
    removed_live_run: bool,
    legacy_source_event_id: str | None = None,
) -> bool:
    """Remove a redacted Matrix event from compacted history, keeping everything before it.

    Callers remove live runs that represent the event first and report whether any
    went. When an archived run represents the event, compaction is undone from that
    run's generation onward: the generation's earlier runs return to the live table,
    the previous generation's summary is replayed again, and the run plus everything
    after it is removed. Archived runs record every event they represent, so an
    event no archived run represents never reached an archive-era summary.
    Afterwards the scope's preserved metadata seen ids are dropped, so messages only
    they covered return as unseen thread context; compacted history's ids stay
    derived from the archive. Returns whether the scope's history changed for this event.
    """
    live_run_ids = [run.run_id for run in session.runs or [] if run.run_id]
    legacy_redaction = _legacy_redaction(
        storage,
        session,
        scope,
        event_id=event_id,
        removed_live_run=removed_live_run,
        legacy_source_event_id=legacy_source_event_id,
    )
    if legacy_redaction == "clear":
        archive.clear_to_legacy(
            storage,
            session_id=session.session_id,
            scope_key=scope.key,
            live_run_ids=live_run_ids,
        )
    elif (
        hit := archive.find_archived_event(
            storage,
            session_id=session.session_id,
            scope_key=scope.key,
            event_id=event_id,
        )
    ) is not None:
        archive.roll_back_to(
            storage,
            session_id=session.session_id,
            scope_key=scope.key,
            hit=hit,
            live_run_ids=live_run_ids,
        )
    elif not removed_live_run:
        return False
    if legacy_redaction == "retire":
        archive.retire_summaries(storage, session_id=session.session_id, scope_key=scope.key)
    target_session = _latest_persisted_session(storage, session)
    _replace_scope_seen_event_ids(target_session, scope, set())
    generation = archive.latest_generation(storage, session_id=session.session_id, scope_key=scope.key)
    if generation is not None:
        _refresh_summary(target_session, generation.summary)
    storage.upsert_session(target_session)
    _adopt_session_fields(session, target_session)
    return True


# LEGACY_COMPAT: Redaction of history compacted before the archive existed.
# Legacy format: A legacy generation (see ``history/legacy_compaction_state.py``) whose summary
# still replays and whose provenance is only the preserved seen ids the migration captured.
# Last legacy release: v2026.9.310; replacement: the next release records exact per-run provenance.
# Handling: That summary cannot be split by run, and every later summary includes it. An event it
# may contain (its captured seen ids or retained source ownership) clears it together with every
# later generation and live run, which may repeat its content. A live run removed for any other
# event only retires the summaries, because provenance from before per-scope seen ids may be
# incomplete; live runs stay, and archived runs stay stored without counting as compacted history.
# This matches how redaction treated such a summary before the archive existed.
# Coverage: tests/test_compaction_redaction.py::test_redacting_legacy_provenance_clears_summary_and_archived_generations,
# tests/test_compaction_redaction.py::test_legacy_provenance_wins_over_a_later_archive_hit,
# tests/test_compaction_redaction.py::test_removing_a_live_run_retires_a_legacy_summary,
# and tests/test_compaction_redaction.py::test_removing_a_live_run_keeps_runs_archived_after_a_legacy_summary.
def _legacy_redaction(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    *,
    event_id: str,
    removed_live_run: bool,
    legacy_source_event_id: str | None,
) -> Literal["clear", "retire"] | None:
    """Return how redacting the event must treat a summary written before the archive existed."""
    if not archive.has_legacy_summary(storage, session_id=session.session_id, scope_key=scope.key):
        return None
    legacy_event_ids = archive.legacy_event_ids(storage, session_id=session.session_id, scope_key=scope.key)
    if event_id in legacy_event_ids or summary_depends_on_source(
        legacy_source_event_id,
        has_summary=True,
        seen_event_ids=legacy_event_ids,
    ):
        return "clear"
    return "retire" if removed_live_run else None


def _parse_state(raw_state: dict[str, Any]) -> HistoryScopeState:
    return HistoryScopeState(force_compact_before_next_run=bool(raw_state.get("force_compact_before_next_run")))


def _state_to_metadata(state: HistoryScopeState) -> dict[str, object]:
    return {"force_compact_before_next_run": state.force_compact_before_next_run}


def _state_is_empty(state: HistoryScopeState) -> bool:
    return not state.force_compact_before_next_run


def reconcile_compaction_state(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> None:
    """Restore the archive invariants before a run uses persisted history (invariants 2 and 3)."""
    live_run_ids = [run.run_id for run in session.runs or [] if run.run_id]
    resurrected = archive.archived_run_ids(storage, session_id=session.session_id, run_ids=live_run_ids)
    if resurrected:
        logger.warning(
            "Removed live runs that compaction already archived",
            session_id=session.session_id,
            scope=scope.key,
            run_count=len(resurrected),
        )
        replace_runs(storage, session, runs_without(session.runs or [], resurrected))
    generation = archive.latest_generation(storage, session_id=session.session_id, scope_key=scope.key)
    if generation is None or _summary_text(session) == generation.summary:
        return
    logger.warning(
        "Restored the replayed summary from the compaction archive",
        session_id=session.session_id,
        scope=scope.key,
    )
    target_session = _latest_persisted_session(storage, session)
    _refresh_summary(target_session, generation.summary)
    # A row with a stale summary was written from a snapshot older than the archive's
    # latest change, so its preserved ids may cover history a redaction removed.
    _replace_scope_seen_event_ids(target_session, scope, set())
    storage.upsert_session(target_session)
    _adopt_session_fields(session, target_session)


def _refresh_summary(session: AgentSession | TeamSession, summary: str | None) -> None:
    """Make the cached ``session.summary`` the archive's latest generation summary."""
    if _summary_text(session) != summary:
        session.summary = SessionSummary(summary=summary, updated_at=datetime.now(UTC)) if summary is not None else None


def _summary_text(session: AgentSession | TeamSession) -> str | None:
    return session.summary.summary if session.summary is not None else None


def _latest_persisted_session(
    storage: BaseDb,
    session: AgentSession | TeamSession,
) -> AgentSession | TeamSession:
    """Return the freshest stored row for one session, or the given session when unavailable."""
    session_type = SessionType.TEAM if isinstance(session, TeamSession) else SessionType.AGENT
    latest_session = storage.get_session(session_id=session.session_id, session_type=session_type)
    return latest_session if isinstance(latest_session, type(session)) else session


def _adopt_session_fields(
    session: AgentSession | TeamSession,
    source: AgentSession | TeamSession,
) -> None:
    """Sync one in-memory session's durable fields from another loaded row."""
    session.metadata = source.metadata
    session.runs = source.runs
    session.summary = source.summary


def update_scope_state_on_latest(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    update: Callable[[HistoryScopeState], HistoryScopeState],
) -> HistoryScopeState:
    """Apply one scope-state update against the freshest stored row and sync the session.

    The update callable sees the latest persisted state, so it can refuse to write
    (return its input unchanged) when the durable row moved since the caller read it.
    """
    target_session = _latest_persisted_session(storage, session)
    latest_state = read_scope_state(target_session, scope)
    next_state = update(latest_state)
    if next_state != latest_state:
        _write_scope_state(target_session, scope, next_state)
        storage.upsert_session(target_session)
    _adopt_session_fields(session, target_session)
    return next_state


def archive_compaction_chunk(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    summary: SessionSummary,
    summary_model: str,
    archived_runs: Sequence[RunOutput | TeamRunOutput],
) -> None:
    """Durably persist one compaction chunk before the next chunk is attempted (invariants 1 and 3).

    ``archived_runs`` is the chunk's complete removed subtree in stored order. The
    archive transaction commits first; the summary then lands on the freshest session
    row. An interruption in between is repaired by the next ``reconcile_compaction_state``.
    """
    archive.archive_runs(
        storage,
        session_id=session.session_id,
        scope_key=scope.key,
        summary=summary.summary,
        summary_model=summary_model,
        runs=archived_runs,
        event_ids={run.run_id: _run_event_ids(run) for run in archived_runs if run.run_id},
    )
    target_session = _latest_persisted_session(storage, session)
    target_session.summary = summary
    storage.upsert_session(target_session)
    _adopt_session_fields(session, target_session)


def save_working_run_changes(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    working_session: AgentSession | TeamSession,
) -> None:
    """Persist edits compaction made to the runs it kept, such as stripped replay fields."""
    save_runs(storage, session, _runs_changed_by_working(session.runs or [], working_session.runs or []))


def _runs_changed_by_working(
    target_runs: list[RunOutput | TeamRunOutput],
    working_runs: list[RunOutput | TeamRunOutput],
) -> list[RunOutput | TeamRunOutput]:
    """Copies of the working-session versions of stored runs whose contents changed."""
    working_by_id = {run.run_id: run for run in working_runs if isinstance(run.run_id, str) and run.run_id}
    changed: list[RunOutput | TeamRunOutput] = []
    for run in target_runs:
        working_run = working_by_id.get(run.run_id) if isinstance(run.run_id, str) else None
        if working_run is not None and working_run.to_dict() != run.to_dict():
            changed.append(deepcopy(working_run))
    return changed


def _read_preserved_scope_seen_event_ids(session: AgentSession | TeamSession, scope: HistoryScope) -> set[str]:
    raw_value = _valid_matrix_history_metadata(session.metadata or {})
    raw_states = raw_value.get("states") if raw_value is not None else None
    raw_state = raw_states.get(scope.key) if isinstance(raw_states, dict) else None
    raw_seen_ids = raw_state.get("seen_event_ids") if isinstance(raw_state, dict) else None
    if not isinstance(raw_seen_ids, list):
        return set()
    return {event_id for event_id in raw_seen_ids if isinstance(event_id, str) and event_id}


def _replace_scope_seen_event_ids(
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    event_ids: set[str],
) -> None:
    """Make ``event_ids`` the scope's preserved seen ids, leaving other scopes untouched."""
    session_metadata = dict(session.metadata or {})
    raw_value = _valid_matrix_history_metadata(session_metadata)
    matrix_history: dict[str, Any] = (
        dict(raw_value) if raw_value is not None else {"version": _MATRIX_HISTORY_METADATA_VERSION}
    )
    raw_states = matrix_history.get("states")
    next_states = dict(raw_states) if isinstance(raw_states, dict) else {}
    if event_ids:
        raw_state = next_states.get(scope.key)
        next_states[scope.key] = {
            **(raw_state if isinstance(raw_state, dict) else {}),
            "seen_event_ids": sorted(event_ids),
        }
    else:
        next_states.pop(scope.key, None)
    if next_states:
        matrix_history["states"] = next_states
        session_metadata[MINDROOM_MATRIX_HISTORY_METADATA_KEY] = matrix_history
    else:
        session_metadata.pop(MINDROOM_MATRIX_HISTORY_METADATA_KEY, None)
    session.metadata = session_metadata


def _valid_matrix_history_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw_value = metadata.get(MINDROOM_MATRIX_HISTORY_METADATA_KEY)
    if not isinstance(raw_value, dict):
        return None
    if raw_value.get("version") != _MATRIX_HISTORY_METADATA_VERSION:
        return None
    return raw_value


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
