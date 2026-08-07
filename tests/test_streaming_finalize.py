"""Regression tests for the streaming terminal transport boundary."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest
from agno.run.agent import RunCompletedEvent, RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom import interactive
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import STREAM_STATUS_ERROR, STREAM_STATUS_KEY
from mindroom.delivery_gateway import (
    CancelledVisibleNoteRequest,
    DeliveryGateway,
    DeliveryGatewayDeps,
    FinalDeliveryRequest,
    FinalizeStreamedResponseRequest,
    ResponseIdentity,
)
from mindroom.dispatch_source import MESSAGE_SOURCE_KIND
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.logging_config import get_logger
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import (
    PostResponseEffectsSupport,
    ResponseOutcome,
    apply_post_response_effects,
)
from mindroom.streaming import StreamingResponse, send_streaming_response
from tests.conftest import (
    bind_runtime_paths,
    ignore_final_delivery_handoff,
    make_conversation_reader_mock,
    make_matrix_client_mock,
    make_outbox_mock,
    message_origin,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path


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


def _client() -> AsyncMock:
    client = make_matrix_client_mock(user_id="@mindroom_code:localhost")
    client.room_get_event_relations = Mock(return_value=_empty_async_iter())
    return client


def _room_send_response(event_id: str) -> MagicMock:
    response = MagicMock()
    response.__class__ = nio.RoomSendResponse
    response.event_id = event_id
    return response


async def _empty_async_iter() -> AsyncIterator[None]:
    if False:
        yield None


async def _empty_stream() -> AsyncIterator[str]:
    if False:
        yield ""


def _streaming_response(config: Config) -> StreamingResponse:
    return StreamingResponse(
        target=MessageTarget.resolve("!room:localhost", None, "$reply"),
        config=config,
        runtime_paths=runtime_paths_for(config),
    )


def _envelope() -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$reply",
        target=MessageTarget.resolve("!room:localhost", None, "$reply"),
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="code",
        origin=message_origin(
            sender_id="@user:localhost",
            requester_id="@user:localhost",
            source_kind=MESSAGE_SOURCE_KIND,
        ),
    )


def _delivery_gateway(tmp_path: Path) -> DeliveryGateway:
    config = _config(tmp_path)
    response_hooks = SimpleNamespace(
        _apply_before_response=AsyncMock(
            return_value=SimpleNamespace(
                response_text="final answer",
                response_kind="ai",
                tool_trace=None,
                extra_content=None,
                envelope=_envelope(),
                suppress=False,
            ),
        ),
        _apply_final_response_transform=AsyncMock(),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )
    return DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=_client(), orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=Mock(),
            redact_message_event=AsyncMock(return_value=True),
            resolver=SimpleNamespace(deps=SimpleNamespace()),
            response_hooks=response_hooks,
            outbox=make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
        ),
    )


@pytest.mark.asyncio
async def test_transport_retry_terminal_send_with_no_event_id_retries_until_send_lands(tmp_path: Path) -> None:
    """Terminal sends should retry even when finalize is sending the first visible event."""
    config = _config(tmp_path)
    streaming = _streaming_response(config)
    streaming.accumulated_text = "hello"
    sleep_mock = AsyncMock()
    delivered = DeliveredMatrixEvent(
        event_id="$terminal-send",
        content_sent={"body": "hello"},
    )

    with (
        patch(
            "mindroom.streaming.send_message_result",
            new=AsyncMock(side_effect=[None, None, delivered]),
        ) as mock_send,
        patch("mindroom.streaming.asyncio.sleep", new=sleep_mock),
    ):
        outcome = await streaming.finalize(_client())

    assert mock_send.await_count == 2
    sleep_mock.assert_not_awaited()
    assert outcome.terminal_status == "completed"
    assert outcome.failure_reason == "terminal_update_failed"
    assert outcome.last_physical_stream_event_id is None


@pytest.mark.asyncio
async def test_completed_terminal_edit_opts_into_sync_recovery_retry(tmp_path: Path) -> None:
    """Completed terminal delivery should opt into the bounded recovery retry."""
    streaming = _streaming_response(_config(tmp_path))
    streaming.event_id = "$placeholder"
    streaming.accumulated_text = "complete answer"
    delivered = DeliveredMatrixEvent(
        event_id="$terminal-edit",
        content_sent={"body": "complete answer"},
    )
    edit = AsyncMock(return_value=delivered)

    with patch("mindroom.streaming.edit_message_result", new=edit):
        outcome = await streaming.finalize(_client())

    assert outcome.terminal_status == "completed"
    assert outcome.failure_reason is None
    edit.assert_awaited_once()
    assert edit.call_args.kwargs["retry_sync_recovery"] is True


@pytest.mark.asyncio
async def test_a_completed_first_event_answer_goes_through_the_durable_sender(tmp_path: Path) -> None:
    """A stream with no placeholder still owes its answer a durable row.

    When the placeholder was suppressed or its send failed, the answer is the
    stream's first visible event and reaches the room through the send path.
    Leaving that path on the plain sender makes exactly those answers
    unrecoverable -- the ones whose turn already had a delivery problem.
    """
    streaming = _streaming_response(_config(tmp_path))
    streaming.event_id = None
    streaming.accumulated_text = "complete answer"
    delivered = DeliveredMatrixEvent(event_id="$answer", content_sent={"body": "complete answer"})
    terminal_send = AsyncMock(return_value=delivered)
    streaming.terminal_send = terminal_send
    plain_send = AsyncMock(return_value=delivered)

    with patch("mindroom.streaming.send_message_result", new=plain_send):
        outcome = await streaming.finalize(_client())

    assert outcome.terminal_status == "completed"
    terminal_send.assert_awaited_once()
    plain_send.assert_not_awaited()
    assert terminal_send.await_args.args[3] == "complete answer"


@pytest.mark.asyncio
async def test_a_cancelled_first_event_note_stays_on_the_plain_sender(tmp_path: Path) -> None:
    """A cancellation note is transport, not an answer, first event or not."""
    streaming = _streaming_response(_config(tmp_path))
    streaming.event_id = None
    streaming.accumulated_text = "partial answer"
    delivered = DeliveredMatrixEvent(event_id="$note", content_sent={"body": "partial answer"})
    terminal_send = AsyncMock(return_value=delivered)
    streaming.terminal_send = terminal_send
    plain_send = AsyncMock(return_value=delivered)

    with patch("mindroom.streaming.send_message_result", new=plain_send):
        await streaming.finalize(_client(), cancelled=True, cancel_source="user_stop")

    terminal_send.assert_not_awaited()
    plain_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_terminal_sync_recovery_error_does_not_backoff(tmp_path: Path) -> None:
    """Cancellation notes retain their immediate terminal-delivery semantics."""
    streaming = _streaming_response(_config(tmp_path))
    streaming.event_id = "$placeholder"
    streaming.accumulated_text = "partial answer"
    edit = AsyncMock(side_effect=nio.SendRetryError("Room timeline recovery is still pending."))
    sleep_mock = AsyncMock()

    with (
        patch("mindroom.streaming.edit_message_result", new=edit),
        patch("mindroom.streaming.asyncio.sleep", new=sleep_mock),
    ):
        outcome = await streaming.finalize(_client(), cancelled=True)

    edit.assert_awaited_once()
    assert edit.call_args.kwargs["retry_sync_recovery"] is False
    sleep_mock.assert_not_awaited()
    assert outcome.terminal_status == "cancelled"
    assert outcome.failure_reason == "cancelled_by_user"


@pytest.mark.asyncio
async def test_completed_terminal_ordinary_exception_keeps_immediate_retry(tmp_path: Path) -> None:
    """Ordinary terminal failures keep the existing two immediate attempts."""
    streaming = _streaming_response(_config(tmp_path))
    streaming.event_id = "$placeholder"
    streaming.accumulated_text = "complete answer"
    edit = AsyncMock(side_effect=RuntimeError("ordinary failure"))
    sleep_mock = AsyncMock()

    with (
        patch("mindroom.streaming.edit_message_result", new=edit),
        patch("mindroom.streaming.asyncio.sleep", new=sleep_mock),
    ):
        outcome = await streaming.finalize(_client())

    assert edit.await_count == 2
    sleep_mock.assert_not_awaited()
    assert outcome.terminal_status == "completed"
    assert outcome.failure_reason == "terminal_update_exception:RuntimeError"


@pytest.mark.asyncio
async def test_transport_cancelled_terminal_update_does_not_sleep_behind_retry_backoff(tmp_path: Path) -> None:
    """Cancelled terminal updates should finish immediately without retry backoff."""
    config = _config(tmp_path)
    streaming = _streaming_response(config)
    streaming.event_id = "$placeholder"
    streaming.accumulated_text = "partial answer"
    sleep_mock = AsyncMock()

    with (
        patch(
            "mindroom.streaming.edit_message_result",
            new=AsyncMock(side_effect=asyncio.CancelledError("user-stop")),
        ),
        patch("mindroom.streaming.asyncio.sleep", new=sleep_mock),
    ):
        outcome = await streaming.finalize(_client(), cancelled=True)

    sleep_mock.assert_not_awaited()
    assert outcome.terminal_status == "cancelled"
    assert outcome.failure_reason == "cancelled_by_user"


@pytest.mark.asyncio
async def test_transport_restart_interrupted_terminal_update_does_not_sleep_behind_retry_backoff(
    tmp_path: Path,
) -> None:
    """Restart-interrupted terminal updates should not sit in edit retry backoff."""
    config = _config(tmp_path)
    streaming = _streaming_response(config)
    streaming.event_id = "$placeholder"
    streaming.accumulated_text = "partial answer"
    sleep_mock = AsyncMock()

    with (
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(return_value=None)) as mock_edit,
        patch("mindroom.streaming.asyncio.sleep", new=sleep_mock),
    ):
        outcome = await streaming.finalize(_client(), restart_interrupted=True)

    assert mock_edit.await_count == 1
    sleep_mock.assert_not_awaited()
    assert outcome.terminal_status == "cancelled"
    assert outcome.failure_reason == "sync_restart_cancelled"
    assert outcome.terminal_update_committed is False


@pytest.mark.asyncio
async def test_transport_restart_interrupted_terminal_update_reports_committed(tmp_path: Path) -> None:
    """A landed restart interruption must carry explicit terminal-update proof."""
    streaming = _streaming_response(_config(tmp_path))
    streaming.event_id = "$placeholder"
    streaming.accumulated_text = "partial answer"

    with patch(
        "mindroom.streaming.edit_message_result",
        new=AsyncMock(
            return_value=DeliveredMatrixEvent(
                event_id="$terminal-edit",
                content_sent={"body": "partial answer\n\n**[Response interrupted by service restart]**"},
            ),
        ),
    ):
        outcome = await streaming.finalize(_client(), restart_interrupted=True)

    assert outcome.terminal_status == "cancelled"
    assert outcome.terminal_update_committed is True
    assert outcome.rendered_body is not None
    assert outcome.rendered_body.endswith("**[Response interrupted by service restart]**")


@pytest.mark.asyncio
async def test_transport_placeholder_only_cancelled_terminal_update_keeps_committed_placeholder_body(
    tmp_path: Path,
) -> None:
    """Cancelled terminal edits must preserve the last committed placeholder body, not the unlanded cancel note."""
    config = _config(tmp_path)
    streaming = _streaming_response(config)
    streaming.event_id = "$placeholder"
    streaming.placeholder_progress_sent = True

    with patch(
        "mindroom.streaming.edit_message_result",
        new=AsyncMock(side_effect=asyncio.CancelledError("user-stop")),
    ):
        outcome = await streaming.finalize(_client(), cancelled=True)

    assert outcome.failure_reason == "cancelled_by_user"
    assert outcome.rendered_body == "Thinking..."
    assert outcome.visible_body_state == "placeholder_only"


@pytest.mark.asyncio
async def test_transport_existing_visible_cancel_without_new_body_preserves_prior_event(
    tmp_path: Path,
) -> None:
    """Cancelling a regeneration before new text lands must not overwrite the old visible reply."""
    config = _config(tmp_path)
    streaming = _streaming_response(config)
    streaming.event_id = "$existing"
    streaming.preserve_existing_visible_on_empty_terminal = True

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock()) as mock_edit:
        outcome = await streaming.finalize(_client(), cancel_source="user_stop")

    mock_edit.assert_not_awaited()
    assert outcome.last_physical_stream_event_id == "$existing"
    assert outcome.terminal_status == "cancelled"
    assert outcome.rendered_body is None
    assert outcome.visible_body_state == "none"
    assert outcome.failure_reason == "cancelled_by_user"


@pytest.mark.asyncio
async def test_transport_failed_terminal_update_drops_committed_interactive_metadata(
    tmp_path: Path,
) -> None:
    """Late terminal failures must not carry interactive metadata into a failed terminal outcome."""
    config = _config(tmp_path)
    streaming = _streaming_response(config)
    streaming.accumulated_text = """```interactive
{"question":"Choose","options":[{"emoji":"✅","label":"Yes","value":"yes"}]}
```"""

    with patch(
        "mindroom.streaming.send_message_result",
        new=AsyncMock(
            return_value=DeliveredMatrixEvent(
                event_id="$interactive",
                content_sent={"body": "Choose"},
            ),
        ),
    ):
        assert await streaming._send_or_edit_message(_client(), is_final=False)

    with patch(
        "mindroom.streaming.edit_message_result",
        new=AsyncMock(return_value=None),
    ):
        transport_outcome = await streaming.finalize(_client(), restart_interrupted=True)

    response_hooks = SimpleNamespace(
        _apply_before_response=AsyncMock(),
        _apply_final_response_transform=AsyncMock(),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=_client(), orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            resolver=Mock(),
            response_hooks=response_hooks,
            outbox=make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
        ),
    )

    outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            stream_transport_outcome=transport_outcome,
            initial_delivery_kind="sent",
            identity=ResponseIdentity(
                response_kind="ai",
                response_envelope=_envelope(),
                correlation_id="corr-interactive-preserved",
            ),
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert transport_outcome.failure_reason == "sync_restart_cancelled"
    assert transport_outcome.rendered_body is not None
    assert outcome.option_map is None
    assert outcome.options_list is None


@pytest.mark.asyncio
async def test_transport_failed_terminal_update_ignores_hidden_canonical_interactive_metadata(
    tmp_path: Path,
) -> None:
    """Preserved visible streamed replies must not register interactive metadata from hidden canonical content."""
    config = _config(tmp_path)
    response_hooks = SimpleNamespace(
        _apply_before_response=AsyncMock(),
        _apply_final_response_transform=AsyncMock(),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=_client(), orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            resolver=Mock(),
            response_hooks=response_hooks,
            outbox=make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
        ),
    )

    outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            stream_transport_outcome=StreamTransportOutcome(
                last_physical_stream_event_id="$visible",
                terminal_status="completed",
                rendered_body="visible plain text",
                visible_body_state="visible_body",
                canonical_final_body_candidate="yes\n\n- ✅ approve",
                failure_reason="terminal_update_failed",
            ),
            initial_delivery_kind="sent",
            identity=ResponseIdentity(
                response_kind="ai",
                response_envelope=_envelope(),
                correlation_id="corr-hidden-canonical-interactive",
            ),
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert outcome.terminal_status == "error"
    assert outcome.failure_reason == "terminal_update_failed"
    assert outcome.final_visible_body == "visible plain text"
    assert dict(outcome.option_map or {}) == {}
    assert list(outcome.options_list or ()) == []


@pytest.mark.asyncio
async def test_transport_empty_adopted_placeholder_finishes_as_error_note(tmp_path: Path) -> None:
    """Completed placeholder-backed runs with no visible text now preserve the committed placeholder."""
    config = _config(tmp_path)
    client = _client()

    async def record_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[3]
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        outcome = await send_streaming_response(
            client=client,
            target=MessageTarget.resolve("!room:localhost", None, "$reply", room_mode=True),
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=_empty_stream(),
            existing_event_id="$thinking",
            adopt_existing_placeholder=True,
        )

    assert outcome.last_physical_stream_event_id == "$thinking"
    assert outcome.terminal_status == "completed"
    assert outcome.rendered_body == "Thinking..."
    assert outcome.visible_body_state == "placeholder_only"


@pytest.mark.asyncio
async def test_final_delivery_failure_replaces_placeholder_with_failure_update(tmp_path: Path) -> None:
    """A failed final placeholder edit should get one clear terminal failure update when possible."""
    gateway = _delivery_gateway(tmp_path)
    edit_outcomes = [False, True]
    object.__setattr__(
        gateway,
        "edit_text",
        AsyncMock(side_effect=lambda _request: edit_outcomes.pop(0)),
    )

    outcome = await gateway.deliver_final(
        FinalDeliveryRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            existing_event_id="$placeholder",
            existing_event_is_placeholder=True,
            response_text="final answer",
            identity=ResponseIdentity(
                response_kind="ai",
                response_envelope=_envelope(),
                correlation_id="corr-final-delivery-failure",
            ),
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert outcome.terminal_status == "error"
    assert outcome.final_visible_event_id == "$placeholder"
    assert outcome.final_visible_body == "Response delivery failed. Please retry."
    assert outcome.delivery_kind == "edited"
    assert outcome.failure_reason == "delivery_failed"
    assert gateway.edit_text.await_count == 2
    failure_update_request = gateway.edit_text.await_args_list[-1].args[0]
    assert failure_update_request.new_text == "Response delivery failed. Please retry."
    assert failure_update_request.extra_content[STREAM_STATUS_KEY] == STREAM_STATUS_ERROR


@pytest.mark.asyncio
async def test_persistent_sync_recovery_barrier_settles_placeholder_as_delivery_failure(tmp_path: Path) -> None:
    """An exhausted final-edit retry should still run placeholder failure settlement."""
    gateway = _delivery_gateway(tmp_path)
    barrier_error = nio.SendRetryError("Room timeline recovery is still pending.")
    # The two attempts take different primitives now. The answer's edit carries a
    # delivery turn and goes out through the outbox as a frozen replace envelope;
    # the placeholder failure notice has no turn and edits directly.
    durable_edit = AsyncMock(side_effect=barrier_error)
    failure_edit = AsyncMock(return_value=None)
    with (
        patch("mindroom.delivery_gateway.send_message_result", new=durable_edit),
        patch("mindroom.delivery_gateway.edit_message_result", new=failure_edit),
    ):
        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!room:localhost", None, "$reply"),
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
                response_text="final answer",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=_envelope(),
                    correlation_id="corr-persistent-sync-recovery-barrier",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

    assert durable_edit.await_count == 1
    assert durable_edit.await_args.kwargs["retry_sync_recovery"] is True
    assert failure_edit.await_count == 1
    assert failure_edit.await_args.kwargs["retry_sync_recovery"] is False
    assert outcome.terminal_status == "error"
    assert outcome.final_visible_event_id == "$placeholder"
    assert outcome.failure_reason == "delivery_failed"


@pytest.mark.asyncio
async def test_persistent_sync_recovery_barrier_returns_new_send_delivery_failure(tmp_path: Path) -> None:
    """An exhausted final-send retry should retain the gateway failure contract."""
    gateway = _delivery_gateway(tmp_path)
    barrier_error = nio.SendRetryError("Room timeline recovery is still pending.")
    with patch(
        "mindroom.delivery_gateway.send_message_result",
        new=AsyncMock(side_effect=barrier_error),
    ) as send:
        outcome = await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!room:localhost", None, "$reply"),
                existing_event_id=None,
                response_text="final answer",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=_envelope(),
                    correlation_id="corr-persistent-sync-recovery-send-barrier",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )

    send.assert_awaited_once()
    assert send.await_args.kwargs["retry_sync_recovery"] is True
    assert outcome.terminal_status == "error"
    assert outcome.final_visible_event_id is None
    assert outcome.failure_reason == "delivery_failed"


@pytest.mark.asyncio
async def test_streaming_placeholder_delivery_failure_stays_terminal_when_failure_update_fails(
    tmp_path: Path,
) -> None:
    """If Matrix rejects the failure update too, finalization still returns a failed visible outcome."""
    config = _config(tmp_path)
    response_hooks = SimpleNamespace(
        _apply_before_response=AsyncMock(),
        _apply_final_response_transform=AsyncMock(),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )
    logger = Mock()
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=_client(), orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=logger,
            redact_message_event=AsyncMock(return_value=True),
            resolver=Mock(),
            response_hooks=response_hooks,
            outbox=make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
        ),
    )
    object.__setattr__(gateway, "edit_text", AsyncMock(return_value=False))

    outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            stream_transport_outcome=StreamTransportOutcome(
                last_physical_stream_event_id="$placeholder",
                terminal_status="error",
                rendered_body="Thinking...",
                visible_body_state="placeholder_only",
                failure_reason="terminal_update_failed",
            ),
            initial_delivery_kind="edited",
            identity=ResponseIdentity(
                response_kind="ai",
                response_envelope=_envelope(),
                correlation_id="corr-stream-delivery-failure",
            ),
            tool_trace=None,
            extra_content=None,
            existing_event_id="$placeholder",
            existing_event_is_placeholder=True,
        ),
    )

    assert outcome.terminal_status == "error"
    assert outcome.final_visible_event_id == "$placeholder"
    assert outcome.final_visible_body is None
    assert outcome.failure_reason == "terminal_update_failed"
    assert outcome.mark_handled is True
    logger.error.assert_called_once_with(
        "Failed to deliver placeholder failure update",
        room_id="!room:localhost",
        event_id="$placeholder",
        response_kind="ai",
        source_event_id="$reply",
        correlation_id="corr-stream-delivery-failure",
        failure_reason="terminal_update_failed",
    )


@pytest.mark.asyncio
async def test_transport_final_event_only_body_uses_canonical_final_candidate(tmp_path: Path) -> None:
    """Final-only provider content should stay pre-visible until the gateway applies before_response."""
    config = _config(tmp_path)
    client = _client()

    async def final_only_stream() -> AsyncIterator[object]:
        yield RunCompletedEvent(content="hello from final event")

    async def record_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[3]
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        outcome = await send_streaming_response(
            client=client,
            target=MessageTarget.resolve("!room:localhost", None, "$reply", room_mode=True),
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=final_only_stream(),
            existing_event_id="$thinking",
            adopt_existing_placeholder=True,
        )

    assert outcome.terminal_status == "completed"
    assert outcome.rendered_body == "Thinking..."
    assert outcome.visible_body_state == "placeholder_only"
    assert outcome.canonical_final_body_candidate == "hello from final event"


@pytest.mark.asyncio
async def test_run_completed_content_does_not_rewrite_visible_stream_text(tmp_path: Path) -> None:
    """Canonical completion content must not replace visible streamed text during streaming."""
    config = _config(tmp_path)
    client = _client()
    captured_edits: list[dict[str, Any]] = []

    async def tool_then_final_content() -> AsyncIterator[object]:
        yield RunContentEvent(content="Let me search...\n\n")
        tool = SimpleNamespace(tool_name="run_shell_command", tool_args={"cmd": "pwd"}, result="ok")
        yield ToolCallStartedEvent(tool=tool)
        yield ToolCallCompletedEvent(tool=tool)
        yield RunCompletedEvent(content="Final answer")

    async def record_edit(*_args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = _args[3]
        captured_edits.append(content)
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        outcome = await send_streaming_response(
            client=client,
            target=MessageTarget.resolve("!room:localhost", None, "$reply", room_mode=True),
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=tool_then_final_content(),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
        )

    assert outcome.last_physical_stream_event_id == "$placeholder"
    assert outcome.rendered_body is not None
    assert outcome.rendered_body.startswith("Let me search...")
    assert "🔧 `run_shell_command` [1]" in outcome.rendered_body
    assert "Final answer" not in outcome.rendered_body
    assert captured_edits[-1]["body"] == outcome.rendered_body


@pytest.mark.asyncio
async def test_streamed_interactive_final_reply_registers_reactions_on_root_event(tmp_path: Path) -> None:
    """A streamed final interactive block should register reactions on the displayed root event."""
    config = _config(tmp_path)
    target = MessageTarget.resolve("!room:localhost", "$thread-root", "$reply")
    client = _client()
    raw_interactive = (
        "```interactive\n"
        "{\n"
        '  "question": "Interactive-question repro test: do the emoji reaction options show up on this message?",\n'
        '  "options": [\n'
        '    {"emoji": "✅", "label": "Yes", "value": "yes"},\n'
        '    {"emoji": "❌", "label": "No", "value": "no"},\n'
        '    {"emoji": "🧪", "label": "Test", "value": "test"}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    formatted_interactive = interactive.parse_and_format_interactive(raw_interactive, extract_mapping=True)
    captured_stream_edits: list[dict[str, Any]] = []

    async def interactive_stream() -> AsyncIterator[str]:
        yield raw_interactive

    async def record_stream_edit(*args: object, **_kwargs: object) -> DeliveredMatrixEvent:
        content = args[3]
        assert isinstance(content, dict)
        captured_stream_edits.append(dict(content))
        return DeliveredMatrixEvent(event_id="$obsolete-edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_stream_edit)):
        stream_outcome = await send_streaming_response(
            client=client,
            target=target,
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=interactive_stream(),
            existing_event_id="$displayed-root",
            adopt_existing_placeholder=True,
        )

    assert stream_outcome.last_physical_stream_event_id == "$displayed-root"
    assert stream_outcome.rendered_body == formatted_interactive.formatted_text
    assert stream_outcome.canonical_final_body_candidate == raw_interactive
    assert captured_stream_edits[-1]["body"] == formatted_interactive.formatted_text

    envelope = _envelope()
    response_hooks = SimpleNamespace(
        _apply_before_response=AsyncMock(),
        _apply_final_response_transform=AsyncMock(
            return_value=SimpleNamespace(
                response_text=raw_interactive,
                response_kind="ai",
                envelope=envelope,
            ),
        ),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=client, orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            resolver=Mock(),
            response_hooks=response_hooks,
            outbox=make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
        ),
    )

    final_outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=target,
            stream_transport_outcome=stream_outcome,
            initial_delivery_kind="sent",
            identity=ResponseIdentity(
                response_kind="ai",
                response_envelope=envelope,
                correlation_id="corr-streamed-interactive",
            ),
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert final_outcome.terminal_status == "completed"
    assert final_outcome.final_visible_event_id == "$displayed-root"
    assert final_outcome.final_visible_body == formatted_interactive.formatted_text
    assert dict(final_outcome.option_map or {}) == {
        "✅": "yes",
        "1": "yes",
        "❌": "no",
        "2": "no",
        "🧪": "test",
        "3": "test",
    }
    assert final_outcome.options_list == (
        {"emoji": "✅", "label": "Yes", "value": "yes"},
        {"emoji": "❌", "label": "No", "value": "no"},
        {"emoji": "🧪", "label": "Test", "value": "test"},
    )

    client.room_send.side_effect = [
        _room_send_response("$reaction-yes"),
        _room_send_response("$reaction-no"),
        _room_send_response("$reaction-test"),
    ]
    interactive._cleanup()
    try:
        support = PostResponseEffectsSupport(
            runtime=SimpleNamespace(client=client, config=config),
            logger=get_logger("tests.post_response"),
            runtime_paths=runtime_paths_for(config),
            delivery_gateway=Mock(),
            conversation_reader=make_conversation_reader_mock(),
        )
        await apply_post_response_effects(
            final_outcome,
            ResponseOutcome(interactive_target=target),
            support.build_deps(
                room_id=target.room_id,
                interactive_agent_name="code",
            ),
        )

        registered = interactive._active_questions["$displayed-root"]
        assert registered.thread_id == "$thread-root"
        assert registered.options == final_outcome.option_map
        reaction_targets = [
            call.kwargs["content"]["m.relates_to"]["event_id"] for call in client.room_send.await_args_list
        ]
        reaction_keys = [call.kwargs["content"]["m.relates_to"]["key"] for call in client.room_send.await_args_list]
        assert reaction_targets == ["$displayed-root", "$displayed-root", "$displayed-root"]
        assert "$obsolete-edit" not in reaction_targets
        assert reaction_keys == ["✅", "❌", "🧪"]
    finally:
        interactive._cleanup()


@pytest.mark.asyncio
async def test_streamed_interactive_metadata_survives_unparseable_canonical_final_body(tmp_path: Path) -> None:
    """Registration should use the same interactive parse that rendered the visible streamed body."""
    config = _config(tmp_path)
    target = MessageTarget.resolve("!room:localhost", "$thread-root", "$reply")
    client = _client()
    raw_interactive = (
        "```interactive\n"
        "{\n"
        '  "question": "What next?",\n'
        '  "options": [\n'
        '    {"emoji": "🎙️", "label": "Transcribe audio", "value": "transcribe"},\n'
        '    {"emoji": "📂", "label": "Inspect incoming", "value": "inspect"}\n'
        "  ]\n"
        "}\n"
        "```"
    )
    formatted_interactive = interactive.parse_and_format_interactive(raw_interactive, extract_mapping=True)

    async def interactive_stream() -> AsyncIterator[str]:
        yield raw_interactive

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(return_value=DeliveredMatrixEvent("$edit", {}))):
        stream_outcome = await send_streaming_response(
            client=client,
            target=target,
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=interactive_stream(),
            existing_event_id="$displayed-root",
            adopt_existing_placeholder=True,
        )

    assert stream_outcome.rendered_body == formatted_interactive.formatted_text
    stream_outcome = replace(
        stream_outcome,
        canonical_final_body_candidate='```interactive\n{"question": "What next?", "options": [',
    )

    response_hooks = SimpleNamespace(
        _apply_before_response=AsyncMock(),
        _apply_final_response_transform=AsyncMock(
            return_value=SimpleNamespace(
                response_text=stream_outcome.canonical_final_body_candidate,
                response_kind="ai",
                envelope=_envelope(),
            ),
        ),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=client, orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            resolver=Mock(),
            response_hooks=response_hooks,
            outbox=make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
        ),
    )

    final_outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=target,
            stream_transport_outcome=stream_outcome,
            initial_delivery_kind="sent",
            identity=ResponseIdentity(
                response_kind="ai",
                response_envelope=_envelope(),
                correlation_id="corr-streamed-interactive-unparseable-canonical",
            ),
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert final_outcome.final_visible_body == formatted_interactive.formatted_text
    assert final_outcome.interactive_metadata is not None
    assert dict(final_outcome.option_map or {}) == {
        "🎙️": "transcribe",
        "1": "transcribe",
        "📂": "inspect",
        "2": "inspect",
    }


@pytest.mark.asyncio
async def test_finalize_streamed_response_restart_interruption_preserves_cancellation_state(tmp_path: Path) -> None:
    """Structured streamed restart interruptions should arrive with cancelled terminal status."""
    config = _config(tmp_path)
    envelope = _envelope()
    response_hooks = SimpleNamespace(
        _apply_before_response=AsyncMock(),
        _apply_final_response_transform=AsyncMock(),
        emit_after_response=AsyncMock(),
        emit_cancelled_response=AsyncMock(),
    )
    gateway = DeliveryGateway(
        DeliveryGatewayDeps(
            runtime=SimpleNamespace(client=_client(), orchestrator=None, config=config, runtime_started_at=0.0),
            runtime_paths=runtime_paths_for(config),
            agent_name="code",
            logger=get_logger("tests.delivery"),
            redact_message_event=AsyncMock(return_value=True),
            resolver=Mock(),
            response_hooks=response_hooks,
            outbox=make_outbox_mock(),
            turn_handoff=ignore_final_delivery_handoff,
        ),
    )

    outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            stream_transport_outcome=StreamTransportOutcome(
                last_physical_stream_event_id="$streaming",
                terminal_status="cancelled",
                rendered_body="partial answer\n\n**[Response interrupted by service restart]**",
                visible_body_state="visible_body",
                terminal_update_committed=True,
                failure_reason="sync_restart_cancelled",
            ),
            initial_delivery_kind="edited",
            identity=ResponseIdentity(
                response_kind="ai",
                response_envelope=envelope,
                correlation_id="corr-stream-restart-cancelled",
            ),
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert outcome.terminal_status == "cancelled"
    assert outcome.final_visible_event_id == "$streaming"
    assert outcome.mark_handled is True
    assert outcome.delivery_kind == "edited"
    response_hooks.emit_after_response.assert_not_awaited()
    response_hooks.emit_cancelled_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_cancelled_placeholder_cleanup_preserves_cancel_source(tmp_path: Path) -> None:
    """A failed cancellation edit and redaction must retain the original restart provenance."""
    gateway = _delivery_gateway(tmp_path)
    gateway.deps.redact_message_event.return_value = False

    with patch.object(DeliveryGateway, "edit_text", new=AsyncMock(return_value=False)):
        outcome = await gateway.deliver_cancelled_visible_note(
            CancelledVisibleNoteRequest(
                target=MessageTarget.resolve("!room:localhost", "$thread", "$reply"),
                event_id="$placeholder",
                existing_event_is_placeholder=True,
                cancel_source="sync_restart",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=_envelope(),
                    correlation_id="corr-cancel-cleanup-failure",
                ),
            ),
        )

    assert outcome.terminal_status == "error"
    assert outcome.final_visible_event_id == "$placeholder"
    assert outcome.cancel_source == "sync_restart"


@pytest.mark.asyncio
async def test_hook_failure_cleanup_propagates_restart_cancellation(tmp_path: Path) -> None:
    """Cancellation during cleanup of an ordinary hook failure must reach source settlement."""
    gateway = _delivery_gateway(tmp_path)
    gateway.deps.response_hooks._apply_before_response.side_effect = RuntimeError("hook failed")
    gateway.deps.redact_message_event.side_effect = asyncio.CancelledError("sync_restart")

    with pytest.raises(asyncio.CancelledError, match="sync_restart"):
        await gateway.deliver_final(
            FinalDeliveryRequest(
                target=MessageTarget.resolve("!room:localhost", "$thread", "$reply"),
                existing_event_id="$placeholder",
                existing_event_is_placeholder=True,
                response_text="answer",
                identity=ResponseIdentity(
                    response_kind="ai",
                    response_envelope=_envelope(),
                    correlation_id="corr-hook-failure-cleanup-cancel",
                ),
                tool_trace=None,
                extra_content=None,
            ),
        )


@pytest.mark.asyncio
async def test_an_oversized_send_freezes_the_payload_matrix_receives(tmp_path: Path) -> None:
    """The frozen row must be the wire event, not the oversized original.

    The sidecar upload used to happen inside the send, after the row was
    claimed and frozen, so the durable payload kept the whole body while
    Matrix received an MXC reference. They disagreed, and a recovery resend
    would upload again -- minting a new MXC, and new encrypted-file keys,
    under a transaction ID the homeserver had already accepted.
    """
    from mindroom.delivery_gateway import SendTextRequest  # noqa: PLC0415 - a request type only this test builds
    from mindroom.event_journal import DeliveryStage  # noqa: PLC0415 - ditto

    gateway = _delivery_gateway(tmp_path)

    await gateway._send_content(
        SendTextRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$src"),
            response_text="",
            delivery_turn_id="$turn",
            delivery_stage=DeliveryStage.FINAL,
        ),
        "!room:localhost",
        {"body": "x" * 100_000, "msgtype": "m.text"},
    )

    rows = gateway.deps.outbox.rows  # type: ignore[attr-defined]
    assert rows, "the send never reached the outbox"
    frozen = next(iter(rows.values())).payload
    # Whether the sidecar upload succeeds or degrades to an inline preview is
    # the homeserver's business; either way the row must hold the *prepared*
    # payload, because that is what goes on the wire and what a resend replays.
    assert len(frozen["body"]) < 100_000, "the row kept the oversized original"


def test_the_gateway_hands_the_final_transform_to_the_stream() -> None:
    """The transform only helps if the stream is actually given it.

    The unit tests for this behaviour drive `StreamingResponse` directly, so
    they pass whether or not the gateway wires the hook in -- deleting the
    wiring leaves them green. This reads the construction instead: the stream
    must be built with a `final_text_transform`, and it must come from the
    request's identity rather than being hardcoded, since a request with no
    identity has no hook to apply.
    """
    import inspect  # noqa: PLC0415 - reading the construction is this test's whole point

    source = inspect.getsource(DeliveryGateway.deliver_stream)

    assert "final_text_transform=self._final_text_transform(request.identity)" in source


@pytest.mark.asyncio
async def test_an_oversized_edit_freezes_the_payload_matrix_receives(tmp_path: Path) -> None:
    """The edit path freezes the wire event too, not just the send path.

    An edit carries the whole replacement body, so it crosses the size limit
    the same way a send does -- and its row is what recovery replays. Pinning
    only the send left this half able to regress on its own.
    """
    from mindroom.delivery_gateway import EditTextRequest  # noqa: PLC0415 - a request type only this test builds

    gateway = _delivery_gateway(tmp_path)

    await gateway._edit_content(
        EditTextRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$src"),
            event_id="$original",
            new_text="x" * 100_000,
            delivery_turn_id="$turn",
        ),
        "!room:localhost",
        {"body": "x" * 100_000, "msgtype": "m.text"},
    )

    rows = gateway.deps.outbox.rows  # type: ignore[attr-defined]
    assert rows, "the edit never reached the outbox"
    frozen = next(iter(rows.values())).payload
    body = frozen.get("m.new_content", frozen).get("body", "")
    assert len(body) < 100_000, "the row kept the oversized original"
