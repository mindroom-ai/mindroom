"""``OldBackend``: the action vocabulary through today's storage-level APIs on the Agno-layout store.

Each primitive calls what production calls at that point, or, where production binds the call to a bot or a lane,
the same store methods in the same order. Saves report what the store did with each run: ``written``, ``removed``,
or ``frozen``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from typing import TYPE_CHECKING, override

from mindroom.ai_runtime import _finalize_queued_notice_in_runs, _recover_prior_queued_notices
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
    MINDROOM_COMPACTION_METADATA_KEY,
    MINDROOM_MATRIX_HISTORY_METADATA_KEY,
)
from mindroom.delegation.execution import persist_delegation_state
from mindroom.history.interrupted_replay import persist_interrupted_replay_snapshot
from mindroom.history.mid_turn_compaction import without_fold_records
from mindroom.history.storage import (
    add_pending_force_compaction_scope,
    append_compaction_chunk,
    append_fold,
    consume_pending_force_compaction_scope,
    ensure_scope_session_row,
    first_scope_session,
    read_chunk_basis,
    read_fold_basis,
    remove_history_of_redacted_events,
    remove_runs_answering,
    set_force_compaction,
)
from mindroom.history.summary_rows import Segment
from mindroom.llm.messages import Message
from mindroom.llm.provider import ModelIdentity
from mindroom.llm.runs import RunStatus
from mindroom.llm.usage import MessageMetrics, TokenCounts
from mindroom.session_storage.agent_storage import create_state_storage, save_compaction_usage
from mindroom.session_storage.codec import run_type
from mindroom.session_storage.schema import schema_sql
from mindroom.session_storage.store import _WrittenRow
from mindroom.session_storage.types import session_state_snapshot
from tests.storage_oracle.backend import REQUESTER_ID, SESSION_ID, Driver, FoldBasisView
from tests.storage_oracle.observe import AGNO_LAYOUT_READERS, observe

if TYPE_CHECKING:
    from collections.abc import Sequence

    from mindroom.delegation.state import DelegationState
    from mindroom.history.interrupted_replay import InterruptedReplaySnapshot
    from mindroom.history.storage import FoldBasis, _ChunkBasis
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore
    from mindroom.session_storage.types import ConversationSession
    from tests.storage_oracle.backend import FoldInput

SUMMARY_MODEL = ModelIdentity(id="oracle-summary-model", provider="oracle", provider_name="oracle")
_SUMMARY_USAGE = MessageMetrics(tokens=TokenCounts(input_tokens=7, output_tokens=3, total_tokens=10))
# The archive layout every release since #2351 created beside the sessions table.
_ARCHIVE_DDL = (
    "CREATE TABLE {s}_compactions (id INTEGER PRIMARY KEY, "
    "session_id TEXT NOT NULL REFERENCES {s}(session_id) ON DELETE CASCADE, "
    "scope_key TEXT NOT NULL, summary TEXT, summary_model TEXT, "
    "legacy INTEGER NOT NULL DEFAULT 0, legacy_event_ids TEXT, created_at INTEGER NOT NULL)",
    "CREATE TABLE {s}_compacted_runs (id INTEGER PRIMARY KEY, "
    "compaction_id INTEGER NOT NULL REFERENCES {s}_compactions(id) ON DELETE CASCADE, "
    "session_id TEXT NOT NULL, run_id TEXT NOT NULL, run_type TEXT, event_ids TEXT NOT NULL, "
    "seen_event_ids TEXT NOT NULL DEFAULT '[]', run_data TEXT, UNIQUE(session_id, run_id))",
)


class OldBackend(Driver):
    """The oracle's backend for the Agno-layout store, the one recordings are made against."""

    store: ConversationStore

    @override
    def open_store(self) -> None:
        self.store = create_state_storage(self.owner, self.root)

    @override
    def load(self) -> ConversationSession | None:
        return self.store.load_session(SESSION_ID, self.kind)

    def _save(
        self,
        session: ConversationSession,
        records: Sequence[AnyRunRecord],
        *,
        usage: bool,
        state: dict[str, object] | None,
    ) -> list[str]:
        """Save copies of ``records`` as ``save_runs`` and ``persist_to`` do, returning what each save did."""
        saved = self.store._save_encoded(
            session,
            [(deepcopy(record).to_dict(), run_type(record)) for record in records],
            record_usage=usage,
            state=state,
        )
        return ["written" if isinstance(save.row, _WrittenRow) else save.row for save in saved]

    @override
    def save_terminal(self, session: ConversationSession, record: AnyRunRecord) -> object:
        return self._save(session, [record], usage=True, state=session_state_snapshot(session.session_state))

    @override
    def save_checkpoint(self, session: ConversationSession, record: AnyRunRecord) -> object:
        return self._save(session, [record], usage=False, state=session_state_snapshot(session.session_state))

    @override
    def save_stale(self, session: ConversationSession, record: AnyRunRecord) -> object:
        return self._save(session, [record], usage=False, state=None)

    @override
    def save_session_state(self, session: ConversationSession, state: dict[str, object]) -> object:
        self.store.save_session_state(session, state)
        return "state"

    @override
    def link_response(self, run_id: str, response_event_id: str) -> object:
        # As ``ConversationStateWriter.apersist_response_event_id_in_session_run`` does in its lane.
        session = self.load()
        if session is None:
            return "no-session"
        run = next((run for run in session.runs if run.run_id == run_id), None)
        if run is None or (run.metadata or {}).get(MATRIX_RESPONSE_EVENT_ID_METADATA_KEY) == response_event_id:
            return "not-loaded" if run is None else "linked"
        linked = deepcopy(run)
        linked.metadata = {**(run.metadata or {}), MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: response_event_id}
        return self._save(session, [linked], usage=False, state=None)

    @override
    def finalize_notice(self, response_turn_id: str) -> object:
        # As ``_finalize_queued_notice_in_new_session_storage`` does.
        session = self.load()
        if session is None:
            return "no-session"
        changed = _finalize_queued_notice_in_runs(session.runs, response_turn_id=response_turn_id)
        return self._save(session, changed, usage=False, state=None) if changed else "unchanged"

    @override
    def scrub_notices(self, session: ConversationSession) -> object:
        # As ``scrub_queued_notice_session_context`` does.
        changed = _recover_prior_queued_notices(session, active_response_turn_id=None)
        return self._save(session, changed, usage=False, state=None) if changed else "unchanged"

    @override
    def persist_delegation(self, session: ConversationSession, record: AnyRunRecord, state: DelegationState) -> object:
        asyncio.run(persist_delegation_state(self.store, session, record, state))
        return None

    @override
    def save_interrupted(
        self,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
        state: dict[str, object] | None,
    ) -> object:
        persist_interrupted_replay_snapshot(
            storage=self.store,
            session=session,
            run_id=run_id,
            snapshot=snapshot,
            state=state,
        )
        return None

    @override
    def discard_run(self, session: ConversationSession, run_id: str) -> object:
        return sorted(self.store.delete_run_subtrees(session, [run_id]))

    @override
    def chunk_basis(self, session: ConversationSession, run_ids: Sequence[str], previous_summary: str | None) -> object:
        return read_chunk_basis(self.store, session, self.scope, run_ids, previous_summary=previous_summary)

    @override
    def append_chunk(self, session: ConversationSession, basis: object, run_ids: Sequence[str], summary: str) -> bool:
        chunk_basis: _ChunkBasis = basis  # type: ignore[assignment]
        return append_compaction_chunk(
            self.store,
            session=session,
            scope=self.scope,
            basis=chunk_basis,
            included_run_ids=run_ids,
            summary=summary,
            summary_model=SUMMARY_MODEL.id,
        )

    @override
    def chunk_summary_usage(self, session: ConversationSession) -> None:
        # ``scope_usage_recorder`` records without an initial session.
        self._summary_usage(session)

    def _summary_usage(self, session: ConversationSession) -> None:
        save_compaction_usage(
            self.store,
            session_id=session.session_id,
            requester_id=REQUESTER_ID,
            model=SUMMARY_MODEL,
            usage=_SUMMARY_USAGE,
            initial_session=None,
        )

    @override
    def fold_basis(self, session: ConversationSession) -> FoldBasisView | None:
        basis = read_fold_basis(self.store, session, self.scope)
        if basis is None:
            return None
        return FoldBasisView(handle=basis, summary=basis.summary, run_ids=tuple(run.run_id for run in basis.runs))

    @override
    def fold_summary_usage(self, session: ConversationSession) -> None:
        # ``compact_running_turn`` creates the session row before the summary call reports its usage.
        ensure_scope_session_row(self.store, session, self.scope)
        self._summary_usage(session)

    @override
    def append_fold(self, session: ConversationSession, basis: FoldBasisView, summary: str, fold: FoldInput) -> bool:
        fold_basis: FoldBasis = basis.handle  # type: ignore[assignment]
        record = fold.record
        call_ids = {
            call["id"]
            for message in fold.messages
            for call in message.tool_calls or []
            if isinstance(call.get("id"), str)
        }
        # As ``mid_turn_compaction._segment`` builds it.
        segment = Segment(
            run_id=record.run_id,
            metadata=without_fold_records(deepcopy(record.metadata)) or {},
            messages=[message.to_dict() for message in fold.messages],
            tools=[tool.to_dict() for tool in record.tools or [] if tool.tool_call_id in call_ids],
            member_responses=[member.to_dict() for member in fold.members],
        )
        return append_fold(
            self.store,
            session=session,
            scope=self.scope,
            basis=fold_basis,
            summary=summary,
            summary_model=SUMMARY_MODEL.id,
            segment=segment,
        )

    @override
    def cut_edit(self, event_id: str) -> object:
        return remove_runs_answering(self.store, SESSION_ID, self.scope, event_id)

    @override
    def redaction_cleanup(self, redacted: set[str]) -> object:
        session = self.load()
        if session is None:
            return []

        async def redacted_history_events(event_ids: tuple[str, ...]) -> dict[str, str | None]:
            return {event_id: None for event_id in event_ids if event_id in redacted}

        return asyncio.run(remove_history_of_redacted_events(self.store, session, self.scope, redacted_history_events))

    @override
    def set_force(self, session: ConversationSession, *, forced: bool) -> None:
        set_force_compaction(self.store, session=session, scope=self.scope, forced=forced)

    @override
    def request_force(self, session: ConversationSession) -> None:
        # As ``request_compaction_before_next_reply`` does: the flag on the stored row, then the tools' state.
        set_force_compaction(
            self.store,
            session=first_scope_session(self.scope, SESSION_ID),
            scope=self.scope,
            forced=True,
        )
        add_pending_force_compaction_scope(session.session_state, self.scope)

    @override
    def prepare_scope_state(self, session: ConversationSession) -> None:
        # As ``_prepare_scope_state_for_run`` does.
        if consume_pending_force_compaction_scope(session, self.scope):
            set_force_compaction(self.store, session=session, scope=self.scope, forced=True)
            self.store.save_session_state(session, session_state_snapshot(session.session_state))

    @override
    def seed_legacy(self, variant: int) -> None:
        sessions = self.names.sessions
        with closing(sqlite3.connect(self.database)) as connection, connection:
            for statement in schema_sql(self.names):
                connection.execute(statement)
            connection.execute(f'DROP TABLE "{sessions}_summaries"')
            connection.execute(f'DROP TABLE "{sessions}_removed_runs"')
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
                run = self._archived_run(run_id)
                connection.execute(
                    f'INSERT INTO "{sessions}_compacted_runs" (compaction_id, session_id, run_id, run_type, '  # noqa: S608
                    "event_ids, seen_event_ids, run_data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        generation_id,
                        SESSION_ID,
                        run_id,
                        run_type(run),
                        json.dumps([f"${run_id}", f"$read-by-{run_id}"]),
                        json.dumps([f"${run_id}"]),
                        json.dumps(run.to_dict()),
                    ),
                )

    def _archived_run(self, run_id: str) -> AnyRunRecord:
        record = self._record(run_id)
        record.status = RunStatus.completed
        record.content = f"answer {run_id}"
        record.messages = [
            Message(role="user", content=f"question {run_id}", id=f"{run_id}.u", created_at=self.determinism.now),
            Message(role="assistant", content=f"answer {run_id}", id=f"{run_id}.a", created_at=self.determinism.now),
        ]
        record.metadata = {
            MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}"],
            "room_id": "!room:example.org",
        }
        return record

    @override
    def observe(self) -> dict[str, object]:
        return observe(self.store, SESSION_ID, self.scope, readers=AGNO_LAYOUT_READERS, run_ids=self.known_run_ids())


__all__ = ["SUMMARY_MODEL", "OldBackend"]
