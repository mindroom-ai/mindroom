"""``NextBackend``: the action vocabulary through the storage v2 store and summary rows.

Each primitive calls what the cutover (PLAN-3095 task 10) makes production call at that point, through
``store_next`` and ``summary_rows_next``. Where a production helper is still bound to the Agno-layout modules, this
module holds a small local equivalent that calls the v2 ones instead (the chunk and fold bases, the redaction loop,
the seen-id and candidate reads, delegation state, and interrupted records); task 10f replaces them with the
cut-over helpers. Helpers that only call methods both stores share (``set_force_compaction``,
``has_queued_force_compaction``, ``save_compaction_usage``) are called as they are.
"""

from __future__ import annotations

import json
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, replace
from functools import partial
from typing import TYPE_CHECKING, override

from mindroom.ai_runtime import _finalize_queued_notice_in_runs, _recover_prior_queued_notices
from mindroom.constants import MATRIX_RESPONSE_EVENT_ID_METADATA_KEY
from mindroom.delegation.state import DELEGATION_STATE_KEY
from mindroom.history.interrupted_replay import _build_interrupted_replay_run, _unfolded
from mindroom.history.run_visibility import replays_in_scope, run_in_scope
from mindroom.history.storage import (
    _legacy_summary_may_contain,
    _read_preserved_scope_seen_event_ids,
    _with_scope_seen_event_ids,
    add_pending_force_compaction_scope,
    consume_pending_force_compaction_scope,
    first_scope_session,
    has_queued_force_compaction,
    set_force_compaction,
)
from mindroom.history.summary_rows_next import (
    CoveredRun,
    Fold,
    append_summary,
    covered_runs,
    cut_at_event,
    cut_at_legacy_row,
    folded_tool_call_ids,
    history_event_ids,
    latest_row,
    legacy_event_ids,
    retire,
    seen_event_ids,
    source_event_ids,
)
from mindroom.llm.runs import RunStatus
from mindroom.session_storage.agent_storage import save_compaction_usage
from mindroom.session_storage.codec import run_columns, run_type
from mindroom.session_storage.schema import TableNames, log_schema_sql
from mindroom.session_storage.store_next import ConversationStore, SaveOutcome, connect_conversation_database
from mindroom.session_storage.types import session_state_snapshot
from mindroom.usage.storage import quote_identifier
from tests.storage_oracle.backend import (
    LEGACY_EVENTS,
    REQUESTER_ID,
    SESSION_ID,
    SUMMARY_MODEL,
    SUMMARY_USAGE,
    Driver,
    FoldBasisView,
)
from tests.storage_oracle.observe import Readers, observe

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import Callable, Collection, Sequence
    from pathlib import Path

    from mindroom.delegation.state import DelegationState
    from mindroom.history.interrupted_replay import InterruptedReplaySnapshot
    from mindroom.history.types import HistoryScope
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.types import ConversationSession
    from tests.storage_oracle.backend import FoldInput


@dataclass(frozen=True)
class _Basis:
    """What a chunk or fold builds on: the scope's latest summary row and the stored runs it covers, as read."""

    based_on: int | None
    covered: tuple[CoveredRun, ...]


def _latest_text(
    store: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
) -> tuple[int | None, str | None]:
    latest = latest_row(store, session_id=session.session_id, scope_key=scope.key)
    if latest is None:
        return None, None
    return latest.id, (latest.summary or "").strip() or None


def read_chunk_basis(
    store: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
    run_ids: Sequence[str],
    *,
    previous_summary: str | None,
) -> _Basis | None:
    """``history.storage.read_chunk_basis`` on the v2 summary rows."""
    based_on, latest_summary = _latest_text(store, session, scope)
    covered = tuple(covered_runs(store, session_id=session.session_id, run_ids=run_ids))
    if latest_summary != previous_summary or {run.run_id for run in covered if run.visible} != set(run_ids):
        return None
    return _Basis(based_on=based_on, covered=covered)


def read_fold_basis(
    store: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
) -> FoldBasisView | None:
    """``history.storage.read_fold_basis`` on the v2 summary rows, as the oracle's basis view."""
    based_on, latest_summary = _latest_text(store, session, scope)
    stored = store.load_session(session.session_id, session.kind)
    runs = [run for run in (stored.runs if stored is not None else []) if replays_in_scope(run, scope)]
    run_ids = [run.run_id for run in runs]
    covered = tuple(covered_runs(store, session_id=session.session_id, run_ids=run_ids))
    if {run.run_id for run in covered if run.visible} != set(run_ids):
        return None
    return FoldBasisView(
        handle=_Basis(based_on=based_on, covered=covered),
        summary=latest_summary,
        run_ids=tuple(run_ids),
    )


