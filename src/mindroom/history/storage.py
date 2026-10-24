"""Runtime owner of durable compaction state.

This module owns the durable compaction-state locations of a stored session;
outside it, only the session loader and run-insertion rules in
``session_storage/store.py`` and the open-time migration in
``history/legacy_compaction_state.py`` read or write summary rows directly:

- the summary rows of the conversation log (``history/summary_rows.py``), positioned
  between the runs of the runs table; replay is the scope's latest summary row
  and the runs after it, and ``session.summary`` is derived from that row on read
- per-scope control state under ``MINDROOM_COMPACTION_METADATA_KEY`` (force flag)
- per-scope consumed Matrix event ids under ``MINDROOM_MATRIX_HISTORY_METADATA_KEY``
  that no stored run carries (team consumption)
- the pending force-compaction scope keys list inside the tools' shared ``session_state``

Nothing else the summary rows already know is stored: the event ids compacted
history represents are derived from them when seen ids are read. A metadata change
applies to the metadata the stored row holds, under the write lock, so it never
restores what another writer dropped, and every write here leaves the in-memory
session holding what the store does.

Compaction appends summary rows and never deletes a run (a mid-turn fold moves
only its own stored run after its row); edits and redactions cut the log,
deleting one element and everything after it, which returns the runs a deleted
summary covered to replay (see ``tests/test_compaction_fuzz.py`` for the invariants).
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import TYPE_CHECKING, Any

from mindroom.background_tasks import run_blocking_until_complete
from mindroom.constants import (
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.history import summary_rows
from mindroom.history.run_visibility import is_model_history_visible_run, run_in_scope
from mindroom.history.types import HistoryScope, HistoryScopeState
from mindroom.legacy_revision_replay import summary_depends_on_source
from mindroom.session_storage.agent_storage import save_compaction_usage
from mindroom.session_storage.lanes import run_session_storage_operation
from mindroom.session_storage.types import ConversationSession

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence

    from mindroom.llm.provider import ModelIdentity
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.llm.usage import MessageMetrics
    from mindroom.session_storage.store import ConversationStore


_COMPACTION_METADATA_VERSION = 2
_MATRIX_HISTORY_METADATA_VERSION = 1
_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY = "mindroom_pending_compaction_scope_keys"


async def record_summary_usage(
    model: ModelIdentity,
    usage: MessageMetrics,
    *,
    storage: ConversationStore,
    session_id: str,
    requester_id: str | None,
    initial_session: ConversationSession | None = None,
) -> None:
    """Record a summary response's counters even when the summary is rejected or later cancelled."""
    await run_blocking_until_complete(
        partial(
            save_compaction_usage,
            storage,
            session_id=session_id,
            requester_id=requester_id,
            model=model,
            usage=usage,
            initial_session=initial_session,
        ),
    )


def new_scope_session(*, session_id: str, scope_id: str, is_team: bool) -> ConversationSession:
    """Return one empty session row for a scope with no stored session yet."""
    created_at = int(datetime.now(UTC).timestamp())
    return ConversationSession(
        session_id=session_id,
        kind="team" if is_team else "agent",
        owner_id=scope_id,
        created_at=created_at,
        updated_at=created_at,
    )


def read_scope_state(session: ConversationSession, scope: HistoryScope) -> HistoryScopeState:
    """Return the scoped compaction state for one session and scope."""
    return _scope_state(session.metadata, scope)


def _scope_state(metadata: dict[str, Any] | None, scope: HistoryScope) -> HistoryScopeState:
    raw_state = _raw_scope_states(metadata).get(scope.key, {})
    return HistoryScopeState(force_compact_before_next_run=bool(raw_state.get("force_compact_before_next_run")))


