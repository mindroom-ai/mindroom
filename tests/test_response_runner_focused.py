"""Focused unit tests for ResponseRunner and ResponseAttemptRunner.

These pin the response-execution seam directly (lifecycle lock, attempt
mechanics, cancellation, streaming vs non-streaming delivery, queued-notice
state, and post-response effects) with mocked collaborators instead of a full
orchestrator/bot boot, so shrinking ``response_runner.py`` has a safety net.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mindroom import background_tasks as background_tasks_module
from mindroom import response_runner
from mindroom.background_tasks import wait_for_background_tasks
from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import STREAM_STATUS_KEY, STREAM_STATUS_PENDING
from mindroom.conversation_resolver import ConversationResolver, MessageContext
from mindroom.delivery_gateway import DeliveryGateway
from mindroom.final_delivery import FinalDeliveryOutcome, StreamTransportOutcome
from mindroom.history.turn_recorder import TurnRecorder
from mindroom.hooks import MessageEnvelope
from mindroom.logging_config import get_logger
from mindroom.matrix.cache import ThreadHistoryResult
from mindroom.matrix.users import AgentMatrixUser
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome, apply_post_response_effects
from mindroom.response_attempt import ResponseAttemptDeps, ResponseAttemptRequest, ResponseAttemptRunner
from mindroom.response_lifecycle import ResponseLifecycleCoordinator
from mindroom.response_payload_preparation import DispatchPayloadInputs, ResponsePayloadPreparation
from mindroom.response_runner import ResponseRequest, _ResponseGenerationOutcome
from mindroom.stop import StopManager
from mindroom.streaming import StreamingDeliveryError
from mindroom.turn_policy import PreparedDispatch
from tests.conftest import (
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    make_matrix_client_mock,
    message_origin,
    patch_response_runner_module,
    replace_response_runner_deps,
    request_envelope,
    runtime_paths_for,
    test_runtime_paths,
    unwrap_extracted_collaborator,
    wrap_extracted_collaborators,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable
    from pathlib import Path

    from nio import AsyncClient


def _config(tmp_path: Path) -> Config:
    return bind_runtime_paths(
        Config(
            agents={"general": AgentConfig(display_name="General", rooms=["!room:localhost"])},
            teams={},
            models={"default": ModelConfig(provider="openai", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        test_runtime_paths(tmp_path),
    )


def _bot(tmp_path: Path) -> AgentBot:
    config = _config(tmp_path)
    agent_user = AgentMatrixUser(
        agent_name="general",
        password=TEST_PASSWORD,
        display_name="General",
        user_id="@mindroom_general:localhost",
    )
    bot = AgentBot(agent_user, tmp_path, config, runtime_paths_for(config), rooms=["!room:localhost"])
    bot.client = make_matrix_client_mock(user_id="@mindroom_general:localhost")
    install_runtime_cache_support(bot)
    wrap_extracted_collaborators(bot)
    return bot


def _target(*, thread_id: str | None = None, reply_to_event_id: str = "$event") -> MessageTarget:
    return MessageTarget.resolve(
        room_id="!room:localhost",
        thread_id=thread_id,
        reply_to_event_id=reply_to_event_id,
        room_mode=thread_id is None,
    )


def _envelope(target: MessageTarget, *, source_event_id: str = "$event") -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id=source_event_id,
        room_id="!room:localhost",
        target=target,
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="general",
        source_kind="message",
        origin=message_origin(sender_id="@user:localhost", requester_id="@user:localhost", source_kind="message"),
    )


def _preparation(target: MessageTarget, envelope: MessageEnvelope) -> ResponsePayloadPreparation:
    return ResponsePayloadPreparation(
        dispatch=PreparedDispatch(
            requester_user_id="@user:localhost",
            context=MessageContext(
                am_i_mentioned=True,
                is_thread=target.resolved_thread_id is not None,
                thread_id=target.resolved_thread_id,
                thread_history=[],
                mentioned_agents=[],
                has_non_agent_mentions=False,
            ),
            target=target,
            correlation_id=envelope.source_event_id,
            envelope=envelope,
        ),
        prompt="hello",
        action_kind="individual",
        payload_inputs=DispatchPayloadInputs(
            message_attachment_ids=(),
            trusted_attachment_ids=(),
            media_events=(),
        ),
        target_member_names=None,
        dispatch_started_at=1.0,
        context_ready_monotonic=2.0,
    )


def _plain_request(target: MessageTarget, *, source_event_id: str = "$event") -> ResponseRequest:
    return ResponseRequest(
        thread_history=[],
        prompt="hello",
        user_id="@user:localhost",
        response_envelope=_envelope(target, source_event_id=source_event_id),
    )


def _completed_outcome(event_id: str = "$response", body: str = "ok") -> FinalDeliveryOutcome:
    return FinalDeliveryOutcome(
        terminal_status="completed",
        event_id=event_id,
        is_visible_response=True,
        final_visible_body=body,
        delivery_kind="sent",
    )


@asynccontextmanager
async def _noop_typing(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


class RecordingStopManager(StopManager):
    """Real StopManager whose deferred clear is made immediate and observable."""

    def __init__(self) -> None:
        super().__init__()
        self.cleared: list[str] = []

    def clear_message(
        self,
        message_id: str,
        client: AsyncClient,
        remove_button: bool = True,
        delay: float = 5.0,
        notify_outbound_redaction: Callable[[str, str], None] | None = None,
    ) -> None:
        """Record the clear request and drop tracking without the production delay."""
        del client, remove_button, delay, notify_outbound_redaction
        self.cleared.append(message_id)
        self.tracked_messages.pop(message_id, None)


def _attempt_runner(tmp_path: Path, stop_manager: StopManager) -> tuple[ResponseAttemptRunner, MagicMock]:
    gateway = MagicMock(spec=DeliveryGateway)
    gateway.send_text = AsyncMock(return_value="$placeholder")
    runner = ResponseAttemptRunner(
        ResponseAttemptDeps(
            client=make_matrix_client_mock(),
            delivery_gateway=gateway,
            stop_manager=stop_manager,
            logger=get_logger("tests.response_attempt"),
            show_stop_button=lambda: False,
            config=_config(tmp_path),
            notify_outbound_event=MagicMock(),
            notify_outbound_redaction=MagicMock(),
        ),
    )
    return runner, gateway


# ---------------------------------------------------------------------------
# 1. Lifecycle lock: serialization, post-lock history refresh, prepare ordering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_requests_serialize_and_refresh_history_under_lock(tmp_path: Path) -> None:
    """Two requests for one thread serialize; each refreshes history under lock, then prepares the payload."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    events: list[str] = []
    refreshed = [ThreadHistoryResult([], is_full_history=True), ThreadHistoryResult([], is_full_history=True)]
    prepare_history_by_turn: dict[int, object] = {}
    gate = asyncio.Event()
    first_turn_started = asyncio.Event()

    def _turn(request: ResponseRequest) -> int:
        return 1 if request.response_envelope.source_event_id == "$event1" else 2

    refresh_calls = 0

    async def fake_fetch(room_id: str, thread_id: str, *, caller_label: str) -> ThreadHistoryResult:
        nonlocal refresh_calls
        assert (room_id, thread_id, caller_label) == ("!room:localhost", "$thread", "dispatch_post_lock_refresh")
        refresh_calls += 1
        events.append(f"refresh:{refresh_calls}")
        return refreshed[refresh_calls - 1]

    async def spy_prepare(request: ResponseRequest) -> ResponseRequest:
        turn = _turn(request)
        events.append(f"prepare:{turn}")
        prepare_history_by_turn[turn] = request.thread_history
        return replace(request, payload_preparation=None, requires_model_history_refresh=False)

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = kwargs["response_function"]
        await response_function(None)  # type: ignore[operator]
        return "$response"

    async def fake_process_and_respond(request: ResponseRequest, **_kwargs: object) -> _ResponseGenerationOutcome:
        turn = _turn(request)
        events.append(f"respond_start:{turn}")
        if turn == 1:
            first_turn_started.set()
            await gate.wait()
        events.append(f"respond_end:{turn}")
        return _ResponseGenerationOutcome(delivery=_completed_outcome(), run_succeeded=True)

    def _request_for(turn: int) -> ResponseRequest:
        target = _target(thread_id="$thread", reply_to_event_id=f"$event{turn}")
        envelope = _envelope(target, source_event_id=f"$event{turn}")
        return ResponseRequest(
            thread_history=[],
            prompt="hello",
            user_id="@user:localhost",
            response_envelope=envelope,
            payload_preparation=_preparation(target, envelope),
            on_lifecycle_lock_acquired=lambda turn=turn: events.append(f"lock:{turn}"),
        )

    with (
        patch.object(ConversationResolver, "fetch_thread_history", new=AsyncMock(side_effect=fake_fetch)),
        patch.object(bot._request_payload_preparer, "prepare", new=AsyncMock(side_effect=spy_prepare)),
        patch.object(
            coordinator,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch.object(coordinator, "process_and_respond", new=AsyncMock(side_effect=fake_process_and_respond)),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=False),
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        first = asyncio.create_task(coordinator.generate_response(_request_for(1)))
        await asyncio.wait_for(first_turn_started.wait(), timeout=2)
        second = asyncio.create_task(coordinator.generate_response(_request_for(2)))
        for _ in range(20):
            await asyncio.sleep(0)
        # The second turn must not enter the locked section while the first is in flight.
        assert "lock:2" not in events
        gate.set()
        assert await asyncio.wait_for(first, timeout=2) == "$response"
        assert await asyncio.wait_for(second, timeout=2) == "$response"

    assert events == [
        "lock:1",
        "refresh:1",
        "prepare:1",
        "respond_start:1",
        "respond_end:1",
        "lock:2",
        "refresh:2",
        "prepare:2",
        "respond_start:2",
        "respond_end:2",
    ]
    # Each turn's payload preparation consumed the history refreshed under its own lock.
    assert prepare_history_by_turn[1] is refreshed[0]
    assert prepare_history_by_turn[2] is refreshed[1]


# ---------------------------------------------------------------------------
# 2. Attempt mechanics: placeholder, stop tracking on success/failure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_placeholder_sent_before_generation_and_passed_to_response_function(tmp_path: Path) -> None:
    """The thinking placeholder lands before generation starts and its event id feeds the attempt."""
    stop_manager = RecordingStopManager()
    runner, gateway = _attempt_runner(tmp_path, stop_manager)
    order: list[object] = []

    async def send_text(request: object) -> str:
        order.append(("placeholder", request.response_text, request.extra_content))  # type: ignore[attr-defined]
        return "$placeholder"

    gateway.send_text = AsyncMock(side_effect=send_text)

    async def respond(message_id: str | None) -> None:
        order.append(("generate", message_id))

    result = await runner.run(
        ResponseAttemptRequest(target=_target(), response_function=respond, thinking_message="Thinking..."),
    )

    assert result == "$placeholder"
    assert order == [
        ("placeholder", "Thinking...", {STREAM_STATUS_KEY: STREAM_STATUS_PENDING}),
        ("generate", "$placeholder"),
    ]


@pytest.mark.asyncio
async def test_existing_event_id_skips_placeholder(tmp_path: Path) -> None:
    """An adopted existing event suppresses the placeholder send entirely."""
    stop_manager = RecordingStopManager()
    runner, gateway = _attempt_runner(tmp_path, stop_manager)
    seen: list[str | None] = []

    async def respond(message_id: str | None) -> None:
        seen.append(message_id)

    result = await runner.run(
        ResponseAttemptRequest(target=_target(), response_function=respond, existing_event_id="$existing"),
    )

    assert result == "$existing"
    assert seen == ["$existing"]
    gateway.send_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_thinking_message_and_existing_event_are_mutually_exclusive(tmp_path: Path) -> None:
    """Passing both a thinking message and an existing event id is a caller bug."""
    runner, _gateway = _attempt_runner(tmp_path, RecordingStopManager())

    async def respond(_message_id: str | None) -> None:
        pytest.fail("response_function must not run")

    with pytest.raises(ValueError, match="mutually exclusive"):
        await runner.run(
            ResponseAttemptRequest(
                target=_target(),
                response_function=respond,
                thinking_message="Thinking...",
                existing_event_id="$existing",
            ),
        )


@pytest.mark.asyncio
async def test_stop_tracking_registered_during_run_and_cleared_on_success(tmp_path: Path) -> None:
    """The attempt is stop-trackable while generating and tracking clears after success."""
    stop_manager = RecordingStopManager()
    runner, _gateway = _attempt_runner(tmp_path, stop_manager)
    target = _target()
    observed: list[tuple[MessageTarget, str | None, bool]] = []

    async def respond(message_id: str | None) -> None:
        assert message_id is not None
        tracked = stop_manager.tracked_messages[message_id]
        observed.append((tracked.target, tracked.run_id, tracked.task.done()))

    result = await runner.run(
        ResponseAttemptRequest(
            target=target,
            response_function=respond,
            thinking_message="Thinking...",
            run_id="run-1",
        ),
    )

    assert result == "$placeholder"
    assert observed == [(target, "run-1", False)]
    assert stop_manager.cleared == ["$placeholder"]
    assert stop_manager.tracked_messages == {}


@pytest.mark.asyncio
async def test_stop_tracking_cleared_on_failure(tmp_path: Path) -> None:
    """Generation failures re-raise but never leave dangling stop tracking."""
    stop_manager = RecordingStopManager()
    runner, _gateway = _attempt_runner(tmp_path, stop_manager)

    async def respond(_message_id: str | None) -> None:
        msg = "generation exploded"
        raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="generation exploded"):
        await runner.run(
            ResponseAttemptRequest(target=_target(), response_function=respond, thinking_message="Thinking..."),
        )

    assert stop_manager.cleared == ["$placeholder"]
    assert stop_manager.tracked_messages == {}