def read_scope_seen_event_ids(store: ConversationStore, session: ConversationSession, scope: HistoryScope) -> set[str]:
    """``history.storage.read_scope_seen_event_ids`` on the v2 summary rows."""
    seen = _read_preserved_scope_seen_event_ids(session.metadata, scope)
    seen |= history_event_ids(store, session_id=session.session_id, scope_key=scope.key, kind="seen")
    for run in session.runs:
        if replays_in_scope(run, scope):
            seen.update(seen_event_ids(run.metadata))
    return seen


def read_scope_history_event_ids(
    store: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
) -> set[str]:
    """``history.storage._read_scope_history_event_ids`` on the v2 summary rows: the redaction candidates."""
    event_ids = history_event_ids(store, session_id=session.session_id, scope_key=scope.key, kind="all")
    for run in session.runs:
        if run_in_scope(run, scope) and run.parent_run_id is None:
            event_ids |= seen_event_ids(run.metadata) | source_event_ids(run.metadata)
    return event_ids


def remove_redacted_event(
    store: ConversationStore,
    *,
    session: ConversationSession,
    scope: HistoryScope,
    event_id: str,
    legacy_source_event_id: str | None = None,
) -> bool:
    """``history.storage._remove_redacted_event_from_history`` on the v2 summary rows."""
    session_id = session.session_id
    legacy = legacy_event_ids(store, session_id=session_id, scope_key=scope.key)
    if legacy is not None and _legacy_summary_may_contain(event_id, legacy_source_event_id, legacy):
        cut_at_legacy_row(store, session=session, scope_key=scope.key)
    elif not cut_at_event(
        store,
        session_id=session_id,
        scope_key=scope.key,
        event_id=event_id,
        match="any",
        session=session,
    ):
        return False
    elif legacy is not None:
        retire(store, session=session, scope_key=scope.key)
    store.update_metadata(session, partial(_with_scope_seen_event_ids, scope=scope, event_ids=set()))
    return True


def remove_history_of_redacted_events(
    store: ConversationStore,
    session: ConversationSession,
    scope: HistoryScope,
    redacted: set[str],
) -> list[str]:
    """``history.storage.remove_history_of_redacted_events``, synchronously, for events whose source is unknown."""
    removed: list[str] = []
    while True:
        candidates = read_scope_history_event_ids(store, session, scope)
        newly_removed = [
            event_id
            for event_id in sorted(redacted & candidates - set(removed))
            if remove_redacted_event(store, session=session, scope=scope, event_id=event_id)
        ]
        if not newly_removed:
            return removed
        removed.extend(newly_removed)


def edit_cut(store: ConversationStore, scope: HistoryScope, event_id: str) -> bool:
    """Remove the history an edited event's answer started, as ``history.storage.remove_runs_answering`` does."""
    return cut_at_event(store, session_id=SESSION_ID, scope_key=scope.key, event_id=event_id, match="answered")


def redaction_cut(store: ConversationStore, scope: HistoryScope, redacted: set[str]) -> list[str]:
    """Run a reply's redaction cleanup over the conversation, which sees every event in ``redacted`` as redacted."""
    session = store.load_session(SESSION_ID, scope.kind)
    return [] if session is None else remove_history_of_redacted_events(store, session, scope, redacted)


def persist_delegation_state(
    store: ConversationStore,
    session: ConversationSession,
    response: AnyRunRecord,
    state: DelegationState,
) -> SaveOutcome | bool:
    """``delegation.execution.persist_delegation_state`` as spec v4 section 3.7 has it; return what it did.

    A paused record saves whole (P1), since its requirements carry the child answers; any other record, or a paused
    one the store refuses as final, gets only its delegation key (P2), and the result says whether that run is stored.
    """
    response.metadata = {**(response.metadata or {}), DELEGATION_STATE_KEY: state.to_dict()}
    if response.status is RunStatus.paused:
        saved = store.save_run(session, deepcopy(response), record_usage=False)
        if saved != "final":
            return saved
    return store.patch_run_metadata(
        session.session_id,
        response.run_id,
        {DELEGATION_STATE_KEY: state.to_dict()},
        session=session,
    )


