"""Characterization pins: dynamic-tool continuations deliver the final body exactly once.

A two-attempt continuation turn (the first attempt ends with a dynamic
``load_tool`` call, the second completes) is driven through the real
response-turn drivers (``run_blocking_response_turn`` /
``stream_response_turn``) at the ResponseRunner seam, with a spying
DeliveryGateway. The pins:

1. no ``deliver_final``/``finalize_streamed_response`` call happens between
   attempt 1 and attempt 2;
2. exactly one final delivery call happens, after the final attempt;
3. the first attempt's text never appears as a delivered terminal body.
"""

from __future__ import annotations

import contextlib
import json
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from agno.models.response import ToolExecution

from mindroom.delivery_gateway import (
    DeliveryGateway,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    StreamingDeliveryRequest,
)
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.response_turn import (
    AttemptResolved,
    BlockingTurnAdapter,
    CompletedAttempt,
    DynamicContinuationRunState,
    ResponseTurnContext,
    StreamingTurnAdapter,
    TurnPartialSnapshot,
    TurnRunState,
    TurnSinks,
    run_blocking_response_turn,
    stream_response_turn,
)
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import _bot, _noop_typing, _plain_request, _target

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
    from contextlib import AbstractContextManager
    from pathlib import Path

    from mindroom.history.runtime import ScopeSessionContext
    from mindroom.history.turn_recorder import TurnRecorder


def _dynamic_tool_execution(tool_name: str = "sleep") -> ToolExecution:
    """Return the dynamic-tools manager call that stops the provider loop."""
    return ToolExecution(
        tool_call_id="call-load_tool",
        tool_name="load_tool",
        tool_args={"tool_name": tool_name},
        result=json.dumps({"status": "loaded", "tool": "dynamic_tools", "tool_name": tool_name}),
        stop_after_tool_call=True,
    )


def _completed_outcome(event_id: str, body: str) -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome(
        terminal_status="completed",
        event_id=event_id,
        is_visible_response=True,
        final_visible_body=body,
        delivery_kind="sent",
    )


def _open_scope() -> AbstractContextManager[ScopeSessionContext | None]:
    return contextlib.nullcontext(None)


def _noop(*_args: object) -> None:
    return None


def _blocking_adapter(
    run_attempt: Callable[[TurnRunState, DynamicContinuationRunState], Awaitable[CompletedAttempt]],
) -> BlockingTurnAdapter:
    return BlockingTurnAdapter(
        open_scope=_open_scope,
        run_attempt=run_attempt,
        snapshot_partial=TurnPartialSnapshot,
        release_attempt_entity=_noop,
        close_runtime_dbs=_noop,
        discard_empty_run=_noop,
    )


def _streaming_adapter(
    run_attempt: Callable[
        [TurnRunState, DynamicContinuationRunState],
        AsyncGenerator[str | AttemptResolved, None],
    ],
) -> StreamingTurnAdapter[str]:
    return StreamingTurnAdapter[str](
        open_scope=_open_scope,
        run_attempt=run_attempt,
        snapshot_partial=TurnPartialSnapshot,
        release_attempt_entity=_noop,
        close_runtime_dbs=_noop,
        discard_empty_run=_noop,
        make_text_chunk=lambda text: text,
    )


def _initial_continuation(
    *,
    prompt: str,
    model_prompt: str | None,
    current_timestamp_ms: float | None,
    current_prompt_is_structured: bool,
) -> DynamicContinuationRunState:
    """Mirror ``ai._initial_agent_continuation`` for the plain request prompt."""
    return DynamicContinuationRunState.initial(
        prompt=prompt,
        model_prompt=model_prompt,
        current_timestamp_ms=current_timestamp_ms,
        current_prompt_is_structured=current_prompt_is_structured,
        current_event_id=None,
        run_id=None,
        continuation_model_prompt_tail="",
    )