def _raw_scope_states(metadata: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Return every scope's stored compaction-state mapping from session metadata."""
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


def _with_scope_state(
    metadata: dict[str, Any] | None,
    scope: HistoryScope,
    state: HistoryScopeState,
) -> dict[str, Any]:
    """Return session ``metadata`` holding ``state`` as one scope's compaction control state.

    Other scopes' stored mappings are kept verbatim, and ``metadata`` itself is left alone.
    """
    raw_states = _raw_scope_states(metadata)
    if state.force_compact_before_next_run:
        raw_states[scope.key] = {"force_compact_before_next_run": True}
    else:
        raw_states.pop(scope.key, None)

    session_metadata = dict(metadata or {})
    if not raw_states:
        session_metadata.pop(MINDROOM_COMPACTION_METADATA_KEY, None)
    else:
        session_metadata[MINDROOM_COMPACTION_METADATA_KEY] = {
            "version": _COMPACTION_METADATA_VERSION,
            "states": raw_states,
        }
    return session_metadata


def add_pending_force_compaction_scope(
    session_state: dict[str, object] | None,
    scope: HistoryScope,
) -> dict[str, object]:
    """Record a next-run compaction request inside the tools' shared ``session_state``."""
    next_session_state = session_state if session_state is not None else {}
    scope_keys = _pending_scope_keys(next_session_state)
    if scope.key not in scope_keys:
        scope_keys.append(scope.key)
    next_session_state[_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY] = scope_keys
    return next_session_state


def consume_pending_force_compaction_scope(session: ConversationSession, scope: HistoryScope) -> bool:
    """Consume one pending next-run compaction request from the session's shared ``session_state``."""
    if scope.key not in (scope_keys := _pending_scope_keys(session.session_state)):
        return False
    remaining_scope_keys = [scope_key for scope_key in scope_keys if scope_key != scope.key]
    if remaining_scope_keys:
        session.session_state[_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY] = remaining_scope_keys
    else:
        session.session_state.pop(_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY, None)
    return True


def has_queued_force_compaction(storage: ConversationStore, session_id: str, scope: HistoryScope) -> bool:
    """Return whether one scope must compact before its next run.

    Reads only the session row's ``metadata`` and ``session_state``, where both requests live; loading the
    session would also decode every stored run, which takes seconds on a long session.
    """
    fields = storage.read_session_fields(session_id)
    if fields is None:
        return False
    forced = _scope_state(fields.metadata, scope).force_compact_before_next_run
    return forced or scope.key in _pending_scope_keys(fields.session_state)


def _pending_scope_keys(session_state: Mapping[str, object] | None) -> list[str]:
    """Return the scopes with a queued next-run compaction request."""
    raw_scope_keys = session_state.get(_PENDING_COMPACTION_SCOPE_KEYS_SESSION_STATE_KEY) if session_state else None
    if not isinstance(raw_scope_keys, list):
        return []
    return [scope_key for scope_key in raw_scope_keys if isinstance(scope_key, str) and scope_key]


def run_event_ids(run: AnyRunRecord) -> set[str]:
    """Return every Matrix event id one model-visible run consumed or answered, as redaction matches them."""
    if not is_model_history_visible_run(run):
        return set()
    return summary_rows.seen_event_ids(run.metadata) | summary_rows.source_event_ids(run.metadata)


def read_scope_seen_event_ids(
    storage: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
) -> set[str]:
    """Return the Matrix event ids one scope's replayed history already represents.

    Loaded runs carry their own ids, the summary rows supply those of compacted
    history, and session metadata adds ids no stored run carries.
    """
    seen_event_ids = _read_preserved_scope_seen_event_ids(session.metadata, scope)
    seen_event_ids |= summary_rows.history_event_ids(
        storage,
        session_id=session.session_id,
        scope_key=scope.key,
        kind="seen",
    )
    for run in session.runs:
        if is_model_history_visible_run(run) and run_in_scope(run, scope):
            seen_event_ids.update(summary_rows.seen_event_ids(run.metadata))
    return seen_event_ids


def _read_scope_history_event_ids(
    storage: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
) -> set[str]:
    """Return every Matrix event id one scope's stored history derives from.

    These are the ids redaction cleanup matches: what each loaded top-level run consumed,
    answered, or wrote, including a run paused for approval, and what compacted
    history represents.
    """
    event_ids = summary_rows.history_event_ids(storage, session_id=session.session_id, scope_key=scope.key, kind="all")
    for run in session.runs:
        if run_in_scope(run, scope) and run.parent_run_id is None:
            event_ids |= summary_rows.seen_event_ids(run.metadata) | summary_rows.source_event_ids(run.metadata)
    return event_ids


def update_scope_seen_event_ids(
    storage: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
    event_ids: list[str],
) -> None:
    """Merge consumed Matrix event ids into the scope's preserved seen ids on ``session``'s stored row."""
    storage.update_metadata(session, partial(_with_merged_scope_seen_event_ids, scope=scope, event_ids=event_ids))


def remove_runs_answering(storage: ConversationStore, session_id: str, scope: HistoryScope, event_id: str) -> bool:
    """Remove the first element of the scope's history that answered ``event_id`` and everything after it.

    Later model output may build on that reply, so the whole suffix goes; when a
    compacted run answered the event, so does the summary that folded it.
    Returns True if anything was removed.
    """
    return summary_rows.cut_at_event(
        storage,
        session_id=session_id,
        scope_key=scope.key,
        event_id=event_id,
        match="answered",
    )


def _remove_redacted_event_from_history(
    storage: ConversationStore,
    *,
    session: ConversationSession,
    scope: HistoryScope,
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
    session_id = session.session_id
    legacy_event_ids = summary_rows.legacy_event_ids(storage, session_id=session_id, scope_key=scope.key)
    if legacy_event_ids is not None and _legacy_summary_may_contain(event_id, legacy_source_event_id, legacy_event_ids):
        summary_rows.cut_at_legacy_row(storage, session_id=session_id, scope_key=scope.key, session=session)
    elif not summary_rows.cut_at_event(
        storage,
        session_id=session_id,
        scope_key=scope.key,
        event_id=event_id,
        match="any",
        session=session,
    ):
        return False
    elif legacy_event_ids is not None:
        summary_rows.retire(storage, session_id=session_id, scope_key=scope.key, session=session)
    storage.update_metadata(session, partial(_with_scope_seen_event_ids, scope=scope, event_ids=set()))
    return True


async def remove_history_of_redacted_events(
    storage: ConversationStore,
    session: ConversationSession,
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
                session=session,
                scope=scope,
                event_id=event_id,
                legacy_source_event_id=redacted[event_id],
            )
            if await run_session_storage_operation(lambda: storage, removal):
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
# later summary of the scope. A cut for any other event, of loaded or folded runs, retires the summaries,
# because provenance from before per-scope seen ids may be incomplete; runs stay, and folded runs stay
# stored without counting as compacted history. This matches how redaction treated such a summary before.
# Coverage: tests/test_compaction_redaction.py::test_redacting_legacy_provenance_clears_summary_and_folded_runs,
# tests/test_compaction_redaction.py::test_legacy_provenance_wins_over_a_later_folded_hit,
# tests/test_compaction_redaction.py::test_removing_a_loaded_run_retires_a_legacy_summary, and
# tests/test_compaction_redaction.py::test_removing_only_folded_runs_retires_a_legacy_summary.
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


def update_scope_state_on_latest(
    storage: ConversationStore,
    *,
    session: ConversationSession,
    scope: HistoryScope,
    update: Callable[[HistoryScopeState], HistoryScopeState],
) -> HistoryScopeState:
    """Apply one scope-state update to the stored row's metadata, which ``session`` then holds; return the new state.

    The update callable sees the latest stored state, so it can refuse to write (return its input unchanged) when
    the durable row moved since the caller read it.
    """

    def change(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
        latest = _scope_state(metadata, scope)
        next_state = update(latest)
        return metadata if next_state == latest else _with_scope_state(metadata, scope, next_state)

    storage.update_metadata(session, change)
    return read_scope_state(session, scope)


@dataclass(frozen=True)
class _ChunkBasis:
    """What one compaction chunk's summary is built on: the latest summary row and the candidate runs, as read."""

    based_on: int | None
    covered: tuple[summary_rows.CoveredRun, ...]


def read_chunk_basis(
    storage: ConversationStore,
    session: ConversationSession,
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
    storage: ConversationStore,
    *,
    session: ConversationSession,
    scope: HistoryScope,
    basis: _ChunkBasis,
    included_run_ids: Collection[str],
    summary: str,
    summary_model: str,
) -> bool:
    """Durably append one compaction chunk's summary row before the next chunk is attempted.

    The append writes nothing when the scope's latest row, or a run the chunk folded,
    changed since ``basis`` was read. ``session`` then holds the stored result.
    Returns whether the row was appended.
    """
    row_id = summary_rows.append_summary(
        storage,
        session_id=session.session_id,
        scope_key=scope.key,
        based_on=basis.based_on,
        covered=[run for run in basis.covered if run.run_id in included_run_ids],
        summary=summary,
        summary_model=summary_model,
        session=session,
    )
    return row_id is not None


@dataclass(frozen=True)
class FoldBasis:
    """What a mid-turn fold builds on: the scope's latest summary row and the stored runs after it, as read."""

    based_on: int | None
    summary: str | None
    runs: tuple[AnyRunRecord, ...]
    covered: tuple[summary_rows.CoveredRun, ...]


def read_fold_basis(storage: ConversationStore, session: ConversationSession, scope: HistoryScope) -> FoldBasis | None:
    """Read the latest summary row and the visible stored runs after it.

    The stored log, not the running loop's own session, is authoritative for what earlier folds covered, so
    the basis is read from it. A stored copy of the running run is paused or cancelled, so it never counts
    as visible.
    Returns ``None`` when the stored history changed between the reads.
    """
    latest = summary_rows.latest_row(storage, session_id=session.session_id, scope_key=scope.key)
    stored = storage.load_session(session.session_id, session.kind)
    runs = [
        run
        for run in (stored.runs if stored is not None else [])
        if is_model_history_visible_run(run) and run_in_scope(run, scope)
    ]
    run_ids = [run.run_id for run in runs]
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
    storage: ConversationStore,
    *,
    session: ConversationSession,
    scope: HistoryScope,
    basis: FoldBasis,
    summary: str,
    summary_model: str,
    segment: summary_rows.Segment,
) -> bool:
    """Append a mid-turn fold's summary row with its segment, unless its basis went stale or a cut removed its run.

    ``session``, the turn's own, then holds what a fresh load does, so a request rebuilt from it within the turn,
    as a delegation continuation is, replays only what the summary does not cover.
    """
    row_id = summary_rows.append_summary(
        storage,
        session_id=session.session_id,
        scope_key=scope.key,
        based_on=basis.based_on,
        covered=basis.covered,
        summary=summary,
        summary_model=summary_model,
        segment=segment,
        session=session,
    )
    return row_id is not None


def run_folded_tool_call_ids(storage: ConversationStore, *, session_id: str, run_id: str) -> set[str]:
    """Return the tool calls the run's mid-turn folds summarized, from their summary rows."""
    return summary_rows.segment_tool_call_ids(storage, session_id=session_id, run_id=run_id)


def ensure_scope_session_row(storage: ConversationStore, session: ConversationSession, scope: HistoryScope) -> None:
    """Create the session row a first turn's fold writes against; the run's own save creates it otherwise."""
    storage.ensure_session_row(
        new_scope_session(session_id=session.session_id, scope_id=scope.scope_id, is_team=scope.kind == "team"),
    )


def save_working_run_changes(
    storage: ConversationStore,
    *,
    session: ConversationSession,
    loaded_runs: Sequence[AnyRunRecord],
    working_session: ConversationSession,
) -> None:
    """Persist the message edits compaction made to the runs it kept, such as stripped replay fields.

    A run is written only when its working messages differ from ``loaded_runs``, the copies
    compaction started from, and then as the copy ``session`` holds now with those messages,
    so what another writer stored meanwhile, such as a completed approval or a linked reply
    event, stays.
    """
    loaded = {run.run_id: _message_dicts(run) for run in loaded_runs}
    current = {run.run_id: run for run in session.runs}
    edited: list[AnyRunRecord] = []
    for run in working_session.runs:
        stored = current.get(run.run_id)
        if stored is None or run.run_id not in loaded or _message_dicts(run) == loaded[run.run_id]:
            continue
        merged = deepcopy(stored)
        merged.messages = deepcopy(run.messages)
        edited.append(merged)
    storage.save_runs(session, edited)


def _message_dicts(run: AnyRunRecord) -> list[dict[str, Any]]:
    return [message.to_dict() for message in run.messages]


def _read_preserved_scope_seen_event_ids(metadata: dict[str, Any] | None, scope: HistoryScope) -> set[str]:
    raw_value = _valid_matrix_history_metadata(metadata or {})
    raw_states = raw_value.get("states") if raw_value is not None else None
    raw_state = raw_states.get(scope.key) if isinstance(raw_states, dict) else None
    raw_seen_ids = raw_state.get("seen_event_ids") if isinstance(raw_state, dict) else None
    if not isinstance(raw_seen_ids, list):
        return set()
    return {event_id for event_id in raw_seen_ids if isinstance(event_id, str) and event_id}


def _with_merged_scope_seen_event_ids(
    metadata: dict[str, Any] | None,
    *,
    scope: HistoryScope,
    event_ids: list[str],
) -> dict[str, Any] | None:
    """Return ``metadata`` with ``event_ids`` added to the scope's preserved seen ids, as it was when it holds them all."""
    existing = _read_preserved_scope_seen_event_ids(metadata, scope)
    merged = existing.union(event_id for event_id in event_ids if event_id)
    return metadata if merged == existing else _with_scope_seen_event_ids(metadata, scope=scope, event_ids=merged)


def _with_scope_seen_event_ids(
    metadata: dict[str, Any] | None,
    *,
    scope: HistoryScope,
    event_ids: set[str],
) -> dict[str, Any]:
    """Return ``metadata`` with ``event_ids`` as the scope's preserved seen ids, leaving other scopes untouched."""
    session_metadata = dict(metadata or {})
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
    return session_metadata


def _valid_matrix_history_metadata(metadata: dict[str, Any]) -> dict[str, Any] | None:
    raw_value = metadata.get(MINDROOM_MATRIX_HISTORY_METADATA_KEY)
    if not isinstance(raw_value, dict):
        return None
    if raw_value.get("version") != _MATRIX_HISTORY_METADATA_VERSION:
        return None
    return raw_value
