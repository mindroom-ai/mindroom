"""Driver-level tests for the shared response-turn seam."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any
from unittest.mock import Mock

import pytest
from structlog.testing import capture_logs

from mindroom import response_turn as response_turn_module
from mindroom.ai_runtime import EMPTY_RESPONSE_NOTICE, queued_message_signal_context
from mindroom.constants import (
    MATRIX_EVENT_ID_METADATA_KEY,
    MATRIX_RESPONSE_EVENT_ID_METADATA_KEY,
    MATRIX_SEEN_EVENT_IDS_METADATA_KEY,
)
from mindroom.delegation.state import DELEGATION_STATE_KEY, DelegationChild, DelegationPendingTool, DelegationState
from mindroom.helper_usage import get_helper_usage_owner
from mindroom.history import storage as history_storage
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunRecord, RunRequirement, RunStatus, TeamRunRecord, ToolExecution
from mindroom.participation import ParticipationGate
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    HandledAttempt,
    ResponseTurnContext,
    SkippedAttempt,
    StreamAttemptResolution,
    TurnAdapter,
    TurnContinuationRunState,
    TurnPartialSnapshot,
    TurnResume,
    TurnRunState,
    TurnSinks,
    is_empty_reply,
    replays_as_turn,
    stream_response_turn,
)
from mindroom.session_storage.agent_storage import create_state_storage
from mindroom.session_storage.store import ConversationStore
from mindroom.session_storage.types import ConversationSession
from mindroom.tool_system.events import ToolTraceEntry
from tests.approval_continuation_helpers import answered_calls
from tests.conftest import seed_session

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from pathlib import Path

    from mindroom.history.interrupted_replay import InterruptedReplaySnapshot


@dataclass
class _FakeTurnRecorder(TurnRecorder):
    """The real turn record, also logging each terminal record the driver writes into it."""

    completed_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupted_calls: list[dict[str, Any]] = field(default_factory=list)

    def record_completed(
        self,
        *,
        run_id: str | None,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
    ) -> None:
        self.completed_calls.append(
            {
                "run_metadata": run_metadata,
                "assistant_text": assistant_text,
                "completed_tools": list(completed_tools),
            },
        )
        super().record_completed(
            run_id=run_id,
            run_metadata=run_metadata,
            assistant_text=assistant_text,
            completed_tools=completed_tools,
        )

    def record_interrupted(
        self,
        *,
        run_id: str | None,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
        original_status: RunStatus,
    ) -> None:
        self.interrupted_calls.append(
            {
                "run_metadata": run_metadata,
                "assistant_text": assistant_text,
                "completed_tools": list(completed_tools),
                "interrupted_tools": list(interrupted_tools),
            },
        )
        super().record_interrupted(
            run_id=run_id,
            run_metadata=run_metadata,
            assistant_text=assistant_text,
            completed_tools=completed_tools,
            interrupted_tools=interrupted_tools,
            original_status=original_status,
        )


@pytest.fixture(autouse=True)
def persisted(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, InterruptedReplaySnapshot]]:
    """Capture each interrupted record the driver stores into its turn's scope, with the run id it stores it under."""
    snapshots: list[tuple[str, InterruptedReplaySnapshot]] = []

    def _persist(*, run_id: str, snapshot: InterruptedReplaySnapshot, **_kwargs: object) -> None:
        snapshots.append((run_id, snapshot))

    monkeypatch.setattr(response_turn_module, "persist_interrupted_replay_snapshot", _persist)
    return snapshots


def _trace(tool_name: str) -> ToolTraceEntry:
    return ToolTraceEntry(type="tool_call_completed", tool_name=tool_name)


def _model_switch_execution(when: str) -> ToolExecution:
    return ToolExecution(
        tool_call_id="call-switch-model",
        tool_name="switch_thread_model",
        tool_args={"model_name": "large", "when": when},
        result=json.dumps(
            {
                "action": "switch",
                "model": "large",
                "status": "ok",
                "tool": "thread_model",
                "when": when,
            },
        ),
        stop_after_call=True,
    )


def _ctx(**overrides: object) -> ResponseTurnContext:
    values: dict[str, Any] = {
        "entity_label": "helper",
        "session_id": "session-1",
        "run_id": "run-1",
        "correlation_id": "corr-1",
        "reply_to_event_id": "$reply",
        "room_id": "!room",
        "thread_id": "$thread",
        "requester_id": "@user:hs",
        "matrix_run_metadata": {"correlation_id": "corr-1"},
    }
    values.update(overrides)
    return ResponseTurnContext(**values)


def _continuation(prompt: str = "hello") -> TurnContinuationRunState:
    return TurnContinuationRunState.initial(
        prompt=prompt,
        model_prompt=None,
        current_timestamp_ms=None,
        current_prompt_is_structured=False,
        current_event_id=None,
        run_id="run-1",
        continuation_model_prompt_tail="",
    )


# The name the fake adapter's queued notices carry: a configured team's, unlike the context's label.
_ENTITY_NAME = "squad"


@dataclass
class _AdapterLog:
    """The scope the fake adapter loaded."""

    scope: Any = None


@dataclass(frozen=True)
class _FixedAttempt:
    """A running attempt whose partial output is fixed."""

    snapshot: TurnPartialSnapshot = field(default_factory=TurnPartialSnapshot)

    def partial_snapshot(self) -> TurnPartialSnapshot:
        return self.snapshot


def _load_scope_factory(log: _AdapterLog) -> Callable[[], ScopeSessionContext]:
    def _load() -> ScopeSessionContext:
        log.scope = ScopeSessionContext(
            scope=HistoryScope(kind="agent", scope_id="general"),
            storage=Mock(spec=ConversationStore),
            session=None,
            session_id="session-1",
            storage_factory=lambda: Mock(spec=ConversationStore),
        )
        return log.scope

    return _load


def test_paused_attempt_from_team_requirement_keeps_invoking_member_identity() -> None:
    """Dropping the member requirement would evaluate and resume a team call under the wrong agent."""
    tool = ToolExecution(
        tool_call_id="call-member",
        tool_name="dangerous",
        tool_args={"value": 1},
        requires_confirmation=True,
    )
    requirement = RunRequirement(tool)
    requirement.member_agent_id = "member-id"
    requirement.member_display_name = "researcher"
    requirement.member_run_id = "member-run"
    response = TeamRunRecord(
        run_id="team-run",
        session_id="team-session",
        status=RunStatus.paused,
        tools=[],
        requirements=[requirement],
        team_id=None,
    )

    paused = response_turn_module.paused_attempt_from_response(
        response,
        toolkit_owners={},
    )

    assert paused is not None
    assert (paused.session_id, paused.run_id) == ("team-session", "team-run")
    assert paused.tools == (tool,)
    assert paused.requirements == (requirement,)
    assert paused.requirements[0].member_display_name == "researcher"


def test_a_paused_run_without_a_session_fails_instead_of_losing_its_pause() -> None:
    """The pause is resumed from the session its record names, so a record that names none cannot pause a reply."""
    tool = ToolExecution(tool_call_id="call", tool_name="dangerous", requires_confirmation=True)
    record = RunRecord(run_id="run-1", status=RunStatus.paused, tools=[tool], requirements=[RunRequirement(tool)])

    with pytest.raises(RuntimeError, match="names no session"):
        response_turn_module.paused_attempt_from_response(record, toolkit_owners={})