def persist_interrupted_replay_snapshot(
    store: ConversationStore,
    *,
    session: ConversationSession,
    run_id: str,
    snapshot: InterruptedReplaySnapshot,
    state: dict[str, object] | None,
) -> tuple[AnyRunRecord, str]:
    """``history.interrupted_replay.persist_interrupted_replay_snapshot`` through P4; return the record and outcome."""
    folded = folded_tool_call_ids(store, session_id=session.session_id, run_id=run_id)
    record = interrupted_record(session, run_id, snapshot, folded)
    return record, store.save_interrupted_record(session, record, state=state)


def interrupted_record(
    session: ConversationSession,
    run_id: str,
    snapshot: InterruptedReplaySnapshot,
    folded: Collection[str],
) -> AnyRunRecord:
    """Return the interrupted record of run ``run_id``, its ``folded`` tool calls left out of the retained context."""
    snapshot = replace(
        snapshot,
        completed_tools=_unfolded(snapshot.completed_tools, folded),
        interrupted_tools=_unfolded(snapshot.interrupted_tools, folded),
    )
    return _build_interrupted_replay_run(snapshot=snapshot, run_id=run_id, session=session)


def _rewritten(changed: Sequence[AnyRunRecord]) -> object:
    """Report a message rewrite as the Agno-layout store reports its saves: one written run per run it changed."""
    return ["written"] * len(changed) if changed else "unchanged"


V2_READERS = Readers(
    seen_event_ids=read_scope_seen_event_ids,
    redaction_candidates=read_scope_history_event_ids,
    folded_tool_call_ids=lambda store, session_id, run_id: folded_tool_call_ids(
        store,
        session_id=session_id,
        run_id=run_id,
    ),
    force_queued=has_queued_force_compaction,
)


def open_v2_store(database: Path) -> ConversationStore:
    """Open ``database`` as storage v2, creating its tables and meta row when missing, as the conversion entry will."""
    database.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect_conversation_database(database)) as connection:
        for statement in log_schema_sql(TableNames.of_database(database)):
            connection.execute(statement)
    return ConversationStore(database)


