"""Driver-level tests for the shared response-turn seam."""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import pytest
from agno.models.response import ToolExecution

from mindroom.response_turn import (
    AttemptResolved,
    BlockingTurnAdapter,
    CancelledAttempt,
    CompletedAttempt,
    ContinuationCapability,
    DynamicContinuationRunState,
    EmptyRunCapability,
    EmptyRunDiscard,
    ErroredAttempt,
    HandledAttempt,
    ResponseTurnContext,
    StandaloneReplaySnapshot,
    StreamAttemptResolution,
    StreamingTurnAdapter,
    TurnPartialSnapshot,
    TurnRunState,
    TurnSinks,
    run_blocking_response_turn,
    stream_response_turn,
)
from mindroom.tool_system.events import ToolTraceEntry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping, Sequence
    from contextlib import AbstractContextManager

    from mindroom.history import ScopeSessionContext

_DEFAULT_CONTINUATION = ContinuationCapability()


@dataclass
class _FakeTurnRecorder:
    """Minimal TurnRecorder double tracking recorded outcomes."""

    run_metadata: dict[str, Any] | None = None
    assistant_text: str = ""
    completed_tools: list[ToolTraceEntry] = field(default_factory=list)
    interrupted_tools: list[ToolTraceEntry] = field(default_factory=list)
    completed_calls: list[dict[str, Any]] = field(default_factory=list)
    interrupted_calls: list[dict[str, Any]] = field(default_factory=list)
    synced_calls: list[dict[str, Any]] = field(default_factory=list)

    def sync_partial_state(
        self,
        *,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
    ) -> None:
        self.synced_calls.append(
            {
                "run_metadata": run_metadata,
                "assistant_text": assistant_text,
                "completed_tools": list(completed_tools),
                "interrupted_tools": list(interrupted_tools),
            },
        )
        self.assistant_text = assistant_text
        self.completed_tools = list(completed_tools)
        self.interrupted_tools = list(interrupted_tools)

    def record_completed(
        self,
        *,
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

    def record_interrupted(
        self,
        *,
        run_metadata: Mapping[str, Any] | None,
        assistant_text: str,
        completed_tools: Sequence[ToolTraceEntry],
        interrupted_tools: Sequence[ToolTraceEntry],
    ) -> None:
        self.interrupted_calls.append(
            {
                "run_metadata": run_metadata,
                "assistant_text": assistant_text,
                "completed_tools": list(completed_tools),
                "interrupted_tools": list(interrupted_tools),
            },
        )


def _trace(tool_name: str) -> ToolTraceEntry:
    return ToolTraceEntry(type="tool_call_completed", tool_name=tool_name)


def _dynamic_tool_execution(tool_name: str = "sleep") -> ToolExecution:
    return ToolExecution(
        tool_call_id="call-load_tool",
        tool_name="load_tool",
        tool_args={"tool_name": tool_name},
        result=json.dumps({"status": "loaded", "tool": "dynamic_tools", "tool_name": tool_name}),
        stop_after_tool_call=True,
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


def _continuation(prompt: str = "hello") -> DynamicContinuationRunState:
    return DynamicContinuationRunState.initial(
        prompt=prompt,
        model_prompt=None,
        current_timestamp_ms=None,
        current_prompt_is_structured=False,
        run_id="run-1",
        continuation_model_prompt_tail="",
    )


@dataclass
class _AdapterLog:
    """Call log shared by the fake adapter callbacks."""

    scope: Any = None
    released: int = 0
    closed: int = 0
    finalized: int = 0
    discards: list[EmptyRunDiscard] = field(default_factory=list)
    persisted: list[StandaloneReplaySnapshot] = field(default_factory=list)
    snapshot: TurnPartialSnapshot = field(default_factory=TurnPartialSnapshot)


def _open_scope_factory(log: _AdapterLog) -> Callable[[], AbstractContextManager[ScopeSessionContext]]:
    def _open() -> AbstractContextManager[ScopeSessionContext]:
        log.scope = object()
        return contextlib.nullcontext(cast("ScopeSessionContext", log.scope))

    return _open


def _blocking_adapter(
    log: _AdapterLog,
    run_attempt: Callable[[TurnRunState, DynamicContinuationRunState], Awaitable[Any]],
    *,
    continuation: ContinuationCapability | None = _DEFAULT_CONTINUATION,
    empty_run: EmptyRunCapability | None = None,
    with_standalone_replay: bool = True,
    unexpected_error_text: Callable[[Exception], str] | None = None,
) -> BlockingTurnAdapter:
    def _persist(_scope: ScopeSessionContext | None, snapshot: StandaloneReplaySnapshot) -> None:
        log.persisted.append(snapshot)

    return BlockingTurnAdapter(
        open_scope=_open_scope_factory(log),
        run_attempt=run_attempt,
        snapshot_partial=lambda: log.snapshot,
        release_attempt_entity=lambda _scope: _bump(log, "released"),
        close_runtime_dbs=lambda _scope: _bump(log, "closed"),
        finalize_attempt=lambda _scope: _bump(log, "finalized"),
        unexpected_error_text=unexpected_error_text,
        continuation=continuation,
        empty_run=empty_run,
        persist_standalone_replay=_persist if with_standalone_replay else None,
    )


def _streaming_adapter(
    log: _AdapterLog,
    run_attempt: Callable[
        [TurnRunState, DynamicContinuationRunState],
        AsyncGenerator[str | AttemptResolved, None],
    ],
    *,
    continuation: ContinuationCapability | None = _DEFAULT_CONTINUATION,
    empty_run: EmptyRunCapability | None = None,
    with_standalone_replay: bool = True,
    unexpected_error_text: Callable[[Exception], str] | None = None,
) -> StreamingTurnAdapter[str]:
    def _persist(_scope: ScopeSessionContext | None, snapshot: StandaloneReplaySnapshot) -> None:
        log.persisted.append(snapshot)

    return StreamingTurnAdapter[str](
        open_scope=_open_scope_factory(log),
        run_attempt=run_attempt,
        snapshot_partial=lambda: log.snapshot,
        release_attempt_entity=lambda _scope: _bump(log, "released"),
        close_runtime_dbs=lambda _scope: _bump(log, "closed"),
        finalize_attempt=lambda _scope: _bump(log, "finalized"),
        make_notice_chunk=lambda text: f"notice:{text}",
        unexpected_error_text=unexpected_error_text,
        continuation=continuation,
        empty_run=empty_run,
        persist_standalone_replay=_persist if with_standalone_replay else None,
    )


def _bump(log: _AdapterLog, attr: str) -> None:
    setattr(log, attr, getattr(log, attr) + 1)


def _empty_run_capability(log: _AdapterLog) -> EmptyRunCapability:
    return EmptyRunCapability(
        notice_text="empty notice",
        discard=lambda _scope, discard: log.discards.append(discard),
    )


async def _collect(stream: AsyncIterator[str]) -> list[str]:
    return [chunk async for chunk in stream]


def test_blocking_completion_records_and_updates_collector() -> None:
    """A completed blocking attempt records the turn and publishes run metadata once."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    collector: dict[str, Any] = {}
    trace = _trace("search")

    async def _attempt(run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        run.run_metadata = {"room_id": "!room"}
        return CompletedAttempt(
            response_text="visible",
            replayable_text="replayable",
            has_visible_content=True,
            completed_tools=(trace,),
            metadata_content={"io.mindroom.ai_run": {"usage": 1}},
        )

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(turn_recorder=cast("Any", recorder), run_metadata_collector=collector),
            continuation=_continuation(),
        ),
    )

    assert result == "visible"
    assert collector == {"io.mindroom.ai_run": {"usage": 1}}
    assert recorder.completed_calls == [
        {
            "run_metadata": {"room_id": "!room"},
            "assistant_text": "replayable",
            "completed_tools": [trace],
        },
    ]
    assert log.finalized == 1
    assert log.closed == 1


def test_blocking_completion_skips_collector_without_metadata_content() -> None:
    """No collector update happens when the attempt resolved without metadata."""
    log = _AdapterLog()
    collector: dict[str, Any] = {}

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        return CompletedAttempt(response_text="done", replayable_text="done", has_visible_content=True)

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(turn_recorder=None, run_metadata_collector=collector),
            continuation=_continuation(),
        ),
    )

    assert result == "done"
    assert collector == {}


def test_blocking_errored_attempt_returns_user_text() -> None:
    """An errored attempt short-circuits to its user-facing text."""
    log = _AdapterLog()

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> ErroredAttempt:
        return ErroredAttempt("friendly error")

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(),
            continuation=_continuation(),
        ),
    )

    assert result == "friendly error"


def test_blocking_cancelled_attempt_records_persists_and_raises() -> None:
    """A cancelled attempt without recorder persists one standalone replay and raises."""
    log = _AdapterLog()

    async def _attempt(run: TurnRunState, _c: DynamicContinuationRunState) -> CancelledAttempt:
        run.run_metadata = {"room_id": "!room"}
        return CancelledAttempt(
            reason="user stop",
            partial_text="partial",
            completed_tools=(_trace("search"),),
            interrupted_tools=(_trace("browse"),),
            session_id="session-live",
            run_id="run-live",
        )

    async def _run() -> None:
        await run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(),
            continuation=_continuation(),
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert len(log.persisted) == 1
    snapshot = log.persisted[0]
    assert snapshot.session_id == "session-live"
    assert snapshot.run_id == "run-live"
    assert snapshot.partial_text == "partial"
    assert snapshot.run_metadata == {"room_id": "!room"}
    assert log.finalized == 1
    assert log.closed == 1


def test_blocking_cancelled_attempt_with_recorder_records_twice() -> None:
    """An in-attempt cancellation records once inline and once from the outer handler."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(run: TurnRunState, _c: DynamicContinuationRunState) -> CancelledAttempt:
        run.run_metadata = {"room_id": "!room"}
        return CancelledAttempt(reason="stop", partial_text="partial")

    async def _run() -> None:
        await run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(turn_recorder=cast("Any", recorder)),
            continuation=_continuation(),
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert len(recorder.interrupted_calls) == 2
    assert recorder.interrupted_calls[0]["assistant_text"] == "partial"
    # The outer handler re-records from the recorder's canonical state.
    assert recorder.interrupted_calls[1]["run_metadata"] == {"room_id": "!room"}
    assert log.persisted == []


def test_blocking_external_cancel_builds_fallback_metadata() -> None:
    """An external cancel before prepare persists a replay with rebuilt run metadata."""
    log = _AdapterLog()
    log.snapshot = TurnPartialSnapshot(attempt_run_id=None)

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        raise asyncio.CancelledError

    async def _run() -> None:
        await run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(),
            continuation=_continuation(),
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert len(log.persisted) == 1
    snapshot = log.persisted[0]
    assert snapshot.run_id == "run-1"
    assert snapshot.partial_text == ""
    run_metadata = snapshot.run_metadata
    assert run_metadata is not None
    assert run_metadata["room_id"] == "!room"
    assert run_metadata["correlation_id"] == "corr-1"
    assert run_metadata["reply_to_event_id"] == "$reply"


def test_blocking_external_cancel_skips_persist_after_inline_persist() -> None:
    """The standalone replay is not persisted twice for one cancelled turn."""
    log = _AdapterLog()

    async def _attempt(run: TurnRunState, _c: DynamicContinuationRunState) -> CancelledAttempt:
        run.run_metadata = {"room_id": "!room"}
        return CancelledAttempt(reason="stop")

    async def _run() -> None:
        await run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(),
            continuation=_continuation(),
        )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert len(log.persisted) == 1


def test_blocking_continuation_advances_and_resets_turn_state() -> None:
    """A dynamic-tool attempt releases the entity and reruns with the continuation prompt."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    prompts: list[str] = []
    first_trace = _trace("load_tool")

    async def _attempt(
        _run: TurnRunState,
        continuation: DynamicContinuationRunState,
    ) -> CompletedAttempt:
        prompts.append(continuation.active_prompt)
        if len(prompts) == 1:
            return CompletedAttempt(
                attempt_run_id="run-1",
                tool_executions=(_dynamic_tool_execution(),),
                completed_tools=(first_trace,),
            )
        return CompletedAttempt(
            response_text="final",
            replayable_text="final",
            has_visible_content=True,
            completed_tools=(_trace("sleep"),),
        )

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(turn_recorder=cast("Any", recorder)),
            continuation=_continuation("original ask"),
        ),
    )

    assert result == "final"
    assert len(prompts) == 2
    assert prompts[0] == "original ask"
    assert "DYNAMIC TOOL CALL COMPLETED" in prompts[1]
    assert log.released == 1
    assert log.finalized == 2
    # The continuation reset synced empty partial state carrying the prior tools.
    assert recorder.synced_calls[-1]["completed_tools"] == [first_trace]
    # The final recording carries the first attempt's tools plus the second's.
    assert recorder.completed_calls[-1]["completed_tools"] == [first_trace, _trace("sleep")]


def test_blocking_continuation_limit_returns_limit_message() -> None:
    """Hitting the continuation limit surfaces the limit message when nothing is visible."""
    log = _AdapterLog()

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        return CompletedAttempt(
            attempt_run_id="run-1",
            tool_executions=(_dynamic_tool_execution(),),
        )

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(),
            continuation=_continuation(),
        ),
    )

    assert "did not produce a final answer" in result


def test_blocking_empty_run_grants_one_retry_then_notice() -> None:
    """The empty-run guard discards, retries once, then falls back to the notice."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    attempts = 0

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        nonlocal attempts
        attempts += 1
        return CompletedAttempt(is_empty=True, session_id="session-live", run_id=f"run-{attempts}")

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt, empty_run=_empty_run_capability(log)),
            TurnSinks(turn_recorder=cast("Any", recorder)),
            continuation=_continuation(),
        ),
    )

    assert result == "empty notice"
    assert attempts == 2
    assert [discard.run_id for discard in log.discards] == ["run-1", "run-2"]
    assert log.released == 1
    assert recorder.completed_calls == [
        {"run_metadata": None, "assistant_text": "", "completed_tools": []},
    ]


