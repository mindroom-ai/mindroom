"""A mid-turn fold appends a summary row that covers stored runs and part of the running run, in the owned store.

Folds sort after the runs they cover and before their own run, load without what they folded, and go with the
turn when an edit or redaction cuts the history they built on.
"""

from __future__ import annotations

from copy import deepcopy
from typing import TYPE_CHECKING

import pytest

from mindroom import constants
from mindroom.history import summary_rows
from mindroom.history.replay import estimate_prompt_visible_history_tokens, estimate_request_messages_tokens
from mindroom.history.request_messages import TurnMessages, build_request, scope_history_messages
from mindroom.history.storage import (
    _remove_redacted_event_from_history,
    append_fold,
    ensure_scope_session_row,
    read_fold_basis,
    read_scope_seen_event_ids,
    remove_runs_answering,
)
from mindroom.history.summary_message import is_scope_summary, mark_summary_follows
from mindroom.history.summary_rows import Segment, folded_tool_call_ids, segment_tool_call_ids
from mindroom.history.types import HistoryPolicy, HistoryScope, ResolvedHistorySettings
from mindroom.llm.messages import Message
from mindroom.llm.runs import RunRecord, RunStatus, TeamRunRecord, ToolExecution
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.types import ConversationSession
from tests.conftest import seed_session
from tests.history_helpers import compact_runs, log_of

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from mindroom.llm.runs import AnyRunRecord
    from mindroom.session_storage.store import ConversationStore

_SCOPE = HistoryScope(kind="agent", scope_id="code")
_TEAM_SCOPE = HistoryScope(kind="team", scope_id="crew")
_ALL_HISTORY = ResolvedHistorySettings(policy=HistoryPolicy(mode="all"), max_tool_calls_from_history=None)


@pytest.fixture
def storage(tmp_path: Path) -> ConversationStore:
    """Use the production conversation storage owner."""
    return create_state_storage("code", tmp_path, session_table="code_sessions")


def _run(
    run_id: str,
    *,
    status: RunStatus = RunStatus.completed,
    messages: Sequence[Message] | None = None,
    seen: Sequence[str] = (),
) -> RunRecord:
    return RunRecord(
        run_id=run_id,
        agent_id="code",
        session_id="session",
        status=status,
        content=f"answer {run_id}",
        messages=list(messages) if messages is not None else [Message(role="user", content=f"question {run_id}")],
        metadata={
            constants.MATRIX_EVENT_ID_METADATA_KEY: f"${run_id}",
            constants.MATRIX_SEEN_EVENT_IDS_METADATA_KEY: [f"${run_id}", *seen],
        },
    )


def _session(run: AnyRunRecord | None = None) -> ConversationSession:
    """Return the session row a run of ``run``'s kind belongs to."""
    if isinstance(run, TeamRunRecord):
        return ConversationSession(session_id="session", kind="team", owner_id="crew")
    return ConversationSession(session_id="session", kind="agent", owner_id="code")


def _seed(storage: ConversationStore, runs: Sequence[RunRecord]) -> ConversationSession:
    return seed_session(
        storage,
        ConversationSession(session_id="session", kind="agent", owner_id="code", runs=list(runs)),
    )


def _save(storage: ConversationStore, run: AnyRunRecord, *, session: ConversationSession | None = None) -> None:
    storage.save_run(session or _session(run), run, record_usage=True)


def _fold(
    storage: ConversationStore,
    run: AnyRunRecord,
    summary: str,
    *,
    folded: Sequence[Message] = (),
    tools: Sequence[ToolExecution] = (),
    member_responses: Sequence[AnyRunRecord] = (),
) -> bool:
    """Fold the stored runs after the latest summary row and part of the running ``run``, as a mid-turn fold does."""
    scope = _TEAM_SCOPE if isinstance(run, TeamRunRecord) else _SCOPE
    session = _session(run)
    basis = read_fold_basis(storage, session, scope)
    assert basis is not None
    ensure_scope_session_row(storage, session, scope)
    return append_fold(
        storage,
        session=session,
        scope=scope,
        basis=basis,
        summary=summary,
        summary_model="model",
        segment=Segment(
            run_id=run.run_id,
            metadata=deepcopy(run.metadata or {}),
            messages=[message.to_dict() for message in folded],
            tools=[tool.to_dict() for tool in tools],
            member_responses=[member.to_dict() for member in member_responses],
        ),
    )


