"""Test that voice handler creates threads properly."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config import Config
from mindroom.voice_handler import handle_voice_message


@pytest.mark.asyncio
async def test_voice_handler_creates_thread_from_voice_message() -> None:
    """Test that voice handler creates a thread from the voice message itself.

    The flow should be:
    1. User sends voice message
    2. Voice handler transcribes it
    3. Voice handler sends transcription in a THREAD starting from the voice message
    """
    # Mock client
    client = AsyncMock()
    client.download = AsyncMock()

    # Mock room
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"

    # Mock voice message event
    voice_event = MagicMock(spec=nio.RoomMessageAudio)
    voice_event.event_id = "$voice123"
    voice_event.sender = "@user:example.com"
    voice_event.url = "mxc://example.com/audio"

    # Mock config
    config = Config.from_yaml()

    # Mock audio download
    mock_response = MagicMock()
    mock_response.body = b"fake audio data"
    client.download.return_value = mock_response

    # Mock transcription, AI processing, and send_message
    with (
        patch("mindroom.voice_handler._transcribe_audio", return_value="what is the weather today"),
        patch("mindroom.voice_handler._process_transcription", return_value="what is the weather today"),
        patch("mindroom.voice_handler.send_message") as mock_send,
    ):
        await handle_voice_message(client, room, voice_event, config)

        # Verify send_message was called
        mock_send.assert_called_once()

        # Get the content that was sent
        call_args = mock_send.call_args
        content = call_args[0][2]  # Third argument is the content

        # Check that the message has the voice prefix
        assert content["body"].startswith("🎤")

        # Check that it's a reply to the voice message
        assert "m.relates_to" in content
        relates_to = content["m.relates_to"]
        assert "m.in_reply_to" in relates_to
        assert relates_to["m.in_reply_to"]["event_id"] == "$voice123"

        # MOST IMPORTANT: Check that it creates a thread from the voice message
        assert "rel_type" in relates_to
        assert relates_to["rel_type"] == "m.thread"
        assert relates_to["event_id"] == "$voice123"  # Thread root is the voice message