@pytest.mark.asyncio
async def test_attempt_without_visible_message_tracks_synthetic_key(tmp_path: Path) -> None:
    """No placeholder and no existing event still produces stop-trackable state."""
    stop_manager = RecordingStopManager()
    runner, gateway = _attempt_runner(tmp_path, stop_manager)
    tracked_keys: list[str] = []

    async def respond(message_id: str | None) -> None:
        assert message_id is None
        tracked_keys.extend(stop_manager.tracked_messages)

    result = await runner.run(ResponseAttemptRequest(target=_target(), response_function=respond))

    assert result is None
    gateway.send_text.assert_not_awaited()
    assert len(tracked_keys) == 1
    assert tracked_keys[0].startswith("__pending_response__:")
    assert stop_manager.cleared == tracked_keys
    assert stop_manager.tracked_messages == {}


# ---------------------------------------------------------------------------
# 3. Cancellation: user stop mid-generation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_stop_mid_generation_cancels_task_and_clears_tracking(tmp_path: Path) -> None:
    """A stop reaction mid-generation cancels the attempt, records the outcome, and clears tracking."""
    stop_manager = RecordingStopManager()
    runner, _gateway = _attempt_runner(tmp_path, stop_manager)
    started = asyncio.Event()
    cancel_reasons: list[str] = []

    async def respond(_message_id: str | None) -> None:
        started.set()
        await asyncio.Event().wait()

    run_task = asyncio.create_task(
        runner.run(
            ResponseAttemptRequest(
                target=_target(),
                response_function=respond,
                thinking_message="Thinking...",
                on_cancelled=cancel_reasons.append,
            ),
        ),
    )
    await asyncio.wait_for(started.wait(), timeout=2)
    tracked = stop_manager.tracked_messages["$placeholder"]

    assert await stop_manager.handle_stop_reaction("$placeholder") is True
    # The attempt survives the cancellation and still reports its visible event id.
    assert await asyncio.wait_for(run_task, timeout=2) == "$placeholder"

    assert tracked.task.cancelled()
    assert cancel_reasons == ["cancelled_by_user"]
    assert stop_manager.cleared == ["$placeholder"]
    assert stop_manager.tracked_messages == {}


