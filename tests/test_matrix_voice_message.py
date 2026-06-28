"""Tests for the Matrix voice message tool."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config.main import Config
from mindroom.custom_tools.matrix_voice_message import MatrixVoiceMessageTools
from mindroom.matrix.client_delivery import DeliveredMatrixEvent
from mindroom.tool_system.runtime_context import ToolRuntimeContext, tool_runtime_context
from tests.conftest import (
    bind_runtime_paths,
    make_conversation_cache_mock,
    make_event_cache_mock,
    runtime_paths_for,
    test_runtime_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

    from agno.tools.function import Function


def _payload(raw: str) -> dict[str, object]:
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


def _matrix_voice_message_function() -> Function:
    tools = MatrixVoiceMessageTools(api_key="sk-test")
    function = tools.async_functions["matrix_voice_message"]
    function.process_entrypoint(strict=False)
    return function


def _mock_client() -> AsyncMock:
    client = AsyncMock(spec=nio.AsyncClient)
    client.user_id = "@mindroom_general:localhost"
    room = MagicMock()
    room.encrypted = False
    client.rooms = {"!room:localhost": room}
    return client


def _context(
    tmp_path: Path,
    *,
    room_id: str = "!room:localhost",
    thread_id: str | None = "$thread-root",
) -> ToolRuntimeContext:
    config = bind_runtime_paths(Config(), test_runtime_paths(tmp_path))
    conversation_cache = make_conversation_cache_mock()
    conversation_cache.get_latest_thread_event_id_if_needed = AsyncMock(return_value="$latest")
    conversation_cache.notify_outbound_message = MagicMock()
    return ToolRuntimeContext(
        agent_name="general",
        room_id=room_id,
        thread_id=thread_id,
        resolved_thread_id=thread_id,
        requester_id="@user:localhost",
        client=_mock_client(),
        config=config,
        runtime_paths=runtime_paths_for(config),
        conversation_cache=conversation_cache,
        event_cache=make_event_cache_mock(),
    )


@pytest.mark.asyncio
async def test_matrix_voice_message_requires_runtime_context() -> None:
    """Tool calls outside Matrix runtime should return a structured error."""
    result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message("hello")

    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["tool"] == "matrix_voice_message"
    assert payload["message"] == "Matrix voice message tool context is unavailable in this runtime path."


@pytest.mark.asyncio
async def test_matrix_voice_message_rejects_empty_text(tmp_path: Path) -> None:
    """Spoken text is required."""
    with tool_runtime_context(_context(tmp_path)):
        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message("  ")

    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["message"] == "text is required and must be non-empty."


@pytest.mark.asyncio
async def test_matrix_voice_message_rejects_unauthorized_room(tmp_path: Path) -> None:
    """Target rooms should use the shared Matrix room authorization check."""
    with (
        tool_runtime_context(_context(tmp_path)),
        patch("mindroom.custom_tools.matrix_voice_message.room_access_allowed", return_value=False),
    ):
        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message(
            "hello",
            room_id="!other:localhost",
        )

    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["room_id"] == "!other:localhost"
    assert payload["message"] == "Not authorized to access the target room."


@pytest.mark.asyncio
async def test_matrix_voice_message_generates_speech_and_sends_to_context_thread(tmp_path: Path) -> None:
    """Successful calls should synthesize speech and send it to the active Matrix thread."""
    context = _context(tmp_path, thread_id="$thread-root")
    speech_response = SimpleNamespace(content=b"voice-bytes")

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as mock_openai,
        patch("mindroom.custom_tools.matrix_voice_message.send_audio_message", new_callable=AsyncMock) as mock_send,
    ):
        mock_openai.return_value.audio.speech.create.return_value = speech_response
        mock_send.return_value = "$voice-event"

        result = await MatrixVoiceMessageTools(
            api_key="sk-test",
            model="tts-test",
            voice="nova",
            response_format="mp3",
        ).matrix_voice_message("Read this aloud", caption="Voice reply")

    mock_openai.assert_called_once_with(api_key="sk-test")
    mock_openai.return_value.audio.speech.create.assert_called_once_with(
        model="tts-test",
        voice="nova",
        input="Read this aloud",
        response_format="mp3",
    )
    context.conversation_cache.get_latest_thread_event_id_if_needed.assert_awaited_once_with(
        "!room:localhost",
        "$thread-root",
        caller_label="matrix_voice_message_tool",
    )
    mock_send.assert_awaited_once_with(
        context.client,
        "!room:localhost",
        b"voice-bytes",
        config=context.config,
        mimetype="audio/mpeg",
        filename="voice-message.mp3",
        caption="Voice reply",
        thread_id="$thread-root",
        latest_thread_event_id="$latest",
        conversation_cache=context.conversation_cache,
    )

    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["event_id"] == "$voice-event"
    assert payload["room_id"] == "!room:localhost"
    assert payload["thread_id"] == "$thread-root"


@pytest.mark.asyncio
async def test_matrix_voice_message_room_sentinel_forces_room_level_send(tmp_path: Path) -> None:
    """thread_id='room' should not inherit the active thread."""
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as mock_openai,
        patch("mindroom.custom_tools.matrix_voice_message.send_audio_message", new_callable=AsyncMock) as mock_send,
    ):
        mock_openai.return_value.audio.speech.create.return_value = SimpleNamespace(content=b"voice-bytes")
        mock_send.return_value = "$voice-event"

        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message(
            "room-level",
            thread_id="room",
        )

    context.conversation_cache.get_latest_thread_event_id_if_needed.assert_not_awaited()
    assert mock_send.await_args.kwargs["thread_id"] is None
    assert mock_send.await_args.kwargs["latest_thread_event_id"] is None

    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["thread_id"] is None


@pytest.mark.asyncio
async def test_matrix_voice_message_companion_message_sends_to_same_thread(tmp_path: Path) -> None:
    """Optional companion text should use the same target before voice delivery."""
    context = _context(tmp_path, thread_id="$thread-root")
    sent_text_content: dict[str, object] | None = None

    async def capture_text_send(
        _client: object,
        _room_id: str,
        content: dict[str, object],
        *,
        config: Config,
    ) -> DeliveredMatrixEvent:
        nonlocal sent_text_content
        assert isinstance(config, Config)
        sent_text_content = content
        return DeliveredMatrixEvent(event_id="$companion-event", content_sent=content)

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as mock_openai,
        patch(
            "mindroom.custom_tools.matrix_conversation_operations.send_message_result",
            side_effect=capture_text_send,
        ),
        patch("mindroom.custom_tools.matrix_voice_message.send_audio_message", new_callable=AsyncMock) as mock_send,
    ):
        mock_openai.return_value.audio.speech.create.return_value = SimpleNamespace(content=b"voice-bytes")
        mock_send.return_value = "$voice-event"

        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message(
            "spoken version",
            caption="Audio body",
            companion_message="Readable transcript",
        )

    assert sent_text_content is not None
    assert sent_text_content["body"] == "Readable transcript"
    assert sent_text_content["m.relates_to"] == {
        "rel_type": "m.thread",
        "event_id": "$thread-root",
        "is_falling_back": True,
        "m.in_reply_to": {"event_id": "$latest"},
    }
    assert sent_text_content["com.mindroom.skip_mentions"] is True
    mock_send.assert_awaited_once()
    assert mock_send.await_args.kwargs["caption"] == "Audio body"
    assert mock_send.await_args.kwargs["thread_id"] == "$thread-root"

    payload = _payload(result)
    assert payload["status"] == "ok"
    assert payload["event_id"] == "$voice-event"
    assert payload["companion_event_id"] == "$companion-event"


@pytest.mark.asyncio
async def test_matrix_voice_message_voice_failure_reports_sent_companion(tmp_path: Path) -> None:
    """A failed voice send should report that the companion text was already delivered."""
    context = _context(tmp_path, thread_id="$thread-root")

    async def text_send(
        _client: object,
        _room_id: str,
        content: dict[str, object],
        *,
        config: Config,
    ) -> DeliveredMatrixEvent:
        assert isinstance(config, Config)
        return DeliveredMatrixEvent(event_id="$companion-event", content_sent=content)

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as mock_openai,
        patch("mindroom.custom_tools.matrix_conversation_operations.send_message_result", side_effect=text_send),
        patch("mindroom.custom_tools.matrix_voice_message.send_audio_message", new_callable=AsyncMock) as mock_send,
    ):
        mock_openai.return_value.audio.speech.create.return_value = SimpleNamespace(content=b"voice-bytes")
        mock_send.return_value = None

        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message(
            "spoken version",
            companion_message="Readable transcript",
        )

    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["companion_event_id"] == "$companion-event"
    assert payload["message"] == "Failed to send voice message to Matrix."


@pytest.mark.asyncio
async def test_matrix_voice_message_generation_error_is_sanitized(tmp_path: Path) -> None:
    """Provider errors should not leak raw API failure text into Matrix."""
    context = _context(tmp_path, thread_id="$thread-root")

    with (
        tool_runtime_context(context),
        patch("mindroom.custom_tools.matrix_voice_message.OpenAI") as mock_openai,
        patch("mindroom.custom_tools.matrix_voice_message.send_audio_message", new_callable=AsyncMock) as mock_send,
    ):
        mock_openai.return_value.audio.speech.create.side_effect = RuntimeError("bad key sk-secret")

        result = await MatrixVoiceMessageTools(api_key="sk-test").matrix_voice_message("hello")

    mock_send.assert_not_awaited()
    payload = _payload(result)
    assert payload["status"] == "error"
    assert payload["message"] == "Failed to generate speech."
    assert "sk-secret" not in result


def test_matrix_voice_message_description_covers_critical_behavior() -> None:
    """Processed description should explain the key Matrix voice behavior."""
    function = _matrix_voice_message_function()
    description = function.description

    assert description is not None
    assert "send a Matrix voice message" in description
    assert "OpenAI text-to-speech" in description
    assert 'thread_id="room"' in description
    assert "companion_message" in description


def test_matrix_voice_message_parameter_descriptions_are_exposed() -> None:
    """Docstring Args should populate the tool parameter schema."""
    function = _matrix_voice_message_function()
    properties = function.parameters["properties"]

    assert "spoken content" in properties["text"]["description"]
    assert "Matrix event body" in properties["caption"]["description"]
    assert "normal text message" in properties["companion_message"]["description"]
    assert 'thread_id="room"' in properties["thread_id"]["description"]