def test_blocking_empty_run_without_continuation_still_retries_once() -> None:
    """The empty-run guard has its own retry slot when continuations are disabled."""
    log = _AdapterLog()
    attempts = 0

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        nonlocal attempts
        attempts += 1
        return CompletedAttempt(is_empty=True)

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt, continuation=None, empty_run=_empty_run_capability(log)),
            TurnSinks(),
            continuation=_continuation(),
        ),
    )

    assert result == "empty notice"
    assert attempts == 2


def test_blocking_empty_retry_borrows_continuation_slot_within_shared_budget() -> None:
    """One empty retry plus dynamic-tool continuations share the iteration budget."""
    log = _AdapterLog()
    attempts = 0

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return CompletedAttempt(is_empty=True, run_id="run-empty")
        return CompletedAttempt(
            attempt_run_id=f"run-{attempts}",
            tool_executions=(_dynamic_tool_execution(),),
        )

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt, empty_run=_empty_run_capability(log)),
            TurnSinks(),
            continuation=_continuation(),
        ),
    )

    # Attempt 1 spends the empty retry; attempts 2-4 continue; attempt 5 sits
    # at the decision limit and settles with the limit message.
    assert attempts == 5
    assert "did not produce a final answer" in result
    assert [discard.run_id for discard in log.discards] == ["run-empty"]


