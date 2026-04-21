"""Regression tests for streamed-response finalization and outer repair."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, TypeVar
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from agno.db.base import SessionType
from agno.metrics import RunMetrics
from agno.run.agent import (
    ModelRequestCompletedEvent,
    RunCompletedEvent,
    RunContentEvent,
    ToolCallCompletedEvent,
    ToolCallStartedEvent,
)

from mindroom import interactive
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import (
    AI_RUN_METADATA_KEY,
    STREAM_STATUS_CANCELLED,
    STREAM_STATUS_COMPLETED,
    STREAM_STATUS_ERROR,
    STREAM_STATUS_KEY,
    STREAM_STATUS_STREAMING,
)
from mindroom.delivery_gateway import (
    DeliveryGateway,
    DeliveryGatewayDeps,
    DeliveryResult,
    FinalizeStreamedResponseRequest,
)
from mindroom.history.types import HistoryScope
from mindroom.hooks import MessageEnvelope, ResponseDraft
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.matrix.message_builder import markdown_to_html
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome, apply_post_response_effects
from mindroom.response_lifecycle import DeliveryOutcome, ResponseLifecycle, StreamingRepair
from mindroom.response_runner import ResponseRequest, ResponseRunner, ResponseRunnerDeps
from mindroom.streaming import (
    _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE,
    PROGRESS_PLACEHOLDER,
    StreamDeliveryState,
    StreamFinalizationOutcome,
    send_streaming_response,
)
from mindroom.tool_system.runtime_context import ToolDispatchContext
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_matrix_client_mock,
    runtime_paths_for,
    test_runtime_paths,
)
from tests.test_ai_user_id import (
    _build_response_runner as _build_issue_181_response_runner,
)
from tests.test_ai_user_id import (
    _config as _issue_181_config,
)
from tests.test_ai_user_id import (
    _make_bot as _make_issue_181_bot,
)
from tests.test_ai_user_id import (
    _prepared_prompt_result,
)
from tests.test_ai_user_id import (
    _response_request as _issue_181_response_request,
)
from tests.test_ai_user_id import (
    _runtime_paths as _issue_181_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

_OperationResult = TypeVar("_OperationResult")


def _config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={"code": AgentConfig(display_name="Code", rooms=["!room:localhost"])},
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        ),
        runtime_paths,
    )


def _room_mode_config(tmp_path: Path) -> Config:
    runtime_paths = test_runtime_paths(tmp_path)
    return bind_runtime_paths(
        Config(
            agents={
                "code": AgentConfig(
                    display_name="Code",
                    rooms=["!room:localhost"],
                    thread_mode="room",
                ),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        ),
        runtime_paths,
    )


def _client() -> AsyncMock:
    client = make_matrix_client_mock(user_id="@mindroom_code:localhost")
    client.room_get_event_relations = Mock(return_value=_empty_async_iter())
    return client


async def _empty_async_iter() -> AsyncIterator[None]:
    if False:
        yield None


async def _stream_text(text: str) -> AsyncIterator[str]:
    yield text


async def _cancelled_stream(*, text: str, message: str) -> AsyncIterator[str]:
    yield text
    raise asyncio.CancelledError(message)


def _response_request(target: MessageTarget) -> ResponseRequest:
    envelope = MessageEnvelope(
        source_event_id="$source",
        room_id=target.room_id,
        target=target,
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="code",
        source_kind="message",
    )
    return ResponseRequest(
        room_id=target.room_id,
        reply_to_event_id=target.reply_to_event_id or "$reply",
        thread_id=target.resolved_thread_id,
        thread_history=[],
        prompt="hello",
        existing_event_id="$placeholder",
        existing_event_is_placeholder=True,
        response_envelope=envelope,
        correlation_id="corr-1",
        target=target,
    )


def _is_cancelled_delivery_result(delivery_result: DeliveryResult | None) -> bool:
    if delivery_result is None:
        return True
    return not delivery_result.suppressed and delivery_result.event_id is None and delivery_result.delivery_kind is None


def _resolve_response_event_id(
    *,
    delivery_result: DeliveryResult | None,
    tracked_event_id: str | None,
    existing_event_id: str | None,
    existing_event_is_placeholder: bool,
) -> str | None:
    if _is_cancelled_delivery_result(delivery_result):
        return None
    assert delivery_result is not None
    if delivery_result.event_id is not None:
        return delivery_result.event_id
    if delivery_result.suppressed or existing_event_is_placeholder:
        return None
    return existing_event_id or tracked_event_id


def _build_lifecycle(target: MessageTarget) -> tuple[ResponseLifecycle, SimpleNamespace]:
    request = _response_request(target)
    delivery_gateway = SimpleNamespace(
        edit_text=AsyncMock(return_value=True),
        deps=SimpleNamespace(response_hooks=SimpleNamespace(emit_cancelled_response=AsyncMock())),
    )
    runner = SimpleNamespace(
        deps=SimpleNamespace(
            delivery_gateway=delivery_gateway,
            logger=SimpleNamespace(info=Mock(), warning=Mock(), error=Mock()),
        ),
        _is_cancelled_delivery_result=_is_cancelled_delivery_result,
        resolve_response_event_id=_resolve_response_event_id,
        _emit_pipeline_timing_summary=Mock(),
        _response_outcome=Mock(return_value="done"),
        _log_post_response_effects_failure=Mock(),
        _emit_session_started_safely=AsyncMock(),
        _should_watch_session_started=Mock(return_value=False),
    )
    lifecycle = ResponseLifecycle(
        runner,
        response_kind="ai",
        request=request,
        response_envelope=request.response_envelope,
        correlation_id=request.correlation_id or "corr-1",
    )
    return lifecycle, runner


async def _finalize_lifecycle(
    *,
    lifecycle: ResponseLifecycle,
    outcome: DeliveryOutcome,
) -> None:
    with patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()):
        await lifecycle.finalize(
            outcome,
            build_post_response_outcome=lambda _resolved_event_id, _delivery_result: SimpleNamespace(),
            post_response_deps=SimpleNamespace(),
        )


def _repair_payload(
    *,
    target: MessageTarget,
    response_text: str,
    extra_content: dict[str, Any] | None = None,
) -> StreamingRepair:
    return StreamingRepair(
        target=target,
        response_text=response_text if response_text.strip() else PROGRESS_PLACEHOLDER,
        extra_content=extra_content,
    )


def _response_hooks(
    *,
    mutate_response_text: str | None = None,
    suppress: bool = False,
) -> SimpleNamespace:
    async def apply_before_response(
        *,
        correlation_id: str,
        envelope: MessageEnvelope,
        response_text: str,
        response_kind: str,
        tool_trace: list[Any] | None,
        extra_content: dict[str, Any] | None,
    ) -> ResponseDraft:
        del correlation_id
        return ResponseDraft(
            response_text=mutate_response_text or response_text,
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
            suppress=suppress,
        )

    return SimpleNamespace(
        apply_before_response=AsyncMock(side_effect=apply_before_response),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )


def _delivery_gateway(
    tmp_path: Path,
    *,
    response_hooks: SimpleNamespace | None = None,
) -> tuple[DeliveryGateway, MessageTarget]:
    config = _room_mode_config(tmp_path)
    target = MessageTarget.resolve("!room:localhost", None, "$reply", room_mode=True)
    runtime = SimpleNamespace(client=_client(), config=config)
    conversation_cache = make_conversation_cache_mock()
    resolver = SimpleNamespace(deps=SimpleNamespace(conversation_cache=conversation_cache))
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=runtime,
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=MagicMock(),
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=resolver,
            response_hooks=response_hooks or _response_hooks(),
        ),
    )
    return gateway, target


def _build_real_response_runner(tmp_path: Path) -> tuple[ResponseRunner, MessageTarget]:
    config = _room_mode_config(tmp_path)
    target = MessageTarget.resolve("!room:localhost", None, "$reply", room_mode=True)
    conversation_cache = make_conversation_cache_mock()
    conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest-thread")
    resolver = SimpleNamespace(
        deps=SimpleNamespace(conversation_cache=conversation_cache),
        build_message_target=Mock(return_value=target),
        fetch_thread_history=AsyncMock(return_value=()),
        resolve_response_thread_root=Mock(return_value=None),
    )

    async def run_in_context(
        *,
        tool_context: object,
        operation: Callable[[], Awaitable[_OperationResult]],
    ) -> _OperationResult:
        del tool_context
        return await operation()

    def stream_in_context(
        *,
        tool_context: object,
        stream_factory: Callable[[], AsyncIterator[object]],
    ) -> AsyncIterator[object]:
        del tool_context
        return stream_factory()

    tool_runtime = SimpleNamespace(
        build_dispatch_context=Mock(return_value=ToolDispatchContext(execution_identity=None)),
        build_execution_identity=Mock(return_value=None),
        run_in_context=run_in_context,
        stream_in_context=Mock(side_effect=stream_in_context),
    )
    response_hooks = _response_hooks()
    runtime = SimpleNamespace(
        client=_client(),
        config=config,
        enable_streaming=True,
        orchestrator=None,
        event_cache=MagicMock(),
    )
    delivery_gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=runtime,
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=MagicMock(),
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=resolver,
            response_hooks=response_hooks,
        ),
    )
    stop_manager = SimpleNamespace(
        tracked_messages={},
        set_current=Mock(),
        clear_message=Mock(),
        update_run_id=Mock(),
        add_stop_button=AsyncMock(),
        remove_stop_button=AsyncMock(),
    )
    post_response_effects = SimpleNamespace(
        build_deps=Mock(return_value=SimpleNamespace()),
        conversation_cache=SimpleNamespace(notify_outbound_redaction=Mock()),
    )
    state_writer = SimpleNamespace(
        create_storage=Mock(return_value=MagicMock()),
        persist_response_event_id_in_session_run=Mock(),
        history_scope=Mock(return_value=HistoryScope(kind="agent", scope_id="code")),
        session_type_for_scope=Mock(return_value=SessionType.AGENT),
    )
    runner = ResponseRunner(
        ResponseRunnerDeps(
            runtime=runtime,
            logger=MagicMock(),
            stop_manager=stop_manager,
            runtime_paths=runtime_paths_for(config),
            storage_path=tmp_path,
            agent_name="code",
            matrix_full_id="@mindroom_code:localhost",
            resolver=resolver,
            tool_runtime=tool_runtime,
            knowledge_access=SimpleNamespace(for_agent=Mock(return_value=None)),
            delivery_gateway=delivery_gateway,
            post_response_effects=post_response_effects,
            state_writer=state_writer,
        ),
    )
    return runner, target


@asynccontextmanager
async def _typing_indicator_stub(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


@pytest.mark.asyncio
async def test_u1_terminal_send_exception_returns_false_and_outer_repair_fires(tmp_path: Path) -> None:
    """Outer repair should recover when the inner terminal streaming edit throws."""
    config = _config(tmp_path)
    stream_state = StreamDeliveryState()
    client = _client()
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    extra_content = {AI_RUN_METADATA_KEY: {"run_id": "run-u1"}}

    async def flaky_edit(*_args: object) -> DeliveredMatrixEvent:
        new_content = _args[3]
        if new_content[STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING:
            return DeliveredMatrixEvent(event_id="$edit-progress", content_sent=dict(new_content))
        msg = "terminal transport"
        raise RuntimeError(msg)

    with (
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=flaky_edit)),
        patch("mindroom.streaming.asyncio.sleep", new=AsyncMock()),
    ):
        event_id, accumulated = await send_streaming_response(
            client=client,
            room_id=target.room_id,
            reply_to_event_id=target.reply_to_event_id,
            thread_id=target.resolved_thread_id,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=_stream_text("partial answer"),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
            extra_content=extra_content,
            stream_state=stream_state,
        )

    assert event_id == "$placeholder"
    assert accumulated == "partial answer"
    assert stream_state.finalization_outcome == StreamFinalizationOutcome(
        terminal_landed=False,
        terminal_event_id="$placeholder",
        terminal_status=STREAM_STATUS_COMPLETED,
        reason="terminal_update_exception:RuntimeError",
    )

    lifecycle, runner = _build_lifecycle(target)
    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=DeliveryResult(
                event_id="$placeholder",
                response_text=accumulated,
                delivery_kind="edited",
            ),
            tracked_event_id="$placeholder",
            stream_finalization=stream_state.finalization_outcome,
            streaming_repair=_repair_payload(
                target=target,
                response_text=accumulated,
                extra_content=extra_content,
            ),
        ),
    )

    runner.deps.delivery_gateway.edit_text.assert_awaited_once()
    repair_request = runner.deps.delivery_gateway.edit_text.await_args.args[0]
    assert repair_request.event_id == "$placeholder"
    assert repair_request.new_text == "partial answer"
    assert repair_request.extra_content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED
    assert repair_request.extra_content[AI_RUN_METADATA_KEY]["run_id"] == "run-u1"


@pytest.mark.asyncio
async def test_u2_second_cancelled_error_during_finalize_does_not_skip_outcome(tmp_path: Path) -> None:
    """The original cancellation should survive a second CancelledError in finalize."""
    config = _config(tmp_path)
    stream_state = StreamDeliveryState()
    client = _client()
    second_cancel = asyncio.CancelledError("second-cancel")

    async def cancel_on_terminal(*_args: object) -> DeliveredMatrixEvent:
        new_content = _args[3]
        if new_content[STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING:
            return DeliveredMatrixEvent(event_id="$edit-progress", content_sent=dict(new_content))
        raise second_cancel

    with (
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=cancel_on_terminal)),
        pytest.raises(asyncio.CancelledError, match="user-stop"),
    ):
        await send_streaming_response(
            client=client,
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=_cancelled_stream(text="partial answer", message="user-stop"),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
            stream_state=stream_state,
        )

    assert stream_state.finalization_outcome == StreamFinalizationOutcome(
        terminal_landed=False,
        terminal_event_id="$placeholder",
        terminal_status=STREAM_STATUS_CANCELLED,
        reason="terminal_update_cancelled",
    )


@pytest.mark.asyncio
async def test_u3_terminal_retry_budget_exhaustion_repairs_from_outer_lifecycle(tmp_path: Path) -> None:
    """Outer repair should fire when the inner terminal edit exhausts its retries."""
    config = _config(tmp_path)
    stream_state = StreamDeliveryState()
    client = _client()
    target = MessageTarget.resolve("!room:localhost", None, "$reply")

    sleep_mock = AsyncMock()
    with (
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(return_value=None)) as mock_edit,
        patch("mindroom.streaming.asyncio.sleep", new=sleep_mock),
    ):
        event_id, accumulated = await send_streaming_response(
            client=client,
            room_id=target.room_id,
            reply_to_event_id=target.reply_to_event_id,
            thread_id=target.resolved_thread_id,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=_stream_text("partial answer"),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
            stream_state=stream_state,
        )

    assert event_id == "$placeholder"
    assert accumulated == "partial answer"
    assert stream_state.finalization_outcome == StreamFinalizationOutcome(
        terminal_landed=False,
        terminal_event_id="$placeholder",
        terminal_status=STREAM_STATUS_COMPLETED,
        reason="terminal_update_failed",
    )
    assert mock_edit.await_count == 7
    assert [call.args[0] for call in sleep_mock.await_args_list] == [2, 4, 8, 16, 32]

    lifecycle, runner = _build_lifecycle(target)
    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=DeliveryResult(
                event_id="$placeholder",
                response_text=accumulated,
                delivery_kind="edited",
            ),
            tracked_event_id="$placeholder",
            stream_finalization=stream_state.finalization_outcome,
            streaming_repair=_repair_payload(target=target, response_text=accumulated),
        ),
    )

    runner.deps.delivery_gateway.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_u4_lifecycle_repairs_when_delivery_result_is_none() -> None:
    """Lifecycle repair should still run when the delivery result is absent."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    lifecycle, runner = _build_lifecycle(target)
    extra_content = {AI_RUN_METADATA_KEY: {"run_id": "run-u4"}}

    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=None,
            delivery_failure_reason="cancelled",
            tracked_event_id="$placeholder",
            stream_finalization=StreamFinalizationOutcome(
                terminal_landed=False,
                terminal_event_id="$placeholder",
                terminal_status=STREAM_STATUS_CANCELLED,
                reason="inner-finalizer-missed",
            ),
            streaming_repair=_repair_payload(
                target=target,
                response_text="partial answer\n\n**[Response cancelled by user]**",
                extra_content=extra_content,
            ),
        ),
    )

    runner.deps.delivery_gateway.edit_text.assert_awaited_once()
    repair_request = runner.deps.delivery_gateway.edit_text.await_args.args[0]
    assert repair_request.event_id == "$placeholder"
    assert repair_request.extra_content[STREAM_STATUS_KEY] == STREAM_STATUS_CANCELLED
    assert repair_request.extra_content[AI_RUN_METADATA_KEY]["run_id"] == "run-u4"
    runner.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()
    assert (
        runner.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.await_args.kwargs[
            "visible_response_event_id"
        ]
        == "$placeholder"
    )