@pytest.mark.parametrize(
    ("recorded", "owners"),
    [("files", {("worker", "write"): "files"}), (None, {})],
)
def test_a_paused_child_s_toolkit_owners_are_the_toolkits_its_sources_recorded(
    recorded: str | None,
    owners: dict[tuple[str, str], str],
) -> None:
    """A source that recorded no toolkit leaves its call unowned, so the owner map never holds a missing owner."""
    tool = ToolExecution(tool_call_id="child-card", tool_name="write", tool_args={}, requires_confirmation=True)
    child = DelegationChild(
        delegation_id="delegation",
        parent_tool_call_id="parent-call",
        caller_agent_name="leader",
        child_agent_name="worker",
        task="write it",
        session_id="child-session",
        run_id="child-run",
        model_name="default",
        requested_model_name="default",
        depth=1,
        execution_identity={},
        agent_mode="standard",
        persona=None,
    )
    state = DelegationState(
        pending_tools=[tool.to_dict()],
        pending_tool_sources={"child-card": DelegationPendingTool(child, "child-call", toolkit_name=recorded)},
        pending_requirements=[RunRequirement(tool).to_dict()],
        pending_child_id="delegation",
    )
    response = RunRecord(
        run_id="parent-run",
        session_id="parent-session",
        status=RunStatus.paused,
        metadata={DELEGATION_STATE_KEY: state.to_dict()},
    )

    paused = response_turn_module.paused_attempt_from_response(
        response,
        toolkit_owners={},
    )

    assert paused is not None
    assert (paused.session_id, paused.run_id) == ("parent-session", "parent-run")
    assert paused.toolkit_owners == owners


@pytest.mark.parametrize(
    ("reason", "note"),
    [("Alice declined it.", "Alice declined it."), (None, "Not approved by requester")],
)
def test_a_denial_is_recorded_with_the_reason_given_or_the_default_note(reason: str | None, note: str) -> None:
    """The model is told the requester's reason, or that the call was not approved when there is none."""
    requirement = RunRequirement(ToolExecution(tool_call_id="call", tool_name="dangerous", requires_confirmation=True))

    [decided] = response_turn_module.apply_exact_approval_decisions(
        [requirement],
        answered_calls(["call"], approved=False, reason=reason),
    )

    assert decided.confirmation_note == note
    assert decided.tool_execution.confirmation_note == note


@pytest.mark.parametrize("decided_ids", [["first"], ["first", "second", "unasked"], ["first", "first-again"]])
def test_decided_calls_that_do_not_name_exactly_the_waiting_calls_decide_nothing(decided_ids: list[str]) -> None:
    """A decision for a call nobody waits for, or none for one that waits, is refused before any call is decided."""
    requirements = [
        RunRequirement(ToolExecution(tool_call_id=call_id, tool_name="dangerous", requires_confirmation=True))
        for call_id in ("first", "second")
    ]

    with pytest.raises(RuntimeError, match="no longer match the approval continuation"):
        response_turn_module.apply_exact_approval_decisions(requirements, answered_calls(decided_ids, approved=True))

    assert [requirement.confirmation for requirement in requirements] == [None, None]


def test_paused_attempt_rejects_duplicate_requirement_call_ids() -> None:
    """One approval decision must never authorize two requirements sharing an ID."""
    requirements = [
        RunRequirement(ToolExecution(tool_call_id="duplicate-call", tool_name=tool_name, requires_confirmation=True))
        for tool_name in ("first", "second")
    ]

    paused_record = RunRecord(
        run_id="run-1",
        session_id="session-1",
        status=RunStatus.paused,
        requirements=requirements,
    )

    with pytest.raises(RuntimeError, match="duplicate tool-call IDs"):
        response_turn_module.paused_attempt_from_response(
            paused_record,
            toolkit_owners={},
        )


@pytest.mark.asyncio
async def test_streaming_paused_attempt_escapes_without_recording_terminal_interruption(
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """Streaming must release the response lifecycle at the same persisted pause boundary."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    tool = ToolExecution(
        tool_call_id="call-stream",
        tool_name="dangerous",
        tool_args={"value": 1},
        requires_confirmation=True,
    )
    paused = response_turn_module.PausedAttempt(
        session_id="session-1",
        run_id="run-stream",
        tools=(tool,),
        toolkit_owners={("general", "dangerous"): "test_toolkit"},
    )

    async def paused_attempt(
        _run: TurnRunState,
        _continuation_state: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(paused)

    with pytest.raises(response_turn_module.ResponsePausedForApproval) as raised:
        await _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, paused_attempt),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            ),
        )

    assert raised.value.paused.run_id == "run-stream"
    assert recorder.outcome == "suspended"
    # The paused run owns the turn; no interrupted record overwrites it.
    assert persisted == []


def _streaming_adapter(
    log: _AdapterLog,
    run_attempt: Callable[
        [TurnRunState, TurnContinuationRunState],
        AsyncGenerator[str | AttemptResolved, None],
    ],
    *,
    load_scope: Callable[[], ScopeSessionContext] | None = None,
    unexpected_error_text: Callable[[Exception], str] = lambda error: f"failed: {error}",
) -> TurnAdapter[str]:
    return TurnAdapter[str](
        load_scope=load_scope or _load_scope_factory(log),
        run_attempt=run_attempt,
        make_text_chunk=lambda text: f"notice:{text}",
        user_message="hello",
        unexpected_error_text=unexpected_error_text,
        entity_name=_ENTITY_NAME,
    )


async def _collect(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


def _streamed(
    attempt: Callable[[TurnRunState, TurnContinuationRunState], Awaitable[StreamAttemptResolution]],
) -> Callable[[TurnRunState, TurnContinuationRunState], AsyncGenerator[str | AttemptResolved, None]]:
    """Run ``attempt`` as an attempt that streams nothing before it resolves."""

    async def _stream(
        run: TurnRunState,
        continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(await attempt(run, continuation))

    return _stream


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["exhaust", "close"])
async def test_helper_usage_owner_stays_inside_attempt_across_task_handoff(finish: str) -> None:
    """The caller's owner must follow each attempt pull and close without leaking at public yields."""
    log = _AdapterLog()
    storage = Mock(spec=ConversationStore)
    scope = ScopeSessionContext(
        scope=HistoryScope(kind="agent", scope_id="general"),
        storage=storage,
        session=None,
        session_id="session-1",
        storage_factory=lambda: storage,
    )
    observed_phases: list[str] = []

    def observe_owner(phase: str) -> None:
        owner = get_helper_usage_owner()
        assert owner is not None
        assert owner.session_id == "session-1"
        assert owner.storage_factory is scope.storage_factory
        observed_phases.append(phase)

    async def streamed_attempt(
        _run: TurnRunState,
        _continuation_state: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        try:
            observe_owner("first")
            yield "first"
            observe_owner("second")
            yield AttemptResolved(CompletedAttempt(replayable_text="first", has_visible_content=True))
        finally:
            observe_owner("close")

    assert get_helper_usage_owner() is None
    stream = stream_response_turn(
        _ctx(),
        _streaming_adapter(log, streamed_attempt, load_scope=lambda: scope),
        TurnSinks(),
        continuation=_continuation(),
    )
    try:
        assert await anext(stream) == "first"
        assert get_helper_usage_owner() is None

        async def finish_in_child() -> None:
            if finish == "close":
                await stream.aclose()
            else:
                assert await _collect(stream) == []
            assert get_helper_usage_owner() is None

        await asyncio.create_task(finish_in_child())
    finally:
        await stream.aclose()
    assert observed_phases == (["first", "close"] if finish == "close" else ["first", "second", "close"])
    assert get_helper_usage_owner() is None


@pytest.mark.asyncio
async def test_scope_session_loads_off_event_loop() -> None:
    """A slow synchronous session load must not stall every response."""
    log = _AdapterLog()
    event_loop_thread = threading.get_ident()
    load_threads: list[int] = []

    def _load_scope() -> ScopeSessionContext:
        load_threads.append(threading.get_ident())
        return _load_scope_factory(log)()

    async def _attempt(
        _run: TurnRunState,
        _continuation_state: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(CompletedAttempt(replayable_text="done", has_visible_content=True))

    assert (
        await _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt, load_scope=_load_scope),
                TurnSinks(),
                continuation=_continuation(),
            ),
        )
        == []
    )
    assert len(load_threads) == 1
    assert load_threads[0] != event_loop_thread