def _loaded(storage: ConversationStore) -> ConversationSession:
    session = storage.load_session("session", "agent")
    assert session is not None
    return session


def _redact(storage: ConversationStore, event_id: str) -> bool:
    return _remove_redacted_event_from_history(storage, _loaded(storage), _SCOPE, event_id=event_id)


def test_a_fold_sorts_after_the_runs_it_covers_and_its_run_after_it(storage: ConversationStore) -> None:
    """Replay is the fold's summary and the rest of the turn; nothing moved."""
    _seed(storage, [_run("r1"), _run("r2")])
    running = _run("live")

    assert _fold(storage, running, "S")
    _save(storage, running)

    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "run:r2", "row:S", "run:live"]
    loaded = _loaded(storage)
    assert [run.run_id for run in loaded.runs] == ["live"]
    assert loaded.summary is not None
    assert loaded.summary.text == "S"


def test_a_second_fold_without_a_stored_run_covers_no_run_twice(storage: ConversationStore) -> None:
    """The running loop still holds the runs it loaded; the log knows the first fold covered them."""
    _seed(storage, [_run("r1")])
    running = _run("live")

    assert _fold(storage, running, "S1")
    assert _fold(storage, running, "S2")
    _save(storage, running)

    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "row:S1", "row:S2", "run:live"]
    assert _loaded(storage).summary.text == "S2"  # type: ignore[union-attr]


def test_a_stored_paused_run_moves_after_its_fold(storage: ConversationStore) -> None:
    """A resumed approval folds the turns that happened while it waited, and sorts after them."""
    paused = _run("paused", status=RunStatus.paused)
    _seed(storage, [_run("r1"), paused, _run("r2")])

    assert _fold(storage, paused, "S")

    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "run:r2", "row:S", "run:paused"]
    # Still paused, it loads; once completed it stays after the row.
    assert [run.run_id for run in _loaded(storage).runs] == ["paused"]
    paused.status = RunStatus.completed
    _save(storage, paused)
    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "run:r2", "row:S", "run:paused"]