@pytest.mark.asyncio
async def test_u5_transport_exception_does_not_mask_original_cancelled_error(tmp_path: Path) -> None:
    """A terminal transport failure must not overwrite the original user cancel."""
    config = _config(tmp_path)
    stream_state = StreamDeliveryState()
    client = _client()

    async def broken_terminal_edit(*_args: object) -> DeliveredMatrixEvent:
        new_content = _args[3]
        if new_content[STREAM_STATUS_KEY] == STREAM_STATUS_STREAMING:
            return DeliveredMatrixEvent(event_id="$edit-progress", content_sent=dict(new_content))
        msg = "transport boom"
        raise RuntimeError(msg)

    with (
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=broken_terminal_edit)),
        patch("mindroom.streaming.asyncio.sleep", new=AsyncMock()),
        pytest.raises(asyncio.CancelledError, match="original-cancel") as exc_info,
    ):
        await send_streaming_response(
            client=client,
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=_cancelled_stream(text="partial answer", message="original-cancel"),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
            stream_state=stream_state,
        )

    assert str(exc_info.value) == "original-cancel"
    assert stream_state.finalization_outcome == StreamFinalizationOutcome(
        terminal_landed=False,
        terminal_event_id="$placeholder",
        terminal_status=STREAM_STATUS_CANCELLED,
        reason="terminal_update_exception:RuntimeError",
    )