def test_blocking_unexpected_error_reraises_without_shaper() -> None:
    """Unexpected exceptions propagate when no error shaper is configured."""
    log = _AdapterLog()

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        msg = "boom"
        raise RuntimeError(msg)

    async def _run() -> None:
        await run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt),
            TurnSinks(),
            continuation=_continuation(),
        )

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(_run())
    assert log.closed == 1


def test_blocking_unexpected_error_uses_shaper_when_configured() -> None:
    """Unexpected exceptions become user-facing text through the configured shaper."""
    log = _AdapterLog()

    async def _attempt(_run: TurnRunState, _c: DynamicContinuationRunState) -> CompletedAttempt:
        msg = "boom"
        raise RuntimeError(msg)

    result = asyncio.run(
        run_blocking_response_turn(
            _ctx(),
            _blocking_adapter(log, _attempt, unexpected_error_text=lambda e: f"shaped: {e}"),
            TurnSinks(),
            continuation=_continuation(),
        ),
    )

    assert result == "shaped: boom"


def test_streaming_turn_yields_chunks_and_filters_sentinel() -> None:
    """The streaming driver forwards attempt chunks and never leaks the sentinel."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(
        run: TurnRunState,
        _c: DynamicContinuationRunState,
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
                TurnSinks(turn_recorder=cast("Any", recorder)),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["hello ", "world"]
    assert recorder.completed_calls[-1]["assistant_text"] == "hello world"
    assert log.finalized == 1


def test_streaming_handled_attempt_ends_turn_without_recording() -> None:
    """A handled attempt (self-reported error) ends the turn without recording."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "friendly error"
        yield AttemptResolved(HandledAttempt())

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt),
                TurnSinks(turn_recorder=cast("Any", recorder)),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["friendly error"]
    assert recorder.completed_calls == []
    assert log.finalized == 1