@pytest.mark.asyncio
async def test_cancel_during_scope_load_waits_for_the_load() -> None:
    """Cancellation waits for an accepted scope load to finish and starts no attempt."""
    log = _AdapterLog()
    enter_started = threading.Event()
    allow_enter = threading.Event()
    loaded = threading.Event()

    def _load_scope() -> ScopeSessionContext:
        enter_started.set()
        if not allow_enter.wait(timeout=5):
            pytest.fail("scope load was never released")
        loaded.set()
        return _load_scope_factory(log)()

    async def _unexpected_attempt(
        _run: TurnRunState,
        _continuation_state: TurnContinuationRunState,
    ) -> CompletedAttempt:
        pytest.fail("a cancelled scope load must not start an attempt")

    turn_task = asyncio.create_task(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(_unexpected_attempt), load_scope=_load_scope),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )
    assert await asyncio.to_thread(enter_started.wait, 1)
    turn_task.cancel()
    allow_enter.set()
    with pytest.raises(asyncio.CancelledError):
        await turn_task

    assert loaded.is_set()


def _history_run(run_id: str, *, team: bool, event_id: str, seen: list[str]) -> RunRecord | TeamRunRecord:
    metadata: dict[str, Any] = {
        MATRIX_EVENT_ID_METADATA_KEY: event_id,
        MATRIX_SEEN_EVENT_IDS_METADATA_KEY: seen,
        MATRIX_RESPONSE_EVENT_ID_METADATA_KEY: f"{event_id}-reply",
    }
    if team:
        return TeamRunRecord(
            run_id=run_id,
            team_id="crew",
            session_id="session-1",
            status=RunStatus.completed,
            metadata=metadata,
        )
    return RunRecord(
        run_id=run_id,
        agent_id="crew",
        session_id="session-1",
        status=RunStatus.completed,
        metadata=metadata,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("redacted_event_id", ["$older", "$newer-reply"])
@pytest.mark.parametrize("team", [False, True])
async def test_turn_removes_history_derived_from_a_redacted_event_no_turn_owns(
    tmp_path: Path,
    redacted_event_id: str,
    team: bool,
) -> None:
    """A message a newer one superseded never gets a turn, yet the newer turn's run read it as context.

    Redacting it, or the reply that read it, must remove that run and everything after it
    before the history is used again, while the history before it stays.
    """
    storage = create_state_storage("crew", tmp_path, HistoryScope(kind="team" if team else "agent", scope_id="crew"))
    runs = [
        _history_run("earlier", team=team, event_id="$earlier", seen=["$earlier"]),
        _history_run("newer", team=team, event_id="$newer", seen=["$older", "$newer"]),
        _history_run("later", team=team, event_id="$later", seen=["$later"]),
    ]
    seed_session(
        storage,
        ConversationSession(session_id="session-1", kind="team" if team else "agent", owner_id="crew", runs=runs),
    )

    def _stored_session() -> ConversationSession | None:
        return storage.load_session("session-1")

    lookups: list[tuple[str, ...]] = []

    async def _redacted_history_events(event_ids: tuple[str, ...]) -> dict[str, str | None]:
        lookups.append(event_ids)
        return dict.fromkeys(set(event_ids) & {redacted_event_id})

    def _load_scope() -> ScopeSessionContext:
        return ScopeSessionContext(
            scope=HistoryScope(kind="team" if team else "agent", scope_id="crew"),
            storage=storage,
            session=_stored_session(),
            session_id="session-1",
            storage_factory=lambda: storage,
        )

    attempt_history: list[list[str | None]] = []

    async def _record_history(run: TurnRunState, _continuation_state: TurnContinuationRunState) -> CompletedAttempt:
        assert run.scope_context is not None
        assert run.scope_context.session is not None
        attempt_history.append([history_run.run_id for history_run in run.scope_context.session.runs or []])
        return CompletedAttempt(response_text="done", replayable_text="done", has_visible_content=True)

    log = _AdapterLog()
    ctx = _ctx(redacted_history_events=_redacted_history_events)
    for _turn in range(2):
        await _collect(
            stream_response_turn(
                ctx,
                _streaming_adapter(log, _streamed(_record_history), load_scope=_load_scope),
                TurnSinks(),
                continuation=_continuation(),
            ),
        )

    assert redacted_event_id in lookups[0]
    # Nothing that derived from the redacted event is left for the next turn to find.
    assert redacted_event_id not in lookups[1]
    assert attempt_history == [["earlier"], ["earlier"]]
    stored = _stored_session()
    assert stored is not None
    assert [history_run.run_id for history_run in stored.runs or []] == ["earlier"]


@pytest.mark.asyncio
async def test_history_cleanup_repeats_until_a_pass_removes_nothing_new(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rollback can restore older runs that read another redacted event; the same response removes those too."""
    histories = iter([{"$x", "$kept"}, {"$z", "$kept"}, {"$z", "$kept"}])
    removed: list[str] = []

    def read_history(*_args: object) -> set[str]:
        return next(histories)

    def remove(*_args: object, event_id: str, legacy_source_event_id: str | None, **_kwargs: object) -> bool:
        assert legacy_source_event_id is None
        removed.append(event_id)
        return True

    async def redacted_history_events(event_ids: tuple[str, ...]) -> dict[str, str | None]:
        return dict.fromkeys(set(event_ids) & {"$x", "$z"})

    monkeypatch.setattr(history_storage, "_read_scope_history_event_ids", read_history)
    monkeypatch.setattr(history_storage, "_remove_redacted_event_from_history", remove)
    scope_context = ScopeSessionContext(
        scope=HistoryScope(kind="agent", scope_id="general"),
        storage=Mock(spec=ConversationStore),
        session=ConversationSession(session_id="session-1", kind="agent", owner_id="general"),
        session_id="session-1",
        storage_factory=lambda: Mock(spec=ConversationStore),
    )

    await response_turn_module._remove_history_of_redacted_events(
        _ctx(redacted_history_events=redacted_history_events),
        scope_context,
    )

    # The third pass still sees "$z" but has nothing new to remove, so it stops.
    assert removed == ["$x", "$z"]


def test_completion_records_and_updates_collector() -> None:
    """A completed attempt records the turn and publishes run metadata once."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    collector: dict[str, Any] = {}
    trace = _trace("search")

    async def _attempt(run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        run.run_metadata = {"room_id": "!room"}
        return CompletedAttempt(
            response_text="visible",
            replayable_text="replayable",
            has_visible_content=True,
            completed_tools=(trace,),
            build_run_metadata=lambda: {"io.mindroom.ai_run": {"usage": 1}},
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(_attempt)),
                TurnSinks(turn_recorder=recorder, run_metadata_collector=collector),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["notice:visible"]
    assert collector == {"io.mindroom.ai_run": {"usage": 1}}
    assert recorder.completed_calls == [
        {
            "run_metadata": {"room_id": "!room"},
            "assistant_text": "replayable",
            "completed_tools": [trace],
        },
    ]


@pytest.mark.parametrize(
    ("presentation", "allow_no_report_response", "expected_chunks"),
    [
        # A quiet turn reports nothing: an empty final text, which replaces any document a team showed.
        pytest.param("Tool: matrix alert sent", True, ["notice:"], id="silent-agent"),
        pytest.param("Team Response: no team prose", True, ["notice:"], id="silent-team"),
        pytest.param(
            "Tool: matrix alert sent",
            False,
            ["notice:Tool: matrix alert sent"],
            id="ordinary-agent",
        ),
        pytest.param(
            "Team Response: no team prose",
            False,
            ["notice:Team Response: no team prose"],
            id="ordinary-team",
        ),
    ],
)
def test_tool_only_agent_and_team_presentations_respect_quiet_delivery(
    presentation: str,
    allow_no_report_response: bool,
    expected_chunks: list[str],
) -> None:
    """Tool presentation is not semantic prose, while tool effects and records survive."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    collector: dict[str, Any] = {}
    independent_alerts: list[str] = []
    trace = _trace("matrix_alert")
    tool = ToolExecution(
        tool_call_id="call-alert",
        tool_name="matrix_alert",
        tool_args={"message": "check completed"},
        result="sent",
    )

    async def _attempt(run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        independent_alerts.append("$alert")
        run.run_metadata = {"tool_count": 1}
        return CompletedAttempt(
            response_text=presentation,
            replayable_text="",
            has_visible_content=False,
            tool_executions=(tool,),
            completed_tools=(trace,),
            build_run_metadata=lambda: {"ai_run": {"tools": 1}},
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(allow_no_report_response=allow_no_report_response),
                _streaming_adapter(log, _streamed(_attempt)),
                TurnSinks(turn_recorder=recorder, run_metadata_collector=collector),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == expected_chunks
    assert independent_alerts == ["$alert"]
    assert collector == {"ai_run": {"tools": 1}}
    assert recorder.completed_calls == [
        {
            "run_metadata": {"tool_count": 1},
            "assistant_text": "",
            "completed_tools": [trace],
        },
    ]


@pytest.mark.parametrize(("allow_no_report_response", "ending"), [(True, ["notice:"]), (False, [])])
def test_a_quiet_turn_without_an_answer_ends_with_an_empty_final_text(
    allow_no_report_response: bool,
    ending: list[str],
) -> None:
    """A quiet turn whose shown output is no answer ends with an empty final text, which a team's chunks give way to."""
    log = _AdapterLog()

    async def _attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "tool document"
        yield AttemptResolved(CompletedAttempt(completed_tools=(_trace("touch"),)))

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(allow_no_report_response=allow_no_report_response),
                _streaming_adapter(log, _attempt),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["tool document", *ending]


def test_completion_skips_collector_without_run_metadata() -> None:
    """No collector update happens when the attempt resolved without metadata."""
    log = _AdapterLog()
    collector: dict[str, Any] = {}

    async def _attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        return CompletedAttempt(response_text="done", replayable_text="done", has_visible_content=True)

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(_attempt)),
                TurnSinks(turn_recorder=None, run_metadata_collector=collector),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["notice:done"]
    assert collector == {}


def test_handled_attempt_preserves_seeded_turn_metadata() -> None:
    """A pre-prepare failure records the Matrix source metadata the turn started with."""
    log = _AdapterLog()
    seeded_metadata = {"matrix_source_event_ids": ["$source"], "matrix_seen_event_ids": ["$source"]}
    recorder = _FakeTurnRecorder()

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(HandledAttempt("friendly error", error=RuntimeError("friendly error")))

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(matrix_run_metadata=seeded_metadata),
                _streaming_adapter(log, _attempt),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["friendly error"]
    assert recorder.interrupted_calls[-1]["run_metadata"] == {
        "matrix_source_event_ids": ["$source"],
        "matrix_seen_event_ids": ["$reply", "$source"],
        "room_id": "!room",
        "thread_id": "$thread",
        "reply_to_event_id": "$reply",
        "requester_id": "@user:hs",
        "correlation_id": "corr-1",
        "model_params": {},
        "matrix_event_id": "$reply",
    }


def test_handled_attempt_persists_its_interrupted_record(
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """A handled error stores one interrupted record of the attempt's partial output, with no caller recorder."""
    log = _AdapterLog()

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.run_metadata = {"room_id": "!room"}
        run.attempt = _FixedAttempt(
            TurnPartialSnapshot(
                assistant_text="partial",
                completed_tools=(_trace("search"),),
                interrupted_tools=(_trace("browse"),),
                attempt_run_id="run-live",
            ),
        )
        yield AttemptResolved(HandledAttempt("friendly error", error=RuntimeError("friendly error")))

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["friendly error"]
    [(run_id, snapshot)] = persisted
    assert run_id == "run-live"
    assert snapshot.partial_text == "partial"
    assert snapshot.completed_tools == (_trace("search"),)
    assert snapshot.interrupted_tools == (_trace("browse"),)
    assert snapshot.run_metadata == {"room_id": "!room"}
    assert snapshot.original_status is RunStatus.error


def test_external_cancel_before_prepare_builds_fallback_metadata(
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """An external cancel before prepare persists a replay with rebuilt run metadata."""
    log = _AdapterLog()

    async def _attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, _streamed(_attempt)),
                    TurnSinks(),
                    continuation=_continuation(),
                ),
            ),
        )

    [(run_id, snapshot)] = persisted
    assert run_id == "run-1"
    assert snapshot.partial_text == ""
    run_metadata = snapshot.run_metadata
    assert run_metadata is not None
    assert run_metadata["room_id"] == "!room"
    assert run_metadata["correlation_id"] == "corr-1"
    assert run_metadata["reply_to_event_id"] == "$reply"