@pytest.mark.asyncio
async def test_u6_outer_repair_prefers_latest_stream_event_id() -> None:
    """Outer repair should target the latest visible stream event when available."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    lifecycle, runner = _build_lifecycle(target)
    tracked_event_id = ResponseRunner._latest_stream_event_id(
        SimpleNamespace(),
        tracked_event_id="$placeholder",
        stream_state=StreamDeliveryState(event_id="$streamed"),
        fallback_event_id=None,
    )

    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=None,
            delivery_failure_reason="cancelled",
            tracked_event_id=tracked_event_id,
            stream_finalization=StreamFinalizationOutcome(
                terminal_landed=False,
                terminal_event_id="$streamed",
                terminal_status=STREAM_STATUS_CANCELLED,
                reason="rotation-race",
            ),
            streaming_repair=_repair_payload(
                target=target,
                response_text="partial answer\n\n**[Response cancelled by user]**",
            ),
        ),
    )

    repair_request = runner.deps.delivery_gateway.edit_text.await_args.args[0]
    assert tracked_event_id == "$streamed"
    assert repair_request.event_id == "$streamed"


@pytest.mark.asyncio
async def test_u7_happy_path_lands_one_terminal_edit_without_outer_duplicate(tmp_path: Path) -> None:
    """No outer repair should run when the terminal streaming edit already landed."""
    config = _config(tmp_path)
    stream_state = StreamDeliveryState()
    client = _client()
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    stream_statuses: list[str] = []

    async def record_edit(*_args: object) -> DeliveredMatrixEvent:
        new_content = _args[3]
        stream_statuses.append(new_content[STREAM_STATUS_KEY])
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(new_content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        event_id, accumulated = await send_streaming_response(
            client=client,
            room_id=target.room_id,
            reply_to_event_id=target.reply_to_event_id,
            thread_id=target.resolved_thread_id,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=_stream_text("complete answer"),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
            extra_content={AI_RUN_METADATA_KEY: {"run_id": "run-u7"}},
            stream_state=stream_state,
        )

    assert event_id == "$placeholder"
    assert accumulated == "complete answer"
    assert stream_statuses == ["streaming", "completed"]
    assert stream_state.finalization_outcome == StreamFinalizationOutcome(
        terminal_landed=True,
        terminal_event_id="$placeholder",
        terminal_status=STREAM_STATUS_COMPLETED,
        reason="terminal_update_applied",
    )

    lifecycle, runner = _build_lifecycle(target)
    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=DeliveryResult(
                event_id="$placeholder",
                response_text=accumulated,
                delivery_kind="edited",
            ),
            tracked_event_id="$placeholder",
            stream_finalization=stream_state.finalization_outcome,
            streaming_repair=_repair_payload(target=target, response_text=accumulated),
        ),
    )

    runner.deps.delivery_gateway.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_repair_swallows_cancel_and_transport_exception() -> None:
    """Outer repair failures should be logged and swallowed so finalize can continue."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply")

    for side_effect in (
        asyncio.CancelledError("repair-cancelled"),
        RuntimeError("transport boom"),
    ):
        lifecycle, runner = _build_lifecycle(target)
        runner.deps.delivery_gateway.edit_text = AsyncMock(side_effect=side_effect)

        await _finalize_lifecycle(
            lifecycle=lifecycle,
            outcome=DeliveryOutcome(
                delivery_result=None,
                delivery_failure_reason="cancelled",
                tracked_event_id="$placeholder",
                stream_finalization=StreamFinalizationOutcome(
                    terminal_landed=False,
                    terminal_event_id="$placeholder",
                    terminal_status=STREAM_STATUS_CANCELLED,
                    reason="outer-repair-missed",
                ),
                streaming_repair=_repair_payload(
                    target=target,
                    response_text="partial answer\n\n**[Response cancelled by user]**",
                ),
            ),
        )

        runner.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_outer_repair_skipped_when_event_suppressed_and_cleaned() -> None:
    """Outer repair should not edit an event that was suppressed and cleaned up."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    lifecycle, runner = _build_lifecycle(target)

    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=None,
            delivery_failure_reason="cancelled",
            tracked_event_id="$placeholder",
            stream_finalization=StreamFinalizationOutcome(
                terminal_landed=False,
                terminal_event_id="$placeholder",
                terminal_status=STREAM_STATUS_COMPLETED,
                reason="suppressed-cleanup",
            ),
            stream_state=StreamDeliveryState(suppressed_and_cleaned=True),
            streaming_repair=_repair_payload(target=target, response_text="suppressed response"),
        ),
    )

    runner.deps.delivery_gateway.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_repair_skipped_when_delivery_was_suppressed_without_cleanup() -> None:
    """Suppressed streamed responses must not be resurrected by the outer repair edit."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    lifecycle, runner = _build_lifecycle(target)

    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=DeliveryResult(
                event_id="$existing-response",
                response_text="suppressed response",
                delivery_kind="edited",
                suppressed=True,
            ),
            delivery_failure_reason="cancelled",
            tracked_event_id="$existing-response",
            stream_finalization=StreamFinalizationOutcome(
                terminal_landed=False,
                terminal_event_id="$existing-response",
                terminal_status=STREAM_STATUS_COMPLETED,
                reason="suppressed-visible-existing-event",
            ),
            stream_state=StreamDeliveryState(suppressed_and_cleaned=False),
            streaming_repair=_repair_payload(target=target, response_text="suppressed response"),
        ),
    )

    runner.deps.delivery_gateway.edit_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_outer_repair_skipped_when_cancelled_after_cleanup_redaction(tmp_path: Path) -> None:
    """Cleanup cancellation must not mark streamed suppression as already completed."""
    gateway, target = _delivery_gateway(tmp_path, response_hooks=_response_hooks(suppress=True))
    request = _response_request(target)
    stream_state = StreamDeliveryState()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def cancelled_cleanup(**_kwargs: object) -> DeliveryResult:
        cleanup_entered.set()
        await asyncio.sleep(0)
        await release_cleanup.wait()
        raise asyncio.CancelledError

    with patch.object(
        DeliveryGateway,
        "cleanup_suppressed_streamed_response",
        new=AsyncMock(side_effect=cancelled_cleanup),
    ):
        finalize_task = asyncio.create_task(
            gateway.finalize_streamed_response(
                FinalizeStreamedResponseRequest(
                    target=target,
                    streamed_event_id="$placeholder",
                    streamed_text="suppressed response",
                    delivery_kind="edited",
                    response_kind="ai",
                    response_envelope=request.response_envelope,
                    correlation_id="corr-suppressed-cleanup-cancelled",
                    tool_trace=None,
                    extra_content=None,
                    stream_state=stream_state,
                    cleanup_suppressed_streamed_event=True,
                ),
            ),
        )
        await cleanup_entered.wait()
        finalize_task.cancel()
        release_cleanup.set()
        with pytest.raises(asyncio.CancelledError):
            await finalize_task

    assert stream_state.suppressed_and_cleaned is False

    lifecycle, runner = _build_lifecycle(target)
    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=None,
            delivery_failure_reason="cancelled",
            tracked_event_id="$placeholder",
            stream_finalization=StreamFinalizationOutcome(
                terminal_landed=False,
                terminal_event_id="$placeholder",
                terminal_status=STREAM_STATUS_CANCELLED,
                reason="suppressed-cleanup-cancelled",
            ),
            stream_state=stream_state,
            streaming_repair=_repair_payload(target=target, response_text="suppressed response"),
        ),
    )

    runner.deps.delivery_gateway.edit_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_hook_driven_final_edit_preserves_terminal_status(tmp_path: Path) -> None:
    """Hook-driven streamed final edits should keep the completed stream status."""
    gateway, target = _delivery_gateway(
        tmp_path,
        response_hooks=_response_hooks(mutate_response_text="mutated final response"),
    )
    request = _response_request(target)
    edited_content: list[dict[str, Any]] = []

    async def record_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[3]
        edited_content.append(content)
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                streamed_event_id="$placeholder",
                streamed_text="original streamed response",
                delivery_kind="edited",
                response_kind="ai",
                response_envelope=request.response_envelope,
                correlation_id="corr-hook-edit",
                tool_trace=None,
                extra_content=None,
            ),
        )

    assert edited_content[0][STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_hook_driven_final_edit_uses_stream_state_terminal_status_when_snapshot_absent(
    tmp_path: Path,
) -> None:
    """Hook-driven fallback edits must preserve terminal status even without extra_content snapshots."""
    gateway, target = _delivery_gateway(
        tmp_path,
        response_hooks=_response_hooks(mutate_response_text="mutated final response"),
    )
    request = _response_request(target)
    edited_content: list[dict[str, Any]] = []
    stream_state = StreamDeliveryState(
        finalization_outcome=StreamFinalizationOutcome(
            terminal_landed=False,
            terminal_event_id="$placeholder",
            terminal_status=STREAM_STATUS_ERROR,
            reason="reasoning-only",
        ),
    )

    async def record_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[3]
        edited_content.append(content)
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                streamed_event_id="$placeholder",
                streamed_text=_NO_VISIBLE_TEXT_AFTER_THINKING_NOTE,
                delivery_kind="edited",
                response_kind="team",
                response_envelope=request.response_envelope,
                correlation_id="corr-hook-error-edit",
                tool_trace=None,
                extra_content=None,
                stream_state=stream_state,
            ),
        )

    assert edited_content[0][STREAM_STATUS_KEY] == STREAM_STATUS_ERROR


