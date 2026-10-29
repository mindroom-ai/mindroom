"""The storage v2 layout keeps its own invariants after every action of generated conversations.

The conversations are the oracle's vocabulary (``tests/storage_oracle/actions.py``) through ``StoreBackend``, drawn by
hypothesis, except that a fold never follows a member's pause within one attempt, since a team run's loop sends no
request after a member pauses. After every action the raw tables are read and checked against the invariants of
PLAN-3095 task 7. The replay check (I6) takes its expectations from an independent model: the place spec 3.2 gives
each run, from the backend's writes alone, the runs each summary's text names, and the message lists the writes stored.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, override

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from mindroom.history.run_visibility import replays_in_scope
from mindroom.llm.runs import RunStatus, TeamRunRecord
from mindroom.session_storage.schema import NON_VISIBLE_RUN_STATUS_VALUES, quote_identifier
from mindroom.session_storage.store import seen_event_ids, source_event_ids
from tests.storage_oracle.actions import KIND_WEIGHTS, NAMED, OPENING_KINDS, Action, Scenario
from tests.storage_oracle.backend import SESSION_ID, summary_tokens
from tests.storage_oracle.determinism import deterministic
from tests.storage_oracle.store_backend import StoreBackend

if TYPE_CHECKING:
    from mindroom.delegation.state import DelegationState
    from mindroom.history.interrupted_replay import InterruptedReplaySnapshot
    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import SaveOutcome
    from mindroom.session_storage.types import ConversationSession
    from tests.storage_oracle.actions import ScenarioKind
    from tests.storage_oracle.backend import FoldBasisView, FoldInput
    from tests.storage_oracle.determinism import Determinism

_NON_VISIBLE = frozenset(NON_VISIBLE_RUN_STATUS_VALUES)


@dataclass(frozen=True)
class _Run:
    run_id: str
    parent_run_id: str | None
    scope_key: str
    status: str
    seq: int
    folded_by: int | None
    metadata: dict[str, Any] | None

    @property
    def visible(self) -> bool:
        return self.parent_run_id is None and self.status not in _NON_VISIBLE


@dataclass(frozen=True)
class _Row:
    id: int
    scope_key: str
    through: int
    summary: str | None
    legacy: bool
    event_ids: frozenset[str]
    seen_event_ids: frozenset[str]
    fold_run_id: str | None
    fold_event_ids: list[str] | None
    fold_seen_event_ids: list[str] | None


@dataclass(frozen=True)
class _Tables:
    """One read of every log table."""

    next_seq: int
    runs: dict[str, _Run]
    messages: dict[str, list[tuple[str, int, int | None, dict[str, Any]]]]
    rows: list[_Row]
    removed: set[str]

    def scope_rows(self, scope_key: str) -> list[_Row]:
        return [row for row in self.rows if row.scope_key == scope_key]


def _read(backend: StoreBackend) -> _Tables:
    names = backend.names
    with closing(sqlite3.connect(backend.store.path)) as connection:
        (next_seq,) = connection.execute(f"SELECT next_seq FROM {quote_identifier(names.log_meta)}").fetchone()  # noqa: S608
        runs = {
            run_id: _Run(run_id, parent, scope_key, status, seq, folded_by, json.loads(metadata) if metadata else None)
            for run_id, session_id, parent, scope_key, status, seq, folded_by, metadata in connection.execute(
                "SELECT run_id, session_id, parent_run_id, scope_key, status, seq, folded_by, metadata "  # noqa: S608
                f"FROM {quote_identifier(names.log_runs)}",
            )
            if session_id == SESSION_ID
        }
        messages: dict[str, list[tuple[str, int, int | None, dict[str, Any]]]] = {}
        for run_id, message_id, ord_, folded_by, data in connection.execute(
            f"SELECT run_id, message_id, ord, folded_by, data FROM {quote_identifier(names.log_messages)} ORDER BY ord",  # noqa: S608
        ):
            messages.setdefault(run_id, []).append((message_id, ord_, folded_by, json.loads(data)))
        rows = [
            _Row(
                row_id,
                scope_key,
                through,
                summary,
                bool(legacy),
                frozenset(json.loads(event_ids)),
                frozenset(json.loads(seen)),
                fold_run_id,
                None if fold_event_ids is None else json.loads(fold_event_ids),
                None if fold_seen is None else json.loads(fold_seen),
            )
            for row_id, scope_key, through, summary, legacy, event_ids, seen, fold_run_id, fold_event_ids, fold_seen in (
                connection.execute(
                    "SELECT id, scope_key, through, summary, legacy, event_ids, seen_event_ids, fold_run_id, "  # noqa: S608
                    f"fold_event_ids, fold_seen_event_ids FROM {quote_identifier(names.log_summaries)} ORDER BY id",
                )
            )
        ]
        removed = {
            run_id
            for (run_id,) in connection.execute(
                f"SELECT run_id FROM {quote_identifier(names.log_removed)} WHERE session_id = ?",  # noqa: S608
                (SESSION_ID,),
            )
        }
    return _Tables(next_seq, runs, messages, rows, removed)


def _latest_barrier_through(rows: list[_Row]) -> int:
    return next((row.through for row in reversed(rows) if row.summary is None), 0)


class _InvariantBackend(StoreBackend):
    """``StoreBackend`` that checks the v2 invariants after every action, against what earlier actions left."""

    folds_after_member_pause = False

    def __init__(self, root: Path, kind: ScenarioKind, determinism: Determinism) -> None:
        self.handed_out: dict[int, str] = {}
        self.seq_counter = 1
        self.visible: dict[str, tuple[int, str]] = {}
        self.folded_rows: dict[tuple[str, str], tuple[int, int, dict[str, Any]]] = {}
        self.retired_rows: dict[int, tuple[frozenset[str], frozenset[str]]] = {}
        self.row_ids: set[int] = set()
        self.barrier_ids: set[int] = set()
        # The independent model of spec 3.2's run positions: each top-level run's place, from the writes alone.
        self.places: dict[str, int] = {}
        self.flagged: set[str] = set()
        self.ticks = 0
        super().__init__(root, kind, determinism)

    def _place(self, run_id: str) -> None:
        self.ticks += 1
        self.places[run_id] = self.ticks

    def _placed(self, run_id: str, parent_run_id: str | None, before: str | None, status: str) -> None:
        """Place a written top-level run as spec 3.2 does: at its first write, and when it turns visible."""
        if parent_run_id is None and (before is None or (before in _NON_VISIBLE and status not in _NON_VISIBLE)):
            self._place(run_id)

    def _live(self, run_id: str) -> bool:
        return self.turn is not None and self.turn.record.run_id == run_id

    @override
    def save_stale(self, session: ConversationSession, record: AnyRunRecord) -> object:
        # A running turn is the only writer of its row until it ends (spec 3.7), so no late copy lands on it.
        if self._live(record.run_id):
            return "live-turn"
        # No writer saves a running record whole (spec 3.6): a minimal-mode delegation only patches its key onto the
        # checkpoint row, so no late writer holds a running copy. The driver keeps the live record among its versions
        # because the Agno-layout store, which made the recordings, saved it whole.
        if record.status is RunStatus.running:
            return "running-copy"
        return super().save_stale(session, record)

    @override
    def save_interrupted(
        self,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
        state: dict[str, object] | None,
    ) -> object:
        # Interrupted records are written after their turn ended, and CLI approval recovery continues in a new turn.
        return "live-turn" if self._live(run_id) else super().save_interrupted(session, run_id, snapshot, state)

    def _stored_status(self, run_id: str) -> str | None:
        row = self.stored_runs().get(run_id)
        return None if row is None else row[1]

    @override
    def _save(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        *,
        usage: bool,
        state: dict[str, object] | None,
    ) -> list[str]:
        before = self._stored_status(record.run_id)
        outcome = super()._save(session, record, usage=usage, state=state)
        if outcome == ["written"]:
            self._placed(record.run_id, record.parent_run_id, before, record.status.value)
        return outcome

    @override
    def _persist_delegation(
        self,
        session: ConversationSession,
        record: AnyRunRecord,
        state: DelegationState,
    ) -> SaveOutcome | bool:
        before = self._stored_status(record.run_id)
        result = super()._persist_delegation(session, record, state)
        if result == "written":
            self._placed(record.run_id, record.parent_run_id, before, record.status.value)
        return result

    @override
    def _save_interrupted(
        self,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
        state: dict[str, object] | None,
    ) -> tuple[AnyRunRecord, SaveOutcome | bool]:
        before = self._stored_status(run_id)
        record, outcome = super()._save_interrupted(session, run_id, snapshot, state)
        if outcome == "written":
            self._placed(run_id, None, before, record.status.value)
        return record, outcome

    @override
    def append_fold(self, session: ConversationSession, basis: FoldBasisView, summary: str, fold: FoldInput) -> bool:
        appended = super().append_fold(session, basis, summary, fold)
        if appended:
            # A fold's commit places its running run after the fold's summary.
            self._place(fold.record.run_id)
            self.flagged.update(member.run_id for member in fold.members)
        return appended

    @override
    def apply(self, action: Action) -> object:
        outcome = super().apply(action)
        self.check()
        return outcome

    def check(self) -> None:
        tables = _read(self)
        self._check_seq(tables)
        self._check_runs(tables)
        self._check_messages(tables)
        self._check_rows(tables)
        self._check_folds(tables)
        self._check_replay(tables)
        self.row_ids = {row.id for row in tables.rows}

    def _check_seq(self, tables: _Tables) -> None:
        # I1: a session's seq values are unique and below the counter, which never goes back nor reuses a number.
        seqs = [run.seq for run in tables.runs.values()]
        assert len(seqs) == len(set(seqs)), "I1: a seq is held twice"
        assert all(seq < tables.next_seq for seq in seqs), "I1: a seq at or past the counter"
        assert tables.next_seq >= self.seq_counter, "I1: the counter went back"
        self.seq_counter = tables.next_seq
        for run in tables.runs.values():
            assert self.handed_out.setdefault(run.seq, run.run_id) == run.run_id, f"I1: seq {run.seq} handed out twice"

    def _check_runs(self, tables: _Tables) -> None:
        for run in tables.runs.values():
            # I8: no removed run, or member of one, has a row.
            assert run.run_id not in tables.removed, f"I8: removed run {run.run_id} has a row"
            assert run.parent_run_id not in tables.removed, f"I8: member {run.run_id} of a removed run has a row"
            # I11: every member's team run is a top-level run.
            parent = tables.runs.get(run.parent_run_id) if run.parent_run_id is not None else None
            assert parent is None or parent.parent_run_id is None, f"I11: {run.run_id}'s team run is a member"
            # I2: a top-level run once visible keeps its seq and status; only P4 replaces its content.
            if run.visible or run.run_id in self.visible:
                kept = self.visible.setdefault(run.run_id, (run.seq, run.status))
                assert kept == (run.seq, run.status), f"I2: visible run {run.run_id} moved or changed status"
        self.visible = {run_id: kept for run_id, kept in self.visible.items() if run_id in tables.runs}
        # I8: a cut that deleted a fold row fenced its running run, so the run's late saves write nothing.
        row_ids = {row.id for row in tables.rows}
        for row_id, fold in self.folds.items():
            assert row_id in row_ids or fold.record.run_id in tables.removed, f"I8: fold row {row_id}'s run not fenced"

    def _check_messages(self, tables: _Tables) -> None:
        for run_id, rows in tables.messages.items():
            # I3: a folded message row stays as its fold left it, by the same summary, while its run is stored.
            for message_id, ord_, folded_by, data in rows:
                if (held := self.folded_rows.get((run_id, message_id))) is not None:
                    assert (folded_by, ord_, data) == held, f"I3: folded message {message_id} of {run_id} changed"
            # I4: the unfolded rows of a run are exactly the list its last write stored, in order.
            expected = self.expected_view(run_id)
            view = [data for _id, _ord, folded_by, data in rows if folded_by is None]
            assert expected is None or view == expected, f"I4: {run_id}'s unfolded rows are not its last saved list"
        for (run_id, message_id), _held in list(self.folded_rows.items()):
            if run_id in tables.runs:
                assert any(row[0] == message_id for row in tables.messages.get(run_id, [])), "I3: a folded row went"
        self.folded_rows = {
            (run_id, message_id): (folded_by, ord_, data)
            for run_id, rows in tables.messages.items()
            if run_id in tables.runs
            for message_id, ord_, folded_by, data in rows
            if folded_by is not None
        }
        # I5: no record holds a message id twice, a paused team run's named members included.
        for record in self.store.list_runs(SESSION_ID):
            for run in (record, *(record.member_responses if isinstance(record, TeamRunRecord) else ())):
                ids = [message.id for message in run.messages]
                assert len(ids) == len(set(ids)), f"I5: {run.run_id} holds a message id twice"

    def _check_rows(self, tables: _Tables) -> None:
        for scope_key in {row.scope_key for row in tables.rows}:
            rows = tables.scope_rows(scope_key)
            # I7: through never decreases in id order, barriers included; a barrier, which no cut deletes, is written
            # at the row before it, and stays at or above whatever survives before it.
            assert all(left.through <= right.through for left, right in itertools.pairwise(rows)), f"I7: {scope_key}"
            for index, row in enumerate(rows):
                assert not row.legacy or row.through == 0, "I7: a legacy row sorts after a run"
                earlier_kept = {earlier.id for earlier in rows[:index]} >= {i for i in self.row_ids if i < row.id}
                if row.summary is None and row.id not in self.row_ids and earlier_kept:
                    assert row.through == (rows[index - 1].through if index else 0), f"I7: barrier {row.id}"
            # I9: a covered visible run's event ids are in the event sets of the row that covers it.
            latest_through, retired = rows[-1].through, _latest_barrier_through(rows)
            for run in tables.runs.values():
                if run.scope_key != scope_key or not run.visible or not retired < run.seq <= latest_through:
                    continue
                # The scope's first row that reaches the run, as P2 finds it; under I7 it follows the latest barrier.
                covering = next((row for row in rows if row.summary is not None and row.through >= run.seq), None)
                assert covering is not None, f"I9: no row covers {run.run_id}"
                seen = seen_event_ids(run.metadata)
                assert seen <= covering.seen_event_ids, f"I9: {run.run_id}'s seen ids"
                assert seen | source_event_ids(run.metadata) <= covering.event_ids, f"I9: {run.run_id}'s event ids"
        barriers = {row.id for row in tables.rows if row.summary is None}
        assert self.barrier_ids <= barriers, "I7: a barrier was deleted"
        self.barrier_ids = barriers
        # I9: a row behind its scope's latest barrier is retired, so its event sets never change again.
        for row in tables.rows:
            if (held := self.retired_rows.get(row.id)) is not None:
                assert (row.event_ids, row.seen_event_ids) == held, f"I9: retired row {row.id}'s event sets changed"
        self.retired_rows = {
            row.id: (row.event_ids, row.seen_event_ids)
            for row in tables.rows
            if any(barrier.id > row.id for barrier in tables.scope_rows(row.scope_key) if barrier.summary is None)
        }

    def _check_folds(self, tables: _Tables) -> None:
        # I10: the members that ended before a fold keep its flag, or an earlier fold's, while their rows stay.
        self.flagged &= tables.runs.keys()
        for member_id in self.flagged:
            assert tables.runs[member_id].folded_by is not None, f"I10: folded member {member_id} lost its flag"
        for row in tables.rows:
            # I10: a fold row keeps the running run's live metadata at its fold as its provenance.
            fold = self.folds.get(row.id)
            if row.fold_run_id is None:
                assert (row.fold_event_ids, row.fold_seen_event_ids) == (None, None), f"I10: row {row.id}"
                continue
            assert fold is not None, f"I10: row {row.id} is no fold this backend appended"
            metadata = fold.record.metadata
            assert row.fold_run_id == fold.record.run_id, f"I10: row {row.id}'s run"
            assert row.fold_event_ids == sorted(source_event_ids(metadata)), f"I10: row {row.id}'s answered ids"
            assert row.fold_seen_event_ids == sorted(seen_event_ids(metadata)), f"I10: row {row.id}'s seen ids"
            # The row's event sets hold what its running run answered and read, so redaction matches them.
            assert {*row.fold_event_ids, *row.fold_seen_event_ids} <= row.event_ids, f"I10: row {row.id}'s events"
            assert set(row.fold_seen_event_ids) <= row.seen_event_ids, f"I10: row {row.id}'s seen events"

    def _check_replay(self, tables: _Tables) -> None:
        """I6: replay is the latest summary's text and the runs no summary covered, in the model's order.

        The model places each run from the backend's writes (spec 3.2), and the summary texts the driver writes name
        every run their summaries covered, so neither the order nor the coverage comes from the store. The load is
        also checked against the raw tables: their latest row, its ``through``, and each run's unfolded rows.
        """
        stored = {run.run_id: run for run in tables.runs.values() if run.parent_run_id is None}
        self.places = {run_id: place for run_id, place in self.places.items() if run_id in stored}
        placed = [run_id for run_id in stored if run_id in self.places]
        by_seq = sorted(placed, key=lambda run_id: stored[run_id].seq)
        assert by_seq == sorted(placed, key=self.places.__getitem__), f"I6: runs placed as {by_seq}"
        session = self.load()
        if session is None:
            return
        replayed = [run for run in session.runs if replays_in_scope(run, self.scope)]
        replayed_ids = [run.run_id for run in replayed]
        modelled = [run_id for run_id in replayed_ids if run_id in self.places]
        assert modelled == sorted(modelled, key=self.places.__getitem__), f"I6: replay order {replayed_ids}"
        rows = tables.scope_rows(self.scope.key)
        if not any(row.summary is None or row.legacy for row in rows):
            # Without a barrier or a legacy summary, the latest summary's text names exactly the covered runs.
            named = summary_tokens(rows[-1].summary) if rows else set()
            visible = {run_id for run_id, run in stored.items() if run.visible and run.scope_key == self.scope.key}
            assert not named & set(replayed_ids), f"I6: summarized runs replay: {sorted(named & set(replayed_ids))}"
            assert visible - set(replayed_ids) <= named, f"I6: runs neither replay nor summarized: {visible}"
        latest = rows[-1] if rows else None
        expected_runs = sorted(
            (
                run
                for run in stored.values()
                if run.scope_key == self.scope.key and run.visible and run.seq > (latest.through if latest else 0)
            ),
            key=lambda run: run.seq,
        )
        assert (session.summary.text if session.summary else None) == (latest.summary if latest else None), "I6"
        assert replayed_ids == [run.run_id for run in expected_runs], "I6: replayed runs"
        for run in replayed:
            rows_of_run = tables.messages.get(run.run_id, [])
            stored_view = [data for _id, _ord, folded_by, data in rows_of_run if folded_by is None]
            expected = self.expected_view(run.run_id)
            assert [message.to_dict() for message in run.messages] == (stored_view if expected is None else expected), (
                f"I6: {run.run_id}'s replayed messages"
            )


_ACTION_INDEX = st.integers(0, 30)


@st.composite
def _scenarios(draw: st.DrawFn) -> Scenario:
    kind: ScenarioKind = draw(st.sampled_from(("agent", "team")))
    kinds = [name for name, weight in KIND_WEIGHTS[kind].items() for _ in range(weight)]
    opening = draw(st.sampled_from((None, *sorted(OPENING_KINDS))))
    actions = [Action(opening, index=draw(st.integers(0, 2)))] if opening is not None else []
    actions += draw(
        st.lists(
            st.builds(Action, kind=st.sampled_from(kinds), index=_ACTION_INDEX, count=st.integers(1, 3)),
            min_size=1,
            max_size=30,
        ),
    )
    return Scenario(kind, tuple(actions))


def _drive(scenario: Scenario) -> list[object]:
    with tempfile.TemporaryDirectory() as root, deterministic() as determinism:
        backend = _InvariantBackend(Path(root), scenario.kind, determinism)
        return [backend.apply(action) for action in scenario.actions]


@pytest.mark.timeout(600)
@settings(max_examples=40, deadline=None, print_blob=True, suppress_health_check=[HealthCheck.too_slow])
@given(scenario=_scenarios())
@example(
    # A fold of a CLI turn takes messages its checkpoint already stored.
    scenario=Scenario("agent", (Action("checkpoint"), Action("fold"))),
)
@example(
    # A resumed approval folds after a later turn completed, so its fold moves it after that turn.
    scenario=Scenario("agent", (Action("pause"), Action("add"), Action("fold", index=1))),
)
@example(
    # A redaction cuts a team turn with its members; a late save of one of them then writes nothing.
    scenario=Scenario("team", (Action("add"), Action("redact"), Action("member_late"))),
)
@example(
    # A redaction of what the running team turn answered cuts its fold, so its next members and fold write nothing.
    scenario=Scenario("team", (Action("fold"), Action("redact"), Action("fold"))),
)
@example(
    # A stale save stores a member of a discarded fold owner again, as a new row without the fold's flag.
    scenario=Scenario("team", (Action("fold_first_turn"), Action("discard", index=1), Action("stale_save"))),
)
@example(
    # An approval resume completes after a later turn, so it moves after that turn.
    scenario=Scenario("agent", (Action("pause"), Action("add"), Action("resume"))),
)
@example(
    # Neither an interrupted record nor a stale copy lands on a running CLI turn's row, which its fold then moves.
    scenario=Scenario("agent", (Action("checkpoint"), Action("interrupted"), Action("fold"))),
)
@example(scenario=Scenario("agent", (Action("cli_delegate"), Action("stale_save", index=3), Action("fold"))))
@example(scenario=NAMED["team_pause_advanced"])
@example(scenario=NAMED["linked_covered_run_redacted"])
@example(scenario=NAMED["discarded_fold_owner_redacted"])
@example(scenario=NAMED["interrupted_voice_playout"])
@example(scenario=NAMED["archive_legacy_in_force_redacted"])
@example(
    # A legacy summary in force retires at the third row; redacting the paused turn before it then cuts the second row
    # and keeps the barrier, whose through now sits above the row left before it.
    scenario=Scenario(
        "agent",
        (
            Action("legacy", index=2),
            Action("pause"),
            Action("add"),
            Action("compact"),
            Action("add"),
            Action("compact"),
            Action("redact", index=7),
            Action("redact", index=3),
        ),
    ),
)
@example(
    # A reply linked to a run the barrier retired joins no row.
    scenario=Scenario(
        "agent",
        (
            Action("legacy", index=2),
            Action("add"),
            Action("compact"),
            Action("add"),
            Action("compact"),
            Action("redact", index=5),
            Action("link", index=0),
        ),
    ),
)
def test_the_v2_invariants_hold_after_every_action(scenario: Scenario) -> None:
    """I1 to I11 hold after every action of a generated conversation."""
    _drive(scenario)


@pytest.mark.parametrize("name", sorted(NAMED))
def test_the_v2_invariants_hold_for_every_named_sequence(name: str) -> None:
    """The named sequences of spec v4 4.7 keep the invariants too."""
    _drive(NAMED[name])


def test_a_late_copy_of_a_crashed_cli_turns_live_record_writes_nothing() -> None:
    """A minimal-mode turn delegates, folds, and crashes; a late copy of its live record then writes nothing.

    The delegation stored only its key on the turn's checkpoint row, so the copy is one no writer saves whole. The
    interrupted record that recovery saves next makes the run visible, and it stays as that record left it.
    """
    outcomes = _drive(
        Scenario(
            "agent",
            (
                Action("fold_first_turn", index=2),
                Action("cli_delegate"),
                Action("add"),
                Action("add"),
                Action("checkpoint"),
                Action("crash_after_fold"),
                Action("stale_save", index=24),
                Action("interrupted", index=27),
            ),
        ),
    )
    assert outcomes[6] == {"run_id": "r1", "saved": "running-copy"}
