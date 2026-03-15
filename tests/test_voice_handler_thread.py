"""Test that voice handler creates threads properly."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import voice_handler
from mindroom.config.main import Config
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths


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
    room = MagicMock()
    room.room_id = "!test:server"
    room.users = {}

    # Mock voice message event
    voice_event = MagicMock(spec=nio.RoomMessageAudio)
    voice_event.event_id = "$voice123"
    voice_event.sender = "@user:example.com"
    voice_event.url = "mxc://example.com/audio"
    voice_event.source = {"content": {}}

    # Mock config
    config = bind_runtime_paths(
        Config(),
        test_runtime_paths(Path(tempfile.mkdtemp())),
    )
    config.voice.enabled = True

    # Mock audio download
    mock_response = MagicMock()
    mock_response.body = b"fake audio data"
    client.download.return_value = mock_response

    # Mock transcription and AI processing
    with (
        patch("mindroom.voice_handler._transcribe_audio", return_value="what is the weather today"),
        patch("mindroom.voice_handler._process_transcription", return_value="what is the weather today"),
    ):
        result = await voice_handler._handle_voice_message(client, room, voice_event, config, runtime_paths_for(config))

        # Verify the handler returns the transcribed message with voice prefix
        assert result == "🎤 what is the weather today"