@pytest.mark.asyncio
async def test_runner_skips_outer_repair_after_successful_hook_terminal_fallback(tmp_path: Path) -> None:
    """A successful hook-driven fallback edit must suppress the later outer repair edit."""
    runner, target = _build_real_response_runner(tmp_path)
    request = replace(
        _response_request(target),
        thread_history=(),
        prompt="hello",
        user_id="@user:localhost",
    )
    final_edits: list[dict[str, Any]] = []

    async def send_placeholder(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[2]
        return DeliveredMatrixEvent(event_id="$placeholder", content_sent=dict(content))

    async def record_streaming_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent | None:
        content = _args[3]
        if content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED:
            return None
        return DeliveredMatrixEvent(event_id="$stream-edit", content_sent=dict(content))

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )

    async def record_final_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[3]
        final_edits.append(content)
        return DeliveredMatrixEvent(event_id="$hook-final", content_sent=dict(content))

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.stream_agent_response", side_effect=lambda **_kwargs: _stream_text("answer")),
        patch("mindroom.response_runner.typing_indicator", _typing_indicator_stub),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
        patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(side_effect=send_placeholder)),
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_streaming_edit)),
        patch("mindroom.streaming.asyncio.sleep", new=AsyncMock()),
        patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_final_edit)),
    ):
        runner.deps.delivery_gateway.deps.response_hooks.apply_before_response = AsyncMock(
            side_effect=mutate_before_response,
        )
        resolved_event_id = await runner.generate_response_locked(
            request,
            resolved_target=target,
        )

    assert resolved_event_id == "$placeholder"
    assert len(final_edits) == 1
    assert final_edits[0]["body"].endswith("Hook footer.")
    assert final_edits[0][STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_outer_repair_reuses_hook_mutated_terminal_payload(tmp_path: Path) -> None:
    """If the hook-driven fallback edit misses too, outer repair must reuse the mutated payload."""
    runner, target = _build_real_response_runner(tmp_path)
    request = replace(
        _response_request(target),
        thread_history=(),
        prompt="hello",
        user_id="@user:localhost",
    )
    final_edits: list[dict[str, Any]] = []

    async def send_placeholder(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[2]
        return DeliveredMatrixEvent(event_id="$placeholder", content_sent=dict(content))

    async def record_streaming_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent | None:
        content = _args[3]
        if content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED:
            return None
        return DeliveredMatrixEvent(event_id="$stream-edit", content_sent=dict(content))

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        mutated_extra_content = dict(extra_content or {})
        mutated_extra_content["hook"] = "kept"
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=mutated_extra_content,
            envelope=envelope,
        )

    async def record_final_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent | None:
        content = _args[3]
        final_edits.append(content)
        if len(final_edits) == 1:
            return None
        return DeliveredMatrixEvent(event_id="$outer-repair", content_sent=dict(content))

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.stream_agent_response", side_effect=lambda **_kwargs: _stream_text("answer")),
        patch("mindroom.response_runner.typing_indicator", _typing_indicator_stub),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
        patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(side_effect=send_placeholder)),
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_streaming_edit)),
        patch("mindroom.streaming.asyncio.sleep", new=AsyncMock()),
        patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_final_edit)),
    ):
        runner.deps.delivery_gateway.deps.response_hooks.apply_before_response = AsyncMock(
            side_effect=mutate_before_response,
        )
        resolved_event_id = await runner.generate_response_locked(
            request,
            resolved_target=target,
        )

    assert resolved_event_id == "$placeholder"
    assert len(final_edits) == 2
    assert final_edits[1]["body"].endswith("Hook footer.")
    assert final_edits[1][STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED
    assert final_edits[1]["hook"] == "kept"
    runner.deps.delivery_gateway.deps.response_hooks.emit_cancelled_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_metadata_only_in_place_hook_mutation_triggers_terminal_reedit(tmp_path: Path) -> None:
    """In-place metadata-only hook edits must still trigger a terminal re-edit with canonical metadata."""
    gateway, target = _delivery_gateway(tmp_path)
    request = _response_request(target)
    final_edits: list[dict[str, Any]] = []
    extra_content = {
        STREAM_STATUS_KEY: STREAM_STATUS_COMPLETED,
        AI_RUN_METADATA_KEY: {"status": "completed", "run_id": "run-metadata-only"},
    }

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        assert extra_content is not None
        ai_run = extra_content[AI_RUN_METADATA_KEY]
        assert isinstance(ai_run, dict)
        ai_run["status"] = "hook-corrupted"
        return ResponseDraft(
            response_text=response_text,
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )

    async def record_final_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[3]
        final_edits.append(content)
        return DeliveredMatrixEvent(event_id="$hook-final", content_sent=dict(content))

    with patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_final_edit)):
        gateway.deps.response_hooks.apply_before_response = AsyncMock(side_effect=mutate_before_response)
        delivery = await gateway.finalize_streamed_response(
            FinalizeStreamedResponseRequest(
                target=target,
                streamed_event_id="$placeholder",
                streamed_text="answer",
                delivery_kind="edited",
                response_kind="ai",
                response_envelope=request.response_envelope,
                correlation_id="corr-metadata-only",
                tool_trace=None,
                extra_content=extra_content,
                stream_state=StreamDeliveryState(
                    finalization_outcome=StreamFinalizationOutcome(
                        terminal_landed=False,
                        terminal_event_id="$placeholder",
                        terminal_status=STREAM_STATUS_COMPLETED,
                        reason="metadata-only-hook-mutation",
                    ),
                ),
            ),
        )

    assert delivery.event_id == "$placeholder"
    assert delivery.delivery_kind == "edited"
    assert len(final_edits) == 1
    assert final_edits[0][AI_RUN_METADATA_KEY]["status"] == "completed"
    assert final_edits[0][STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_outer_repair_updates_post_response_delivery_result(tmp_path: Path) -> None:
    """Post-response effects must see the repaired visible delivery, not the pre-repair failure."""
    runner, target = _build_real_response_runner(tmp_path)
    request = replace(
        _response_request(target),
        thread_history=(),
        prompt="hello",
        user_id="@user:localhost",
    )
    post_response_outcomes: list[object] = []

    async def send_placeholder(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[2]
        return DeliveredMatrixEvent(event_id="$placeholder", content_sent=dict(content))

    async def record_streaming_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent | None:
        content = _args[3]
        if content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED:
            return None
        return DeliveredMatrixEvent(event_id="$stream-edit", content_sent=dict(content))

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )

    final_edit_attempts = 0

    async def record_final_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent | None:
        nonlocal final_edit_attempts
        content = _args[3]
        final_edit_attempts += 1
        if final_edit_attempts == 1:
            assert content["body"].endswith("Hook footer.")
            return None
        return DeliveredMatrixEvent(event_id="$outer-repair", content_sent=dict(content))

    async def capture_post_response_effects(outcome: object, _deps: object) -> None:
        post_response_outcomes.append(outcome)

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.stream_agent_response", side_effect=lambda **_kwargs: _stream_text("answer")),
        patch("mindroom.response_runner.typing_indicator", _typing_indicator_stub),
        patch(
            "mindroom.response_lifecycle.apply_post_response_effects",
            new=AsyncMock(side_effect=capture_post_response_effects),
        ),
        patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(side_effect=send_placeholder)),
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_streaming_edit)),
        patch("mindroom.streaming.asyncio.sleep", new=AsyncMock()),
        patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_final_edit)),
    ):
        runner.deps.delivery_gateway.deps.response_hooks.apply_before_response = AsyncMock(
            side_effect=mutate_before_response,
        )
        resolved_event_id = await runner.generate_response_locked(
            request,
            resolved_target=target,
        )

    assert resolved_event_id == "$placeholder"
    assert len(post_response_outcomes) == 1
    outcome = post_response_outcomes[0]
    assert outcome.resolved_event_id == "$placeholder"
    delivery_result = outcome.delivery_result
    assert delivery_result is not None
    assert delivery_result.event_id == "$placeholder"
    assert delivery_result.delivery_kind == "edited"
    assert delivery_result.suppressed is False


