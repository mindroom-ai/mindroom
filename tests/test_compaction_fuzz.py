"""Shrinkable mixed-operation coverage for the conversation log's compaction invariants.

Each example drives one conversation through appends, compaction chunks, chunks
whose summary call races another writer, approvals that pause and later
complete, stale whole-row session writes, late saves of old runs, redactions,
and reopens, starting from an empty or a pre-archive database. Summaries name
the runs they cover, so after every step the test checks exactly what replay
represents.
"""

from __future__ import annotations

import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest
from agno.db.sqlite import SqliteDb
from agno.models.message import Message
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from agno.session.agent import AgentSession
from agno.session.summary import SessionSummary
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from mindroom import constants
from mindroom.history import summary_rows
from mindroom.history.replay import current_summary_text
from mindroom.history.storage import (
    _remove_redacted_event_from_history,
    append_compaction_chunk,
    read_chunk_basis,
    read_scope_seen_event_ids,
)
from mindroom.history.types import HistoryScope
from mindroom.session_storage.agent_storage import create_state_storage, get_agent_session
from tests.conftest import seed_session
from tests.history_helpers import folded_run_ids, stored_run_ids

_SCOPE = HistoryScope(kind="agent", scope_id="code")
_SESSION = "session"
_LEGACY_RUNS = ("legacy-1", "legacy-2")


@dataclass(frozen=True)
class Action:
    """One generated operation on the conversation."""

    kind: str
    count: int = 1
    index: int = 0


def _event(run_id: str) -> str:
    return f"${run_id}"


def _summary_of(run_ids: set[str]) -> str:
    return "|".join(sorted(run_ids))


def _covered(session: AgentSession) -> set[str]:
    """Return the run ids the replayed summary covers."""
    return set(session.summary.summary.split("|")) if session.summary is not None else set()