# ---------------------------------------------------------------------------
# 4. Streaming vs non-streaming delivery through DeliveryGateway
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_streaming_response_delivers_through_deliver_final(tmp_path: Path) -> None:
    """The non-streaming path hands the generated text to DeliveryGateway.deliver_final once."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    deliver_final = AsyncMock(return_value=_completed_outcome("$response", body="final text"))

    with (
        patch.object(DeliveryGateway, "deliver_final", new=deliver_final),
        patch_response_runner_module(
            ai_response=AsyncMock(return_value="final text"),
            typing_indicator=_noop_typing,
        ),
    ):
        generation = await coordinator.process_and_respond(_plain_request(_target()))

    assert generation.delivery.event_id == "$response"
    deliver_final.assert_awaited_once()
    final_request = deliver_final.await_args.args[0]
    assert final_request.response_text == "final text"
    assert final_request.target.room_id == "!room:localhost"
    assert final_request.response_kind == "ai"
    assert final_request.existing_event_id is None


@pytest.mark.asyncio
async def test_streaming_response_streams_then_finalizes_through_gateway(tmp_path: Path) -> None:
    """The streaming path delivers via deliver_stream, then finalizes the same transport outcome."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    transport = StreamTransportOutcome(
        last_physical_stream_event_id="$stream",
        terminal_status="completed",
        rendered_body="streamed body",
        visible_body_state="visible_body",
    )
    deliver_stream = AsyncMock(return_value=transport)
    finalize = AsyncMock(return_value=_completed_outcome("$stream", body="streamed body"))

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=deliver_stream),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        patch_response_runner_module(
            stream_agent_response=fake_stream,
            typing_indicator=_noop_typing,
        ),
    ):
        generation = await coordinator.process_and_respond_streaming(_plain_request(_target()))

    assert generation.delivery.event_id == "$stream"
    deliver_stream.assert_awaited_once()
    assert deliver_stream.await_args.args[0].existing_event_id is None
    finalize.assert_awaited_once()
    finalize_request = finalize.await_args.args[0]
    assert finalize_request.stream_transport_outcome is transport
    assert finalize_request.initial_delivery_kind == "sent"
    assert finalize_request.response_kind == "ai"