@pytest.mark.asyncio
async def test_cancelled_outer_repair_keeps_post_response_delivery_cancelled() -> None:
    """Cancelled outer repairs must not look like delivered responses to post effects."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    lifecycle, _runner = _build_lifecycle(target)
    post_response_outcomes: list[ResponseOutcome] = []

    async def capture_post_response_effects(outcome: ResponseOutcome, _deps: object) -> None:
        post_response_outcomes.append(outcome)

    with patch(
        "mindroom.response_lifecycle.apply_post_response_effects",
        new=AsyncMock(side_effect=capture_post_response_effects),
    ):
        resolved_event_id = await lifecycle.finalize(
            DeliveryOutcome(
                delivery_result=None,
                delivery_failure_reason="cancelled",
                tracked_event_id="$placeholder",
                stream_finalization=StreamFinalizationOutcome(
                    terminal_landed=False,
                    terminal_event_id="$placeholder",
                    terminal_status=STREAM_STATUS_CANCELLED,
                    reason="outer-cancelled-repair",
                ),
                streaming_repair=_repair_payload(
                    target=target,
                    response_text="partial answer\n\n**[Response cancelled by user]**",
                ),
            ),
            build_post_response_outcome=lambda resolved_event_id, effective_delivery_result: ResponseOutcome(
                resolved_event_id=resolved_event_id,
                delivery_result=effective_delivery_result,
                response_run_id="run-cancelled",
                session_id="session-cancelled",
                thread_summary_room_id="!room:localhost",
                thread_summary_thread_id="$thread",
            ),
            post_response_deps=SimpleNamespace(),
        )

    assert resolved_event_id == "$placeholder"
    assert len(post_response_outcomes) == 1
    assert post_response_outcomes[0].resolved_event_id == "$placeholder"
    assert post_response_outcomes[0].delivery_result is None


@pytest.mark.asyncio
async def test_post_response_effects_ignore_resolved_event_without_delivered_response() -> None:
    """Resolved ids alone must not trigger linkage persistence or thread summaries."""
    persist_response_event_id = Mock()
    should_queue_thread_summary = Mock(return_value=True)
    queue_thread_summary = Mock()

    await apply_post_response_effects(
        ResponseOutcome(
            resolved_event_id="$placeholder",
            delivery_result=None,
            response_run_id="run-cancelled",
            session_id="session-cancelled",
            thread_summary_room_id="!room:localhost",
            thread_summary_thread_id="$thread",
        ),
        PostResponseEffectsDeps(
            logger=MagicMock(),
            persist_response_event_id=persist_response_event_id,
            should_queue_thread_summary=should_queue_thread_summary,
            queue_thread_summary=queue_thread_summary,
        ),
    )

    persist_response_event_id.assert_not_called()
    should_queue_thread_summary.assert_not_called()
    queue_thread_summary.assert_not_called()


@pytest.mark.asyncio
async def test_interactive_outer_repair_preserves_option_metadata() -> None:
    """Outer repair must keep interactive option metadata for later button registration."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    lifecycle, _runner = _build_lifecycle(target)
    raw_interactive = """Please choose:
```interactive
{"question":"Pick one","options":[{"emoji":"1️⃣","label":"One","value":"one"}]}
```"""
    post_response_outcomes: list[ResponseOutcome] = []

    async def capture_post_response_effects(outcome: ResponseOutcome, _deps: object) -> None:
        post_response_outcomes.append(outcome)

    with patch(
        "mindroom.response_lifecycle.apply_post_response_effects",
        new=AsyncMock(side_effect=capture_post_response_effects),
    ):
        await lifecycle.finalize(
            DeliveryOutcome(
                delivery_result=None,
                delivery_failure_reason="terminal-edit-missed",
                tracked_event_id="$placeholder",
                stream_finalization=StreamFinalizationOutcome(
                    terminal_landed=False,
                    terminal_event_id="$placeholder",
                    terminal_status=STREAM_STATUS_COMPLETED,
                    reason="interactive-outer-repair",
                ),
                streaming_repair=ResponseRunner._build_streaming_repair(
                    SimpleNamespace(),
                    target=target,
                    response_text=raw_interactive,
                    tool_trace=None,
                    extra_content=None,
                ),
            ),
            build_post_response_outcome=lambda resolved_event_id, effective_delivery_result: ResponseOutcome(
                resolved_event_id=resolved_event_id,
                delivery_result=effective_delivery_result,
                interactive_target=target,
            ),
            post_response_deps=SimpleNamespace(),
        )

    interactive_response = interactive.parse_and_format_interactive(raw_interactive, extract_mapping=True)
    assert len(post_response_outcomes) == 1
    delivery_result = post_response_outcomes[0].delivery_result
    assert delivery_result is not None
    assert delivery_result.option_map == interactive_response.option_map
    assert delivery_result.options_list == interactive_response.options_list