def test_a_cancel_after_the_turn_completed_keeps_its_completed_record(
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """A stop that lands while a completed turn finishes its attempt leaves the turn's completed record as it is."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(CompletedAttempt(replayable_text="done", has_visible_content=True))

    def _cancel_once_completed(_resolution: CompletedAttempt) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, _attempt),
                    TurnSinks(turn_recorder=recorder, on_completed=_cancel_once_completed),
                    continuation=_continuation(),
                ),
            ),
        )

    assert recorder.outcome == "completed"
    assert recorder.interrupted_calls == []
    assert persisted == []


def test_external_cancel_skips_persist_after_handled_attempt_persisted(
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """A cancel after a handled error persisted its replay does not persist a second one."""
    log = _AdapterLog()

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.run_metadata = {"room_id": "!room"}
        yield AttemptResolved(HandledAttempt("friendly error", error=RuntimeError("friendly error")))

    def _cancel_once_failed(_error: Exception) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, _attempt),
                    TurnSinks(on_failed=_cancel_once_failed),
                    continuation=_continuation(),
                ),
            ),
        )

    assert len(persisted) == 1


@pytest.mark.asyncio
async def test_interrupted_record_is_stored_off_the_event_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Storing a stopped turn's record is blocking storage work, so it runs through the thread offload boundary."""
    in_worker = False
    stored_in_worker: list[bool] = []
    original_to_thread = asyncio.to_thread

    async def fake_to_thread(function: Callable[..., object], *args: object, **kwargs: object) -> object:
        nonlocal in_worker
        if function is not response_turn_module._persist_interrupted:
            return await original_to_thread(function, *args, **kwargs)
        in_worker = True
        try:
            return function(*args, **kwargs)
        finally:
            in_worker = False

    def _persist(**_kwargs: object) -> None:
        stored_in_worker.append(in_worker)

    async def _attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        raise asyncio.CancelledError

    monkeypatch.setattr(response_turn_module.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(response_turn_module, "persist_interrupted_replay_snapshot", _persist)
    with pytest.raises(asyncio.CancelledError):
        await _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(_AdapterLog(), _streamed(_attempt)),
                TurnSinks(),
                continuation=_continuation(),
            ),
        )

    assert stored_in_worker == [True]


