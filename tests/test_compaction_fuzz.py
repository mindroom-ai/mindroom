"""Shrinkable mixed-operation coverage for the compaction archive invariants.

Each example drives one conversation through appends, compaction chunks, chunks
interrupted after the archive commit, stale whole-row session writes, redactions,
and reopens, starting from an empty or a pre-archive database. Summaries name the
runs they cover, so after every step (and the reconcile the next run would do)
the test can check exactly what replay represents.
"""

from __future__ import annotations

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
from mindroom.agent_storage import create_state_storage, get_agent_session
from mindroom.agents import remove_run_by_event_id
from mindroom.history import archive
from mindroom.history.storage import (
    archive_compaction_chunk,
    read_scope_seen_event_ids,
    reconcile_compaction_state,
    remove_redacted_event_from_compaction,
)
from mindroom.history.types import HistoryScope
from tests.conftest import seed_session
from tests.history_helpers import archived_run_ids

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
    """Drive one conversation and check the archive invariants after every step."""

    def __init__(self, root: Path, *, legacy: bool) -> None:
        self._root = root
        self._created: list[str] = []
        self._events: list[str] = []
        self._redacted: set[str] = set()
        self._snapshots: list[AgentSession] = []
        seeding = self._open()
        if legacy:
            seed_session(seeding, self._legacy_session())
            self._events.extend(_event(run_id) for run_id in _LEGACY_RUNS)
        else:
            seed_session(seeding, AgentSession(session_id=_SESSION, agent_id="code"))
        seeding.close()
        self._storage = self._open()
        self._present = self._present_run_ids()

    def run(self, actions: list[Action]) -> None:
        operations = {
            "add": self._add,
            "compact": self._compact,
            "interrupt": self._interrupt,
            "snapshot": self._snapshot,
            "stale_write": self._stale_write,
            "resurrect": self._resurrect,
            "reopen": self._reopen,
        }
        for action in actions:
            redacted_event_id = self._redact(action) if action.kind == "redact" else operations[action.kind](action)
            self._check(redacted_event_id)

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

    def _add(self, _action: Action) -> None:
        run_id = f"r{len(self._created)}"
        self._storage.upsert_run(
            run=RunOutput(
                run_id=run_id,
                agent_id="code",
                session_id=_SESSION,
                status=RunStatus.completed,
                content=f"answer {run_id}",
                messages=[Message(role="user", content=f"question {run_id}")],
                metadata={
                    constants.MATRIX_EVENT_ID_METADATA_KEY: _event(run_id),
                    constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [_event(run_id)],
                },
            ),
            session_id=_SESSION,
            user_id=None,
        )
        self._created.append(run_id)
        self._events.append(_event(run_id))

    def _chunk(self, count: int) -> tuple[AgentSession, list[RunOutput], str] | None:
        """Return the oldest live runs a compaction would fold next, after the reconcile it runs first."""
        session = self._load()
        reconcile_compaction_state(self._storage, session, _SCOPE)
        session = self._load()
        chunk = [run for run in session.runs or [] if isinstance(run, RunOutput)][:count]
        if not chunk:
            return None
        return session, chunk, _summary_of(_covered(session) | {run.run_id for run in chunk if run.run_id})

    def _compact(self, action: Action) -> None:
        chunk = self._chunk(action.count)
        if chunk is None:
            return
        session, runs, summary = chunk
        archive_compaction_chunk(
            storage=self._storage,
            session=session,
            scope=_SCOPE,
            summary=SessionSummary(summary=summary),
            summary_model="fuzz-model",
            archived_runs=runs,
        )

    def _interrupt(self, action: Action) -> None:
        """Commit a chunk's archive transaction but lose the session write that follows it."""
        chunk = self._chunk(action.count)
        if chunk is None:
            return
        _session, runs, summary = chunk
        archive.archive_runs(
            self._storage,
            session_id=_SESSION,
            scope_key=_SCOPE.key,
            summary=summary,
            summary_model="fuzz-model",
            runs=runs,
            event_ids={run.run_id: {_event(run.run_id)} for run in runs if run.run_id},
            seen_event_ids={run.run_id: {_event(run.run_id)} for run in runs if run.run_id},
        )

    def _snapshot(self, _action: Action) -> None:
        self._snapshots.append(self._load())

    def _stale_write(self, action: Action) -> None:
        """Write an older snapshot's whole session row back, as a concurrent run's end would."""
        if self._snapshots:
            self._storage.upsert_session(self._snapshots[action.index % len(self._snapshots)])

    def _resurrect(self, action: Action) -> None:
        """Save an archived run's old copy as live again, as a late run save would."""
        archived = self._archived_content_ids()
        copies = [run for snapshot in self._snapshots for run in snapshot.runs or [] if run.run_id in archived]
        if copies:
            run = copies[action.index % len(copies)]
            self._storage.upsert_run(run=run, session_id=_SESSION, user_id=None)

    def _redact(self, action: Action) -> str | None:
        if not self._events:
            return None
        event_id = self._events[action.index % len(self._events)]
        removed = remove_run_by_event_id(
            self._storage,
            _SESSION,
            event_id,
            include_seen_event_ids=True,
            remove_following_runs=True,
        )
        remove_redacted_event_from_compaction(
            self._storage,
            self._load(),
            _SCOPE,
            event_id=event_id,
            removed_live_run=removed,
        )
        self._redacted.add(event_id)
        return event_id

    def _reopen(self, _action: Action) -> None:
        self._storage.close()
        self._storage = self._open()

    def _archived_content_ids(self) -> set[str]:
        return set(archived_run_ids(self._storage, _SESSION)) - set(_LEGACY_RUNS)

    def _archived_event_ids(self) -> set[str]:
        with self._storage.db_engine.connect() as connection:
            return {
                row[0]
                for row in connection.exec_driver_sql(
                    "SELECT value FROM code_sessions_compacted_runs AS archived, json_each(archived.event_ids) "
                    "WHERE archived.session_id = ?",
                    (_SESSION,),
                )
            }

    def _present_run_ids(self) -> set[str]:
        return {run.run_id for run in self._load().runs or [] if run.run_id} | self._archived_content_ids()

    def _check(self, redacted_event_id: str | None) -> None:
        session = self._load()
        reconcile_compaction_state(self._storage, session, _SCOPE)
        session = self._load()
        live = {run.run_id for run in session.runs or [] if run.run_id}
        archived = self._archived_content_ids()
        covered = _covered(session)

        # Compacted runs never reappear in replay.
        assert not live & archived
        # The cached summary is the latest generation's.
        generation = archive.latest_generation(self._storage, session_id=_SESSION, scope_key=_SCOPE.key)
        if generation is not None:
            assert (session.summary.summary if session.summary is not None else None) == generation.summary
        # Redacted content is gone from live runs, the archive, and the replayed summary.
        archived_event_ids = self._archived_event_ids()
        for event_id in self._redacted:
            assert event_id[1:] not in live
            assert event_id[1:] not in covered
            assert event_id not in archived_event_ids
        # Nothing is lost except by redacting that run or an earlier one.
        present = live | archived
        lost = self._present - present
        if redacted_event_id is None:
            assert not lost
        elif redacted_event_id[1:] in self._created:
            first = self._created.index(redacted_event_id[1:])
            assert all(self._created.index(run_id) >= first for run_id in lost)
        self._present = present
        # Seen ids are exactly the events replay represents, so nothing is repeated or dropped.
        expected_seen = {_event(run_id) for run_id in live | covered}
        assert read_scope_seen_event_ids(self._storage, session, _SCOPE) == expected_seen


_ACTIONS = st.builds(
    Action,
    kind=st.sampled_from(
        ("add", "add", "add", "compact", "interrupt", "snapshot", "stale_write", "resurrect", "redact", "reopen"),
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
        Action("interrupt"),
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
    # Retiring a legacy summary for a live redaction leaves archived runs outside replay.
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
def test_generated_compaction_histories_keep_the_archive_invariants(actions: list[Action], *, legacy: bool) -> None:
    """Interleaved compaction, interruption, stale writes, and redaction never lose or leak history."""
    with tempfile.TemporaryDirectory() as root:
        runner = _Runner(Path(root), legacy=legacy)
        try:
            runner.run(actions)
        finally:
            runner.close()