class NextBackend(Driver):
    """The oracle's backend for the storage v2 store."""

    store: ConversationStore

    @override
    def open_store(self) -> None:
        self.store = open_v2_store(self.database)

    @override
    def load(self) -> ConversationSession | None:
        return self.store.load_session(SESSION_ID, self.kind)

    def _save(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        *,
        usage: bool,
        state: dict[str, object] | None,
    ) -> list[str]:
        return [self.store.save_run(session, deepcopy(record), record_usage=usage, state=state)]

    @override
    def save_terminal(self, session: ConversationSession, record: AnyRunRecord) -> object:
        return self._save(session, record, usage=True, state=session_state_snapshot(session.session_state))

    @override
    def save_checkpoint(self, session: ConversationSession, record: AnyRunRecord) -> object:
        return self._save(session, record, usage=False, state=session_state_snapshot(session.session_state))

    @override
    def save_stale(self, session: ConversationSession, record: AnyRunRecord) -> object:
        return self._save(session, record, usage=False, state=None)

    @override
    def save_session_state(self, session: ConversationSession, state: dict[str, object]) -> object:
        self.store.save_session_state(session, state)
        return "state"

    @override
    def link_response(self, run_id: str, response_event_id: str) -> object:
        # The response link is one metadata patch of the stored run (P2), in the lane.
        patched = self.store.patch_run_metadata(
            SESSION_ID,
            run_id,
            {MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: response_event_id},
        )
        return ["written"] if patched else "not-stored"

    def _rewrite(self, edit: Callable[[list[AnyRunRecord]], list[AnyRunRecord]]) -> list[AnyRunRecord]:
        """Rewrite the loaded runs' messages with ``edit`` (P3); return the runs the edit changed."""
        changed: list[AnyRunRecord] = []

        def recorded(runs: list[AnyRunRecord]) -> list[AnyRunRecord]:
            changed.extend(edit(runs))
            return changed

        self.store.rewrite_run_messages(SESSION_ID, self.kind, recorded)
        return changed

    @override
    def finalize_notice(self, response_turn_id: str) -> object:
        if not self.store.has_session(SESSION_ID, self.kind):
            return "no-session"
        return _rewritten(self._rewrite(partial(_finalize_queued_notice_in_runs, response_turn_id=response_turn_id)))

    @override
    def scrub_notices(self, session: ConversationSession) -> object:
        # The in-memory check first; P3 only when it finds a crash-left notice. The check's copies are dropped, so the
        # rewrite gets their ids again, as the Agno-layout store's one edit got them.
        with self.determinism.ids_repeated():
            found = _recover_prior_queued_notices(session, active_response_turn_id=None)
        if not found:
            return "unchanged"
        return _rewritten(
            self._rewrite(
                lambda runs: _recover_prior_queued_notices(replace(session, runs=runs), active_response_turn_id=None),
            ),
        )

    @override
    def persist_delegation(self, session: ConversationSession, record: AnyRunRecord, state: DelegationState) -> object:
        persist_delegation_state(self.store, session, record, state)
        return None

    @override
    def save_interrupted(
        self,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
        state: dict[str, object] | None,
    ) -> object:
        persist_interrupted_replay_snapshot(self.store, session=session, run_id=run_id, snapshot=snapshot, state=state)
        return None

    @override
    def discard_run(self, session: ConversationSession, run_id: str) -> object:
        return sorted(self.store.delete_runs([run_id], session_id=session.session_id, session=session))

    @override
    def chunk_basis(self, session: ConversationSession, run_ids: Sequence[str], previous_summary: str | None) -> object:
        return read_chunk_basis(self.store, session, self.scope, run_ids, previous_summary=previous_summary)

    @override
    def append_chunk(self, session: ConversationSession, basis: object, run_ids: Sequence[str], summary: str) -> bool:
        assert isinstance(basis, _Basis)
        row_id = append_summary(
            self.store,
            session_id=session.session_id,
            scope_key=self.scope.key,
            based_on=basis.based_on,
            covered=[run for run in basis.covered if run.run_id in run_ids],
            summary=summary,
            session=session,
        )
        return row_id is not None

    def _summary_usage(self, session: ConversationSession, initial_session: ConversationSession | None) -> None:
        save_compaction_usage(
            self.store,
            session_id=session.session_id,
            requester_id=REQUESTER_ID,
            model=SUMMARY_MODEL,
            usage=SUMMARY_USAGE,
            initial_session=initial_session,
        )

    @override
    def chunk_summary_usage(self, session: ConversationSession) -> None:
        self._summary_usage(session, None)

    @override
    def fold_basis(self, session: ConversationSession) -> FoldBasisView | None:
        return read_fold_basis(self.store, session, self.scope)

    @override
    def fold_summary_usage(self, session: ConversationSession) -> None:
        # The fold's usage recorder creates the session row a first turn's summary usage needs (spec 3.4).
        self._summary_usage(session, first_scope_session(self.scope, session.session_id))

    @override
    def append_fold(self, session: ConversationSession, basis: FoldBasisView, summary: str, fold: FoldInput) -> bool:
        handle = basis.handle
        assert isinstance(handle, _Basis)
        row_id = append_summary(
            self.store,
            session_id=session.session_id,
            scope_key=self.scope.key,
            based_on=handle.based_on,
            covered=handle.covered,
            summary=summary,
            fold=Fold(
                run=fold.record,
                messages=fold.messages,
                member_run_ids=frozenset(member.run_id for member in fold.members),
            ),
            session=session,
        )
        return row_id is not None

    @override
    def cut_edit(self, event_id: str) -> object:
        return edit_cut(self.store, self.scope, event_id)

    @override
    def redaction_cleanup(self, redacted: set[str]) -> object:
        return redaction_cut(self.store, self.scope, redacted)

    @override
    def set_force(self, session: ConversationSession, *, forced: bool) -> None:
        set_force_compaction(self.store, session=session, scope=self.scope, forced=forced)

    @override
    def request_force(self, session: ConversationSession) -> None:
        set_force_compaction(
            self.store,
            session=first_scope_session(self.scope, SESSION_ID),
            scope=self.scope,
            forced=True,
        )
        add_pending_force_compaction_scope(session.session_state, self.scope)

    @override
    def prepare_scope_state(self, session: ConversationSession) -> None:
        if consume_pending_force_compaction_scope(session, self.scope):
            set_force_compaction(self.store, session=session, scope=self.scope, forced=True)
            self.store.save_session_state(session, session_state_snapshot(session.session_state))

    @override
    def seed_legacy(self, variant: int) -> None:
        """Write what the conversion makes of ``Driver.seed_legacy``'s older databases, then reopen it.

        The conversion (task 9) adopts the pre-archive summary as a legacy row at ``through = 0`` holding its
        preserved seen ids, and drops the adopted metadata keys; it turns each archive generation into a row after
        its restored runs, a legacy generation at 0 and a barrier at the previous row's ``through``.
        """
        with closing(connect_conversation_database(self.database)) as connection, connection:
            if variant == 0:
                self._insert_session(connection, {})
                self._insert_row(
                    connection,
                    through=0,
                    summary="legacy summary",
                    legacy=True,
                    event_ids=LEGACY_EVENTS[0][:2],
                    seen=LEGACY_EVENTS[0][:2],
                )
            else:
                self._insert_session(connection, None)
                self._insert_row(
                    connection,
                    through=0,
                    summary="legacy summary",
                    legacy=True,
                    event_ids=("$legacy-0",),
                    seen=("$legacy-0",),
                )
                generations = [("S:a0,legacy", "a0")]
                if variant == 1:
                    generations += [(None, None), ("S:a1", "a1")]
                through = 0
                for summary, run_id in generations:
                    if run_id is not None:
                        through = self._insert_archived_run(connection, run_id)
                    event_ids = () if run_id is None else (f"${run_id}", f"$read-by-{run_id}")
                    seen = () if run_id is None else (f"${run_id}",)
                    self._insert_row(
                        connection,
                        through=through,
                        summary=summary,
                        legacy=False,
                        event_ids=event_ids,
                        seen=seen,
                    )
        self.open_store()

    def _insert_session(self, connection: sqlite3.Connection, metadata: dict[str, object] | None) -> None:
        owner_column = "team_id" if self.kind == "team" else "agent_id"
        connection.execute(
            f'INSERT INTO "{self.names.sessions}" (session_id, session_type, {owner_column}, session_data, metadata, '  # noqa: S608
            "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                SESSION_ID,
                self.kind,
                self.owner,
                json.dumps({"session_state": {}}),
                json.dumps(metadata),
                self.determinism.now,
                self.determinism.now,
            ),
        )

    def _insert_row(
        self,
        connection: sqlite3.Connection,
        *,
        through: int,
        summary: str | None,
        legacy: bool,
        event_ids: Sequence[str],
        seen: Sequence[str],
    ) -> None:
        connection.execute(
            f"INSERT INTO {quote_identifier(self.names.log_summaries)} (session_id, scope_key, through, summary, "  # noqa: S608
            "legacy, event_ids, seen_event_ids, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                SESSION_ID,
                self.scope.key,
                through,
                summary,
                int(legacy),
                json.dumps(sorted(event_ids)),
                json.dumps(sorted(seen)),
                self.determinism.now,
            ),
        )

    def _insert_archived_run(self, connection: sqlite3.Connection, run_id: str) -> int:
        """Insert an archived run as the conversion restores it, with the next ``seq``; return that ``seq``."""
        names = self.names
        record = self.archived_run(run_id)
        data = record.to_dict()
        (seq,) = connection.execute(
            f"UPDATE {quote_identifier(names.log_meta)} SET next_seq = next_seq + 1 RETURNING next_seq - 1",  # noqa: S608
        ).fetchone()
        columns = run_columns(data, run_type(record))
        connection.execute(
            f"INSERT INTO {quote_identifier(names.log_runs)} (run_id, session_id, parent_run_id, kind, scope_key, "  # noqa: S608
            "status, seq, created_at, updated_at, metadata, record, requirements, tools) "
            "VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                SESSION_ID,
                columns.kind,
                columns.scope_key,
                columns.status,
                seq,
                record.created_at,
                record.created_at,
                columns.metadata,
                columns.record,
                columns.requirements,
                columns.tools,
            ),
        )
        connection.executemany(
            f"INSERT INTO {quote_identifier(names.log_messages)} (run_id, message_id, ord, data) VALUES (?, ?, ?, ?)",  # noqa: S608
            [
                (run_id, message["id"], position, json.dumps(message))
                for position, message in enumerate(data["messages"])
            ],
        )
        return seq

    @override
    def observe(self) -> dict[str, object]:
        return observe(self.store, SESSION_ID, self.scope, readers=V2_READERS, run_ids=self.known_run_ids())