@pytest.mark.asyncio
async def test_a_second_cancel_waits_for_the_interrupted_record_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cancellation that lands while the stopped turn's record is stored ends the turn only once the record is stored.

    The reply settles and the conversation's next turn reads history once the turn ends.
    """
    store_started = threading.Event()
    release_store = threading.Event()
    stored: list[str] = []

    def _persist(*, run_id: str, **_kwargs: object) -> None:
        store_started.set()
        assert release_store.wait(timeout=5)
        stored.append(run_id)

    async def _attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        raise asyncio.CancelledError

    monkeypatch.setattr(response_turn_module, "persist_interrupted_replay_snapshot", _persist)
    turn = asyncio.create_task(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(_AdapterLog(), _streamed(_attempt)),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )
    assert await asyncio.to_thread(store_started.wait, 5)
    turn.cancel()
    done, _pending = await asyncio.wait({turn}, timeout=0.2)
    assert not done
    release_store.set()
    with pytest.raises(asyncio.CancelledError):
        await turn

    assert stored == ["run-1"]


def test_continuation_advances_and_resets_turn_state() -> None:
    """A model-switch attempt is dropped, and the turn reruns with the continuation prompt."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    prompts: list[str] = []
    live_at_start: list[object] = []
    prior_tools_at_start: list[tuple[ToolTraceEntry, ...]] = []
    first_trace = _trace("switch_thread_model")

    async def _attempt(
        run: TurnRunState,
        continuation: TurnContinuationRunState,
    ) -> CompletedAttempt:
        live_at_start.append(run.attempt)
        prior_tools_at_start.append(run.prior_completed_tools)
        run.attempt = _FixedAttempt()
        prompts.append(continuation.active_prompt)
        if len(prompts) == 1:
            return CompletedAttempt(
                attempt_run_id="run-1",
                tool_executions=(_model_switch_execution("next-turn"),),
                completed_tools=(first_trace,),
            )
        return CompletedAttempt(
            response_text="final",
            replayable_text="final",
            has_visible_content=True,
            completed_tools=(_trace("sleep"),),
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(_attempt)),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation("original ask"),
            ),
        ),
    )

    assert chunks == ["notice:final"]
    assert len(prompts) == 2
    assert prompts[0] == "original ask"
    assert "MODEL SWITCH TOOL CALL COMPLETED" in prompts[1]
    assert live_at_start == [None, None]
    # The continuation dropped the spent attempt and carries its finished tools into the next one.
    assert prior_tools_at_start == [(), (first_trace,)]
    # The final recording carries the first attempt's tools plus the second's.
    assert recorder.completed_calls[-1]["completed_tools"] == [first_trace, _trace("sleep")]


def test_a_stop_between_continuation_attempts_records_the_turn_under_the_attempt_it_was_starting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Stop while a continuation prepares its next attempt stores the turn as a new run, after the spent attempts.

    Recording it under the turn's first run id would replace that attempt's stored run and put the stopped turn
    before the second attempt's work in replayed history.
    """
    log = _AdapterLog()
    run_ids: list[str | None] = []
    stored: list[str] = []

    def _persist(*, run_id: str, **_kwargs: object) -> None:
        stored.append(run_id)

    async def _attempt(run: TurnRunState, continuation: TurnContinuationRunState) -> CompletedAttempt:
        run_ids.append(continuation.active_run_id)
        if len(run_ids) == 3:
            # Stopped while the third attempt prepares, before it starts.
            raise asyncio.CancelledError
        run.attempt = _FixedAttempt()
        return CompletedAttempt(
            attempt_run_id=continuation.active_run_id,
            tool_executions=(_model_switch_execution("next-turn"),),
        )

    monkeypatch.setattr(response_turn_module, "persist_interrupted_replay_snapshot", _persist)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, _streamed(_attempt)),
                    TurnSinks(),
                    continuation=_continuation("original ask"),
                ),
            ),
        )

    first, second, starting = run_ids
    assert first == "run-1"
    assert len({first, second, starting}) == 3
    assert stored == [starting]


def test_an_empty_run_s_retry_runs_under_a_run_id_of_its_own() -> None:
    """The retry never reuses the empty attempt's run id, whose usage the retry's save would replace.

    The turn records the retry's run, the run that answered.
    """
    recorder = _FakeTurnRecorder()
    run_ids: list[str | None] = []

    async def _attempt(_run: TurnRunState, continuation: TurnContinuationRunState) -> CompletedAttempt:
        run_ids.append(continuation.active_run_id)
        if len(run_ids) == 1:
            return CompletedAttempt(is_empty=True, attempt_run_id=continuation.active_run_id)
        return CompletedAttempt(response_text="Answer.", attempt_run_id=continuation.active_run_id)

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(_AdapterLog(), _streamed(_attempt)),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["notice:Answer."]
    [first, retry] = run_ids
    assert first == "run-1"
    assert retry not in (None, "run-1")
    assert recorder.run_id == retry


@pytest.mark.parametrize("hidden", [False, True])
def test_after_toolcall_switch_selects_new_continuation_model(hidden: bool) -> None:
    """Dropping the requested alias during continuation would rebuild the old model."""
    log = _AdapterLog()
    active_models: list[str | None] = []

    async def _attempt(
        _run: TurnRunState,
        continuation: TurnContinuationRunState,
    ) -> CompletedAttempt:
        active_models.append(continuation.active_model_name)
        if len(active_models) == 1:
            return CompletedAttempt(
                attempt_run_id="run-1",
                tool_executions=() if hidden else (_model_switch_execution("after-toolcall"),),
                control_calls=(_model_switch_execution("after-toolcall").call,) if hidden else (),
            )
        return CompletedAttempt(response_text="final", replayable_text="final", has_visible_content=True)

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(_attempt)),
                TurnSinks(),
                continuation=_continuation("original ask"),
            ),
        ),
    )

    assert chunks == ["notice:final"]
    assert active_models == [None, "large"]


@pytest.mark.parametrize("hidden", [False, True])
def test_next_turn_switch_keeps_current_model_for_continuation(hidden: bool) -> None:
    """Re-reading the persisted override would apply a next-turn switch too early."""
    log = _AdapterLog()
    active_models: list[str | None] = []

    async def _attempt(
        _run: TurnRunState,
        continuation: TurnContinuationRunState,
    ) -> CompletedAttempt:
        active_models.append(continuation.active_model_name)
        if len(active_models) == 1:
            return CompletedAttempt(
                attempt_run_id="run-1",
                runtime_model_name="default",
                tool_executions=() if hidden else (_model_switch_execution("next-turn"),),
                control_calls=(_model_switch_execution("next-turn").call,) if hidden else (),
            )
        return CompletedAttempt(response_text="final", replayable_text="final", has_visible_content=True)

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(_attempt)),
                TurnSinks(),
                continuation=_continuation("original ask"),
            ),
        ),
    )

    assert chunks == ["notice:final"]
    assert active_models == [None, "default"]


def test_empty_run_grants_one_retry_then_notice() -> None:
    """The empty-run guard logs each empty run, drops the spent attempt, retries once, then falls back to the notice."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    attempts = 0
    live_at_start: list[object] = []

    async def _attempt(run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        nonlocal attempts
        attempts += 1
        live_at_start.append(run.attempt)
        run.attempt = _FixedAttempt()
        return CompletedAttempt(is_empty=True, run_id=f"run-{attempts}", output_tokens=attempts)

    with capture_logs() as logs:
        chunks = asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, _streamed(_attempt)),
                    TurnSinks(turn_recorder=recorder),
                    continuation=_continuation(),
                ),
            ),
        )

    assert chunks == [f"notice:{EMPTY_RESPONSE_NOTICE}"]
    assert attempts == 2
    # The notice is delivery-only.
    assert [
        (entry["run_id"], entry["output_tokens"]) for entry in logs if entry["event"] == "model_returned_empty_response"
    ] == [("run-1", 1), ("run-2", 2)]
    assert live_at_start == [None, None]
    # The notice-only turn still records an empty completion.
    assert recorder.completed_calls == [
        {"run_metadata": None, "assistant_text": "", "completed_tools": []},
    ]


def test_empty_retry_borrows_continuation_slot_within_shared_budget() -> None:
    """One empty retry plus model-switch continuations share the iteration budget."""
    log = _AdapterLog()
    attempts = 0

    async def _attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return CompletedAttempt(is_empty=True, run_id="run-empty")
        return CompletedAttempt(
            attempt_run_id=f"run-{attempts}",
            tool_executions=(_model_switch_execution("next-turn"),),
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(_attempt)),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )

    # Attempt 1 spends the empty retry; attempts 2-4 continue; attempt 5 sits
    # at the decision limit and settles with the limit message.
    assert attempts == 5
    [limit_notice] = chunks
    assert "did not produce a final answer" in limit_notice


