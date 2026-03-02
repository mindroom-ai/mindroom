"""Tests for voice message handling functionality."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Audio

from mindroom import voice_handler
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.voice import VoiceConfig, VoiceLLMConfig, VoiceSTTConfig


class TestVoiceHandler:
    """Test voice message handler functionality."""

    def test_voice_handler_disabled_by_default(self) -> None:
        """Test that voice handler is disabled when not configured."""
        config = Config()
        assert not config.voice.enabled

    def test_voice_handler_enabled_with_config(self) -> None:
        """Test that voice handler is enabled when configured."""
        config = Config(
            voice=VoiceConfig(
                enabled=True,
                stt=VoiceSTTConfig(provider="openai", model="whisper-1"),
                intelligence=VoiceLLMConfig(model="default"),
            ),
        )
        assert config.voice.enabled
        assert config.voice.stt.provider == "openai"
        assert config.voice.stt.model == "whisper-1"
        assert config.voice.intelligence.model == "default"

    @pytest.mark.asyncio
    async def test_voice_handler_ignores_when_disabled(self) -> None:
        """Test that voice handler does nothing when disabled."""
        config = Config()

        # Mock objects
        client = AsyncMock()
        room = MagicMock()
        event = MagicMock()

        # Should return immediately without processing
        await voice_handler.handle_voice_message(client, room, event, config)

        # Verify no processing occurred
        client.download.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_transcription_basic(self) -> None:
        """Test basic transcription processing."""
        from mindroom.config.agent import AgentConfig, TeamConfig  # noqa: PLC0415

        config = Config(
            voice=VoiceConfig(enabled=True),
            agents={
                "research": AgentConfig(display_name="ResearchAgent", role="Research agent"),
                "code": AgentConfig(display_name="CodeAgent", role="Code agent"),
            },
            teams={
                "dev_team": TeamConfig(
                    display_name="Development Team",
                    role="Dev team",
                    agents=["code"],
                ),
            },
        )

        # Mock the AI model
        with patch("mindroom.voice_handler._process_transcription") as mock_process:
            mock_process.return_value = "@research help me with this"

            result = await voice_handler._process_transcription("research help me with this", config)
            assert "@research" in result

    @pytest.mark.asyncio
    async def test_voice_handler_uses_room_scoped_entities_for_transcription(self) -> None:
        """Test voice transcription prompt is scoped to entities present in the room."""
        config = Config(
            voice=VoiceConfig(enabled=True),
            agents={
                "openclaw": AgentConfig(display_name="OpenClaw Agent", role="OpenClaw role"),
                "code": AgentConfig(display_name="Code Agent", role="Coding role"),
            },
        )

        client = AsyncMock()
        room = MagicMock(spec=nio.MatrixRoom)
        room.users = {
            f"@mindroom_openclaw:{config.domain}": MagicMock(),
            f"@mindroom_router:{config.domain}": MagicMock(),
            "@alice:example.com": MagicMock(),
        }
        event = MagicMock(spec=nio.RoomMessageAudio)
        event.sender = "@alice:example.com"

        with (
            patch(
                "mindroom.voice_handler.download_audio",
                new=AsyncMock(return_value=Audio(content=b"audio", mime_type="audio/ogg")),
            ),
            patch("mindroom.voice_handler._transcribe_audio", return_value="help me"),
            patch("mindroom.voice_handler._process_transcription", new_callable=AsyncMock) as mock_process,
        ):
            mock_process.return_value = "@openclaw help me"
            result = await voice_handler.handle_voice_message(client, room, event, config)

        assert result == "🎤 @openclaw help me"
        assert mock_process.await_count == 1
        assert mock_process.await_args.kwargs["available_agent_names"] == ["openclaw"]
        assert mock_process.await_args.kwargs["available_team_names"] == []

    @pytest.mark.asyncio
    async def test_download_audio_unencrypted(self) -> None:
        """Test downloading unencrypted audio messages."""
        Config(voice=VoiceConfig(enabled=True))  # Just to verify it works, not used in test

        # Mock client and event
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomMessageAudio)

        with (
            patch(
                "mindroom.voice_handler.download_media_bytes",
                new=AsyncMock(return_value=b"audio_data"),
            ) as mock_download,
            patch("mindroom.voice_handler.media_mime_type", return_value="audio/ogg"),
        ):
            result = await voice_handler.download_audio(client, event)

        assert result is not None
        assert result.content == b"audio_data"
        assert result.mime_type == "audio/ogg"
        mock_download.assert_awaited_once_with(client, event)

    @pytest.mark.asyncio
    async def test_download_audio_encrypted(self) -> None:
        """Test downloading and decrypting encrypted audio messages."""
        Config(voice=VoiceConfig(enabled=True))  # Just to verify it works, not used in test

        # Mock client and encrypted event
        client = AsyncMock()
        event = MagicMock(spec=nio.RoomEncryptedAudio)

        with (
            patch(
                "mindroom.voice_handler.download_media_bytes",
                new=AsyncMock(return_value=b"decrypted_audio_data"),
            ) as mock_download,
            patch("mindroom.voice_handler.media_mime_type", return_value="audio/mpeg"),
        ):
            result = await voice_handler.download_audio(client, event)

        assert result is not None
        assert result.content == b"decrypted_audio_data"
        assert result.mime_type == "audio/mpeg"
        mock_download.assert_awaited_once_with(client, event)