def test_a_run_loads_without_the_messages_its_segments_hold(storage: ConversationStore) -> None:
    """A copy saved before a fold, such as a paused approval, replays only what the fold left in the run."""
    prompt = Message(role="user", content="do it")
    call = Message(role="assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "probe"}}])
    result = Message(role="tool", tool_call_id="c1", content="probe output")
    later = Message(role="assistant", content="step after the fold")
    paused = _run("paused", status=RunStatus.paused, messages=[prompt, call, result, later])
    _seed(storage, [paused])

    assert _fold(storage, paused, "S", folded=[call, result])

    for _ in range(2):
        loaded = _loaded(storage)
        assert [message.content for message in loaded.runs[0].messages] == ["do it", "step after the fold"]


def test_a_team_run_loads_without_the_messages_its_segments_hold(storage: ConversationStore) -> None:
    """Team sessions' folded runs load the same way."""
    prompt = Message(role="user", content="do it")
    call = Message(role="assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "probe"}}])
    result = Message(role="tool", tool_call_id="c1", content="probe output")
    later = Message(role="assistant", content="step after the fold")
    team_run = TeamRunRecord(
        run_id="team-paused",
        team_id="crew",
        session_id="session",
        status=RunStatus.paused,
        messages=[prompt, call, result, later],
    )
    seed_session(storage, ConversationSession(session_id="session", kind="team", owner_id="crew", runs=[team_run]))
    assert _fold(storage, team_run, "T", folded=[call, result])
    team_loaded = storage.load_session("session", "team")
    assert team_loaded is not None
    stored_team_run = next(run for run in team_loaded.runs if run.run_id == "team-paused")
    assert [message.content for message in stored_team_run.messages] == ["do it", "step after the fold"]


def test_redacting_an_event_only_the_running_turn_read_cuts_at_its_fold(storage: ConversationStore) -> None:
    """The running turn has no stored row yet, so its fold row carries what it read, and the cut fences the run."""
    _seed(storage, [_run("r1")])
    running = _run("live", seen=["$context"])
    assert _fold(storage, running, "S")
    _save(storage, _run("r3"))

    assert _redact(storage, "$context")

    assert log_of(storage, _SCOPE.key, "session") == ["run:r1"]
    # The turn's own save, once it ends, writes nothing: its first part is gone.
    _save(storage, running)
    assert log_of(storage, _SCOPE.key, "session") == ["run:r1"]
    assert _loaded(storage).summary is None


def test_an_edit_cuts_at_a_fold_only_for_the_message_its_turn_answered(storage: ConversationStore) -> None:
    """A message the running turn only read is no reply of it, so editing that message keeps the fold."""
    _seed(storage, [_run("r1")])
    running = _run("live", seen=["$context"])
    assert _fold(storage, running, "S")

    assert not remove_runs_answering(storage, "session", _SCOPE, "$context")
    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "row:S"]
    assert remove_runs_answering(storage, "session", _SCOPE, "$live")
    assert log_of(storage, _SCOPE.key, "session") == ["run:r1"]


def test_a_cut_takes_a_run_that_folded_on_both_sides_of_its_pause_whole(storage: ConversationStore) -> None:
    """Redacting a turn the resumed run folded removes that run, its earlier fold, and everything after it."""
    _seed(storage, [_run("r1")])
    running = _run("approval")
    assert _fold(storage, running, "S1")
    running.status = RunStatus.paused
    _save(storage, running)
    _save(storage, _run("r2"))
    _save(storage, _run("r3"))
    assert _fold(storage, running, "S2")
    assert log_of(storage, _SCOPE.key, "session") == [
        "run:r1",
        "row:S1",
        "run:r2",
        "run:r3",
        "row:S2",
        "run:approval",
    ]

    assert _redact(storage, "$r3")

    assert log_of(storage, _SCOPE.key, "session") == ["run:r1"]
    loaded = _loaded(storage)
    assert [run.run_id for run in loaded.runs] == ["r1"]
    assert loaded.summary is None


def test_editing_a_turn_an_older_resumed_run_folded_removes_that_run(storage: ConversationStore) -> None:
    """The resumed run's fold summarized the edited turn, so the run built on it goes; the runs before both stay."""
    paused = _run("paused", status=RunStatus.paused)
    _seed(storage, [_run("r1")])
    _save(storage, paused)
    _save(storage, _run("r2"))
    compact_runs(storage, _loaded(storage), _SCOPE, ["r1"], "S0")
    assert _fold(storage, paused, "S1")
    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "row:S0", "run:r2", "row:S1", "run:paused"]

    assert remove_runs_answering(storage, "session", _SCOPE, "$r2")

    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "row:S0"]
    assert _loaded(storage).summary.text == "S0"  # type: ignore[union-attr]


def test_editing_a_retried_turn_whose_second_attempt_folded_the_first_removes_both(storage: ConversationStore) -> None:
    """Both attempts answered the edited message, so neither the first attempt nor the fold survives."""
    first = _run("attempt-1")
    _seed(storage, [_run("r0"), first])
    second = _run("attempt-2")
    assert second.metadata is not None
    second.metadata[constants.MATRIX_EVENT_ID_METADATA_KEY] = "$attempt-1"
    assert _fold(storage, second, "S")
    _save(storage, second)

    assert remove_runs_answering(storage, "session", _SCOPE, "$attempt-1")

    assert log_of(storage, _SCOPE.key, "session") == ["run:r0"]


def test_editing_a_message_a_later_bot_triggered_turn_folded_removes_that_turn(storage: ConversationStore) -> None:
    """A later turn's fold covered the edited turn, so the edit removes the fold and the later turn."""
    _seed(storage, [_run("r0"), _run("r1")])
    later = _run("bot-turn")
    assert _fold(storage, later, "S")
    _save(storage, later)

    assert remove_runs_answering(storage, "session", _SCOPE, "$r1")

    assert log_of(storage, _SCOPE.key, "session") == ["run:r0"]