@pytest.mark.asyncio
async def test_interactive_response_repair_preserves_formatting() -> None:
    """Repair edits should preserve interactive formatting instead of raw JSON blocks."""
    target = MessageTarget.resolve("!room:localhost", None, "$reply")
    lifecycle, runner = _build_lifecycle(target)
    raw_interactive = """Please choose:
```interactive
{"question":"Pick one","options":[{"emoji":"1️⃣","label":"One","value":"one"}]}
```"""
    expected_formatted = interactive.parse_and_format_interactive(
        raw_interactive,
        extract_mapping=False,
    ).formatted_text
    streaming_repair = ResponseRunner._build_streaming_repair(
        SimpleNamespace(),
        target=target,
        response_text=raw_interactive,
        tool_trace=None,
        extra_content=None,
    )

    await _finalize_lifecycle(
        lifecycle=lifecycle,
        outcome=DeliveryOutcome(
            delivery_result=None,
            delivery_failure_reason="cancelled",
            tracked_event_id="$placeholder",
            stream_finalization=StreamFinalizationOutcome(
                terminal_landed=False,
                terminal_event_id="$placeholder",
                terminal_status=STREAM_STATUS_CANCELLED,
                reason="interactive-missed-terminal",
            ),
            streaming_repair=streaming_repair,
        ),
    )

    repair_request = runner.deps.delivery_gateway.edit_text.await_args.args[0]
    assert repair_request.new_text == expected_formatted
    assert "```interactive" not in repair_request.new_text


@pytest.mark.asyncio
async def test_final_event_content_keeps_visible_tool_markers(tmp_path: Path) -> None:
    """Final completion content must not erase visible tool markers already streamed."""
    config = _config(tmp_path)
    client = _client()
    captured_edits: list[dict[str, Any]] = []

    async def tool_then_final_content() -> AsyncIterator[object]:
        tool = SimpleNamespace(tool_name="run_shell_command", tool_args={"cmd": "pwd"}, result="ok")
        yield ToolCallStartedEvent(tool=tool)
        yield ToolCallCompletedEvent(tool=tool)
        yield RunCompletedEvent(content="Final answer")

    async def record_edit(*_args: object) -> DeliveredMatrixEvent:
        content = _args[3]
        captured_edits.append(content)
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        event_id, accumulated = await send_streaming_response(
            client=client,
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=tool_then_final_content(),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
        )

    assert event_id == "$placeholder"
    assert "🔧 `run_shell_command` [1]" in accumulated
    assert accumulated.endswith("Final answer")
    assert captured_edits[-1]["body"] == accumulated


