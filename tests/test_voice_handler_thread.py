"""Test that voice handler creates threads properly."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config import Config
from mindroom.voice_handler import handle_voice_message


@pytest.mark.asyncio
async def test_voice_handler_returns_transcription() -> None:
    """Test that voice handler returns the transcribed message.

    The flow should be:
    1. User sends voice message
    2. Voice handler transcribes it
    3. Voice handler returns the transcription with voice prefix
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

    # Mock transcription and AI processing
    with (
        patch("mindroom.voice_handler._transcribe_audio", return_value="what is the weather today"),
        patch("mindroom.voice_handler._process_transcription", return_value="what is the weather today"),
    ):
        result = await handle_voice_message(client, room, voice_event, config)

        # Verify the handler returns the transcribed message with voice prefix
        assert result == "🎤 what is the weather today"
