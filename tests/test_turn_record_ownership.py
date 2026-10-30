"""The turn driver is the only writer of a turn's record, and stores an interrupted turn once, in its own scope."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from unittest.mock import Mock

import pytest

from mindroom import response_turn as response_turn_module
from mindroom.cancellation import request_task_cancel
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import RunStatus
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    HandledAttempt,
    ResponseTurnContext,
    TurnAdapter,
    TurnContinuationRunState,
    TurnPartialSnapshot,
    TurnRunState,
    TurnSinks,
    stream_response_turn,
)
from mindroom.session_storage.store import ConversationStore
from mindroom.tool_system.events import ToolTraceEntry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

    from mindroom.history.interrupted_replay import InterruptedReplaySnapshot
    from mindroom.session_storage.types import ConversationSession

pytestmark = pytest.mark.asyncio


def _ctx() -> ResponseTurnContext:
    return ResponseTurnContext(
        entity_label="helper",
        session_id="session-1",
        run_id="run-1",
        correlation_id="corr-1",
        reply_to_event_id="$reply",
        room_id="!room",
        thread_id="$thread",
        requester_id="@user:hs",
        matrix_run_metadata=None,
        prompt="hello",
    )


def _continuation() -> TurnContinuationRunState:
    return TurnContinuationRunState.initial(
        prompt="hello",
        model_prompt=None,
        current_timestamp_ms=None,
        current_prompt_is_structured=False,
        current_event_id=None,
        run_id="run-1",
        continuation_model_prompt_tail="",
    )


class _Partial:
    """A running attempt that has shown some text and finished one call."""

    def partial_snapshot(self) -> TurnPartialSnapshot:
        return TurnPartialSnapshot(
            assistant_text="partial answer",
            completed_tools=(ToolTraceEntry(type="tool_call_completed", tool_name="search"),),
            attempt_run_id="attempt-1",
        )


@dataclass
class _Stored:
    """What the driver stored: one entry per interrupted record it persisted."""

    rows: list[tuple[ConversationSession, str, InterruptedReplaySnapshot]] = field(default_factory=list)


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> _Stored:
    """Capture the driver's interrupted-record writes."""
    result = _Stored()

    def _persist(
        *,
        storage: ConversationStore,
        session: ConversationSession,
        run_id: str,
        snapshot: InterruptedReplaySnapshot,
    ) -> None:
        del storage
        result.rows.append((session, run_id, snapshot))

    monkeypatch.setattr(response_turn_module, "persist_interrupted_replay_snapshot", _persist)
    return result


def _adapter(
    run_attempt: Callable[[TurnRunState, TurnContinuationRunState], AsyncIterator[str | AttemptResolved]],
    *,
    kind: Literal["agent", "team"] = "agent",
) -> TurnAdapter[str]:
    def _load() -> ScopeSessionContext:
        return ScopeSessionContext(
            scope=HistoryScope(kind=kind, scope_id="helper"),
            storage=Mock(spec=ConversationStore),
            session=None,
            session_id="session-1",
            storage_factory=lambda: Mock(spec=ConversationStore),
        )

    return TurnAdapter[str](
        load_scope=_load,
        run_attempt=run_attempt,
        make_text_chunk=lambda text: f"notice:{text}",
        user_message="hello",
        unexpected_error_text=str,
        entity_name="helper",
    )


async def _showing_text_then_waiting(
    run: TurnRunState,
    _continuation: TurnContinuationRunState,
) -> AsyncGenerator[str | AttemptResolved, None]:
    run.attempt = _Partial()
    yield "partial answer"
    await asyncio.Event().wait()
    yield AttemptResolved(CompletedAttempt())


async def test_a_consumer_that_stops_reading_leaves_the_turn_recorded_as_stopped_once(stored: _Stored) -> None:
    """A Stop that lands in the consumer, while it writes what it read, records the turn stopped, not failed."""
    record = TurnRecorder()
    stream = stream_response_turn(
        _ctx(),
        _adapter(_showing_text_then_waiting),
        TurnSinks(turn_recorder=record, response_event_id=lambda: "$shown"),
        continuation=_continuation(),
    )

    assert await anext(stream) == "partial answer"
    await stream.aclose()

    assert record.outcome == "interrupted"
    assert record.original_status is RunStatus.cancelled
    assert record.user_message == "hello"
    assert record.assistant_text == "partial answer"
    assert [tool.tool_name for tool in record.completed_tools] == ["search"]
    assert len(stored.rows) == 1
    session, run_id, snapshot = stored.rows[0]
    assert run_id == "attempt-1"
    assert session.kind == "agent"
    assert snapshot.original_status is RunStatus.cancelled
    assert snapshot.run_metadata["matrix_response_event_id"] == "$shown"


