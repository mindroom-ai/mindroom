"""Background readers follow every Matrix turn of a session in its log order, compacted or not, once it completes."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from mindroom.constants import MATRIX_EVENT_ID_METADATA_KEY, MATRIX_SEEN_EVENT_IDS_METADATA_KEY
from mindroom.history import session_runs
from mindroom.history.session_runs import (
    Cursor,
    DueConversation,
    _read_conversation,
    due_conversations,
    matrix_turns,
    start_seq,
    unredacted,
    updated_sessions,
)
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus
from mindroom.session_storage import summary_rows
from mindroom.session_storage.agent_storage import create_agent_state_storage
from mindroom.session_storage.schema import quote_identifier
from mindroom.session_storage.store import ConversationStore, LoggedRun
from mindroom.session_storage.summary_rows import Fold
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session
from tests.history_helpers import compact_runs

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

ROOM = "!room:example.test"
SESSION = f"{ROOM}:$thread"
SCOPE = "agent:mind"


@pytest.fixture
def storage(tmp_path: Path) -> Iterator[ConversationStore]:
    """The production conversation storage owner."""
    db = create_agent_state_storage("mind", tmp_path)
    assert isinstance(db, ConversationStore)
    return db


def _run(
    run_id: str,
    created_at: int,
    *,
    room_id: str | None = ROOM,
    parent_run_id: str | None = None,
    status: RunStatus = RunStatus.completed,
    seen: tuple[str, ...] = (),
) -> RunRecord:
    metadata: dict[str, object] = {"room_id": room_id, "thread_id": "$thread"} if room_id is not None else {}
    return RunRecord(
        run_id=run_id,
        session_id=SESSION,
        agent_id="mind",
        parent_run_id=parent_run_id,
        created_at=created_at,
        status=status,
        metadata={
            **metadata,
            MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}", *seen],
        },
        messages=[Message(role="user", content=f"q {run_id}"), Message(role="assistant", content=f"a {run_id}")],
    )


def _session() -> ConversationSession:
    return ConversationSession(kind="agent", session_id=SESSION, owner_id="mind")


def _seed(storage: ConversationStore, runs: list[RunRecord], session_id: str = SESSION) -> ConversationSession:
    return seed_session(storage, ConversationSession(kind="agent", session_id=session_id, owner_id="mind", runs=runs))


def _save(storage: ConversationStore, *runs: RunRecord) -> None:
    for run in runs:
        storage.save_run(_session(), run, record_usage=False)


def _places(turns: list[LoggedRun]) -> list[tuple[int, str]]:
    return [(seq, run.run_id) for seq, run in turns]


def _cursor(updated_at: int = 0, retry_at: float = 0) -> Cursor:
    return {"updated_at": updated_at, "failures": 0, "failing_since": 0, "retry_at": retry_at}


def _journal(*redacted: str) -> session_runs.RedactedEventIds:
    async def journal(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del agent_name, room_id
        return frozenset(redacted) & frozenset(event_ids)

    return journal


def _set_updated_at(storage: ConversationStore, session_id: str, updated_at: int) -> None:
    with storage.transaction(write=True) as connection:
        connection.execute(
            f"UPDATE {quote_identifier(storage.names.sessions)} SET updated_at = ? WHERE session_id = ?",  # noqa: S608
            (updated_at, session_id),
        )


def test_matrix_turns_read_compacted_and_later_turns_in_log_order(storage: ConversationStore) -> None:
    """Turns a summary folded are still read, in log order with the later ones, each once."""
    session = _seed(storage, [_run("r1", 100), _run("r2", 200), _run("r3", 300)])
    compact_runs(storage, session, ["r1", "r2"], "summary")
    loaded = storage.load_session(SESSION)
    assert loaded is not None
    assert [run.run_id for run in loaded.runs or []] == ["r3"]

    assert _places(matrix_turns(storage, SESSION)) == [(1, "r1"), (2, "r2"), (3, "r3")]


def test_a_turn_approved_after_later_turns_takes_the_next_place(storage: ConversationStore) -> None:
    """A paused turn is listed where it waits, and once it completes it sorts after the turns it waited for."""
    paused = _run("asks", 100, status=RunStatus.paused)
    _save(storage, paused, _run("after", 200))
    assert _places(matrix_turns(storage, SESSION)) == [(1, "asks"), (2, "after")]

    paused.status = RunStatus.completed
    _save(storage, paused)

    assert _places(matrix_turns(storage, SESSION)) == [(2, "after"), (3, "asks")]


def test_unfinished_member_and_non_matrix_runs_are_excluded(storage: ConversationStore) -> None:
    """Only completed or paused top-level Matrix turns are listed."""
    _seed(
        storage,
        [
            _run("done", 100),
            _run("asks", 150, status=RunStatus.paused),
            _run("failed", 200, status=RunStatus.error),
            _run("stopped", 250, status=RunStatus.cancelled),
            _run("member", 300, parent_run_id="done"),
            _run("api", 400, room_id=None),
        ],
    )

    assert [run.run_id for _, run in matrix_turns(storage, SESSION)] == ["done", "asks"]


def test_start_seq_starts_before_the_first_completed_turn_that_is_not_older() -> None:
    """Older turns stay unread, a paused turn is read once it completes, and nothing new starts after the log."""
    turns = [
        LoggedRun(1, _run("old", 100)),
        LoggedRun(2, _run("asks", 300, status=RunStatus.paused)),
        LoggedRun(3, _run("new", 200)),
        LoggedRun(4, _run("late", 50)),
    ]

    assert start_seq(turns, older=lambda run: run.created_at < 150) == 2
    assert start_seq(turns, older=lambda run: run.created_at < 1000) == 4
    assert start_seq(turns[:2], older=lambda run: run.created_at < 150) == 2
    assert start_seq([], older=lambda _run: False) == 0


def test_updated_sessions_lists_sessions_changed_after_the_cursor(storage: ConversationStore) -> None:
    """Sessions are listed with their last update, oldest first, and workflow participant sessions are skipped."""
    _seed(storage, [_run("r1", 100)], session_id="!a:x:$t1")
    _seed(storage, [_run("r2", 100)], session_id="!a:x:$t1:dynamic_workflow:w:p")

    listed = updated_sessions(storage, since=0)

    assert [session_id for session_id, _ in listed] == ["!a:x:$t1"]
    assert updated_sessions(storage, since=listed[0][1]) == []


def test_the_due_scan_returns_quiet_conversations_with_news_and_keeps_others_out_of_the_limit(
    storage: ConversationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """News, a second of quiet, and no retry wait make a conversation due; empty and unreadable ones take no place."""
    names = ("1api", "2broken", "3caught", "4retry", "5skipped", "6first", "7second", "8busy")
    sessions = {name: f"{ROOM}:${name}" for name in names}
    for name, session_id in sessions.items():
        _seed(storage, [_run(f"r-{name}", 100, room_id=None if name == "1api" else ROOM)], session_id=session_id)
        _set_updated_at(storage, session_id, 1000)
    # A conversation updated in the pass's own second could be updated again within it, so it waits even without idle.
    _set_updated_at(storage, sessions["8busy"], 2000)
    real = session_runs._read_conversation

    def turns(store: ConversationStore, session_id: str, *, agent_name: str, updated_at: int) -> DueConversation:
        if session_id == sessions["2broken"]:
            msg = "corrupt session row"
            raise ValueError(msg)
        return real(store, session_id, agent_name=agent_name, updated_at=updated_at)

    monkeypatch.setattr(session_runs, "_read_conversation", turns)
    cursors = {sessions["3caught"]: _cursor(updated_at=1000), sessions["4retry"]: _cursor(retry_at=2001)}

    def scan(limit: int) -> session_runs._DueScan:
        return due_conversations(
            storage,
            cursors,
            agent_name="mind",
            since=0,
            now=2000,
            idle_seconds=0,
            limit=limit,
            skip=lambda session_id: session_id == sessions["5skipped"],
        )

    first = scan(1)
    assert [conversation.session_id for conversation in first.due] == [sessions["6first"]]
    assert _places(first.due[0].turns) == [(6, "r-6first")]
    assert first.empty == [(sessions["1api"], 1000)]
    assert [(unreadable.session_id, str(unreadable.error)) for unreadable in first.unreadable] == [
        (sessions["2broken"], "corrupt session row"),
    ]
    assert [conversation.session_id for conversation in scan(10).due] == [sessions["6first"], sessions["7second"]]


@pytest.mark.asyncio
async def test_unredacted_keeps_the_completed_turns_before_a_redaction_cut(storage: ConversationStore) -> None:
    """A turn that read a redacted event goes with every later turn, and nothing is returned while the agent cannot tell."""
    _save(storage, _run("kept", 100), _run("asks", 150, status=RunStatus.paused), _run("gone", 200), _run("later", 300))
    conversation = _read_conversation(storage, SESSION, agent_name="mind", updated_at=0)
    asked: list[tuple[str, str, tuple[str, ...]]] = []

    async def journal(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        asked.append((agent_name, room_id, event_ids))
        return frozenset({"$gone"})

    async def not_running(agent_name: str, room_id: str, event_ids: tuple[str, ...]) -> frozenset[str] | None:
        del agent_name, room_id, event_ids
        return None

    kept = await unredacted(conversation, "mind", journal)

    assert _places(kept or []) == [(1, "kept")]
    assert asked == [("mind", ROOM, ("$asks", "$gone", "$kept", "$later"))]
    assert await unredacted(conversation, "mind", not_running) is None
    assert _places(await unredacted(conversation, "mind", _journal()) or []) == [
        (1, "kept"),
        (3, "gone"),
        (4, "later"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [RunStatus.paused, RunStatus.cancelled, RunStatus.error])
async def test_a_turn_that_did_not_complete_and_read_a_redacted_message_keeps_later_turns_out(
    storage: ConversationStore,
    status: RunStatus,
) -> None:
    """The redaction removes the stopped, failed, or waiting turn that read the message and every turn after it."""
    _save(storage, _run("before", 100), _run("read", 200, status=status, seen=("$secret",)), _run("after", 300))

    kept = await unredacted(
        _read_conversation(storage, SESSION, agent_name="mind", updated_at=0),
        "mind",
        _journal("$secret"),
    )

    assert _places(kept or []) == [(1, "before")]


@pytest.mark.asyncio
async def test_a_turn_that_folded_before_its_pause_keeps_the_turns_after_its_fold_out(
    storage: ConversationStore,
) -> None:
    """A turn folds, pauses, a later turn completes, then the first completes; redacting what it folded cuts at its fold.

    The cut takes the resumed turn whole, so the turn placed between its fold and its completion goes too.
    """
    _save(storage, _run("first", 100))
    running = _run("approval", 200, status=RunStatus.running, seen=("$secret",))
    assert (
        summary_rows.append_summary(
            storage,
            session_id=SESSION,
            scope_key=SCOPE,
            based_on=None,
            covered=summary_rows.covered_runs(storage, session_id=SESSION, run_ids=["first"]),
            summary="S",
            fold=Fold(run=running, messages=(), member_run_ids=frozenset()),
        )
        is not None
    )
    running.status = RunStatus.paused
    _save(storage, running, _run("later", 300))
    running.status = RunStatus.completed
    _save(storage, running)
    conversation = _read_conversation(storage, SESSION, agent_name="mind", updated_at=0)
    assert _places(conversation.turns) == [(1, "first"), (3, "later"), (4, "approval")]

    kept = await unredacted(conversation, "mind", _journal("$secret"))

    assert _places(kept or []) == [(1, "first")]


@pytest.mark.asyncio
async def test_a_cut_while_the_journal_is_asked_still_keeps_the_turns_it_removed_out(
    storage: ConversationStore,
) -> None:
    """The agent's next reply can cut the redacted turns while the journal is asked; the reader decides from its snapshot.

    Reading the cut after the journal answered would find no turn holding the event and keep the removed turns.
    """
    _save(storage, _run("before", 100), _run("read", 200, seen=("$secret",)), _run("after", 300))
    conversation = _read_conversation(storage, SESSION, agent_name="mind", updated_at=0)

    async def journal_while_a_reply_cuts(
        agent_name: str,
        room_id: str,
        event_ids: tuple[str, ...],
    ) -> frozenset[str] | None:
        del agent_name, room_id, event_ids
        assert summary_rows.cut_at_event(storage, session_id=SESSION, scope_key=SCOPE, event_id="$secret", match="any")
        return frozenset({"$secret"})

    kept = await unredacted(conversation, "mind", journal_while_a_reply_cuts)

    assert _places(kept or []) == [(1, "before")]
    assert _places(matrix_turns(storage, SESSION)) == [(1, "before")]