class _Runner:
    """Drive one conversation and check the log invariants after every step."""

    def __init__(self, root: Path, *, legacy: bool) -> None:
        self._root = root
        self._created: list[str] = []
        self._redacted: set[str] = set()
        self._removed: set[str] = set()
        self._paused: list[str] = []
        self._snapshots: list[AgentSession] = []
        # The lowest index a redaction in the current step may delete from; ``None`` when it may delete nothing.
        self._cut_floor: int | None = None
        self._moved: set[str] = set()
        seeding = self._open()
        if legacy:
            seed_session(seeding, self._legacy_session())
        else:
            seed_session(seeding, AgentSession(session_id=_SESSION, agent_id="code"))
        seeding.close()
        if legacy:
            # Releases before the archive had no summary-row tables, so the next open adopts the state.
            with sqlite3.connect(root / "sessions" / "code.db") as connection:
                connection.execute("DROP TABLE IF EXISTS code_sessions_summaries")
                connection.execute("DROP TABLE IF EXISTS code_sessions_removed_runs")
        self._storage = self._open()
        self._stored = set(stored_run_ids(self._storage, _SESSION))
        self._indexes = self._run_indexes()

    def run(self, actions: list[Action]) -> None:
        operations = {
            "add": self._add,
            "pause": self._pause,
            "resume": self._resume,
            "compact": self._compact,
            "race": self._race,
            "snapshot": self._snapshot,
            "stale_write": self._stale_write,
            "resurrect": self._resurrect,
            "redact": self._redact,
            "reopen": self._reopen,
        }
        for action in actions:
            self._cut_floor = None
            self._moved = set()
            operations[action.kind](action)
            self._check()

    def close(self) -> None:
        self._storage.close()

    def _open(self) -> SqliteDb:
        db = create_state_storage("code", self._root, subdir="sessions", session_table="code_sessions")
        assert isinstance(db, SqliteDb)
        return db

    @staticmethod
    def _legacy_session() -> AgentSession:
        """Return a session as the destructive compactor left it after folding the legacy runs."""
        return AgentSession(
            session_id=_SESSION,
            agent_id="code",
            summary=SessionSummary(summary=_summary_of(set(_LEGACY_RUNS))),
            metadata={
                constants.MINDROOM_COMPACTION_METADATA_KEY: {
                    "version": 2,
                    "states": {_SCOPE.key: {"compacted_run_ids": list(_LEGACY_RUNS)}},
                },
                constants.MINDROOM_MATRIX_HISTORY_METADATA_KEY: {
                    "version": 1,
                    "states": {_SCOPE.key: {"seen_event_ids": [_event(run_id) for run_id in _LEGACY_RUNS]}},
                },
            },
        )

    def _load(self) -> AgentSession:
        session = get_agent_session(self._storage, _SESSION)
        assert session is not None
        return session

    def _new_run(self, run_id: str, status: RunStatus) -> RunOutput:
        return RunOutput(
            run_id=run_id,
            agent_id="code",
            session_id=_SESSION,
            status=status,
            content=f"answer {run_id}",
            messages=[Message(role="user", content=f"question {run_id}")],
            metadata={
                constants.MATRIX_EVENT_ID_METADATA_KEY: _event(run_id),
                constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [_event(run_id)],
            },
        )

    def _add(self, _action: Action) -> None:
        run_id = f"r{len(self._created)}"
        self._storage.upsert_run(run=self._new_run(run_id, RunStatus.completed), session_id=_SESSION, user_id=None)
        self._created.append(run_id)

    def _pause(self, _action: Action) -> None:
        run_id = f"r{len(self._created)}"
        self._storage.upsert_run(run=self._new_run(run_id, RunStatus.paused), session_id=_SESSION, user_id=None)
        self._created.append(run_id)
        self._paused.append(run_id)

    def _resume(self, action: Action) -> None:
        """Complete a waiting approval, which then sorts after the turns it waited for."""
        waiting = [run_id for run_id in self._paused if run_id in self._stored]
        if not waiting:
            return
        run_id = waiting[action.index % len(waiting)]
        self._paused.remove(run_id)
        self._storage.upsert_run(run=self._new_run(run_id, RunStatus.completed), session_id=_SESSION, user_id=None)
        self._moved.add(run_id)

    def _chunk(self, count: int) -> tuple[AgentSession, list[str], str] | None:
        """Return the oldest loaded visible runs a compaction would fold next and the summary it would write."""
        session = self._load()
        chunk = [
            run.run_id
            for run in session.runs or []
            if isinstance(run, RunOutput) and run.run_id and run.status == RunStatus.completed
        ][:count]
        if not chunk:
            return None
        return session, chunk, _summary_of(_covered(session) | set(chunk))

    def _append(self, session: AgentSession, chunk: list[str], summary: str, basis: object) -> bool:
        return append_compaction_chunk(
            storage=self._storage,
            session=session,
            scope=_SCOPE,
            basis=basis,  # type: ignore[arg-type]
            included_run_ids=chunk,
            summary=SessionSummary(summary=summary),
            summary_model="fuzz-model",
        )

    def _basis(self, session: AgentSession, run_ids: list[str]) -> object:
        return read_chunk_basis(
            self._storage,
            session,
            _SCOPE,
            run_ids,
            previous_summary=current_summary_text(session),
        )

    def _compact(self, action: Action) -> None:
        chunk = self._chunk(action.count)
        if chunk is None:
            return
        session, run_ids, summary = chunk
        basis = self._basis(session, run_ids)
        assert basis is not None
        assert self._append(session, run_ids, summary, basis)

    def _race(self, action: Action) -> None:
        """Load a chunk, let another compaction or a redaction commit before its basis read or during its summary call."""
        chunk = self._chunk(action.count)
        if chunk is None:
            return
        session, run_ids, summary = chunk
        compaction = action.index % 2 == 0
        during_summary_call = action.index % 4 >= 2
        basis = self._basis(session, run_ids) if during_summary_call else None
        assert basis is not None or not during_summary_call
        if compaction:
            self._compact(Action("compact", count=1))
        else:
            self._redact(action)
        if not during_summary_call:
            basis = self._basis(session, run_ids)
            assert basis is None or not compaction
        if basis is not None:
            appended = self._append(session, run_ids, summary, basis)
            if compaction:
                assert not appended
            elif not during_summary_call:
                # A redaction that left the chunk and its summary alone, committed before the basis read.
                assert appended

    def _snapshot(self, _action: Action) -> None:
        self._snapshots.append(self._load())

    def _stale_write(self, action: Action) -> None:
        """Write an older snapshot's whole session row back, as a concurrent run's end would."""
        if self._snapshots:
            self._storage.upsert_session(self._snapshots[action.index % len(self._snapshots)])

    def _resurrect(self, action: Action) -> None:
        """Save an old copy of a run that no longer loads, as a late run save would."""
        loaded = {run.run_id for run in self._load().runs or []}
        copies = [run for snapshot in self._snapshots for run in snapshot.runs or [] if run.run_id not in loaded]
        if copies:
            run = copies[action.index % len(copies)]
            self._storage.upsert_run(run=run, session_id=_SESSION, user_id=None)

    def _redact(self, action: Action) -> None:
        events = [_event(run_id) for run_id in (*_LEGACY_RUNS, *self._created)]
        event_id = events[action.index % len(events)]
        run_id = event_id[1:]
        before = set(stored_run_ids(self._storage, _SESSION))
        # The cut starts at the earliest element holding the event: the legacy row before every run while
        # its summary is in force, otherwise the run that answered it unless that run is retired or gone.
        if run_id in _LEGACY_RUNS:
            legacy = summary_rows.legacy_event_ids(self._storage, session_id=_SESSION, scope_key=_SCOPE.key)
            floor = -1 if legacy is not None else None
        elif run_id in before - self._behind_latest_barrier():
            floor = self._run_indexes()[run_id]
        else:
            floor = None
        if floor is not None:
            self._cut_floor = floor if self._cut_floor is None else min(self._cut_floor, floor)
        _remove_redacted_event_from_history(self._storage, self._load(), _SCOPE, event_id=event_id)
        self._removed |= before - set(stored_run_ids(self._storage, _SESSION))
        self._redacted.add(event_id)

    def _reopen(self, _action: Action) -> None:
        self._storage.close()
        self._storage = self._open()

    def _run_indexes(self) -> dict[str, int]:
        with sqlite3.connect(self._root / "sessions" / "code.db") as connection:
            return dict(connection.execute("SELECT run_id, run_index FROM code_sessions_runs").fetchall())

    def _behind_latest_barrier(self) -> set[str]:
        with sqlite3.connect(self._root / "sessions" / "code.db") as connection:
            (barrier,) = connection.execute(
                "SELECT after_run_index FROM code_sessions_summaries WHERE summary IS NULL ORDER BY id DESC LIMIT 1",
            ).fetchone() or (None,)
        return {run_id for run_id, index in self._run_indexes().items() if barrier is not None and index <= barrier}

    def _check(self) -> None:
        session = self._load()
        loaded = {run.run_id for run in session.runs or [] if run.run_id}
        visible = {run.run_id for run in session.runs or [] if run.run_id and run.status == RunStatus.completed}
        folded = set(folded_run_ids(self._storage, _SESSION))
        covered = _covered(session)
        stored = set(stored_run_ids(self._storage, _SESSION))
        indexes = self._run_indexes()

        # Replay is the latest summary plus the visible runs after it; no folded run loads.
        assert not loaded & folded
        # Every folded run after the latest barrier is covered by the replayed summary (I1).
        retired = self._behind_latest_barrier()
        if session.summary is not None:
            assert folded - retired <= covered
        # The summary covers only runs that are stored and not retired, and the legacy runs while their
        # summary is in force, so a retired or cut run never comes back through a chunk's input.
        assert covered - set(_LEGACY_RUNS) <= stored - retired
        if covered & set(_LEGACY_RUNS):
            assert summary_rows.legacy_event_ids(self._storage, session_id=_SESSION, scope_key=_SCOPE.key)
        # Compaction moves no run row; only a completed approval moves, to the end.
        for run_id, index in self._indexes.items():
            if run_id in indexes and run_id not in self._moved:
                assert indexes[run_id] == index
        for run_id in self._moved & set(indexes):
            assert indexes[run_id] == max(indexes.values())
        # Redacted content never replays: it is gone from loaded runs, the summary, and every stored run
        # except a retired one behind the latest barrier, which neither replays nor counts as seen.
        for event_id in self._redacted:
            assert event_id[1:] not in loaded
            assert event_id[1:] not in covered
            assert event_id[1:] not in stored - retired
        # A run a cut removed never returns, whatever late save arrives.
        assert not stored & self._removed
        # Nothing stored is lost except by a redaction, which loses only the runs at or after its cut.
        lost = self._stored - stored
        if self._cut_floor is None:
            assert not lost
        else:
            assert all(self._indexes[run_id] >= self._cut_floor for run_id in lost)
        self._stored = stored
        self._indexes = indexes
        # Seen ids are exactly the events replay represents, so nothing is repeated or dropped.
        expected_seen = {_event(run_id) for run_id in visible | covered}
        assert read_scope_seen_event_ids(self._storage, session, _SCOPE) == expected_seen