def test_a_fold_is_refused_once_a_cut_removed_its_run(storage: ConversationStore) -> None:
    """A redaction that removed the running turn's first fold keeps every later fold of that turn out too."""
    _seed(storage, [_run("r1")])
    running = _run("live", seen=["$context"])
    assert _fold(storage, running, "S1")
    assert _redact(storage, "$context")

    assert not _fold(storage, running, "S2")
    assert log_of(storage, _SCOPE.key, "session") == ["run:r1"]


def test_a_fold_is_refused_when_another_compaction_appended_first(storage: ConversationStore) -> None:
    """A pre-reply compaction that commits during a long turn's summary call wins; the fold builds on stale history."""
    session = _seed(storage, [_run("r1"), _run("r2")])
    running = _run("live")
    basis = read_fold_basis(storage, _session(), _SCOPE)
    assert basis is not None
    compact_runs(storage, session, _SCOPE, ["r1"], "S-pre-reply")

    assert not append_fold(
        storage,
        session=session,
        scope=_SCOPE,
        basis=basis,
        summary="S-fold",
        summary_model="model",
        segment=Segment(run_id="live", metadata=running.metadata or {}, messages=[], tools=[], member_responses=[]),
    )
    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "row:S-pre-reply", "run:r2"]


def test_a_fold_racing_a_retirement_is_refused(storage: ConversationStore) -> None:
    """Retirement appends a barrier, so a fold read before it would summarize retired history again."""
    session = _seed(storage, [_run("r1")])
    basis = read_fold_basis(storage, _session(), _SCOPE)
    assert basis is not None
    summary_rows.retire(storage, session_id="session", scope_key=_SCOPE.key)

    assert not append_fold(
        storage,
        session=session,
        scope=_SCOPE,
        basis=basis,
        summary="S",
        summary_model="model",
        segment=Segment(run_id="live", metadata={}, messages=[], tools=[], member_responses=[]),
    )


def test_retiring_summaries_removes_runs_that_folded_mid_turn(storage: ConversationStore) -> None:
    """A run whose stored part starts mid-turn would replay without what it built on once its summary retires."""
    _seed(storage, [_run("r1")])
    done = _run("done")
    assert _fold(storage, done, "S1")
    _save(storage, done)
    running = _run("live")
    assert _fold(storage, running, "S2")

    summary_rows.retire(storage, session_id="session", scope_key=_SCOPE.key)

    assert log_of(storage, _SCOPE.key, "session") == ["run:r1", "row:S1", "row:S2", "row:None"]
    _save(storage, running)
    assert "run:live" not in log_of(storage, _SCOPE.key, "session")
    assert _loaded(storage).summary is None


