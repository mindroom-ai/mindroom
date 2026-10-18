"""Runtime owner of durable compaction state.

This module owns the durable compaction-state locations of a stored Agno session;
outside it, only the session loader and run-insertion rules in
``session_storage/agno_compat_sqlite.py`` and the open-time migration in
``history/legacy_compaction_state.py`` read or write summary rows directly:

- the summary rows of the conversation log (``history/summary_rows.py``), positioned
  between the runs of Agno's runs table; replay is the scope's latest summary row
  and the runs after it, and ``session.summary`` is derived from that row on read
- per-scope control state under ``MINDROOM_COMPACTION_METADATA_KEY`` (force flag)
- per-scope consumed Matrix event ids under ``MINDROOM_MATRIX_HISTORY_METADATA_KEY``
  that no stored run carries (team consumption)
- the pending force-compaction scope keys list inside Agno ``session_state``

Nothing else the summary rows already know is stored: the event ids compacted
history represents are derived from them when seen ids are read. Metadata seen ids
have no summary-row counterpart, so a stale whole-row write can restore ids a
redaction dropped; that only withholds those messages from unseen thread context.

Compaction appends summary rows and never deletes a run (a mid-turn fold moves
only its own stored run after its row); edits and redactions cut the log,
deleting one element and everything after it, which returns the runs a deleted
summary covered to replay (see ``tests/test_compaction_fuzz.py`` for the invariants).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from agno.db.base import SessionType
from agno.db.sqlite import SqliteDb
from agno.db.utils import deserialize_session_json_fields
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.session.agent import AgentSession
from agno.session.team import TeamSession
from sqlalchemy import JSON, column, inspect, select, table

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import (
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.history import summary_rows
from mindroom.history.run_visibility import is_model_history_visible_run
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.legacy_revision_replay import summary_depends_on_source
from mindroom.session_storage.agent_storage import (
    ensure_session_row,
    save_compaction_usage,
    save_runs,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence

    from agno.db.base import BaseDb
    from agno.models.base import Model
    from agno.models.response import ModelResponse
    from agno.session.summary import SessionSummary


_COMPACTION_METADATA_VERSION = 2
_MATRIX_HISTORY_METADATA_VERSION = 1
_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY = "mindroom_pending_compaction_scope_keys"


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

    Other scopes' stored mappings are kept verbatim.
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


def has_queued_force_compaction(storage: BaseDb, session_id: str, scope: HistoryScope) -> bool:
    """Return whether one scope must compact before its next run.

    Reads only the session row's ``metadata`` and ``session_data``, where both
    requests live. ``get_session`` would also decode every stored run, which
    takes seconds on a long session.
    """
    if not isinstance(storage, SqliteDb):
        msg = "Reading queued compaction requires SQLite session storage"
        raise TypeError(msg)
    sessions = table(
        storage.session_table_name,
        column("session_id"),
        column("metadata", JSON),
        column("session_data", JSON),
    )
    with storage.db_engine.connect() as connection:
        if not inspect(connection).has_table(storage.session_table_name):
            return False
        row = connection.execute(
            select(sessions.c.metadata, sessions.c.session_data).where(sessions.c.session_id == session_id),
        ).first()
    if row is None:
        return False
    fields = deserialize_session_json_fields(dict(row._mapping))
    # Holds only the two fields the requests are parsed from; it is never saved.
    session = (TeamSession if scope.kind == "team" else AgentSession)(
        session_id=session_id,
        metadata=fields["metadata"],
        session_data=fields["session_data"],
    )
    return read_scope_state(session, scope).force_compact_before_next_run or _has_pending_force_compaction_scope(
        session,
        scope,
    )


def _has_pending_force_compaction_scope(
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


def run_event_ids(run: RunOutput | TeamRunOutput) -> set[str]:
    """Return every Matrix event id one model-visible run consumed or answered, as redaction matches them."""
    if not is_model_history_visible_run(run):
        return set()
    return summary_rows.seen_event_ids(run.metadata) | summary_rows.source_event_ids(run.metadata)


def read_scope_seen_event_ids(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> set[str]:
    """Return the Matrix event ids one scope's replayed history already represents.

    Loaded runs carry their own ids, the summary rows supply those of compacted
    history, and session metadata adds ids no stored run carries.
    """
    seen_event_ids = _read_preserved_scope_seen_event_ids(session, scope)
    seen_event_ids |= summary_rows.history_event_ids(
        storage,
        session_id=session.session_id,
        scope_key=scope.key,
        kind="seen",
    )
    for run in session.runs or []:
        if not is_model_history_visible_run(run):
            continue
        if _scope_for_run(run) != scope:
            continue
        seen_event_ids.update(summary_rows.seen_event_ids(run.metadata))
    return seen_event_ids


def _read_scope_history_event_ids(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> set[str]:
    """Return every Matrix event id one scope's stored history derives from.

    These are the ids redaction cleanup matches: what each loaded top-level run consumed,
    answered, or wrote, including a run paused for approval, and what compacted
    history represents.
    """
    event_ids = summary_rows.history_event_ids(storage, session_id=session.session_id, scope_key=scope.key, kind="all")
    for run in session.runs or []:
        if isinstance(run, (RunOutput, TeamRunOutput)) and run.parent_run_id is None and _scope_for_run(run) == scope:
            event_ids |= summary_rows.seen_event_ids(run.metadata) | summary_rows.source_event_ids(run.metadata)
    return event_ids


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


def remove_runs_answering(storage: BaseDb, session_id: str, scope: HistoryScope, event_id: str) -> bool:
    """Remove the first element of the scope's history that answered ``event_id`` and everything after it.

    Later model output may build on that reply, so the whole suffix goes; when a
    compacted run answered the event, so does the summary that folded it.
    Returns True if anything was removed.
    """
    return (
        summary_rows.cut_at_event(
            storage,
            session_id=session_id,
            scope_key=scope.key,
            event_id=event_id,
            match="answered",
        )
        is not None
    )


def _remove_redacted_event_from_history(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    *,
    event_id: str,
    legacy_source_event_id: str | None = None,
) -> bool:
    """Remove the part of one scope's history that derives from a redacted Matrix event.

    The first element that answered, read, or wrote the event goes with everything
    after it, and the runs a removed summary covered replay again. Afterwards the
    scope's preserved metadata seen ids are dropped, so messages only they covered
    return as unseen thread context. ``session`` is synced to the stored result.
    Returns whether the scope's history changed.
    """
    legacy_event_ids = summary_rows.legacy_event_ids(storage, session_id=session.session_id, scope_key=scope.key)
    if legacy_event_ids is not None and _legacy_summary_may_contain(event_id, legacy_source_event_id, legacy_event_ids):
        summary_rows.cut_at_legacy_row(storage, session_id=session.session_id, scope_key=scope.key)
    else:
        result = summary_rows.cut_at_event(
            storage,
            session_id=session.session_id,
            scope_key=scope.key,
            event_id=event_id,
            match="any",
        )
        if result is None:
            return False
        if legacy_event_ids is not None and result == "loaded":
            summary_rows.retire(storage, session_id=session.session_id, scope_key=scope.key)
    target_session = _latest_persisted_session(storage, session)
    _replace_scope_seen_event_ids(target_session, scope, set())
    storage.upsert_session(target_session)
    _adopt_session_fields(session, target_session)
    return True


async def remove_history_of_redacted_events(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    redacted_history_events: Callable[[tuple[str, ...]], Awaitable[Mapping[str, str | None]]],
) -> list[str]:
    """Remove the part of one scope's history derived from events since redacted, and return those events.

    ``redacted_history_events`` maps each redacted event to the source a legacy summary
    consumed it through, or None. A cut can bring older runs back to replay that read
    another redacted event, so the check repeats until a pass removes nothing new.
    ``session`` is synced to the stored result.
    """
    removed_event_ids: list[str] = []
    while True:
        event_ids = await run_blocking_until_complete(_read_scope_history_event_ids, storage, session, scope)
        if not event_ids:
            return removed_event_ids
        redacted = await redacted_history_events(tuple(sorted(event_ids)))
        newly_removed: list[str] = []
        for event_id in sorted(set(redacted).difference(removed_event_ids)):
            removal = partial(
                _remove_redacted_event_from_history,
                storage,
                session,
                scope,
                event_id=event_id,
                legacy_source_event_id=redacted[event_id],
            )
            if await run_blocking_until_complete(removal):
                newly_removed.append(event_id)
        if not newly_removed:
            return removed_event_ids
        removed_event_ids.extend(newly_removed)


# LEGACY_COMPAT: Redaction of history compacted before the archive existed.
# Legacy format: A legacy summary row (see ``history/legacy_compaction_state.py``) whose summary
# is still in force and whose provenance is only the seen ids the migration captured.
# Last legacy release: v2026.9.314; replacement: v2026.9.315 recorded exact per-run provenance.
# Handling: That summary cannot be split by run, and every later summary includes it. An event it
# may contain (its captured seen ids or retained source ownership) cuts at it, removing every run and
# later summary of the scope. A loaded run removed for any other event retires the summaries, because
# provenance from before per-scope seen ids may be incomplete; runs stay, and folded runs stay stored
# without counting as compacted history. This matches how redaction treated such a summary before.
# Coverage: tests/test_compaction_redaction.py::test_redacting_legacy_provenance_clears_summary_and_folded_runs,
# tests/test_compaction_redaction.py::test_legacy_provenance_wins_over_a_later_folded_hit,
# tests/test_compaction_redaction.py::test_removing_a_loaded_run_retires_a_legacy_summary, and
# tests/test_compaction_redaction.py::test_no_retirement_when_only_folded_runs_hold_the_event.
def _legacy_summary_may_contain(
    event_id: str,
    legacy_source_event_id: str | None,
    legacy_event_ids: frozenset[str],
) -> bool:
    """Return whether a summary written before the archive existed may contain the redacted event."""
    return event_id in legacy_event_ids or summary_depends_on_source(
        legacy_source_event_id,
        has_summary=True,
        seen_event_ids=set(legacy_event_ids),
    )


def _parse_state(raw_state: dict[str, Any]) -> HistoryScopeState:
    return HistoryScopeState(force_compact_before_next_run=bool(raw_state.get("force_compact_before_next_run")))


def _state_to_metadata(state: HistoryScopeState) -> dict[str, object]:
    return {"force_compact_before_next_run": state.force_compact_before_next_run}


def _state_is_empty(state: HistoryScopeState) -> bool:
    return not state.force_compact_before_next_run


def _latest_persisted_session(
    storage: BaseDb,
    session: AgentSession | TeamSession,
) -> AgentSession | TeamSession:
    """Return the freshest stored row for one session, or the given session when unavailable."""
    latest_session = storage.get_session(session_id=session.session_id, session_type=_session_type(session))
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


@dataclass(frozen=True)
class _ChunkBasis:
    """What one compaction chunk's summary is built on: the latest summary row and the candidate runs, as read."""

    based_on: int | None
    covered: tuple[summary_rows.CoveredRun, ...]


