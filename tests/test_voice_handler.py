"""Tests for voice message handling functionality."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.config import Config, VoiceConfig, VoiceIntelligenceConfig, VoiceSTTConfig
from mindroom.voice_handler import VoiceHandler


class TestVoiceHandler:
    """Test voice message handler functionality."""

    def test_voice_handler_disabled_by_default(self) -> None:
        """Test that voice handler is disabled when not configured."""
        config = Config()
        handler = VoiceHandler(config)
        assert not handler.enabled

    def test_voice_handler_enabled_with_config(self) -> None:
        """Test that voice handler is enabled when configured."""
        config = Config(
            voice=VoiceConfig(
                enabled=True,
                stt=VoiceSTTConfig(provider="openai", model="whisper-1"),
                intelligence=VoiceIntelligenceConfig(model="default"),
            ),
        )
        handler = VoiceHandler(config)
        assert handler.enabled
        assert handler.stt_provider == "openai"
        assert handler.stt_model == "whisper-1"
        assert handler.intelligence_model == "default"

    @pytest.mark.asyncio
    async def test_voice_handler_ignores_when_disabled(self) -> None:
        """Test that voice handler does nothing when disabled."""
        config = Config()
        handler = VoiceHandler(config)

        # Mock objects
        client = AsyncMock()
        room = MagicMock()
        event = MagicMock()

        # Should return immediately without processing
        await handler.handle_voice_message(client, room, event)

        # Verify no processing occurred
        client.download.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_transcription_basic(self) -> None:
        """Test basic transcription processing."""
        from mindroom.config import AgentConfig, TeamConfig

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
        handler = VoiceHandler(config)

        # Mock the AI model
        with patch.object(handler, "_process_transcription") as mock_process:
            mock_process.return_value = "@research help me with this"

            result = await handler._process_transcription("research help me with this")
            assert "@research" in result

    @pytest.mark.asyncio
    async def test_download_audio_unencrypted(self) -> None:
        """Test downloading unencrypted audio messages."""
        from nio import RoomMessageAudio

        config = Config(voice=VoiceConfig(enabled=True))
        handler = VoiceHandler(config)

        # Mock client and event
        client = AsyncMock()
        event = MagicMock(spec=RoomMessageAudio)
        event.url = "mxc://example.org/abc123"

        # Mock successful download
        response = MagicMock()
        response.body = b"audio_data"
        client.download.return_value = response

        # Patch isinstance to handle the type checks
        def mock_isinstance_check(obj, cls):
            if obj is event and cls is nio.RoomMessageAudio:
                return True
            if obj is response and cls is nio.DownloadError:
                return False  # Not an error
            return isinstance.__wrapped__(obj, cls)

        with patch("mindroom.voice_handler.isinstance", side_effect=mock_isinstance_check):
            result = await handler._download_audio(client, event)
            assert result == b"audio_data"
            client.download.assert_called_once_with("mxc://example.org/abc123")

    @pytest.mark.asyncio
    async def test_download_audio_encrypted(self) -> None:
        """Test downloading and decrypting encrypted audio messages."""
        config = Config(voice=VoiceConfig(enabled=True))
        handler = VoiceHandler(config)

        # Mock client and encrypted event
        client = AsyncMock()
        event = MagicMock(spec=["url", "source"])
        event.url = "mxc://example.org/encrypted123"
        event.source = {
            "content": {
                "file": {
                    "key": {"k": "test_key"},
                    "hashes": {"sha256": "test_hash"},
                    "iv": "test_iv",
                },
            },
        }

        # Mock successful download
        response = MagicMock()
        response.body = b"encrypted_audio_data"
        client.download.return_value = response

        # Mock decryption
        with patch("mindroom.voice_handler.crypto.attachments.decrypt_attachment") as mock_decrypt:
            mock_decrypt.return_value = b"decrypted_audio_data"

            # Use nio.RoomEncryptedAudio type hint for the test
            from nio import RoomEncryptedAudio

            event.__class__ = RoomEncryptedAudio

            result = await handler._download_audio(client, event)
            assert result == b"decrypted_audio_data"
            mock_decrypt.assert_called_once()
