"""``StoreBackend``: the action vocabulary through the conversation store and the helpers production calls.

Each primitive calls what production calls at that point, or, where production binds the call to a bot or a lane,
the same store methods in the same order. A production helper that returns nothing of what it stored runs on
``_Writes``, which keeps what each run write returned, so the backend reports and checks it. An older database is
written as an older release left it and opened through the conversion, as the runtime opens it.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, cast, override

from mindroom.ai_runtime import _finalize_queued_notice_in_runs, _recover_prior_queued_notices
from mindroom.constants import (
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.delegation.execution import persist_delegation_state
from mindroom.history.interrupted_replay import persist_interrupted_replay_snapshot
from mindroom.history.storage import (
    append_compaction_chunk,
    append_fold,
    first_scope_session,
    read_chunk_basis,
    read_fold_basis,
    remove_history_of_redacted_events,
    remove_runs_answering,
    set_force_compaction,
)
from mindroom.history.summary_rows import Fold, latest_row
from mindroom.llm.runs import RunStatus
from mindroom.session_storage.agent_storage import create_state_storage, save_compaction_usage
from mindroom.session_storage.codec import run_kind
from mindroom.session_storage.legacy_agno3_schema import AgnoTableNames, agno_runs_sql
from mindroom.session_storage.schema import quote_identifier, usage_table_sql
from mindroom.session_storage.store import connect_conversation_database
from mindroom.session_storage.types import session_state_snapshot
from tests.conftest import seed_session_state
from tests.storage_oracle.backend import (
    REQUESTER_ID,
    SESSION_ID,
    SUMMARY_MODEL,
    SUMMARY_USAGE,
    Driver,
    FoldBasisView,
)
from tests.storage_oracle.observe import observe

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Mapping, Sequence
    from pathlib import Path

    from mindroom.delegation.state import DelegationState
    from mindroom.history.interrupted_replay import InterruptedReplaySnapshot
    from mindroom.history.storage import FoldBasis, _ChunkBasis
    from mindroom.history.types import HistoryScope
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore, SaveOutcome
    from mindroom.session_storage.types import ConversationSession
    from tests.storage_oracle.actions import Action, ScenarioKind
    from tests.storage_oracle.backend import FoldInput
    from tests.storage_oracle.determinism import Determinism

# The compaction archive every release from #2351 until the summary rows created beside the sessions table.
_ARCHIVE_DDL = (
    "CREATE TABLE {s}_compactions (id INTEGER PRIMARY KEY, "
    "session_id TEXT NOT NULL REFERENCES {s}(session_id) ON DELETE CASCADE, "
    "scope_key TEXT NOT NULL, summary TEXT, summary_model TEXT, "
    "legacy INTEGER NOT NULL DEFAULT 0, legacy_event_ids TEXT, created_at INTEGER NOT NULL)",
    "CREATE INDEX {s}_compactions_scope ON {s}_compactions (session_id, scope_key)",
    "CREATE TABLE {s}_compacted_runs (id INTEGER PRIMARY KEY, "
    "compaction_id INTEGER NOT NULL REFERENCES {s}_compactions(id) ON DELETE CASCADE, "
    "session_id TEXT NOT NULL, run_id TEXT NOT NULL, run_type TEXT, event_ids TEXT NOT NULL, "
    "seen_event_ids TEXT NOT NULL DEFAULT '[]', run_data TEXT, UNIQUE(session_id, run_id))",
    "CREATE INDEX {s}_compacted_runs_generation ON {s}_compacted_runs (compaction_id)",
)


@dataclass
class _Writes:
    """A store that keeps each run write's record and result, for a production helper that returns neither.

    Every other attribute is the store's own.
    """

    store: ConversationStore
    results: list[tuple[AnyRunRecord | None, SaveOutcome | bool]] = field(default_factory=list)

    def __getattr__(self, name: str) -> object:
        return getattr(self.store, name)

    def save_run(
        self,
        session: ConversationSession,
        run: AnyRunRecord,
        *,
        record_usage: bool,
        state: dict[str, Any] | None = None,
    ) -> SaveOutcome:
        outcome = self.store.save_run(session, run, record_usage=record_usage, state=state)
        self.results.append((run, outcome))
        return outcome

    def save_interrupted_record(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        *,
        state: dict[str, Any] | None = None,
    ) -> Literal["written", "removed"]:
        outcome = self.store.save_interrupted_record(session, record, state=state)
        self.results.append((record, outcome))
        return outcome

    def patch_run_metadata(
        self,
        session_id: str,
        run_id: str,
        changes: Mapping[str, Any],
        *,
        session: ConversationSession | None = None,
    ) -> bool:
        patched = self.store.patch_run_metadata(session_id, run_id, changes, session=session)
        self.results.append((None, patched))
        return patched


def edit_cut(store: ConversationStore, scope: HistoryScope, event_id: str) -> bool:
    """Remove the history an edited event's answer started, as an edit's cleanup does."""
    return remove_runs_answering(store, SESSION_ID, scope, event_id)


def redaction_cut(store: ConversationStore, redacted: set[str]) -> list[str]:
    """Run a reply's redaction cleanup over the conversation, which sees every event in ``redacted`` as redacted."""
    session = store.load_session(SESSION_ID)
    if session is None:
        return []

    async def redacted_history_events(event_ids: tuple[str, ...]) -> dict[str, str | None]:
        return {event_id: None for event_id in event_ids if event_id in redacted}

    return asyncio.run(remove_history_of_redacted_events(store, session, redacted_history_events))