def read_chunk_basis(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    run_ids: Sequence[str],
    *,
    previous_summary: str | None,
) -> _ChunkBasis | None:
    """Read what a chunk folding some of ``run_ids`` after ``previous_summary`` builds on, before its summary call.

    The chunk summarizes the session as it was loaded, so this returns ``None`` when the stored history has
    changed since: the latest row's text is not ``previous_summary``, or a candidate run is gone or hidden.
    """
    latest = summary_rows.latest_row(storage, session_id=session.session_id, scope_key=scope.key)
    covered = tuple(summary_rows.covered_runs(storage, session_id=session.session_id, run_ids=run_ids))
    latest_summary = (latest.summary or "").strip() or None if latest is not None else None
    if latest_summary != previous_summary or {run.run_id for run in covered if run.visible} != set(run_ids):
        return None
    return _ChunkBasis(based_on=latest.id if latest is not None else None, covered=covered)


def append_compaction_chunk(
    *,
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    basis: _ChunkBasis,
    included_run_ids: Collection[str],
    summary: SessionSummary,
    summary_model: str,
) -> bool:
    """Durably append one compaction chunk's summary row before the next chunk is attempted.

    The append writes nothing when the scope's latest row, or a run the chunk folded,
    changed since ``basis`` was read. ``session`` is synced to the stored result.
    Returns whether the row was appended.
    """
    row_id = summary_rows.append_summary(
        storage,
        session_id=session.session_id,
        scope_key=scope.key,
        based_on=basis.based_on,
        covered=[run for run in basis.covered if run.run_id in included_run_ids],
        summary=summary.summary,
        summary_model=summary_model,
    )
    if row_id is None:
        return False
    _adopt_session_fields(session, _latest_persisted_session(storage, session))
    return True


