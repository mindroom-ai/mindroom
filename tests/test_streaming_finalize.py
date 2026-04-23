"""Regression tests for the streaming terminal transport boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, patch

import pytest
from agno.run.agent import RunCompletedEvent, RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.delivery_gateway import DeliveryGateway, DeliveryGatewayDeps, FinalizeStreamedResponseRequest
from mindroom.final_delivery import StreamTransportOutcome
from mindroom.hooks import MessageEnvelope
from mindroom.logging_config import get_logger
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.message_target import MessageTarget
from mindroom.post_response_effects import PostResponseEffectsDeps, ResponseOutcome
from mindroom.response_lifecycle import DeliveryOutcome, ResponseLifecycle
from mindroom.streaming import (
    StreamingResponse,
    send_streaming_response,
)
from tests.conftest import bind_runtime_paths, make_matrix_client_mock, runtime_paths_for, test_runtime_paths

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


async def _empty_async_iter() -> AsyncIterator[None]:
    if False:
        yield None


async def _empty_stream() -> AsyncIterator[str]:
    if False:
        yield ""


def _streaming_response(config: Config) -> StreamingResponse:
    return StreamingResponse(
        room_id="!room:localhost",
        reply_to_event_id="$reply",
        thread_id=None,
        sender_domain="localhost",
        config=config,
        runtime_paths=runtime_paths_for(config),
    )


def _envelope() -> MessageEnvelope:
    return MessageEnvelope(
        source_event_id="$reply",
        room_id="!room:localhost",
        target=MessageTarget.resolve("!room:localhost", None, "$reply"),
        requester_id="@user:localhost",
        sender_id="@user:localhost",
        body="hello",
        attachment_ids=(),
        mentioned_agents=(),
        agent_name="code",
        source_kind="message",
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
    assert outcome.terminal_operation == "send"
    assert outcome.terminal_result == "failed"
    assert outcome.last_physical_stream_event_id is None


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
    assert outcome.terminal_result == "cancelled"
    assert outcome.terminal_status == "cancelled"


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
    assert outcome.terminal_result == "failed"
    assert outcome.terminal_status == "cancelled"


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

    assert outcome.terminal_result == "cancelled"
    assert outcome.rendered_body == "Thinking..."
    assert outcome.visible_body_state == "placeholder_only"


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
        apply_before_response=AsyncMock(),
        apply_final_response_transform=AsyncMock(),
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
            sender_domain="localhost",
            resolver=Mock(),
            response_hooks=response_hooks,
        ),
    )

    outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            stream_transport_outcome=transport_outcome,
            initial_delivery_kind="sent",
            response_kind="ai",
            response_envelope=_envelope(),
            correlation_id="corr-interactive-preserved",
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert transport_outcome.terminal_result == "failed"
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
        apply_before_response=AsyncMock(),
        apply_final_response_transform=AsyncMock(),
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
            sender_domain="localhost",
            resolver=Mock(),
            response_hooks=response_hooks,
        ),
    )

    outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            stream_transport_outcome=StreamTransportOutcome(
                last_physical_stream_event_id="$visible",
                terminal_operation="edit",
                terminal_result="failed",
                terminal_status="error",
                rendered_body="visible plain text",
                visible_body_state="visible_body",
                canonical_final_body_candidate="yes\n\n- ✅ approve",
                failure_reason="terminal_update_failed",
            ),
            initial_delivery_kind="sent",
            response_kind="ai",
            response_envelope=_envelope(),
            correlation_id="corr-hidden-canonical-interactive",
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert outcome.final_visible_body == "visible plain text"
    assert dict(outcome.option_map or {}) == {}
    assert list(outcome.options_list or ()) == []


@pytest.mark.asyncio
async def test_transport_empty_adopted_placeholder_finishes_as_error_note(tmp_path: Path) -> None:
    """Completed placeholder-backed runs with no visible text now preserve the committed placeholder."""
    config = _config(tmp_path)
    client = _client()

    async def record_edit(*_args: object) -> DeliveredMatrixEvent:
        content = _args[3]
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        outcome = await send_streaming_response(
            client=client,
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=_empty_stream(),
            existing_event_id="$thinking",
            adopt_existing_placeholder=True,
            room_mode=True,
        )

    assert outcome.last_physical_stream_event_id == "$thinking"
    assert outcome.terminal_status == "completed"
    assert outcome.rendered_body == "Thinking..."
    assert outcome.visible_body_state == "placeholder_only"


@pytest.mark.asyncio
async def test_transport_final_event_only_body_uses_canonical_final_candidate(tmp_path: Path) -> None:
    """Final-only provider content should stay pre-visible until the gateway applies before_response."""
    config = _config(tmp_path)
    client = _client()

    async def final_only_stream() -> AsyncIterator[object]:
        yield RunCompletedEvent(content="hello from final event")

    async def record_edit(*_args: object) -> DeliveredMatrixEvent:
        content = _args[3]
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        outcome = await send_streaming_response(
            client=client,
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=final_only_stream(),
            existing_event_id="$thinking",
            adopt_existing_placeholder=True,
            room_mode=True,
        )

    assert outcome.terminal_status == "completed"
    assert outcome.rendered_body == "Thinking..."
    assert outcome.visible_body_state == "placeholder_only"
    assert outcome.terminal_operation == "none"
    assert outcome.terminal_result == "not_attempted"
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

    async def record_edit(*_args: object) -> DeliveredMatrixEvent:
        content = _args[3]
        captured_edits.append(content)
        return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(content))

    with patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)):
        outcome = await send_streaming_response(
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

    assert outcome.last_physical_stream_event_id == "$placeholder"
    assert outcome.rendered_body is not None
    assert outcome.rendered_body.startswith("Let me search...")
    assert "🔧 `run_shell_command` [1]" in outcome.rendered_body
    assert "Final answer" not in outcome.rendered_body
    assert captured_edits[-1]["body"] == outcome.rendered_body


@pytest.mark.asyncio
async def test_final_response_transform_failure_keeps_visible_stream_text(tmp_path: Path) -> None:
    """A failed one-shot final transform edit must keep the visible streamed text and resolve cleanly."""
    config = _config(tmp_path)
    envelope = _envelope()
    response_hooks = SimpleNamespace(
        apply_before_response=AsyncMock(
            return_value=SimpleNamespace(
                response_text="chunk",
                response_kind="ai",
                tool_trace=None,
                extra_content=None,
                envelope=envelope,
                suppress=False,
            ),
        ),
        apply_final_response_transform=AsyncMock(
            return_value=SimpleNamespace(
                response_text="updated text",
                response_kind="ai",
                envelope=envelope,
            ),
        ),
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
            sender_domain="localhost",
            resolver=Mock(),
            response_hooks=response_hooks,
        ),
    )
    object.__setattr__(gateway, "edit_text", AsyncMock(return_value=False))

    outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            stream_transport_outcome=StreamTransportOutcome(
                last_physical_stream_event_id="$streaming",
                terminal_operation="send",
                terminal_result="succeeded",
                terminal_status="completed",
                rendered_body="chunk",
                visible_body_state="visible_body",
            ),
            initial_delivery_kind="sent",
            response_kind="ai",
            response_envelope=envelope,
            correlation_id="corr-final-transform-failure",
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert outcome.terminal_status == "completed"
    assert outcome.final_visible_event_id == "$streaming"
    assert outcome.final_visible_body == "chunk"
    response_hooks.apply_before_response.assert_not_awaited()
    response_hooks.apply_final_response_transform.assert_awaited_once()
    gateway.edit_text.assert_awaited_once()
    runner = SimpleNamespace(
        deps=SimpleNamespace(
            delivery_gateway=SimpleNamespace(
                deps=SimpleNamespace(response_hooks=response_hooks),
            ),
        ),
        _log_post_response_effects_failure=Mock(),
        _emit_pipeline_timing_summary=Mock(),
        _response_outcome=Mock(return_value=None),
    )
    lifecycle = ResponseLifecycle(
        runner=runner,
        response_kind="ai",
        request=Mock(),
        response_envelope=envelope,
        correlation_id="corr-final-transform-failure",
    )
    finalized = await lifecycle.finalize(
        DeliveryOutcome(final_delivery_outcome=outcome),
        build_post_response_outcome=lambda delivered: ResponseOutcome(
            resolved_event_id=delivered.final_visible_event_id,
            interactive_event_id=delivered.final_visible_event_id,
            compaction_event_id=delivered.final_visible_event_id,
            suppressed=delivered.suppressed,
        ),
        post_response_deps=PostResponseEffectsDeps(logger=get_logger("tests.post_response")),
    )

    assert finalized.response_text == "chunk"
    assert finalized.delivery_kind == "sent"
    response_hooks.emit_after_response.assert_awaited_once()
    after_kwargs = response_hooks.emit_after_response.await_args.kwargs
    assert after_kwargs["response_text"] == "chunk"
    assert after_kwargs["delivery_kind"] == "sent"
    response_hooks.emit_cancelled_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_streamed_response_restart_interruption_preserves_cancellation_state(tmp_path: Path) -> None:
    """Structured streamed restart interruptions should arrive with cancelled terminal status."""
    config = _config(tmp_path)
    envelope = _envelope()
    response_hooks = SimpleNamespace(
        apply_before_response=AsyncMock(),
        apply_final_response_transform=AsyncMock(),
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
            sender_domain="localhost",
            resolver=Mock(),
            response_hooks=response_hooks,
        ),
    )

    outcome = await gateway.finalize_streamed_response(
        FinalizeStreamedResponseRequest(
            target=MessageTarget.resolve("!room:localhost", None, "$reply"),
            stream_transport_outcome=StreamTransportOutcome(
                last_physical_stream_event_id="$streaming",
                terminal_operation="edit",
                terminal_result="succeeded",
                terminal_status="cancelled",
                rendered_body="partial answer\n\n**[Response interrupted by service restart]**",
                visible_body_state="visible_body",
                failure_reason="sync_restart_cancelled",
            ),
            initial_delivery_kind="edited",
            response_kind="ai",
            response_envelope=envelope,
            correlation_id="corr-stream-restart-cancelled",
            tool_trace=None,
            extra_content=None,
        ),
    )

    assert outcome.terminal_status == "cancelled"
    assert outcome.final_visible_event_id == "$streaming"
    assert outcome.mark_handled is True
    response_hooks.emit_after_response.assert_not_awaited()
    response_hooks.emit_cancelled_response.assert_not_awaited()