def test_an_empty_run_s_metadata_stays_out_of_collector() -> None:
    """An empty run's payload must not ride out on a later resolution.

    The retry's resolution owns the collector; here it errors without
    metadata, so the collector must stay empty rather than describe a run
    that history never holds.
    """
    log = _AdapterLog()
    collector: dict[str, Any] = {}
    attempts = 0

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield AttemptResolved(
                CompletedAttempt(
                    is_empty=True,
                    run_id="run-empty",
                    build_run_metadata=lambda: {"io.mindroom.ai_run": {"run_id": "run-empty", "status": "completed"}},
                ),
            )
            return
        yield AttemptResolved(HandledAttempt("friendly error", error=RuntimeError("friendly error")))

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(run_metadata_collector=collector),
                continuation=_continuation(),
            ),
        ),
    )

    assert attempts == 2
    assert chunks == ["friendly error"]
    assert collector == {}


def test_superseded_continuation_metadata_stays_out_of_collector() -> None:
    """Only the terminal attempt's metadata reaches the collector on continuation."""
    log = _AdapterLog()
    collector: dict[str, Any] = {}
    attempts = 0

    async def _attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> CompletedAttempt:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return CompletedAttempt(
                attempt_run_id="run-1",
                tool_executions=(_model_switch_execution("next-turn"),),
                build_run_metadata=lambda: {"io.mindroom.ai_run": {"run_id": "run-1"}},
            )
        return CompletedAttempt(
            response_text="final",
            replayable_text="final",
            has_visible_content=True,
            build_run_metadata=lambda: {"io.mindroom.ai_run": {"run_id": "run-2"}},
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(_attempt)),
                TurnSinks(run_metadata_collector=collector),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["notice:final"]
    assert collector == {"io.mindroom.ai_run": {"run_id": "run-2"}}