@pytest.mark.asyncio
async def test_runner_end_to_end_outer_repair_fires_on_missed_terminal(tmp_path: Path) -> None:
    """A real runner streaming flow should surface the missed-terminal outer repair path."""
    runner, target = _build_real_response_runner(tmp_path)
    request = ResponseRequest(
        room_id=target.room_id,
        reply_to_event_id=target.reply_to_event_id or "$reply",
        thread_id=target.source_thread_id,
        thread_history=(),
        prompt="hello",
        user_id="@user:localhost",
        correlation_id="corr-e2e-repair",
        target=target,
    )
    streaming_statuses: list[str] = []
    outer_repair_statuses: list[str] = []

    async def send_placeholder(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[2]
        return DeliveredMatrixEvent(event_id="$placeholder", content_sent=dict(content))

    async def record_streaming_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent | None:
        content = _args[3]
        streaming_statuses.append(content[STREAM_STATUS_KEY])
        if content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED:
            return None
        return DeliveredMatrixEvent(event_id="$stream-edit", content_sent=dict(content))

    async def record_outer_repair(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[3]
        outer_repair_statuses.append(content[STREAM_STATUS_KEY])
        return DeliveredMatrixEvent(event_id="$outer-repair", content_sent=dict(content))

    with (
        patch("mindroom.response_runner.should_use_streaming", new=AsyncMock(return_value=True)),
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch(
            "mindroom.response_runner.stream_agent_response",
            side_effect=lambda **_kwargs: _stream_text("partial answer"),
        ),
        patch("mindroom.response_runner.typing_indicator", _typing_indicator_stub),
        patch("mindroom.response_lifecycle.apply_post_response_effects", new=AsyncMock()),
        patch("mindroom.delivery_gateway.send_message_result", new=AsyncMock(side_effect=send_placeholder)),
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_streaming_edit)),
        patch("mindroom.streaming.asyncio.sleep", new=AsyncMock()),
        patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_outer_repair)),
    ):
        resolved_event_id = await runner.generate_response_locked(
            request,
            resolved_target=target,
        )

    assert resolved_event_id == "$placeholder"
    assert streaming_statuses[0] == STREAM_STATUS_STREAMING
    assert all(status == STREAM_STATUS_COMPLETED for status in streaming_statuses[1:])
    assert outer_repair_statuses == [STREAM_STATUS_COMPLETED]


@asynccontextmanager
async def _noop_typing(*_args: object, **_kwargs: object) -> AsyncIterator[None]:
    yield


def _make_streaming_agent(*events: object) -> MagicMock:
    agent = MagicMock()
    agent.model = MagicMock()
    agent.model.__class__.__name__ = "OpenAIChat"
    agent.model.id = "test-model"
    agent.name = "GeneralAgent"
    agent.add_history_to_context = False

    def fake_arun(*_args: object, **kwargs: object) -> AsyncIterator[object]:
        assert kwargs["stream"] is True
        assert kwargs["stream_events"] is True

        async def stream() -> AsyncIterator[object]:
            for event in events:
                yield event

        return stream()

    agent.arun = MagicMock(side_effect=fake_arun)
    return agent


async def _run_streaming_finalize_scenario(
    tmp_path: Path,
    *events: object,
    before_response_hook: object | None = None,
) -> tuple[object, list[dict[str, object]]]:
    runtime_paths = _issue_181_runtime_paths(tmp_path)
    config = bind_runtime_paths(_issue_181_config(), runtime_paths)
    bot = _make_issue_181_bot(tmp_path, config=config, runtime_paths=runtime_paths)
    coordinator = _build_issue_181_response_runner(
        bot,
        config=config,
        runtime_paths=runtime_paths,
        storage_path=tmp_path,
        requester_id="@alice:localhost",
    )
    conversation_cache = SimpleNamespace(
        get_latest_thread_event_id_if_needed=AsyncMock(return_value=None),
        notify_outbound_message=MagicMock(),
    )
    bot._conversation_resolver.deps = SimpleNamespace(conversation_cache=conversation_cache)
    captured_edits: list[dict[str, object]] = []

    async def record_edit(
        _client: object,
        _room_id: str,
        _event_id: str,
        new_content: dict[str, object],
        _new_text: str,
    ) -> DeliveredMatrixEvent:
        captured_edits.append(new_content)
        return DeliveredMatrixEvent(event_id="$edit", content_sent=new_content)

    async def apply_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        if before_response_hook is not None:
            return await before_response_hook(
                correlation_id=correlation_id,
                envelope=envelope,
                response_text=response_text,
                response_kind=response_kind,
                tool_trace=tool_trace,
                extra_content=extra_content,
            )
        return ResponseDraft(
            response_text=response_text,
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )

    agent = _make_streaming_agent(*events)
    delivery_gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=coordinator.deps.runtime,
            runtime_paths=runtime_paths,
            agent_name=coordinator.deps.agent_name,
            logger=coordinator.deps.logger,
            redact_message_event=AsyncMock(return_value=True),
            sender_domain="localhost",
            resolver=coordinator.deps.resolver,
            response_hooks=SimpleNamespace(
                apply_before_response=AsyncMock(side_effect=apply_before_response),
                emit_after_response=AsyncMock(),
                emit_cancelled_response=AsyncMock(),
            ),
        ),
    )
    coordinator.deps = replace(coordinator.deps, delivery_gateway=delivery_gateway)
    request = replace(
        _issue_181_response_request(prompt="Hello", user_id="@alice:localhost"),
        existing_event_id="$thinking",
        existing_event_is_placeholder=True,
    )
    request = replace(
        request,
        response_envelope=coordinator._response_envelope_for_request(
            request,
            resolved_target=coordinator.deps.resolver.build_message_target.return_value,
        ),
        correlation_id=coordinator._correlation_id_for_request(request),
    )

    with (
        patch("mindroom.ai._prepare_agent_and_prompt", new=AsyncMock(return_value=_prepared_prompt_result(agent))),
        patch("mindroom.response_runner.ensure_request_knowledge_managers", new=AsyncMock(return_value={})),
        patch("mindroom.response_runner.typing_indicator", new=_noop_typing),
        patch("mindroom.delivery_gateway.edit_message_result", new=AsyncMock(side_effect=record_edit)),
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)),
    ):
        delivery = await coordinator.process_and_respond_streaming(request)

    return delivery, captured_edits