@pytest.mark.asyncio
async def test_streaming_midstream_failure_persists_partial_and_finalizes_error(tmp_path: Path) -> None:
    """A mid-stream delivery failure persists the partial turn and finalizes the error transport outcome."""
    bot = _bot(tmp_path)
    # Mock the logger collaborator: the production rich traceback renderer is pathologically
    # slow on mock-laden tracebacks, and the log call itself is part of the pinned fallback.
    coordinator = replace_response_runner_deps(bot, logger=MagicMock())
    error_transport = StreamTransportOutcome(
        last_physical_stream_event_id="$stream",
        terminal_status="error",
        rendered_body="partial body",
        visible_body_state="visible_body",
        failure_reason="boom",
    )
    error_outcome = FinalDeliveryOutcome(
        terminal_status="error",
        event_id="$stream",
        is_visible_response=True,
        final_visible_body="partial body",
        failure_reason="boom",
    )
    deliver_stream = AsyncMock(
        side_effect=StreamingDeliveryError(
            RuntimeError("boom"),
            event_id="$stream",
            accumulated_text="partial body",
            tool_trace=[],
            transport_outcome=error_transport,
        ),
    )
    finalize = AsyncMock(return_value=error_outcome)
    persist = MagicMock()

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    with (
        patch.object(DeliveryGateway, "deliver_stream", new=deliver_stream),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=finalize),
        patch("mindroom.response_runner.persist_interrupted_replay_snapshot", new=persist),
        patch_response_runner_module(
            stream_agent_response=fake_stream,
            typing_indicator=_noop_typing,
        ),
    ):
        generation = await coordinator.process_and_respond_streaming(_plain_request(_target()))

    # The failure does not propagate: it is logged and becomes a finalized error outcome.
    assert generation.delivery is error_outcome
    coordinator.deps.logger.exception.assert_called_once_with("Error in streaming response", error="boom")
    finalize.assert_awaited_once()
    assert finalize.await_args.args[0].stream_transport_outcome is error_transport
    # The partial reply was captured as an interrupted-replay snapshot exactly once.
    persist.assert_called_once()
    snapshot = persist.call_args.kwargs["snapshot"]
    assert snapshot.partial_text == "partial body"
    assert snapshot.response_event_id == "$stream"
    assert persist.call_args.kwargs["is_team"] is False