@pytest.mark.asyncio
async def test_blocking_continuation_delivers_final_exactly_once_after_last_attempt(tmp_path: Path) -> None:
    """A blocking continuation turn calls deliver_final once, after the final attempt."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    events: list[str] = []
    attempts: list[str] = []
    delivered: list[FinalDeliveryRequest] = []

    async def spy_deliver_final(_self: DeliveryGateway, request: FinalDeliveryRequest) -> FinalDeliveryOutcome:
        events.append("deliver_final")
        delivered.append(request)
        return _completed_outcome("$response", body=request.response_text)

    async def run_attempt(_run: TurnRunState, continuation: DynamicContinuationRunState) -> CompletedAttempt:
        attempts.append(continuation.active_prompt)
        events.append(f"attempt:{len(attempts)}")
        # No final delivery may happen before the last attempt settles.
        assert not delivered
        if len(attempts) == 1:
            return CompletedAttempt(
                response_text="first-attempt draft",
                replayable_text="first-attempt draft",
                has_visible_content=True,
                attempt_run_id="run-1",
                tool_executions=(_dynamic_tool_execution(),),
            )
        return CompletedAttempt(
            response_text="final answer",
            replayable_text="final answer",
            has_visible_content=True,
            attempt_run_id="run-2",
        )

    async def fake_ai_response(
        *args: object,
        prompt: str,
        model_prompt: str | None,
        current_timestamp_ms: float | None,
        current_prompt_is_structured: bool,
        turn_recorder: TurnRecorder | None,
        run_metadata_collector: dict[str, Any] | None,
        **_kwargs: object,
    ) -> str:
        return await run_blocking_response_turn(
            cast("ResponseTurnContext", args[0]),
            _blocking_adapter(run_attempt),
            TurnSinks(
                turn_recorder=turn_recorder,
                run_metadata_collector=run_metadata_collector,
            ),
            continuation=_initial_continuation(
                prompt=prompt,
                model_prompt=model_prompt,
                current_timestamp_ms=current_timestamp_ms,
                current_prompt_is_structured=current_prompt_is_structured,
            ),
        )

    with (
        patch.object(DeliveryGateway, "deliver_final", new=spy_deliver_final),
        patch_response_runner_module(ai_response=fake_ai_response, typing_indicator=_noop_typing),
    ):
        generation = await coordinator._process_and_respond(_plain_request(_target()))

    assert attempts[0] == "hello"
    assert len(attempts) == 2
    assert "DYNAMIC TOOL CALL COMPLETED" in attempts[1]
    assert len(delivered) == 1
    assert delivered[0].response_text == "final answer"
    assert events == ["attempt:1", "attempt:2", "deliver_final"]
    assert generation.delivery.event_id == "$response"


@pytest.mark.asyncio
async def test_streaming_continuation_finalizes_exactly_once_after_last_attempt(tmp_path: Path) -> None:
    """A streaming continuation turn finalizes once, after the final attempt's stream."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    events: list[str] = []
    attempts: list[str] = []
    streamed_chunks: list[str] = []
    finalize_requests: list[FinalizeStreamedResponseRequest] = []

    async def spy_deliver_stream(_self: DeliveryGateway, request: StreamingDeliveryRequest) -> StreamTransportOutcome:
        events.append("deliver_stream")
        streamed_chunks.extend([chunk async for chunk in request.response_stream])
        return StreamTransportOutcome(
            last_physical_stream_event_id="$stream",
            terminal_status="completed",
            rendered_body="".join(streamed_chunks),
            visible_body_state="visible_body",
        )

    async def spy_finalize(
        _self: DeliveryGateway,
        request: FinalizeStreamedResponseRequest,
    ) -> FinalDeliveryOutcome:
        events.append("finalize")
        finalize_requests.append(request)
        return _completed_outcome("$stream", body=request.stream_transport_outcome.visible_body_text)

    async def run_attempt(
        _run: TurnRunState,
        continuation: DynamicContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        attempts.append(continuation.active_prompt)
        events.append(f"attempt:{len(attempts)}")
        # No stream finalization may happen between attempts.
        assert not finalize_requests
        if len(attempts) == 1:
            yield "loading tool… "
            yield AttemptResolved(
                CompletedAttempt(
                    response_text="first-attempt terminal text",
                    replayable_text="first-attempt terminal text",
                    attempt_run_id="run-1",
                    tool_executions=(_dynamic_tool_execution(),),
                ),
            )
            return
        yield "final answer"
        yield AttemptResolved(
            CompletedAttempt(
                replayable_text="final answer",
                has_visible_content=True,
                attempt_run_id="run-2",
            ),
        )

    def fake_stream_agent_response(
        *args: object,
        prompt: str,
        model_prompt: str | None,
        current_timestamp_ms: float | None,
        current_prompt_is_structured: bool,
        turn_recorder: TurnRecorder | None,
        run_metadata_collector: dict[str, Any] | None,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        return stream_response_turn(
            cast("ResponseTurnContext", args[0]),
            _streaming_adapter(run_attempt),
            TurnSinks(
                turn_recorder=turn_recorder,
                run_metadata_collector=run_metadata_collector,
            ),
            continuation=_initial_continuation(
                prompt=prompt,
                model_prompt=model_prompt,
                current_timestamp_ms=current_timestamp_ms,
                current_prompt_is_structured=current_prompt_is_structured,
            ),
        )

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=spy_deliver_stream),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=spy_finalize),
        patch_response_runner_module(stream_agent_response=fake_stream_agent_response, typing_indicator=_noop_typing),
    ):
        generation = await coordinator._process_and_respond_streaming(_plain_request(_target()))

    assert attempts[0] == "hello"
    assert len(attempts) == 2
    assert "DYNAMIC TOOL CALL COMPLETED" in attempts[1]
    # The first attempt's terminal text is superseded by the rerun and never delivered.
    assert streamed_chunks == ["loading tool… ", "final answer"]
    assert len(finalize_requests) == 1
    transport = finalize_requests[0].stream_transport_outcome
    assert "first-attempt terminal text" not in transport.visible_body_text
    assert transport.visible_body_text == "loading tool… final answer"
    assert events == ["deliver_stream", "attempt:1", "attempt:2", "finalize"]
    assert generation.delivery.event_id == "$stream"