_ACTIONS = st.builds(
    Action,
    kind=st.sampled_from(
        (
            "add",
            "add",
            "add",
            "pause",
            "resume",
            "compact",
            "race",
            "snapshot",
            "stale_write",
            "resurrect",
            "redact",
            "reopen",
        ),
    ),
    count=st.integers(1, 3),
    index=st.integers(0, 30),
)


@pytest.mark.timeout(300)
@settings(max_examples=60, deadline=None, print_blob=True, suppress_health_check=[HealthCheck.too_slow])
@given(actions=st.lists(_ACTIONS, min_size=1, max_size=30), legacy=st.booleans())
@example(
    actions=[
        Action("add"),
        Action("add"),
        Action("snapshot"),
        Action("compact"),
        Action("race"),
        Action("redact", index=3),
        Action("stale_write"),
        Action("reopen"),
    ],
    legacy=True,
)
@example(
    actions=[
        Action("add"),
        Action("add"),
        Action("add"),
        Action("snapshot"),
        Action("compact", count=2),
        Action("resurrect"),
        Action("compact"),
        Action("redact", index=1),
        Action("stale_write"),
        Action("add"),
        Action("compact", count=2),
    ],
    legacy=False,
)
@example(
    # Retiring a legacy summary for a loaded redaction leaves folded runs outside replay.
    actions=[
        Action("add"),
        Action("add"),
        Action("compact"),
        Action("redact", index=3),
        Action("add"),
        Action("compact"),
    ],
    legacy=True,
)
@example(
    # A stale copy of a run a redaction removed is saved again.
    actions=[
        Action("add"),
        Action("add"),
        Action("snapshot"),
        Action("compact"),
        Action("redact", index=0),
        Action("resurrect"),
    ],
    legacy=False,
)
@example(
    # A retirement barrier survives a later cut that lands before it.
    actions=[
        Action("add"),
        Action("pause"),
        Action("add"),
        Action("compact"),
        Action("redact", index=4),
        Action("redact", index=3),
        Action("add"),
    ],
    legacy=True,
)
@example(
    # An approval paused before a compaction completes after it and replays after the summary.
    actions=[
        Action("pause"),
        Action("add"),
        Action("add"),
        Action("compact", count=2),
        Action("resume"),
        Action("compact"),
    ],
    legacy=False,
)
@example(
    # A redaction of a run the chunk loaded lands before the chunk reads its basis.
    actions=[Action("add"), Action("add"), Action("add"), Action("race", count=3, index=9)],
    legacy=False,
)
@example(
    # A redaction retires the legacy summary the chunk loaded before the chunk reads its basis.
    actions=[Action("add"), Action("add"), Action("add"), Action("race", count=1, index=9)],
    legacy=True,
)
def test_generated_compaction_histories_keep_the_log_invariants(actions: list[Action], *, legacy: bool) -> None:
    """Interleaved compaction, races, approvals, stale writes, and redaction never lose, repeat, or leak history."""
    with tempfile.TemporaryDirectory() as root:
        runner = _Runner(Path(root), legacy=legacy)
        try:
            runner.run(actions)
        finally:
            runner.close()