@pytest.mark.asyncio
async def test_streaming_midstream_failure_persists_partial_off_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Interrupted replay snapshot persistence should run through the thread offload boundary."""
    bot = _bot(tmp_path)
    coordinator = replace_response_runner_deps(bot, logger=MagicMock())
    error_transport = StreamTransportOutcome(
        last_physical_stream_event_id="$stream",
        terminal_status="error",
        rendered_body="partial body",
        visible_body_state="visible_body",
        failure_reason="boom",
    )
    error_outcome = FinalDeliveryOutcome(
        terminal_status="error",
        event_id="$stream",
        is_visible_response=True,
        final_visible_body="partial body",
        failure_reason="boom",
    )
    in_worker = False

    async def fake_to_thread(function: object, *args: object, **kwargs: object) -> object:
        nonlocal in_worker
        in_worker = True
        try:
            return function(*args, **kwargs)  # type: ignore[misc]
        finally:
            in_worker = False

    def persist(**_kwargs: object) -> None:
        assert in_worker

    async def fake_stream(*_args: object, **_kwargs: object) -> AsyncIterator[str]:
        yield "chunk"

    monkeypatch.setattr(response_runner.asyncio, "to_thread", fake_to_thread)
    with (
        patch.object(
            DeliveryGateway,
            "deliver_stream",
            new=AsyncMock(
                side_effect=StreamingDeliveryError(
                    RuntimeError("boom"),
                    event_id="$stream",
                    accumulated_text="partial body",
                    tool_trace=[],
                    transport_outcome=error_transport,
                ),
            ),
        ),
        patch.object(DeliveryGateway, "finalize_streamed_response", new=AsyncMock(return_value=error_outcome)),
        patch("mindroom.response_runner.persist_interrupted_replay_snapshot", new=persist),
        patch_response_runner_module(
            stream_agent_response=fake_stream,
            typing_indicator=_noop_typing,
        ),
    ):
        await coordinator.process_and_respond_streaming(_plain_request(_target()))


@pytest.mark.asyncio
async def test_agent_streaming_sync_restart_cancelled_outcome_registers_retry(tmp_path: Path) -> None:
    """A visible stream cancelled by sync restart should be retried even when no outer task cancel fired."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    retries: list[str] = []
    cancelled_outcome = FinalDeliveryOutcome(
        terminal_status="cancelled",
        event_id="$stream",
        is_visible_response=True,
        final_visible_body="partial",
        failure_reason="sync_restart_cancelled",
    )

    async def fake_run_cancellable_response(**kwargs: object) -> str:
        response_function = kwargs["response_function"]
        await response_function("$thinking")  # type: ignore[operator]
        return "$thinking"

    with (
        patch.object(
            coordinator,
            "run_cancellable_response",
            new=AsyncMock(side_effect=fake_run_cancellable_response),
        ),
        patch.object(
            coordinator,
            "process_and_respond_streaming",
            new=AsyncMock(return_value=_ResponseGenerationOutcome(delivery=cancelled_outcome, run_succeeded=False)),
        ),
        patch_response_runner_module(
            should_use_streaming=AsyncMock(return_value=True),
            apply_post_response_effects=AsyncMock(),
        ),
    ):
        result = await coordinator.generate_response(
            replace(
                _plain_request(_target()),
                on_sync_restart_cancelled=lambda: retries.append("retry"),
            ),
        )

    assert result == "$stream"
    assert retries == ["retry"]