def test_a_fold_survives_a_crash_and_a_late_pre_fold_copy_loads_without_what_it_folded(
    storage: ConversationStore,
) -> None:
    """The restarted attempt replays the fold's summary; a copy saved from before the fold cannot replay it twice."""
    _seed(storage, [_run("r1"), _run("r2")])
    call = Message(role="assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "probe"}}])
    result = Message(role="tool", tool_call_id="c1", content="probe output")
    crashed = _run("crashed", messages=[Message(role="user", content="do it"), call, result])
    assert _fold(storage, crashed, "S", folded=[call, result])

    loaded = _loaded(storage)
    assert loaded.summary is not None
    assert loaded.summary.text == "S"
    assert loaded.runs == []
    crashed.status = RunStatus.cancelled
    _save(storage, crashed)
    stored = next(run for run in _loaded(storage).runs if run.run_id == "crashed")
    assert [message.content for message in stored.messages] == ["do it"]


def test_a_fold_keeps_the_events_its_turn_read_seen_after_the_turn_stops(storage: ConversationStore) -> None:
    """Thread context a stopped turn folded into the summary does not return as unseen context."""
    _seed(storage, [_run("r1")])
    running = _run("live", seen=["$context"])
    assert _fold(storage, running, "S")
    running.status = RunStatus.cancelled
    _save(storage, running)

    assert "$context" in read_scope_seen_event_ids(storage, _loaded(storage), _SCOPE)


def test_redacting_a_team_turn_that_folded_removes_its_member_runs(storage: ConversationStore) -> None:
    """A team turn's fold goes with the turn, and its delegated member runs go with it."""
    member = RunRecord(run_id="member", agent_id="helper", session_id="session", parent_run_id="team-run")
    team_run = TeamRunRecord(
        run_id="team-run",
        team_id="crew",
        session_id="session",
        metadata={constants.MATRIX_EVENT_ID_METADATA_KEY: "$team"},
        member_responses=[member],
    )
    team_session = ConversationSession(
        session_id="session",
        kind="team",
        owner_id="crew",
        runs=[TeamRunRecord(run_id="t0", team_id="crew", session_id="session")],
    )
    seed_session(storage, team_session)
    assert _fold(storage, team_run, "S", member_responses=[member])
    _save(storage, team_run)
    _save(storage, member, session=team_session)
    session = storage.load_session("session", "team")
    assert session is not None

    assert _remove_redacted_event_from_history(storage, session, _TEAM_SCOPE, event_id="$team")

    assert log_of(storage, _TEAM_SCOPE.key, "session") == ["run:t0"]
    reloaded = storage.load_session("session", "team")
    assert reloaded is not None
    assert [run.run_id for run in reloaded.runs] == ["t0"]


def test_a_runs_summary_rows_name_every_tool_call_its_folds_summarized(storage: ConversationStore) -> None:
    """Every interrupted record write reads the folded calls from the rows, so they stay out after a restart too."""
    call = Message(role="assistant", tool_calls=[{"id": "c1", "type": "function", "function": {"name": "probe"}}])
    nested = TeamRunRecord(
        run_id="nested",
        team_id="inner",
        member_responses=[RunRecord(run_id="deep", tools=[ToolExecution(tool_call_id="deep-call")])],
    )
    member = RunRecord(run_id="member", tools=[ToolExecution(tool_call_id="member-call")])
    _seed(storage, [])
    running = _run("live")
    assert _fold(storage, running, "S1", folded=[call], tools=[ToolExecution(tool_call_id="execution-call")])
    assert _fold(storage, running, "S2", member_responses=[member, nested])

    assert segment_tool_call_ids(storage, session_id="session", run_id="live") == {
        "c1",
        "execution-call",
        "member-call",
        "deep-call",
    }
    assert segment_tool_call_ids(storage, session_id="session", run_id="other") == set()
    assert folded_tool_call_ids({}) == set()


def test_a_fold_completed_after_a_later_turn_replays_its_summary_first(storage: ConversationStore) -> None:
    """A fold's prompt keeps the summary after it only while its run replays first.

    An approval that completes after a later turn sorts behind that turn, which was built on the summary, so the
    summary replays first again, ahead of that turn, as replay planning counts.
    """
    summary = "Completed work. " * 500
    _seed(storage, [])
    prompt = Message(role="user", content="Task A")
    mark_summary_follows(prompt)
    paused = _run("a", status=RunStatus.paused, messages=[prompt, Message(role="assistant", content="A done")])
    assert _fold(storage, paused, summary)
    _save(storage, paused)
    _save(
        storage,
        _run("b", messages=[Message(role="user", content="Task B"), Message(role="assistant", content="B done")]),
    )
    paused.status = RunStatus.completed
    _save(storage, paused)
    session = _loaded(storage)
    assert [run.run_id for run in session.runs] == ["b", "a"]

    replayed = scope_history_messages(
        session.runs,
        scope=_SCOPE,
        num_history_runs=None,
        num_history_messages=None,
        max_tool_calls_from_history=None,
    )
    built = build_request(
        system=[Message(role="system", content="You are Code.")],
        summary=session.summary,
        history=replayed,
        turn=TurnMessages(prompt=Message(role="user", content="Task C")),
    )

    history = [message for message in built if message.role != "system"]
    assert is_scope_summary(history[0])
    assert history[1].content == "Task B"
    assert [message.content for message in history[-3:]] == ["Task A", "A done", "Task C"]
    planned = estimate_prompt_visible_history_tokens(session=session, scope=_SCOPE, history_settings=_ALL_HISTORY)
    assert estimate_request_messages_tokens([m for m in built if m.from_history], replay_model=None) == planned
