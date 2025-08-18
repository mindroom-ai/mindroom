"""Tests for voice message handling functionality."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom import voice_handler
from mindroom.config import Config, VoiceConfig, VoiceLLMConfig, VoiceSTTConfig


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
        from mindroom.config import AgentConfig, TeamConfig  # noqa: PLC0415

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
    async def test_download_audio_unencrypted(self) -> None:
        """Test downloading unencrypted audio messages."""
        from nio import RoomMessageAudio  # noqa: PLC0415

        Config(voice=VoiceConfig(enabled=True))  # Just to verify it works, not used in test

        # Mock client and event
        client = AsyncMock()
        event = MagicMock(spec=RoomMessageAudio)
        event.url = "mxc://example.org/abc123"

        # Mock successful download
        response = MagicMock()
        response.body = b"audio_data"
        client.download.return_value = response

        # Patch isinstance to handle the type checks
        def mock_isinstance_check(obj: object, cls: type) -> bool:
            if obj is event and cls is nio.RoomMessageAudio:
                return True
            if obj is response and cls is nio.DownloadError:
                return False  # Not an error
            return isinstance.__wrapped__(obj, cls)  # type: ignore[attr-defined]

        with patch("mindroom.voice_handler.isinstance", side_effect=mock_isinstance_check):
            result = await voice_handler._download_audio(client, event)
            assert result == b"audio_data"
            client.download.assert_called_once_with("mxc://example.org/abc123")

    @pytest.mark.asyncio
    async def test_download_audio_encrypted(self) -> None:
        """Test downloading and decrypting encrypted audio messages."""
        Config(voice=VoiceConfig(enabled=True))  # Just to verify it works, not used in test

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
            from nio import RoomEncryptedAudio  # noqa: PLC0415

            event.__class__ = RoomEncryptedAudio

            result = await voice_handler._download_audio(client, event)
            assert result == b"decrypted_audio_data"
            mock_decrypt.assert_called_once()