@dataclass(frozen=True)
class FoldBasis:
    """What a mid-turn fold builds on: the scope's latest summary row and the stored runs after it, as read."""

    based_on: int | None
    summary: str | None
    runs: tuple[RunOutput | TeamRunOutput, ...]
    covered: tuple[summary_rows.CoveredRun, ...]


def read_fold_basis(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
) -> FoldBasis | None:
    """Read the latest summary row and the visible stored runs after it.

    The running loop's own session still holds every run it loaded when the turn started, so an earlier
    fold of the same turn would be summarized again; the stored log knows what that fold covered. A stored
    copy of the running run is paused or cancelled, so it never counts as visible.
    Returns ``None`` when the stored history changed between the reads.
    """
    latest = summary_rows.latest_row(storage, session_id=session.session_id, scope_key=scope.key)
    stored = storage.get_session(session_id=session.session_id, session_type=_session_type(session))
    runs = [
        run
        for run in (stored.runs or [] if isinstance(stored, (AgentSession, TeamSession)) else [])
        if is_model_history_visible_run(run) and _scope_for_run(run) == scope
    ]
    run_ids = [run.run_id for run in runs if run.run_id]
    covered = tuple(summary_rows.covered_runs(storage, session_id=session.session_id, run_ids=run_ids))
    if {run.run_id for run in covered if run.visible} != set(run_ids):
        return None
    return FoldBasis(
        based_on=latest.id if latest is not None else None,
        summary=(latest.summary or "").strip() or None if latest is not None else None,
        runs=tuple(runs),
        covered=covered,
    )