@pytest.mark.asyncio
async def test_cancelled_interrupted_persistence_offload_keeps_running(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A second cancellation should not cancel the in-flight persistence worker."""
    bot = _bot(tmp_path)
    coordinator = replace_response_runner_deps(bot)
    started = asyncio.Event()
    release = asyncio.Event()
    persisted: list[str] = []

    async def fake_to_thread(function: object, *args: object, **kwargs: object) -> object:
        started.set()
        await release.wait()
        return function(*args, **kwargs)  # type: ignore[misc]

    def persist(**kwargs: object) -> None:
        persisted.append(str(kwargs["session_id"]))

    monkeypatch.setattr(response_runner.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(coordinator, "_persist_interrupted_recorder", persist)

    task = asyncio.create_task(
        coordinator._persist_interrupted_recorder_off_loop(
            recorder=TurnRecorder(user_message="hello"),
            session_scope=coordinator.deps.state_writer.history_scope(),
            session_id="session",
            execution_identity=None,
            run_id="run",
            is_team=False,
            response_event_id="$response",
        ),
    )
    await started.wait()

    registered_tasks = background_tasks_module._tasks_for_owner(coordinator.deps.runtime)
    assert len(registered_tasks) == 1
    assert registered_tasks[0].get_name() == "persist_interrupted_recorder"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    release.set()
    await wait_for_background_tasks(timeout=1.0, owner=coordinator.deps.runtime)

    assert persisted == ["session"]


# ---------------------------------------------------------------------------
# 5. Queued-notice state (response_lifecycle.py)
# ---------------------------------------------------------------------------


def _queued_envelope(source_event_id: str) -> MessageEnvelope:
    return request_envelope(
        room_id="!room:localhost",
        thread_id="$thread",
        reply_to_event_id=source_event_id,
        agent_name="general",
    )


def test_reserve_waiting_human_message_requires_active_turn() -> None:
    """No queued-human notice is reserved when the conversation is idle."""
    coordinator = ResponseLifecycleCoordinator()
    envelope = _queued_envelope("$first")

    assert coordinator.reserve_waiting_human_message(target=envelope.target, response_envelope=envelope) is None


@pytest.mark.asyncio
async def test_queued_human_notice_is_registered_exactly_once() -> None:
    """A request arriving mid-turn registers one queued notice that drains when it owns the lock."""
    coordinator = ResponseLifecycleCoordinator()
    first_envelope = _queued_envelope("$first")
    second_envelope = _queued_envelope("$second")
    gate = asyncio.Event()
    in_first_turn = asyncio.Event()
    pending_during_second_turn: list[int] = []

    async def first_turn(_target: MessageTarget) -> str:
        in_first_turn.set()
        await gate.wait()
        return "first"

    first = asyncio.create_task(
        coordinator.run_locked_response(
            target=first_envelope.target,
            response_envelope=first_envelope,
            queued_notice_reservation=None,
            pipeline_timing=None,
            locked_operation=first_turn,
        ),
    )
    await asyncio.wait_for(in_first_turn.wait(), timeout=2)
    assert coordinator.has_active_response_for_target(first_envelope.target)

    reservation = coordinator.reserve_waiting_human_message(
        target=second_envelope.target,
        response_envelope=second_envelope,
    )
    assert reservation is not None
    queued_signal = coordinator._thread_queued_signals[("!room:localhost", "$thread")]
    assert queued_signal.pending_human_messages == 1
    # A duplicate reservation for the same queued event must not double-register.
    assert (
        coordinator.reserve_waiting_human_message(target=second_envelope.target, response_envelope=second_envelope)
        is None
    )
    assert queued_signal.pending_human_messages == 1

    async def second_turn(_target: MessageTarget) -> str:
        pending_during_second_turn.append(queued_signal.pending_human_messages)
        return "second"

    second = asyncio.create_task(
        coordinator.run_locked_response(
            target=second_envelope.target,
            response_envelope=second_envelope,
            queued_notice_reservation=reservation,
            pipeline_timing=None,
            locked_operation=second_turn,
        ),
    )
    for _ in range(10):
        await asyncio.sleep(0)
    # While the queued turn waits for the lock the notice stays pending for the in-flight turn.
    assert queued_signal.pending_human_messages == 1

    gate.set()
    assert await asyncio.wait_for(first, timeout=2) == "first"
    assert await asyncio.wait_for(second, timeout=2) == "second"
    # The reservation is consumed exactly when the queued request becomes the active turn.
    assert pending_during_second_turn == [0]
    assert queued_signal.pending_human_messages == 0
    assert not coordinator.has_active_response_for_target(first_envelope.target)


@pytest.mark.asyncio
async def test_duplicate_queued_request_without_reservation_registers_one_notice() -> None:
    """Re-dispatching the same queued event while a turn runs never double-counts the notice."""
    coordinator = ResponseLifecycleCoordinator()
    first_envelope = _queued_envelope("$first")
    second_envelope = _queued_envelope("$second")
    gate = asyncio.Event()
    in_first_turn = asyncio.Event()

    async def first_turn(_target: MessageTarget) -> str:
        in_first_turn.set()
        await gate.wait()
        return "first"

    async def queued_turn(_target: MessageTarget) -> str:
        return "queued"

    def run_queued() -> asyncio.Task[str]:
        return asyncio.create_task(
            coordinator.run_locked_response(
                target=second_envelope.target,
                response_envelope=second_envelope,
                queued_notice_reservation=None,
                pipeline_timing=None,
                locked_operation=queued_turn,
            ),
        )

    first = asyncio.create_task(
        coordinator.run_locked_response(
            target=first_envelope.target,
            response_envelope=first_envelope,
            queued_notice_reservation=None,
            pipeline_timing=None,
            locked_operation=first_turn,
        ),
    )
    await asyncio.wait_for(in_first_turn.wait(), timeout=2)

    queued_one = run_queued()
    queued_two = run_queued()
    for _ in range(10):
        await asyncio.sleep(0)
    queued_signal = coordinator._thread_queued_signals[("!room:localhost", "$thread")]
    assert queued_signal.pending_human_messages == 1

    gate.set()
    assert await asyncio.wait_for(first, timeout=2) == "first"
    assert await asyncio.wait_for(queued_one, timeout=2) == "queued"
    assert await asyncio.wait_for(queued_two, timeout=2) == "queued"
    assert queued_signal.pending_human_messages == 0


# ---------------------------------------------------------------------------
# 6. Post-response effects ordering and gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_success_runs_post_response_effects_after_delivery(tmp_path: Path) -> None:
    """A successful turn applies post-response effects only after visible delivery completed."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    order: list[str] = []
    effect_outcomes: list[FinalDeliveryOutcome] = []

    async def fake_send_text(_request: object) -> str:
        order.append("placeholder")
        return "$placeholder"

    async def fake_ai_response(*_args: object, **_kwargs: object) -> str:
        order.append("generate")
        return "final text"

    async def fake_deliver_final(_request: object) -> FinalDeliveryOutcome:
        order.append("deliver_final")
        return _completed_outcome("$response", body="final text")

    async def fake_post_effects(final_outcome: FinalDeliveryOutcome, *_args: object) -> None:
        order.append("post_effects")
        effect_outcomes.append(final_outcome)

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(side_effect=fake_send_text)),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(side_effect=fake_deliver_final)),
        patch_response_runner_module(
            ai_response=AsyncMock(side_effect=fake_ai_response),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
    ):
        result = await coordinator.generate_response(_plain_request(_target()))

    assert result == "$response"
    assert order == ["placeholder", "generate", "deliver_final", "post_effects"]
    assert effect_outcomes[0].terminal_status == "completed"
    assert effect_outcomes[0].final_visible_event_id == "$response"