def _rewritten(changed: Sequence[AnyRunRecord]) -> object:
    """Report a message rewrite as the Agno-layout store reported its saves: one written run per run it changed."""
    return ["written"] * len(changed) if changed else "unchanged"


def replay_view(messages: Sequence[dict[str, Any]], status: str, folded: Collection[str]) -> list[dict[str, Any]]:
    """Return the replay view rule 5 of spec v4 3.6 stores for a run saved with ``messages`` and ``status``.

    History copies are never stored, system messages only while the run is paused, and what a fold took never
    replays.
    """
    return [
        message
        for message in messages
        if not message.get("from_history")
        and (status == RunStatus.paused.value or message["role"] != "system")
        and message["id"] not in folded
    ]


class StoreBackend(Driver):
    """The oracle's backend for the conversation store.

    It keeps what each run's writes stored, so a test can check what the store shows against it: the last message
    list a full save or a message rewrite stored, with the status rule 5 applied (``saved``), the messages each run's
    folds took (``folded``), and each fold row's input (``folds``).
    """

    store: ConversationStore

    def __init__(self, root: Path, kind: ScenarioKind, determinism: Determinism) -> None:
        self.saved: dict[str, tuple[list[dict[str, Any]], str]] = {}
        self.folded: dict[str, set[str]] = {}
        self.folds: dict[int, FoldInput] = {}
        super().__init__(root, kind, determinism)

    def expected_view(self, run_id: str) -> list[dict[str, Any]] | None:
        """Return the replay view run ``run_id``'s writes leave, or None when this backend wrote none of it."""
        if run_id not in self.saved:
            return None
        messages, status = self.saved[run_id]
        return replay_view(messages, status, self.folded.get(run_id, set()))

    def stored_runs(self) -> dict[str, tuple[str | None, str, int]]:
        """Return every stored run's team run, status, and ``seq``."""
        with closing(connect_conversation_database(self.store.path)) as connection:
            rows = connection.execute(
                f"SELECT run_id, parent_run_id, status, seq FROM {quote_identifier(self.names.log_runs)}",  # noqa: S608
            ).fetchall()
        return {run_id: (parent, status, seq) for run_id, parent, status, seq in rows}

    @override
    def apply(self, action: Action) -> object:
        outcome = super().apply(action)
        stored = self.stored_runs()
        for held in (self.saved, self.folded):
            for run_id in [run_id for run_id in held if run_id not in stored]:
                del held[run_id]
        return outcome

    @override
    def open_store(self) -> None:
        self.store = create_state_storage(self.owner, self.root, self.scope)

    @override
    def load(self) -> ConversationSession | None:
        return self.store.load_session(SESSION_ID)

    def _written(self, record: AnyRunRecord, status: str | None = None) -> None:
        self.saved[record.run_id] = ([message.to_dict() for message in record.messages], status or record.status.value)

    def _save(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        *,
        usage: bool,
        state: dict[str, object] | None,
    ) -> list[str]:
        outcome = self.store.save_run(session, deepcopy(record), record_usage=usage, state=state)
        if outcome == "written":
            self._written(record)
        return [outcome]

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
        # No v2 writer stores a state alone; a late run save carrying it writes it the same way.
        seed_session_state(self.store, session, state)
        return "state"

    @override
    def link_response(self, run_id: str, response_event_id: str) -> object:
        # As ``ConversationStateWriter.apersist_response_event_id_in_session_run`` does in its lane.
        patched = self.store.patch_run_metadata(
            SESSION_ID,
            run_id,
            {MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: response_event_id},
        )
        return ["written"] if patched else "not-stored"

    def _rewrite(self, edit: Callable[[list[AnyRunRecord]], list[AnyRunRecord]]) -> list[AnyRunRecord]:
        """Rewrite the loaded runs' messages with ``edit``; return the runs the edit changed."""
        changed: list[AnyRunRecord] = []
        statuses: dict[str, str] = {}

        def recorded(runs: list[AnyRunRecord]) -> list[AnyRunRecord]:
            statuses.update((run.run_id, run.status.value) for run in runs)
            changed.extend(edit(runs))
            return changed

        self.store.rewrite_run_messages(SESSION_ID, recorded)
        for run in changed:
            # Only the loaded runs store, each under its stored status.
            if run.run_id in statuses:
                self._written(run, statuses[run.run_id])
        return changed

    @override
    def finalize_notice(self, response_turn_id: str) -> object:
        # As ``_finalize_queued_notice_in_new_session_storage`` does.
        if self.store.load_session(SESSION_ID) is None:
            return "no-session"
        return _rewritten(self._rewrite(partial(_finalize_queued_notice_in_runs, response_turn_id=response_turn_id)))

    @override
    def scrub_notices(self, session: ConversationSession) -> object:
        # As ``scrub_queued_notice_session_context`` does: the in-memory check first, and a rewrite only when it finds
        # a crash-left notice. The check's copies are dropped, so the rewrite gets their ids again.
        recover = partial(_recover_prior_queued_notices, active_response_turn_id=None)
        with self.determinism.ids_repeated():
            found = recover(session.runs)
        return _rewritten(self._rewrite(recover)) if found else "unchanged"

    @override
    def persist_delegation(self, session: ConversationSession, record: AnyRunRecord, state: DelegationState) -> object:
        self._persist_delegation(session, record, state)
        return None

    def _persist_delegation(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        state: DelegationState,
    ) -> SaveOutcome | bool:
        """Run ``persist_delegation_state``; return its last write's result: a full save's outcome or whether P2 hit."""
        writes = _Writes(self.store)
        asyncio.run(persist_delegation_state(cast("ConversationStore", writes), session, record, state))
        saved, result = writes.results[-1]
        if result == "written" and saved is not None:
            self._written(saved)
        return result

    @override
    def save_interrupted(
        self,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
        state: dict[str, object] | None,
    ) -> object:
        self._save_interrupted(session, run_id, snapshot, state)
        return None

    def _save_interrupted(
        self,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
        state: dict[str, object] | None,
    ) -> tuple[AnyRunRecord, SaveOutcome | bool]:
        """Run ``persist_interrupted_replay_snapshot``; return the record it saved and the save's outcome."""
        writes = _Writes(self.store)
        persist_interrupted_replay_snapshot(
            storage=cast("ConversationStore", writes),
            session=session,
            run_id=run_id,
            snapshot=snapshot,
            state=state,
        )
        [(record, outcome)] = writes.results
        assert record is not None
        if outcome == "written":
            self._written(record)
        return record, outcome

    @override
    def discard_run(self, session: ConversationSession, run_id: str) -> object:
        return sorted(self.store.delete_runs([run_id], session_id=session.session_id, session=session))

    @override
    def chunk_basis(self, session: ConversationSession, run_ids: Sequence[str], previous_summary: str | None) -> object:
        return read_chunk_basis(self.store, session, self.scope, run_ids, previous_summary=previous_summary)

    @override
    def append_chunk(self, session: ConversationSession, basis: object, run_ids: Sequence[str], summary: str) -> bool:
        return append_compaction_chunk(
            self.store,
            session=session,
            scope=self.scope,
            basis=cast("_ChunkBasis", basis),
            included_run_ids=run_ids,
            summary=summary,
        )

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
        # ``scope_usage_recorder`` records without an initial session.
        self._summary_usage(session, None)

    @override
    def fold_basis(self, session: ConversationSession) -> FoldBasisView | None:
        basis = read_fold_basis(self.store, session, self.scope)
        if basis is None:
            return None
        return FoldBasisView(handle=basis, summary=basis.summary, run_ids=tuple(run.run_id for run in basis.runs))

    @override
    def fold_summary_usage(self, session: ConversationSession) -> None:
        # The fold's usage recorder creates the session row a first turn's summary usage needs (spec 3.4).
        self._summary_usage(session, first_scope_session(self.scope, session.session_id))

    @override
    def append_fold(self, session: ConversationSession, basis: FoldBasisView, summary: str, fold: FoldInput) -> bool:
        run_id = fold.record.run_id
        had_row = run_id in self.stored_runs()
        appended = append_fold(
            self.store,
            session=session,
            scope=self.scope,
            basis=cast("FoldBasis", basis.handle),
            summary=summary,
            fold=Fold(
                run=fold.record,
                messages=fold.messages,
                member_run_ids=frozenset(member.run_id for member in fold.members),
            ),
        )
        if not appended:
            return False
        # The fold's row is its scope's newest, since nothing else writes meanwhile.
        row = latest_row(self.store, session_id=session.session_id, scope_key=self.scope.key)
        assert row is not None
        self.folds[row.id] = fold
        self.folded.setdefault(run_id, set()).update(message.id for message in fold.messages)
        if not had_row:
            # The fold stored the running run as a cancelled row holding only what it took.
            self.saved[run_id] = ([message.to_dict() for message in fold.messages], RunStatus.cancelled.value)
        return True

    @override
    def cut_edit(self, event_id: str) -> object:
        return edit_cut(self.store, self.scope, event_id)

    @override
    def redaction_cleanup(self, redacted: set[str]) -> object:
        return redaction_cut(self.store, redacted)

    @override
    def set_force(self, session: ConversationSession, *, forced: bool) -> None:
        set_force_compaction(self.store, session=session, scope=self.scope, forced=forced)

    @override
    def request_force(self, session: ConversationSession) -> None:
        # As ``request_compaction_before_next_reply`` does: the flag on the stored row.
        del session
        set_force_compaction(
            self.store,
            session=first_scope_session(self.scope, SESSION_ID),
            scope=self.scope,
            forced=True,
        )

    @override
    def prepare_scope_state(self, session: ConversationSession) -> None:
        # ``_prepare_scope_state_for_run`` reads the flag the session loaded; it writes only when compaction is
        # unavailable, which no recorded sequence configures.
        del session

    @override
    def seed_legacy(self, variant: int) -> None:
        # The opened database has no session row yet, so the older one replaces it.
        names = AgnoTableNames(self.owner)
        self.database.unlink()
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for statement in (*agno_runs_sql(names), usage_table_sql(names)):
                connection.execute(statement)
            if variant == 0:
                self._seed_pre_archive(connection)
            else:
                self._seed_archive(connection, retired=variant == 1)
        self.open_store()

    def _insert_session(
        self,
        connection: sqlite3.Connection,
        metadata: dict[str, object] | None,
        summary: str | None,
    ) -> None:
        owner_column = "team_id" if self.kind == "team" else "agent_id"
        connection.execute(
            f'INSERT INTO "{self.names.sessions}" (session_id, session_type, {owner_column}, session_data, metadata, '  # noqa: S608
            "summary, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                SESSION_ID,
                self.kind,
                self.owner,
                json.dumps({"session_state": {}}),
                json.dumps(metadata),
                None if summary is None else json.dumps({"summary": summary}),
                self.determinism.now,
                self.determinism.now,
            ),
        )

    def _seed_pre_archive(self, connection: sqlite3.Connection) -> None:
        """A conversation the destructive compactor left: a replayed summary, tombstones, and preserved seen ids."""
        legacy_runs = ["legacy-1", "legacy-2"]
        self._insert_session(
            connection,
            {
                MINDROOM_COMPACTION_METADATA_KEY: {
                    "version": 2,
                    "states": {self.scope.key: {"compacted_run_ids": legacy_runs}},
                },
                MINDROOM_MATRIX_HISTORY_METADATA_KEY: {
                    "version": 1,
                    "states": {self.scope.key: {"seen_event_ids": [f"${run_id}" for run_id in legacy_runs]}},
                },
            },
            "legacy summary",
        )

    def _seed_archive(self, connection: sqlite3.Connection, *, retired: bool) -> None:
        """A compaction archive: a legacy generation and an ordinary one, then, when ``retired``, a barrier and another."""
        sessions = self.names.sessions
        generations: list[tuple[str | None, int, list[str], list[str]]] = [
            ("legacy summary", 1, ["$legacy-0"], []),
            ("S:a0,legacy", 0, [], ["a0"]),
        ]
        if retired:
            generations += [(None, 0, [], []), ("S:a1", 0, [], ["a1"])]
        self._insert_session(connection, None, generations[-1][0])
        for statement in _ARCHIVE_DDL:
            connection.execute(statement.format(s=sessions))
        for summary, legacy, legacy_event_ids, archived in generations:
            generation_id = connection.execute(
                f'INSERT INTO "{sessions}_compactions" (session_id, scope_key, summary, summary_model, legacy, '  # noqa: S608
                "legacy_event_ids, created_at) VALUES (?, ?, ?, 'summary-model', ?, ?, ?)",
                (SESSION_ID, self.scope.key, summary, legacy, json.dumps(legacy_event_ids), self.determinism.now),
            ).lastrowid
            for run_id in archived:
                run = self.archived_run(run_id)
                connection.execute(
                    f'INSERT INTO "{sessions}_compacted_runs" (compaction_id, session_id, run_id, run_type, '  # noqa: S608
                    "event_ids, seen_event_ids, run_data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        SESSION_ID,
                        run_id,
                        run_kind(run),
                        json.dumps([f"${run_id}", f"$read-by-{run_id}"]),
                        json.dumps([f"${run_id}"]),
                        json.dumps(run.to_dict()),
                    ),
                )

    @override
    def observe(self) -> dict[str, object]:
        return observe(self.store, SESSION_ID, self.scope, run_ids=self.known_run_ids())
