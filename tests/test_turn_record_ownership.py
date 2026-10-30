"""The turn driver is the only writer of a turn's record, and marks an interrupted turn's run once, in its own scope."""

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
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    HandledAttempt,
    ResponseTurnContext,
    TurnAdapter,
    TurnContinuationRunState,
    TurnRunState,
    TurnSinks,
    stream_response_turn,
)
from mindroom.session_storage.store import ConversationStore

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable

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


@dataclass(frozen=True)
class _Partial:
    """A running attempt, whose run its loop stored."""

    run_id: str = "attempt-1"


@dataclass
class _Stored:
    """What the driver marked: the scope kind, run id, and failure text of each run it marked interrupted."""

    rows: list[tuple[str, str, str | None]] = field(default_factory=list)


@pytest.fixture
def stored(monkeypatch: pytest.MonkeyPatch) -> _Stored:
    """Capture the driver's interrupted marks."""
    result = _Stored()

    def _mark(_ctx: object, scope_context: ScopeSessionContext, run_id: str, error: str | None) -> None:
        result.rows.append((scope_context.scope.kind, run_id, error))

    monkeypatch.setattr(response_turn_module, "_mark_interrupted", _mark)
    return result


def _adapter(
    run_attempt: Callable[[TurnRunState, TurnContinuationRunState], AsyncIterator[str | AttemptResolved]],
    *,
    kind: Literal["agent", "team"] = "agent",
    storage: ConversationStore | None = None,
) -> TurnAdapter[str]:
    def _load() -> ScopeSessionContext:
        return ScopeSessionContext(
            scope=HistoryScope(kind=kind, scope_id="helper"),
            storage=storage or Mock(spec=ConversationStore),
            session=None,
            session_id="session-1",
            storage_factory=lambda: Mock(spec=ConversationStore),
        )

    return TurnAdapter[str](
        load_scope=_load,
        run_attempt=run_attempt,
        make_text_chunk=lambda text: f"notice:{text}",
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
        TurnSinks(turn_recorder=record),
        continuation=_continuation(),
    )

    assert await anext(stream) == "partial answer"
    await stream.aclose()

    assert (record.outcome, record.run_id) == ("interrupted", "attempt-1")
    assert stored.rows == [("agent", "attempt-1", None)]


async def test_a_stopped_turn_whose_run_cannot_be_marked_still_stops() -> None:
    """A storage failure loses the mark but never replaces the Stop the turn is unwinding with."""
    storage = Mock(spec=ConversationStore)
    storage.get_run.side_effect = OSError("disk full")
    record = TurnRecorder()
    stream = stream_response_turn(
        _ctx(),
        _adapter(_showing_text_then_waiting, storage=storage),
        TurnSinks(turn_recorder=record),
        continuation=_continuation(),
    )

    assert await anext(stream) == "partial answer"
    await stream.aclose()

    assert record.outcome == "interrupted"
    storage.get_run.assert_called_once_with("attempt-1")


async def test_a_process_shutdown_records_nothing_so_replay_continues_the_reply(stored: _Stored) -> None:
    """A shutdown that hands the turn to a successor leaves its record pending and marks no run."""
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
    assert stored.rows == [("agent", "attempt-1", "the model failed")]


async def test_a_completed_turn_is_not_recorded_again_when_its_consumer_closes(stored: _Stored) -> None:
    """Closing the stream after the turn settled leaves the completed record and marks nothing."""
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
    """A delegated child or a voice call reads the record; its own owner records the interruption."""
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


async def test_a_team_turn_marks_its_run_in_the_team_s_scope(stored: _Stored) -> None:
    """The run is marked in the scope the turn's runs are stored in."""
    stream = stream_response_turn(
        _ctx(),
        _adapter(_showing_text_then_waiting, kind="team"),
        TurnSinks(),
        continuation=_continuation(),
    )
    await anext(stream)
    await stream.aclose()

    assert [kind for kind, _run_id, _error in stored.rows] == ["team"]