@pytest.mark.asyncio
async def test_streaming_finalize_surfaces_reasoning_only_run_as_error(tmp_path: Path) -> None:
    """A run that only emits thinking blocks should finalize as a visible error."""
    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(reasoning_content="pondering"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(
            content=None,
            reasoning_content="pondering",
            metrics=RunMetrics(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        ),
    )

    final_content = captured_edits[-1]
    ai_run = final_content[AI_RUN_METADATA_KEY]

    assert delivery.response_text == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
    assert final_content["body"] == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
    assert ai_run["status"] == "error"
    assert ai_run["usage"] == {
        "input_tokens": 6,
        "output_tokens": 0,
        "total_tokens": 6,
        "cache_read_tokens": 46449,
    }
    assert final_content["formatted_body"] == markdown_to_html(_NO_VISIBLE_TEXT_AFTER_THINKING_NOTE)


@pytest.mark.asyncio
async def test_streaming_finalize_keeps_error_status_after_hook_reedit(tmp_path: Path) -> None:
    """A hook-driven terminal re-edit must preserve the error stream status."""

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        mutated_extra_content = dict(extra_content or {})
        mutated_extra_content.pop(STREAM_STATUS_KEY, None)
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=mutated_extra_content,
            envelope=envelope,
        )

    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(reasoning_content="pondering"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(content=None, reasoning_content="pondering"),
        before_response_hook=mutate_before_response,
    )

    final_content = captured_edits[-1]

    assert delivery.response_text.endswith("Hook footer.")
    assert final_content["body"].endswith("Hook footer.")
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR


@pytest.mark.asyncio
async def test_streaming_finalize_keeps_error_status_after_in_place_hook_mutation(tmp_path: Path) -> None:
    """An in-place hook mutation cannot erase the terminal error stream status."""

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        assert extra_content is not None
        extra_content.pop(STREAM_STATUS_KEY, None)
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )

    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(reasoning_content="pondering"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(content=None, reasoning_content="pondering"),
        before_response_hook=mutate_before_response,
    )

    final_content = captured_edits[-1]

    assert delivery.response_text.endswith("Hook footer.")
    assert final_content["body"].endswith("Hook footer.")
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR


@pytest.mark.asyncio
async def test_streaming_finalize_restores_ai_run_after_hook_override(tmp_path: Path) -> None:
    """A hook-driven terminal re-edit cannot override ai_run terminal status."""

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        mutated_extra_content = dict(extra_content or {})
        mutated_extra_content[AI_RUN_METADATA_KEY] = {"status": STREAM_STATUS_COMPLETED}
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=mutated_extra_content,
            envelope=envelope,
        )

    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(reasoning_content="pondering"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(content=None, reasoning_content="pondering"),
        before_response_hook=mutate_before_response,
    )

    final_content = captured_edits[-1]
    ai_run = final_content[AI_RUN_METADATA_KEY]

    assert delivery.response_text.endswith("Hook footer.")
    assert final_content["body"].endswith("Hook footer.")
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR
    assert ai_run["status"] == STREAM_STATUS_ERROR


@pytest.mark.asyncio
async def test_streaming_finalize_keeps_normal_visible_output_completed(tmp_path: Path) -> None:
    """Visible streamed text should finalize normally without injected errors."""
    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(content="hello"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(content="hello"),
    )

    final_content = captured_edits[-1]
    ai_run = final_content[AI_RUN_METADATA_KEY]

    assert delivery.response_text == "hello"
    assert final_content["body"] == "hello"
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED
    assert ai_run["status"] == "completed"
    assert final_content["formatted_body"] == markdown_to_html("hello")


@pytest.mark.asyncio
async def test_streaming_finalize_uses_final_event_content_when_no_content_chunks_exist(tmp_path: Path) -> None:
    """A final-content-only stream should surface the final event content as the visible reply."""
    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunCompletedEvent(content="hello from final event"),
    )

    final_content = captured_edits[-1]

    assert delivery.response_text == "hello from final event"
    assert final_content["body"] == "hello from final event"
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_hidden_tool_reasoning_only_stream_still_finishes_as_error(tmp_path: Path) -> None:
    """Hidden tool calls should not count as visible output for reasoning-only runs."""
    config = _config(tmp_path)
    client = _client()
    captured_edits: list[dict[str, Any]] = []

    async def hidden_tool_reasoning_stream() -> AsyncIterator[object]:
        yield RunContentEvent(reasoning_content="pondering")
        yield ToolCallStartedEvent(tool=SimpleNamespace(tool_name="hidden"))
        yield RunCompletedEvent(reasoning_content="pondering")

    async def record_edit(*_args: object) -> DeliveredMatrixEvent:
        content = _args[3]
        captured_edits.append(content)
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        event_id, accumulated = await send_streaming_response(
            client=client,
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=hidden_tool_reasoning_stream(),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
            show_tool_calls=False,
        )

    assert event_id == "$placeholder"
    assert accumulated == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
    assert captured_edits[-1]["body"] == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
    assert captured_edits[-1][STREAM_STATUS_KEY] == STREAM_STATUS_ERROR


@pytest.mark.asyncio
async def test_streaming_finalize_restores_completed_status_when_snapshot_absent(tmp_path: Path) -> None:
    """A hook-driven final re-edit should restore completed when no stream-status snapshot exists."""

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )

    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(content="hello"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(content="hello"),
        before_response_hook=mutate_before_response,
    )

    final_content = captured_edits[-1]

    assert delivery.response_text.endswith("Hook footer.")
    assert final_content["body"].endswith("Hook footer.")
    assert final_content[STREAM_STATUS_KEY] == STREAM_STATUS_COMPLETED


@pytest.mark.asyncio
async def test_streaming_finalize_restores_deepcopied_ai_run_after_in_place_hook_mutation(tmp_path: Path) -> None:
    """A terminal ai_run snapshot must be deep-copied before hooks mutate nested metadata."""

    async def mutate_before_response(
        *,
        correlation_id: str,
        envelope: object,
        response_text: str,
        response_kind: str,
        tool_trace: object,
        extra_content: dict[str, object] | None,
    ) -> ResponseDraft:
        del correlation_id
        assert extra_content is not None
        ai_run = extra_content[AI_RUN_METADATA_KEY]
        assert isinstance(ai_run, dict)
        ai_run["status"] = "completed"
        usage = ai_run["usage"]
        assert isinstance(usage, dict)
        usage["output_tokens"] = 999
        ai_run["model"] = "fake-model"
        return ResponseDraft(
            response_text=f"{response_text}\n\nHook footer.",
            response_kind=response_kind,
            tool_trace=tool_trace,
            extra_content=extra_content,
            envelope=envelope,
        )

    delivery, captured_edits = await _run_streaming_finalize_scenario(
        tmp_path,
        RunContentEvent(reasoning_content="pondering"),
        ModelRequestCompletedEvent(input_tokens=6, output_tokens=0, cache_read_tokens=46449),
        RunCompletedEvent(content=None, reasoning_content="pondering"),
        before_response_hook=mutate_before_response,
    )

    final_content = captured_edits[-1]
    ai_run = final_content[AI_RUN_METADATA_KEY]

    assert delivery.response_text.endswith("Hook footer.")
    assert final_content["body"].endswith("Hook footer.")
    assert ai_run["status"] == STREAM_STATUS_ERROR
    assert ai_run["usage"]["output_tokens"] == 0
    assert ai_run["model"] == {"config": "default", "id": "test-model", "provider": "openai"}
