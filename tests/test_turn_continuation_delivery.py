"""Characterization pins: same-turn continuations deliver the final body exactly once.

A two-attempt continuation turn (the first attempt ends with a
``switch_thread_model`` call for the current response, the second completes) is driven through the real
response-turn driver (``stream_response_turn``, which the runner collects for
non-streaming delivery) through ``ResponseRunner.generate_response``, with a spying DeliveryGateway. The pins:

1. no ``deliver_final``/``finalize_streamed_response`` call happens between
   attempt 1 and attempt 2;
2. exactly one final delivery call happens, after the final attempt;
3. the first attempt's text never appears as a delivered terminal body.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom.delivery_gateway import (
    DeliveryGateway,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    StreamingDeliveryRequest,
)
from mindroom.history.session_context import ScopeSessionContext
from mindroom.history.types import HistoryScope
from mindroom.llm.runs import ToolExecution
from mindroom.response_sources import Owned
from mindroom.response_turn import (
    AttemptResolved,
    CompletedAttempt,
    ResponseTurnContext,
    TurnAdapter,
    TurnContinuationRunState,
    TurnRunState,
    TurnSinks,
    stream_response_turn,
)
from tests.conftest import patch_response_runner_module, unwrap_extracted_collaborator
from tests.response_runner_helpers import (
    _bot,
    _noop_typing,
    _plain_request,
    _target,
    completed_outcome,
    stream_transport,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable
    from pathlib import Path

    from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome


def _continuing_tool_execution() -> ToolExecution:
    """Return a model switch for the current response, which forces one more attempt."""
    return ToolExecution(
        tool_call_id="call-switch-after-toolcall",
        tool_name="switch_thread_model",
        tool_args={"model_name": "large", "when": "after-toolcall"},
        result=json.dumps(
            {"action": "switch", "model": "large", "status": "ok", "tool": "thread_model", "when": "after-toolcall"},
        ),
        stop_after_call=True,
    )


def _load_scope() -> ScopeSessionContext:
    storage = MagicMock()
    return ScopeSessionContext(
        scope=HistoryScope(kind="agent", scope_id="general"),
        storage=storage,
        session=None,
        session_id="session-1",
        storage_factory=lambda: storage,
    )


def _stream_turn(
    ctx: ResponseTurnContext,
    sinks: TurnSinks,
    run_attempt: Callable[
        [TurnRunState, TurnContinuationRunState],
        AsyncGenerator[str | AttemptResolved, None],
    ],
) -> AsyncIterator[str]:
    """Drive the turn as ``ai.stream_agent_response`` does, from the plain request prompt."""
    return stream_response_turn(
        ctx,
        TurnAdapter[str](
            load_scope=_load_scope,
            run_attempt=run_attempt,
            make_text_chunk=lambda text: text,
            user_message=ctx.prompt,
            unexpected_error_text=str,
            entity_name=ctx.entity_label,
        ),
        sinks,
        continuation=TurnContinuationRunState.initial(
            prompt=ctx.prompt,
            model_prompt=ctx.model_prompt,
            current_timestamp_ms=ctx.current_timestamp_ms,
            current_prompt_is_structured=ctx.current_prompt_is_structured,
            current_event_id=None,
            run_id=None,
            continuation_model_prompt_tail="",
        ),
    )


@pytest.mark.asyncio
async def test_non_streaming_continuation_delivers_final_exactly_once_after_last_attempt(tmp_path: Path) -> None:
    """A continuation turn delivered without streaming calls deliver_final once, after the final attempt."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    events: list[str] = []
    attempts: list[str] = []
    delivered: list[FinalDeliveryRequest] = []

    async def spy_deliver_final(_self: DeliveryGateway, request: FinalDeliveryRequest) -> FinalDeliveryOutcome:
        events.append("deliver_final")
        delivered.append(request)
        return completed_outcome("$response", body=request.response_text)

    async def run_attempt(
        _run: TurnRunState,
        continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        attempts.append(continuation.active_prompt)
        events.append(f"attempt:{len(attempts)}")
        # No final delivery may happen before the last attempt settles.
        assert not delivered
        if len(attempts) == 1:
            yield AttemptResolved(
                CompletedAttempt(
                    response_text="first-attempt draft",
                    replayable_text="first-attempt draft",
                    has_visible_content=True,
                    attempt_run_id="run-1",
                    tool_executions=(_continuing_tool_execution(),),
                ),
            )
            return
        yield AttemptResolved(
            CompletedAttempt(
                response_text="final answer",
                replayable_text="final answer",
                has_visible_content=True,
                attempt_run_id="run-2",
            ),
        )

    def fake_stream_agent_response(
        ctx: ResponseTurnContext,
        *_args: object,
        sinks: TurnSinks,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        return _stream_turn(ctx, sinks, run_attempt)

    effects = AsyncMock()
    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch.object(DeliveryGateway, "deliver_final", new=spy_deliver_final),
        patch_response_runner_module(
            stream_agent_response=fake_stream_agent_response,
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=effects,
        ),
    ):
        fate = await coordinator.generate_response(_plain_request(_target()))

    assert attempts[0] == "hello"
    assert len(attempts) == 2
    assert "MODEL SWITCH TOOL CALL COMPLETED" in attempts[1]
    assert len(delivered) == 1
    assert delivered[0].response_text == "final answer"
    assert events == ["attempt:1", "attempt:2", "deliver_final"]
    assert fate == Owned()
    # The one delivered answer reached the reply's post-response effects.
    assert effects.await_args.args[0].final_visible_event_id == "$response"


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
        return stream_transport("$stream", "".join(streamed_chunks))

    async def spy_finalize(
        _self: DeliveryGateway,
        request: FinalizeStreamedResponseRequest,
    ) -> FinalDeliveryOutcome:
        events.append("finalize")
        finalize_requests.append(request)
        return completed_outcome("$stream", body=request.stream_transport_outcome.visible_body_text)

    async def run_attempt(
        _run: TurnRunState,
        continuation: TurnContinuationRunState,
    ) -> AsyncGenerator[str | AttemptResolved, None]:
        attempts.append(continuation.active_prompt)
        events.append(f"attempt:{len(attempts)}")
        # No stream finalization may happen between attempts.
        assert not finalize_requests
        if len(attempts) == 1:
            yield "switching models… "
            yield AttemptResolved(
                CompletedAttempt(
                    response_text="first-attempt terminal text",
                    replayable_text="first-attempt terminal text",
                    attempt_run_id="run-1",
                    tool_executions=(_continuing_tool_execution(),),
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
        ctx: ResponseTurnContext,
        *_args: object,
        sinks: TurnSinks,
        **_kwargs: object,
    ) -> AsyncIterator[str]:
        return _stream_turn(ctx, sinks, run_attempt)

    effects = AsyncMock()
    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch.object(DeliveryGateway, "deliver_stream", new=spy_deliver_stream),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=spy_finalize),
        patch_response_runner_module(
            stream_agent_response=fake_stream_agent_response,
            should_use_streaming=AsyncMock(return_value=True),
            typing_indicator=_noop_typing,
            apply_post_response_effects=effects,
        ),
    ):
        fate = await coordinator.generate_response(_plain_request(_target()))

    assert attempts[0] == "hello"
    assert len(attempts) == 2
    assert "MODEL SWITCH TOOL CALL COMPLETED" in attempts[1]
    # The first attempt's terminal text is superseded by the rerun and never delivered.
    assert streamed_chunks == ["switching models… ", "final answer"]
    assert len(finalize_requests) == 1
    transport = finalize_requests[0].stream_transport_outcome
    assert "first-attempt terminal text" not in transport.visible_body_text
    assert transport.visible_body_text == "switching models… final answer"
    assert events == ["deliver_stream", "attempt:1", "attempt:2", "finalize"]
    assert fate == Owned()
    # The one delivered answer reached the reply's post-response effects.
    assert effects.await_args.args[0].final_visible_event_id == "$stream"