@pytest.mark.asyncio
async def test_delivery_failure_emits_cancelled_hook_and_passes_error_outcome_to_effects(tmp_path: Path) -> None:
    """A failed delivery emits the cancelled hook, skips after-response, and forwards the error outcome."""
    bot = _bot(tmp_path)
    coordinator = unwrap_extracted_collaborator(bot._response_runner)
    error_outcome = FinalDeliveryOutcome(
        terminal_status="error",
        event_id=None,
        failure_reason="delivery_failed",
    )
    effect_outcomes: list[FinalDeliveryOutcome] = []

    async def fake_post_effects(final_outcome: FinalDeliveryOutcome, *_args: object) -> None:
        effect_outcomes.append(final_outcome)

    with (
        patch.object(DeliveryGateway, "send_text", new=AsyncMock(return_value="$placeholder")),
        patch.object(DeliveryGateway, "deliver_final", new=AsyncMock(return_value=error_outcome)),
        patch.object(
            bot._delivery_gateway.deps.response_hooks,
            "emit_after_response",
            new=AsyncMock(),
        ) as mock_after,
        patch.object(
            bot._delivery_gateway.deps.response_hooks,
            "emit_cancelled_response",
            new=AsyncMock(),
        ) as mock_cancelled,
        patch_response_runner_module(
            ai_response=AsyncMock(return_value="final text"),
            should_use_streaming=AsyncMock(return_value=False),
            typing_indicator=_noop_typing,
            apply_post_response_effects=AsyncMock(side_effect=fake_post_effects),
        ),
    ):
        result = await coordinator.generate_response(_plain_request(_target()))

    assert result is None
    mock_after.assert_not_awaited()
    mock_cancelled.assert_awaited_once()
    assert mock_cancelled.await_args.kwargs["failure_reason"] == "delivery_failed"
    # The effects step still runs, but receives the error outcome so success effects are gated off.
    assert effect_outcomes == [error_outcome]


@pytest.mark.asyncio
async def test_apply_post_response_effects_gates_success_only_side_effects() -> None:
    """Memory persistence and run-event linkage run on success and stay off after a failed delivery."""
    memory_calls: list[str] = []
    persisted: list[tuple[str, str]] = []
    deps = PostResponseEffectsDeps(
        logger=get_logger("tests.post_effects"),
        queue_memory_persistence=lambda: memory_calls.append("memory"),
        persist_response_event_id=lambda run_id, event_id: persisted.append((run_id, event_id)),
    )

    await apply_post_response_effects(
        _completed_outcome("$response", body="ok"),
        ResponseOutcome(response_run_id="run-1", run_succeeded=True),
        deps,
    )
    assert memory_calls == ["memory"]
    assert persisted == [("run-1", "$response")]

    await apply_post_response_effects(
        FinalDeliveryOutcome(terminal_status="error", event_id=None, failure_reason="delivery_failed"),
        ResponseOutcome(response_run_id="run-1", run_succeeded=False),
        deps,
    )
    # The failed delivery added neither memory persistence nor run-event linkage.
    assert memory_calls == ["memory"]
    assert persisted == [("run-1", "$response")]