async def test_a_stopped_turn_whose_row_cannot_be_stored_still_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """A storage failure loses the replay row but never replaces the Stop the turn is unwinding with."""

    def _failing_persist(**_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(response_turn_module, "persist_interrupted_replay_snapshot", _failing_persist)
    record = TurnRecorder()
    stream = stream_response_turn(
        _ctx(),
        _adapter(_showing_text_then_waiting),
        TurnSinks(turn_recorder=record),
        continuation=_continuation(),
    )

    assert await anext(stream) == "partial answer"
    await stream.aclose()

    assert record.outcome == "interrupted"


async def test_a_process_shutdown_records_nothing_so_replay_continues_the_reply(stored: _Stored) -> None:
    """A shutdown that hands the turn to a successor leaves its record pending and stores no interrupted row."""
    record = TurnRecorder()
    shown = asyncio.Event()

    async def _consume() -> None:
        stream = stream_response_turn(
            _ctx(),
            _adapter(_showing_text_then_waiting),
            TurnSinks(turn_recorder=record),
            continuation=_continuation(),
        )
        async for _chunk in stream:
            shown.set()

    task = asyncio.create_task(_consume())
    await shown.wait()
    request_task_cancel(task, process_shutdown=True)
    with pytest.raises(asyncio.CancelledError):
        await task

    assert record.outcome == "pending"
    assert stored.rows == []


async def test_a_failure_is_recorded_before_its_text_reaches_the_consumer(stored: _Stored) -> None:
    """A consumer that closes the stream as soon as it reads the failure text cannot turn the failure into a stop."""
    record = TurnRecorder()

    async def _failing(
        run: TurnRunState,
        _continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.attempt = _Partial()
        yield AttemptResolved(HandledAttempt("the model failed", error=RuntimeError("the model failed")))

    stream = stream_response_turn(
        _ctx(),
        _adapter(_failing),
        TurnSinks(turn_recorder=record),
        continuation=_continuation(),
    )
    # A failure's text is a bare string, not the adapter's text chunk: readers take it as a failed run's reply.
    assert await anext(stream) == "the model failed"
    await stream.aclose()

    assert record.outcome == "interrupted"
    assert record.original_status is RunStatus.error
    assert len(stored.rows) == 1
    assert stored.rows[0][2].original_status is RunStatus.error


async def test_a_completed_turn_is_not_recorded_again_when_its_consumer_closes(stored: _Stored) -> None:
    """Closing the stream after the turn settled leaves the completed record and stores nothing."""
    record = TurnRecorder()

    async def _completing(
        run: TurnRunState,
        _continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.attempt = _Partial()
        yield "the answer"
        yield AttemptResolved(CompletedAttempt(replayable_text="the answer", has_visible_content=True, run_id="r"))

    stream = stream_response_turn(
        _ctx(),
        _adapter(_completing),
        TurnSinks(turn_recorder=record),
        continuation=_continuation(),
    )
    assert [chunk async for chunk in stream] == ["the answer"]
    await stream.aclose()

    assert record.outcome == "completed"
    assert record.assistant_text == "the answer"
    assert stored.rows == []


async def test_a_turn_is_recorded_completed_before_its_final_text_reaches_the_consumer(stored: _Stored) -> None:
    """A consumer may close the stream as soon as it reads the text that ends the turn; the turn stays completed."""
    record = TurnRecorder()

    async def _completing_with_final_text(
        run: TurnRunState,
        _continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.attempt = _Partial()
        yield AttemptResolved(
            CompletedAttempt(
                response_text="the answer",
                replayable_text="the answer",
                has_visible_content=True,
                run_id="r",
            ),
        )

    stream = stream_response_turn(
        _ctx(),
        _adapter(_completing_with_final_text),
        TurnSinks(turn_recorder=record),
        continuation=_continuation(),
    )
    assert await anext(stream) == "notice:the answer"
    assert record.outcome == "completed"
    await stream.aclose()

    assert record.outcome == "completed"
    assert stored.rows == []


async def test_a_turn_whose_interruption_has_another_owner_is_recorded_but_not_stored(stored: _Stored) -> None:
    """A delegated child or a voice call reads the record; its own owner stores the interruption."""
    record = TurnRecorder()
    stream = stream_response_turn(
        _ctx(),
        _adapter(_showing_text_then_waiting),
        TurnSinks(turn_recorder=record, persist_interrupted=False),
        continuation=_continuation(),
    )
    await anext(stream)
    await stream.aclose()

    assert record.outcome == "interrupted"
    assert stored.rows == []


async def test_a_team_turn_stores_its_interrupted_row_as_the_team_s(stored: _Stored) -> None:
    """The row's kind comes from the scope the turn's runs are stored in."""
    stream = stream_response_turn(
        _ctx(),
        _adapter(_showing_text_then_waiting, kind="team"),
        TurnSinks(),
        continuation=_continuation(),
    )
    await anext(stream)
    await stream.aclose()

    assert [session.kind for session, _run_id, _snapshot in stored.rows] == ["team"]