def test_streaming_cancelled_attempt_records_updates_collector_and_raises() -> None:
    """A cancelled streaming attempt records, publishes metadata, persists, and raises."""
    log = _AdapterLog()
    collector: dict[str, Any] = {}

    async def _attempt(
        run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.run_metadata = {"room_id": "!room"}
        yield "partial"
        yield AttemptResolved(
            CancelledAttempt(
                reason="stop",
                partial_text="partial",
                metadata_content={"io.mindroom.ai_run": {"status": "cancelled"}},
            ),
        )

    collected: list[str] = []

    async def _run() -> None:
        async for chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(run_metadata_collector=collector),
            continuation=_continuation(),
        ):
            # A comprehension would lose the chunks yielded before the cancel.
            collected.append(chunk)  # noqa: PERF401

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert collected == ["partial"]
    assert collector == {"io.mindroom.ai_run": {"status": "cancelled"}}
    assert len(log.persisted) == 1
    assert log.finalized == 1
    assert log.closed == 1


def test_streaming_cancelled_attempt_with_recorder_records_twice() -> None:
    """An in-attempt stream cancel records inline, then again from the live snapshot."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    log.snapshot = TurnPartialSnapshot(assistant_text="snapshot partial")

    async def _attempt(
        run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        run.run_metadata = {"room_id": "!room"}
        yield "partial"
        yield AttemptResolved(CancelledAttempt(reason="stop", partial_text="attempt partial"))

    async def _run() -> None:
        async for _chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(turn_recorder=cast("Any", recorder)),
            continuation=_continuation(),
        ):
            pass

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert len(recorder.interrupted_calls) == 2
    assert recorder.interrupted_calls[0]["assistant_text"] == "attempt partial"
    # Unlike blocking, the streaming outer handler re-records from the live
    # snapshot rather than the recorder's canonical state.
    assert recorder.interrupted_calls[1]["assistant_text"] == "snapshot partial"
    assert log.persisted == []


def test_streaming_external_cancel_records_snapshot_partials() -> None:
    """An external cancel records the adapter's live partial snapshot."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    log.snapshot = TurnPartialSnapshot(
        assistant_text="live partial",
        completed_tools=(_trace("search"),),
        interrupted_tools=(_trace("browse"),),
        attempt_run_id="run-live",
    )

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "chunk"
        raise asyncio.CancelledError

    collected: list[str] = []

    async def _run() -> None:
        async for chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(turn_recorder=cast("Any", recorder)),
            continuation=_continuation(),
        ):
            # A comprehension would lose the chunks yielded before the cancel.
            collected.append(chunk)  # noqa: PERF401

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run())

    assert collected == ["chunk"]
    assert len(recorder.interrupted_calls) == 1
    assert recorder.interrupted_calls[0]["assistant_text"] == "live partial"
    assert recorder.interrupted_calls[0]["interrupted_tools"] == [_trace("browse")]
    assert log.finalized == 1


