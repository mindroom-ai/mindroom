"""Regression tests for the streaming terminal transport boundary."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, Mock, call, patch

import pytest
from agno.run.agent import RunCompletedEvent, RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent

from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.matrix.client import DeliveredMatrixEvent
from mindroom.streaming import (
    _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE,
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

    assert mock_send.await_count == 3
    assert sleep_mock.await_args_list == [call(2), call(4)]
    assert outcome.terminal_operation == "send"
    assert outcome.terminal_result == "succeeded"
    assert outcome.last_physical_stream_event_id == "$terminal-send"


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
    assert outcome.terminal_status == "error"


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
async def test_transport_failed_terminal_update_preserves_committed_interactive_metadata(
    tmp_path: Path,
) -> None:
    """Late terminal failures must keep the committed interactive mapping for the visible streamed question."""
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
        outcome = await streaming.finalize(_client(), restart_interrupted=True)

    assert outcome.terminal_result == "failed"
    assert outcome.rendered_body is not None
    assert outcome.option_map == {"✅": "yes", "1": "yes"}
    assert outcome.options_list == ({"emoji": "✅", "label": "Yes", "value": "yes"},)


@pytest.mark.asyncio
async def test_transport_empty_adopted_placeholder_finishes_as_error_note(tmp_path: Path) -> None:
    """Completed placeholder-backed runs with no visible text must not leave Thinking... behind."""
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
    assert outcome.terminal_status == "error"
    assert outcome.rendered_body == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
    assert outcome.visible_body_state == "visible_body"


@pytest.mark.asyncio
async def test_transport_final_event_content_keeps_visible_tool_markers(tmp_path: Path) -> None:
    """Final completion content must preserve visible order for text that appeared before a tool marker."""
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
    assert outcome.rendered_body.endswith("Final answer")
    assert captured_edits[-1]["body"] == outcome.rendered_body


@pytest.mark.asyncio
async def test_transport_failed_terminal_edit_preserves_last_committed_visible_body(tmp_path: Path) -> None:
    """A failed terminal rewrite must report the last body that actually reached Matrix."""
    config = _config(tmp_path)
    client = _client()
    sleep_mock = AsyncMock()
    first_stream_edit_landed = asyncio.Event()

    async def stream() -> AsyncIterator[object]:
        yield RunContentEvent(content="hel")
        await asyncio.wait_for(first_stream_edit_landed.wait(), timeout=1)
        yield RunCompletedEvent(content="hello")

    async def record_edit(
        _client: object,
        _room_id: str,
        _event_id: str,
        new_content: dict[str, Any],
        _new_text: str,
    ) -> DeliveredMatrixEvent | None:
        if new_content["io.mindroom.stream_status"] == "streaming":
            first_stream_edit_landed.set()
            return DeliveredMatrixEvent(event_id="$edit", content_sent=dict(new_content))
        return None

    with (
        patch("mindroom.streaming.edit_message_result", new=AsyncMock(side_effect=record_edit)) as mock_edit,
        patch("mindroom.streaming.asyncio.sleep", new=sleep_mock),
    ):
        outcome = await send_streaming_response(
            client=client,
            room_id="!room:localhost",
            reply_to_event_id="$reply",
            thread_id=None,
            sender_domain="localhost",
            config=config,
            runtime_paths=runtime_paths_for(config),
            response_stream=stream(),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
        )

    assert mock_edit.await_count == 7
    assert sleep_mock.await_args_list == [call(2), call(4), call(8), call(16), call(32)]
    assert outcome.terminal_result == "failed"
    assert outcome.rendered_body == "hel"
    assert outcome.visible_body_state == "visible_body"


@pytest.mark.asyncio
async def test_transport_hidden_tool_reasoning_only_finishes_as_error(tmp_path: Path) -> None:
    """Reasoning-only hidden-tool runs must not count as visible output success."""
    config = _config(tmp_path)
    client = _client()

    async def hidden_tool_reasoning_stream() -> AsyncIterator[object]:
        yield RunContentEvent(reasoning_content="pondering")
        yield ToolCallStartedEvent(tool=SimpleNamespace(tool_name="hidden"))
        yield RunCompletedEvent(reasoning_content="pondering")

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
            response_stream=hidden_tool_reasoning_stream(),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
            show_tool_calls=False,
        )

    assert outcome.terminal_status == "error"
    assert outcome.rendered_body == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE


@pytest.mark.asyncio
async def test_transport_hidden_tool_only_finishes_as_error(tmp_path: Path) -> None:
    """Hidden-tool-only runs without visible text must not finish as Thinking...."""
    config = _config(tmp_path)
    client = _client()

    async def hidden_tool_only_stream() -> AsyncIterator[object]:
        yield ToolCallStartedEvent(tool=SimpleNamespace(tool_name="hidden"))
        yield RunCompletedEvent()

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
            response_stream=hidden_tool_only_stream(),
            existing_event_id="$placeholder",
            adopt_existing_placeholder=True,
            room_mode=True,
            show_tool_calls=False,
        )

    assert outcome.terminal_status == "error"
    assert outcome.rendered_body == _NO_VISIBLE_TEXT_AFTER_THINKING_NOTE