def append_fold(
    storage: BaseDb,
    *,
    session: AgentSession | TeamSession,
    scope: HistoryScope,
    basis: FoldBasis,
    summary: str,
    summary_model: str,
    segment: summary_rows.Segment,
) -> bool:
    """Append a mid-turn fold's summary row with its segment, unless its basis went stale or a cut removed its run."""
    return (
        summary_rows.append_summary(
            storage,
            session_id=session.session_id,
            scope_key=scope.key,
            based_on=basis.based_on,
            covered=basis.covered,
            summary=summary,
            summary_model=summary_model,
            segment=segment,
        )
        is not None
    )


def run_folded_tool_call_ids(storage: BaseDb, *, session_id: str, run_id: str) -> set[str]:
    """Return the tool calls the run's mid-turn folds summarized, from their summary rows."""
    # Summary rows live in SQLite session storage only.
    if not isinstance(storage, SqliteDb):
        return set()
    return summary_rows.segment_tool_call_ids(storage, session_id=session_id, run_id=run_id)


def ensure_scope_session_row(storage: BaseDb, session: AgentSession | TeamSession, scope: HistoryScope) -> None:
    """Create the session row a first turn's fold writes against; Agno writes its own when the run ends."""
    ensure_session_row(
        storage,
        new_scope_session(session_id=session.session_id, scope_id=scope.scope_id, is_team=scope.kind == "team"),
    )


def _session_type(session: AgentSession | TeamSession) -> SessionType:
    return SessionType.TEAM if isinstance(session, TeamSession) else SessionType.AGENT


def save_working_run_changes(
    storage: BaseDb,
    session: AgentSession | TeamSession,
    loaded_runs: Sequence[RunOutput | TeamRunOutput],
    working_session: AgentSession | TeamSession,
) -> None:
    """Persist the message edits compaction made to the runs it kept, such as stripped replay fields.

    A run is written only when its working messages differ from ``loaded_runs``, the copies
    compaction started from, and then as the copy ``session`` holds now with those messages,
    so what another writer stored meanwhile, such as a completed approval or a linked reply
    event, stays.
    """
    loaded = {run.run_id: _message_dicts(run) for run in loaded_runs if run.run_id}
    current = {run.run_id: run for run in session.runs or [] if run.run_id}
    edited: list[RunOutput | TeamRunOutput] = []
    for run in working_session.runs or []:
        stored = current.get(run.run_id)
        if stored is None or run.run_id not in loaded or _message_dicts(run) == loaded[run.run_id]:
            continue
        merged = deepcopy(stored)
        merged.messages = deepcopy(run.messages)
        edited.append(merged)
    save_runs(storage, session, edited)


def _message_dicts(run: RunOutput | TeamRunOutput) -> list[dict[str, Any]]:
    return [message.to_dict() for message in run.messages or []]


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