def test_streaming_external_cancel_without_recorder_persists_snapshot() -> None:
    """A recorder-less external cancel persists the standalone replay from the snapshot."""
    log = _AdapterLog()
    log.snapshot = TurnPartialSnapshot(assistant_text="live partial", attempt_run_id="run-live")

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
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

    assert len(log.persisted) == 1
    assert log.persisted[0].partial_text == "live partial"
    assert log.persisted[0].run_id == "run-live"


def test_streaming_empty_run_retries_then_yields_notice_and_records() -> None:
    """The streaming empty-run guard retries once, then yields the notice and records."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()
    attempts = 0

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        nonlocal attempts
        attempts += 1
        yield AttemptResolved(CompletedAttempt(is_empty=True, run_id=f"run-{attempts}"))

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt, empty_run=_empty_run_capability(log)),
                TurnSinks(turn_recorder=cast("Any", recorder)),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["notice:empty notice"]
    assert attempts == 2
    assert [discard.run_id for discard in log.discards] == ["run-1", "run-2"]
    # The notice-only turn still records an empty completion.
    assert recorder.completed_calls[-1]["assistant_text"] == ""


def test_streaming_continuation_advances_then_finishes() -> None:
    """A streamed dynamic-tool attempt continues the turn and streams the second attempt."""
    log = _AdapterLog()
    prompts: list[str] = []

    async def _attempt(
        _run: TurnRunState,
        continuation: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        prompts.append(continuation.active_prompt)
        if len(prompts) == 1:
            yield "loading tool"
            yield AttemptResolved(
                CompletedAttempt(attempt_run_id="run-1", tool_executions=(_dynamic_tool_execution(),)),
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

    assert chunks == ["loading tool", "final answer"]
    assert len(prompts) == 2
    assert "DYNAMIC TOOL CALL COMPLETED" in prompts[1]
    assert log.released == 1
    assert log.finalized == 2


def test_streaming_continuation_limit_yields_limit_message() -> None:
    """Hitting the continuation limit mid-stream yields the limit notice chunk."""
    log = _AdapterLog()

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield AttemptResolved(
            CompletedAttempt(attempt_run_id="run-1", tool_executions=(_dynamic_tool_execution(),)),
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


def test_streaming_finalize_runs_when_attempt_raises() -> None:
    """The per-attempt finalize hook runs even when the attempt raises mid-stream."""
    log = _AdapterLog()

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "chunk"
        msg = "stream blew up"
        raise RuntimeError(msg)

    async def _run() -> None:
        async for _chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(),
            continuation=_continuation(),
        ):
            pass

    with pytest.raises(RuntimeError, match="stream blew up"):
        asyncio.run(_run())
    assert log.finalized == 1
    assert log.closed == 1


def test_streaming_unexpected_error_yields_shaped_text() -> None:
    """Unexpected streaming exceptions become one shaped terminal chunk when configured."""
    log = _AdapterLog()

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "chunk"
        msg = "boom"
        raise RuntimeError(msg)

    chunks = asyncio.run(
        _collect(
            stream_response_turn(
                _ctx(),
                _streaming_adapter(log, _attempt, unexpected_error_text=lambda e: f"shaped: {e}"),
                TurnSinks(),
                continuation=_continuation(),
            ),
        ),
    )

    assert chunks == ["chunk", "shaped: boom"]


def test_streaming_attempt_without_sentinel_raises() -> None:
    """A streaming attempt that never yields its sentinel fails loudly."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "chunk"

    async def _run() -> None:
        async for _chunk in stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(turn_recorder=cast("Any", recorder)),
            continuation=_continuation(),
        ):
            pass

    with pytest.raises(RuntimeError, match="AttemptResolved sentinel"):
        asyncio.run(_run())
    assert recorder.completed_calls == []
    assert log.finalized == 1
    assert log.closed == 1


def test_streaming_aclose_runs_cleanup_without_recording() -> None:
    """Closing the driver generator mid-stream cleans up and records nothing."""
    log = _AdapterLog()
    recorder = _FakeTurnRecorder()

    async def _attempt(
        _run: TurnRunState,
        _c: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        yield "first"
        yield "second"
        yield AttemptResolved(CompletedAttempt(replayable_text="full", has_visible_content=True))

    async def _run() -> None:
        stream = stream_response_turn(
            _ctx(),
            _streaming_adapter(log, _attempt),
            TurnSinks(turn_recorder=cast("Any", recorder)),
            continuation=_continuation(),
        )
        assert await anext(stream) == "first"
        await stream.aclose()

    asyncio.run(_run())

    assert log.closed == 1
    assert log.finalized == 1
    assert recorder.completed_calls == []
    assert recorder.interrupted_calls == []


def test_stream_resolution_union_covers_handled() -> None:
    """The streaming resolution union accepts the handled sentinel."""
    resolution: StreamAttemptResolution = HandledAttempt()
    assert isinstance(resolution, HandledAttempt)