def test_streaming_turn_yields_chunks_and_filters_sentinel() -> None:
    """The streaming driver forwards attempt chunks and never leaks the sentinel."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.run_metadata = {"room_id": "!room"}
        yield "hello "
        yield "world"
        yield AttemptResolved(
            CompletedAttempt(replayable_text="hello world", has_visible_content=True),
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["hello ", "world"]
    assert recorder.completed_calls[-1]["assistant_text"] == "hello world"


def test_streaming_handled_attempt_records_and_publishes_its_metadata() -> None:
    """A handled streaming error records the turn and publishes the metadata the attempt hands over."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    collector: dict[str, Any] = {}
    error_metadata = {"io.mindroom.ai_run": {"status": "error"}}

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(
            HandledAttempt(
                "friendly error",
                error=RuntimeError("friendly error"),
                build_run_metadata=lambda: error_metadata,
            ),
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(turn_recorder=recorder, run_metadata_collector=collector),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["friendly error"]
    assert collector == error_metadata
    assert recorder.completed_calls == []
    assert len(recorder.interrupted_calls) == 1


def test_streaming_external_cancel_records_snapshot_partials(
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """An external cancel records the running attempt's partial output and stores that record once."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.attempt = _FixedAttempt(
            TurnPartialSnapshot(
                assistant_text="live partial",
                completed_tools=(_trace("search"),),
                interrupted_tools=(_trace("browse"),),
                attempt_run_id="run-live",
            ),
        )
        yield "chunk"
        raise asyncio.CancelledError

    collected: list[str] = []

    async def _run() -> None:
        async for chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(turn_recorder=recorder),
            continuation=_continuation(),
        ):
            # A comprehension would lose the chunks yielded before the cancel.
            collected.append(chunk)  # noqa: PERF401

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert collected == ["chunk"]
    assert len(recorder.interrupted_calls) == 1
    assert recorder.interrupted_calls[0]["assistant_text"] == "live partial"
    assert recorder.interrupted_calls[0]["completed_tools"] == [_trace("search")]
    assert recorder.interrupted_calls[0]["interrupted_tools"] == [_trace("browse")]
    assert [(run_id, snapshot.partial_text) for run_id, snapshot in persisted] == [("run-live", "live partial")]


def test_streaming_external_cancel_without_recorder_persists_snapshot(
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """A recorder-less external cancel persists the interrupted record from the running attempt's partial output."""
    log = _AdapterLog()

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.attempt = _FixedAttempt(TurnPartialSnapshot(assistant_text="live partial", attempt_run_id="run-live"))
        yield "chunk"
        raise asyncio.CancelledError

    async def _run() -> None:
        async for _chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(),
            continuation=_continuation(),
        ):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    [(run_id, snapshot)] = persisted
    assert snapshot.partial_text == "live partial"
    assert run_id == "run-live"


def test_streaming_no_report_empty_run_completes_without_retry_or_notice() -> None:
    """An allowed streamed empty completion records success without yielding a notice."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    attempts = 0
    runs: list[TurnRunState] = []

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        nonlocal attempts
        attempts += 1
        runs.append(run)
        run.attempt = _FixedAttempt()
        yield AttemptResolved(CompletedAttempt(is_empty=True, run_id="run-empty"))

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(allow_no_report_response=True),
                _streaming_adapter(log, _attempt),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == []
    assert attempts == 1
    assert runs[0].attempt is not None
    assert recorder.completed_calls == [
        {"run_metadata": None, "assistant_text": "", "completed_tools": []},
    ]


@pytest.mark.parametrize("hidden", [False, True])
def test_streaming_continuation_advances_then_finishes(hidden: bool) -> None:
    """A streamed model-switch attempt is dropped, and the turn continues and streams the second attempt."""
    log = _AdapterLog()
    prompts: list[str] = []
    live_at_start: list[object] = []

    async def _attempt(
        run: TurnRunState,
        continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        live_at_start.append(run.attempt)
        run.attempt = _FixedAttempt()
        prompts.append(continuation.active_prompt)
        if len(prompts) == 1:
            yield "switching models"
            yield AttemptResolved(
                CompletedAttempt(
                    attempt_run_id="run-1",
                    tool_executions=() if hidden else (_model_switch_execution("next-turn"),),
                    control_calls=(_model_switch_execution("next-turn").call,) if hidden else (),
                ),
            )
            return
        yield "final answer"
        yield AttemptResolved(CompletedAttempt(replayable_text="final answer", has_visible_content=True))

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(),
                continuation=_continuation("original ask"),
            ),
        ),
    )

    assert chunks == ["switching models", "final answer"]
    assert len(prompts) == 2
    assert "MODEL SWITCH TOOL CALL COMPLETED" in prompts[1]
    assert live_at_start == [None, None]


def test_a_stop_before_the_next_attempt_starts_records_none_of_the_spent_attempt() -> None:
    """A stop between a dropped attempt and the next one records no text or calls of the dropped attempt.

    Its finished calls already carried over through the turn state, so keeping them here would count them twice.
    """
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    switch = _trace("switch_thread_model")
    attempts = 0

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise asyncio.CancelledError
        run.attempt = _FixedAttempt(
            TurnPartialSnapshot(
                assistant_text="stale partial",
                completed_tools=(switch,),
                interrupted_tools=(_trace("browse"),),
                attempt_run_id="run-1",
            ),
        )
        yield "stale partial"
        yield AttemptResolved(
            CompletedAttempt(
                attempt_run_id="run-1",
                tool_executions=(_model_switch_execution("next-turn"),),
                completed_tools=(switch,),
            ),
        )

    async def _run() -> None:
        async for _chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(turn_recorder=recorder),
            continuation=_continuation(),
        ):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert attempts == 2
    [interrupted] = recorder.interrupted_calls
    assert interrupted["assistant_text"] == ""
    assert interrupted["completed_tools"] == [switch]
    assert interrupted["interrupted_tools"] == []


def test_streaming_continuation_limit_yields_limit_message() -> None:
    """Hitting the continuation limit mid-stream yields the limit notice chunk."""
    log = _AdapterLog()

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(
            CompletedAttempt(attempt_run_id="run-1", tool_executions=(_model_switch_execution("next-turn"),)),
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )

    assert len(chunks) == 1
    assert chunks[0].startswith("notice:")
    assert "did not produce a final answer" in chunks[0]


@pytest.mark.parametrize(("saved_count", "expected_attempts"), [(0, 4), (2, 2), (4, 0)])
def test_resumed_continuation_uses_remaining_budget(saved_count: int, expected_attempts: int) -> None:
    """Restoring a model-switch continuation cannot reset the logical turn budget or leak stale text."""
    log = _AdapterLog()
    prompts: list[str] = []
    completed: list[CompletedAttempt] = []

    async def attempt(
        _run: TurnRunState,
        continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        prompts.append(continuation.active_prompt)
        yield AttemptResolved(
            CompletedAttempt(attempt_run_id="fresh", tool_executions=(_model_switch_execution("next-turn"),)),
        )

    restored = CompletedAttempt(
        response_text="stale",
        attempt_run_id="saved",
        tool_executions=(_model_switch_execution("next-turn"),),
    )
    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, attempt),
                TurnSinks(on_completed=completed.append),
                continuation=_continuation(),
                resume=TurnResume(attempt=restored, continuation_count=saved_count),
            ),
        ),
    )
    assert len(prompts) == expected_attempts
    assert all("MODEL SWITCH TOOL CALL COMPLETED" in prompt for prompt in prompts)
    assert len(chunks) == 1
    assert "did not produce a final answer" in chunks[0]
    assert "stale" not in chunks[0]
    assert len(completed) == 1
    assert completed[0].status is RunStatus.completed
    assert completed[0].build_run_metadata is None


def test_resumed_continuation_preserves_count_on_next_pause() -> None:
    """A second approval must save the consumed fresh continuation attempt."""
    log = _AdapterLog()
    completed: list[CompletedAttempt] = []

    async def attempt(
        _run: TurnRunState,
        _continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(
            response_turn_module.PausedAttempt(session_id="session", run_id="fresh", tools=(), toolkit_owners={}),
        )

    with pytest.raises(response_turn_module.ResponsePausedForApproval) as raised:
        asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, attempt),
                    TurnSinks(on_completed=completed.append),
                    continuation=_continuation(),
                    resume=TurnResume(
                        attempt=CompletedAttempt(tool_executions=(_model_switch_execution("next-turn"),)),
                        continuation_count=2,
                    ),
                ),
            ),
        )
    assert raised.value.paused.continuation_count == 3
    assert completed == []


def test_fresh_continuation_saves_count_before_first_approval() -> None:
    """The budget starts with the first continuation, before any approval checkpoint."""
    log = _AdapterLog()
    calls = 0
    completed: list[CompletedAttempt] = []

    async def attempt(
        _run: TurnRunState,
        _continuation: TurnContinuationRunState,
    ) -> CompletedAttempt | response_turn_module.PausedAttempt:
        nonlocal calls
        calls += 1
        if calls == 1:
            return CompletedAttempt(tool_executions=(_model_switch_execution("next-turn"),))
        return response_turn_module.PausedAttempt(session_id="session", run_id="fresh", tools=(), toolkit_owners={})

    async def execute() -> None:
        await _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _streamed(attempt)),
                TurnSinks(on_completed=completed.append),
                continuation=_continuation(),
            ),
        )

    with pytest.raises(response_turn_module.ResponsePausedForApproval) as raised:
        asyncio.run(execute())
    assert raised.value.paused.continuation_count == 1
    assert calls == 2
    assert completed == []


@pytest.mark.parametrize(
    "resolution",
    [HandledAttempt("failed", error=RuntimeError("failed")), SkippedAttempt()],
)
def test_noncompletion_does_not_publish_completed_attempt(resolution: StreamAttemptResolution) -> None:
    """Handled errors and quiet turns cannot report typed success."""
    completed: list[CompletedAttempt] = []

    async def attempt(
        _run: TurnRunState,
        _continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(resolution)

    asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(_AdapterLog(), attempt),
                TurnSinks(on_completed=completed.append),
                continuation=_continuation(),
            ),
        ),
    )
    assert completed == []


def test_a_started_attempt_hands_the_scope_to_queued_notice_finalization_even_when_it_raises() -> None:
    """An attempt that started ran in the turn's scope, so the notices it saw finalize there under the adapter's name."""
    log = _AdapterLog()

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.attempt = _FixedAttempt()
        yield "chunk"
        msg = "stream blew up"
        raise RuntimeError(msg)

    with queued_message_signal_context(None) as notices:
        chunks = asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, _attempt),
                    TurnSinks(),
                    continuation=_continuation(),
                ),
            ),
        )

    assert chunks == ["chunk", "failed: stream blew up"]
    assert list(notices.storage_targets) == [(_ENTITY_NAME, "session-1", "agent")]


def test_an_attempt_that_fails_before_it_starts_hands_over_no_scope() -> None:
    """An attempt that resolves without starting, as one whose preparation failed, ran nothing in the scope."""
    log = _AdapterLog()

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(HandledAttempt("friendly error", error=RuntimeError("friendly error")))

    with queued_message_signal_context(None) as notices:
        chunks = asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, _attempt),
                    TurnSinks(),
                    continuation=_continuation(),
                ),
            ),
        )

    assert chunks == ["friendly error"]
    assert notices.storage_targets == {}


def test_the_attempt_a_continuation_drops_still_hands_its_scope_to_queued_notice_finalization() -> None:
    """The spent attempt started in the scope, so the scope is handed over even when the next attempt never starts."""
    log = _AdapterLog()
    attempts = 0

    async def _attempt(
        run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            run.attempt = _FixedAttempt()
            yield AttemptResolved(
                CompletedAttempt(attempt_run_id="run-1", tool_executions=(_model_switch_execution("next-turn"),)),
            )
            return
        yield AttemptResolved(HandledAttempt("friendly error", error=RuntimeError("friendly error")))

    with queued_message_signal_context(None) as notices:
        chunks = asyncio.run(
            _collect(
                stream_response_turn(
                    _ctx(),
                    _streaming_adapter(log, _attempt),
                    TurnSinks(),
                    continuation=_continuation(),
                ),
            ),
        )

    assert chunks == ["friendly error"]
    assert attempts == 2
    assert list(notices.storage_targets) == [(_ENTITY_NAME, "session-1", "agent")]


def test_streaming_unexpected_error_yields_its_shaped_error_text() -> None:
    """An unexpected exception becomes the adapter's shaped error text, a bare string as every failed turn's reply is.

    The caller's failure sink receives the error and its text, as it does for a failure an attempt handled.
    """
    log = _AdapterLog()
    error = RuntimeError("boom")
    failures: list[HandledAttempt] = []

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "chunk"
        raise error

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt, unexpected_error_text=lambda e: f"shaped: {e}"),
                TurnSinks(on_failed=failures.append),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["chunk", "shaped: boom"]
    assert failures == [HandledAttempt("shaped: boom", error=error)]


def test_streaming_completed_response_text_is_emitted_after_settle() -> None:
    """A resolution-carried final document is emitted once the attempt settles."""
    log = _AdapterLog()

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(
            CompletedAttempt(response_text="final doc", replayable_text="final doc", has_visible_content=True),
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["notice:final doc"]


def test_streaming_empty_terminal_text_is_not_leaked_before_retry() -> None:
    """An empty attempt's fallback document is superseded by the retry, not emitted.

    Pre-fix, the team terminal branch yielded its formatted text before the
    driver settled, so an empty run leaked "No team response generated."
    ahead of the retry (or the empty notice).
    """
    log = _AdapterLog()
    attempts = 0

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield AttemptResolved(
                CompletedAttempt(response_text="No team response generated.", is_empty=True, run_id="run-1"),
            )
            return
        yield AttemptResolved(
            CompletedAttempt(response_text="Recovered", replayable_text="Recovered", has_visible_content=True),
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )

    assert attempts == 2
    assert chunks == ["notice:Recovered"]


def test_streaming_continuation_does_not_emit_first_attempt_terminal_text() -> None:
    """A model-switch attempt's terminal text is superseded by the rerun's."""
    log = _AdapterLog()
    attempts = 0

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            yield AttemptResolved(
                CompletedAttempt(
                    response_text="stale first-attempt text",
                    attempt_run_id="run-1",
                    tool_executions=(_model_switch_execution("next-turn"),),
                ),
            )
            return
        yield AttemptResolved(
            CompletedAttempt(response_text="final", replayable_text="final", has_visible_content=True),
        )

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )

    assert attempts == 2
    assert chunks == ["notice:final"]


def test_streaming_attempt_without_sentinel_fails_the_turn() -> None:
    """A streaming attempt that never yields its sentinel fails the turn instead of ending it silently."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(
        _run: TurnRunState,
        _c: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "chunk"

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks[0] == "chunk"
    assert "AttemptResolved sentinel" in chunks[1]
    assert recorder.completed_calls == []
    assert recorder.original_status is RunStatus.error


def test_streaming_aclose_runs_cleanup_and_records_the_stopped_turn(
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """A consumer that stops reading mid-stream cleans the attempt up and leaves the turn recorded as stopped, once.

    The attempt's own cleanup runs before the driver hands its scope to queued-notice finalization.
    """
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    attempt_closed = False
    handed_over_at_close: list[object] = []

    with queued_message_signal_context(None) as notices:

        async def _attempt(
            run: TurnRunState,
            _c: TurnContinuationRunState,
        ) -> AsyncGenerator[str | AttemptResolved, None]:
            nonlocal attempt_closed
            run.attempt = _FixedAttempt()
            try:
                yield "first"
                yield "second"
                yield AttemptResolved(CompletedAttempt(replayable_text="full", has_visible_content=True))
            finally:
                handed_over_at_close.extend(notices.storage_targets)
                attempt_closed = True

        async def _run() -> None:
            stream = stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            )
            assert await anext(stream) == "first"
            await stream.aclose()
            assert attempt_closed

        asyncio.run(_run())

    assert handed_over_at_close == []
    assert list(notices.storage_targets) == [(_ENTITY_NAME, "session-1", "agent")]
    assert recorder.completed_calls == []
    assert len(recorder.interrupted_calls) == 1
    assert recorder.original_status is RunStatus.cancelled
    assert [snapshot.original_status for _run_id, snapshot in persisted] == [RunStatus.cancelled]


def test_stream_resolution_union_covers_handled() -> None:
    """The streaming resolution union accepts the handled sentinel."""
    resolution: StreamAttemptResolution = HandledAttempt("failed", error=RuntimeError("failed"))
    assert isinstance(resolution, HandledAttempt)


def test_turn_adapter_callback_surface_stays_within_baseline() -> None:
    """The turn adapter must not grow beyond the reviewed callback baseline."""
    assert len(fields(TurnAdapter)) <= 7


def test_declined_participation_settles_once_without_retry() -> None:
    """A quiet decision settles the turn as skipped, with no retry and no completed record."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    attempts = 0
    gate = ParticipationGate()
    gate.decline("Already answered.")

    async def attempt(_run: TurnRunState, _c: TurnContinuationRunState) -> SkippedAttempt:
        nonlocal attempts
        attempts += 1
        return SkippedAttempt()

    async def execute() -> str:
        chunks = await _collect(
            stream_response_turn(
                _ctx(participation=gate),
                _streaming_adapter(log, _streamed(attempt)),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            ),
        )
        return "".join(chunks)

    assert asyncio.run(execute()) == ""
    assert attempts == 1
    assert recorder.outcome == "skipped"
    assert recorder.completed_calls == []


def _terminal_record(
    status: RunStatus = RunStatus.completed,
    *,
    content: str | None = None,
    tools: list[ToolExecution] | None = None,
    parent_run_id: str | None = None,
) -> RunRecord:
    return RunRecord(
        run_id="run-1",
        agent_id="helper",
        status=status,
        content=content,
        tools=tools,
        parent_run_id=parent_run_id,
    )


@pytest.mark.parametrize(
    ("record", "empty"),
    [
        (_terminal_record(), True),
        (_terminal_record(content=" \n"), True),
        (_terminal_record(content="Answer."), False),
        (_terminal_record(tools=[_model_switch_execution("next-turn")]), False),
        (_terminal_record(RunStatus.error), False),
        (_terminal_record(RunStatus.cancelled), False),
        (_terminal_record(RunStatus.paused), False),
    ],
)
def test_an_empty_reply_is_a_completed_run_with_no_text_and_no_tool_call(*, record: RunRecord, empty: bool) -> None:
    """Whitespace is no text; a run that stopped, failed, or paused is no empty reply."""
    assert is_empty_reply(record) is empty


def _gate(state: str) -> ParticipationGate | None:
    gate = None if state == "absent" else ParticipationGate()
    if state == "silent":
        assert gate is not None
        gate.decline("Already answered.")
    elif state == "approved":
        assert gate is not None
        gate.approve_existing_response()
    return gate


@pytest.mark.parametrize(
    ("gate_state", "status", "replays"),
    [
        ("pending", RunStatus.error, False),
        ("pending", RunStatus.cancelled, True),
        ("silent", RunStatus.completed, False),
        ("silent", RunStatus.error, False),
        ("silent", RunStatus.cancelled, True),
        ("approved", RunStatus.completed, True),
        ("approved", RunStatus.error, True),
        ("absent", RunStatus.completed, True),
    ],
)
def test_a_run_the_participation_gate_has_not_approved_is_no_turn_unless_it_was_cancelled(
    *,
    gate_state: str,
    status: RunStatus,
    replays: bool,
) -> None:
    """A declined run, or one that failed before the decision, keeps only its usage; a stopped one is recorded."""
    record = _terminal_record(status, content="Answer.")

    assert replays_as_turn(_ctx(participation=_gate(gate_state)), record, is_empty=False) is replays


@pytest.mark.parametrize(
    ("record", "quiet", "replays"),
    [
        (_terminal_record(), False, False),
        (_terminal_record(), True, True),
        (_terminal_record(content="Answer."), False, True),
        (_terminal_record(parent_run_id="team-run"), False, True),
    ],
)
def test_an_empty_reply_is_no_turn_outside_a_quiet_turn(*, record: RunRecord, quiet: bool, replays: bool) -> None:
    """A quiet turn keeps its empty reply, and a team's member run is stored with its team run."""
    assert replays_as_turn(_ctx(allow_no_report_response=quiet), record, is_empty=is_empty_reply(record)) is replays


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_state", ["absent", "pending", "silent", "approved"])
async def test_outer_turn_failure_is_quiet_until_participation_approves(
    gate_state: str,
    persisted: list[tuple[str, InterruptedReplaySnapshot]],
) -> None:
    """Scope preparation failure cannot emit error text before primary approval."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    gate = None if gate_state == "absent" else ParticipationGate()
    if gate is not None and gate_state == "approved":
        gate.approve_existing_response()
    elif gate is not None and gate_state == "silent":
        gate.decline("already_answered")

    def broken_scope() -> ScopeSessionContext | None:
        message = "Scope unavailable"
        raise RuntimeError(message)

    async def attempt(_run: TurnRunState, _continuation: TurnContinuationRunState) -> CompletedAttempt:
        pytest.fail("Scope must load before an attempt")

    result = "".join(
        await _collect(
            stream_response_turn(
                _ctx(participation=gate),
                _streaming_adapter(
                    log,
                    _streamed(attempt),
                    load_scope=broken_scope,
                    unexpected_error_text=lambda _: "Visible error",
                ),
                TurnSinks(turn_recorder=recorder),
                continuation=_continuation(),
            ),
        ),
    )
    if gate_state in {"absent", "approved"}:
        assert "Visible error" in result
        assert recorder.outcome == "interrupted"
    else:
        assert result == ""
        assert gate is not None
        assert gate.is_silent
        assert gate.decided.is_set()
        assert gate.decision is not None
        assert gate.decision.reason == ("already_answered" if gate_state == "silent" else "run_failed_before_decision")
        assert recorder.outcome == "skipped"
        assert recorder.interrupted_calls == []
        assert persisted == []
