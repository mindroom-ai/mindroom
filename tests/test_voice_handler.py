"""Tests for voice message handling functionality."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Audio

from mindroom import voice_handler
from mindroom.config.agent import AgentConfig
from mindroom.config.main import Config
from mindroom.config.voice import VoiceConfig, _VoiceLLMConfig, _VoiceSTTConfig
from mindroom.constants import ATTACHMENT_IDS_KEY
from tests.conftest import bind_runtime_paths, runtime_paths_for, test_runtime_paths
from tests.identity_helpers import persist_actual_entity_accounts

TEST_VOICE_ACCOUNT_PASSWORD = "pw"  # noqa: S105


def _runtime_bound_config(config: Config) -> Config:
    """Return a runtime-bound config for voice handler tests."""
    bound = bind_runtime_paths(config, test_runtime_paths(Path(tempfile.mkdtemp())))
    _persist_voice_handler_accounts(bound)
    return bound


def _persist_voice_handler_accounts(config: Config) -> None:
    runtime_paths = runtime_paths_for(config)
    persist_actual_entity_accounts(config, runtime_paths, password=TEST_VOICE_ACCOUNT_PASSWORD)


def _matrix_room(
    room_id: str,
    *,
    members: tuple[str, ...] = (),
    members_synced: bool = True,
) -> nio.MatrixRoom:
    room = nio.MatrixRoom(room_id=room_id, own_user_id="@mindroom_router:localhost")
    for member_id in members:
        room.add_member(member_id, None, None)
    room.members_synced = members_synced
    return room


async def _handle_voice_message(
    client: nio.AsyncClient,
    room: nio.MatrixRoom,
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
    *,
    audio: Audio | None = None,
) -> str | None:
    """Run voice handling with the explicit runtime bound to the test config."""
    return await voice_handler._handle_voice_message(
        client,
        room,
        event,
        config,
        runtime_paths_for(config),
        audio=audio,
    )


async def _process_transcription(transcription: str, config: Config, **kwargs: object) -> str:
    """Run transcription processing with the explicit runtime bound to the test config."""
    return await voice_handler._process_transcription(
        transcription,
        config,
        runtime_paths_for(config),
        **kwargs,
    )


async def _prepare_voice_message(
    client: nio.AsyncClient,
    storage_path: Path,
    room: nio.MatrixRoom,
    event: nio.RoomMessageAudio | nio.RoomEncryptedAudio,
    config: Config,
    *,
    thread_id: str | None,
) -> voice_handler._PreparedVoiceMessage | None:
    """Prepare one voice message with the explicit runtime bound to the test config."""
    return await voice_handler.prepare_voice_message(
        client,
        storage_path,
        room,
        event,
        config,
        runtime_paths=runtime_paths_for(config),
        thread_id=thread_id,
    )


class TestVoiceHandler:
    """Test voice message handler functionality."""

    def test_voice_handler_disabled_by_default(self) -> None:
        """Test that voice handler is disabled when not configured."""
        config = Config()
        assert not config.voice.enabled
        assert config.voice.visible_router_echo

    def test_voice_handler_enabled_with_config(self) -> None:
        """Test that voice handler is enabled when configured."""
        config = _runtime_bound_config(
            Config(
                voice=VoiceConfig(
                    enabled=True,
                    stt=_VoiceSTTConfig(provider="openai", model="whisper-1"),
                    intelligence=_VoiceLLMConfig(model="default"),
                ),
            ),
        )
        assert config.voice.enabled
        assert config.voice.stt.provider == "openai"
        assert config.voice.stt.model == "whisper-1"
        assert config.voice.intelligence.model == "default"

    def test_sanitize_unavailable_mentions_uses_exact_aliases(self) -> None:
        """Voice mention sanitizing should match exact Matrix mention aliases."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "foo": AgentConfig(display_name="Foo"),
                    "mindroom_foo": AgentConfig(display_name="Prefixed Foo"),
                },
            ),
        )

        sanitized = voice_handler._sanitize_unavailable_mentions(
            "@mindroom_foo help @mindroom_mindroom_foo",
            allowed_entities={"foo"},
            config=config,
            runtime_paths=runtime_paths_for(config),
        )

        assert sanitized == "mindroom_foo help @mindroom_mindroom_foo"

    @pytest.mark.asyncio
    async def test_voice_handler_ignores_when_disabled(self) -> None:
        """Test that voice handler does nothing when disabled."""
        config = _runtime_bound_config(Config())

        # Mock objects
        client = AsyncMock()
        room = MagicMock()
        event = MagicMock()

        # Should return immediately without processing
        await _handle_voice_message(client, room, event, config)

        # Verify no processing occurred
        client.download.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_transcription_basic(self) -> None:
        """Test basic transcription processing."""
        from mindroom.config.agent import AgentConfig, TeamConfig  # noqa: PLC0415

        config = _runtime_bound_config(
            Config(
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
            ),
        )

        # Mock the AI model
        with patch("mindroom.voice_handler._process_transcription") as mock_process:
            mock_process.return_value = "@research help me with this"

            result = await _process_transcription("research help me with this", config)
            assert "@research" in result

    @pytest.mark.asyncio
    async def test_voice_handler_uses_room_scoped_entities_for_transcription(self) -> None:
        """Test voice transcription prompt is scoped to entities present in the room."""
        config = _runtime_bound_config(
            Config(
                voice=VoiceConfig(enabled=True),
                agents={
                    "openclaw": AgentConfig(display_name="OpenClaw Agent", role="OpenClaw role"),
                    "code": AgentConfig(display_name="Code Agent", role="Coding role"),
                },
            ),
        )

        client = AsyncMock()
        domain = config.get_domain(runtime_paths_for(config))
        room = _matrix_room(
            "!voice:localhost",
            members=(
                f"@actual_openclaw:{domain}",
                f"@actual_router:{domain}",
                "@alice:example.com",
            ),
        )
        event = MagicMock(spec=nio.RoomMessageAudio)
        event.event_id = "$voice"
        event.sender = "@alice:example.com"
        event.body = "voice.ogg"
        event.source = {"content": {"body": "voice.ogg"}}

        with (
            patch(
                "mindroom.voice_handler._download_audio",
                new=AsyncMock(return_value=Audio(content=b"audio", mime_type="audio/ogg")),
            ),
            patch("mindroom.voice_handler._transcribe_audio", return_value="help me"),
            patch("mindroom.voice_handler._process_transcription", new_callable=AsyncMock) as mock_process,
        ):
            mock_process.return_value = "@openclaw help me"
            result = await _handle_voice_message(client, room, event, config)

        assert result == "🎤 @openclaw help me"
        assert mock_process.await_count == 1
        assert mock_process.await_args.kwargs["available_agent_names"] == ["openclaw"]
        assert mock_process.await_args.kwargs["available_team_names"] == []

    @pytest.mark.asyncio
    async def test_download_audio_unencrypted(self) -> None:
        """Test downloading unencrypted audio messages."""
        _runtime_bound_config(Config(voice=VoiceConfig(enabled=True)))  # Just to verify it works, not used in test

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
            result = await voice_handler._download_audio(client, event)

        assert result is not None
        assert result.content == b"audio_data"
        assert result.mime_type == "audio/ogg"
        mock_download.assert_awaited_once_with(client, event)

    @pytest.mark.asyncio
    async def test_download_audio_encrypted(self) -> None:
        """Test downloading and decrypting encrypted audio messages."""
        _runtime_bound_config(Config(voice=VoiceConfig(enabled=True)))  # Just to verify it works, not used in test

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
            result = await voice_handler._download_audio(client, event)

        assert result is not None
        assert result.content == b"decrypted_audio_data"
        assert result.mime_type == "audio/mpeg"
        mock_download.assert_awaited_once_with(client, event)

    @pytest.mark.asyncio
    async def test_prepare_voice_message_clears_inflight_task_after_failed_download(self, tmp_path: Path) -> None:
        """Failed normalization should not leave stale in-flight task entries behind."""
        config = _runtime_bound_config(Config(authorization={"default_room_access": True}))
        client = AsyncMock()
        room = _matrix_room("!test:server", members=("@alice:example.com",))
        event = MagicMock(spec=nio.RoomMessageAudio)
        event.event_id = "$voice123"
        event.sender = "@alice:example.com"
        event.body = "voice.ogg"
        event.source = {"content": {"body": "voice.ogg"}}

        voice_handler._voice_normalization_cache.clear()
        voice_handler._voice_normalization_tasks.clear()
        cache_key = voice_handler._voice_cache_key(tmp_path, room.room_id, event.event_id, None)

        with patch("mindroom.voice_handler._download_audio", new=AsyncMock(return_value=None)):
            prepared = await _prepare_voice_message(
                client,
                tmp_path,
                room,
                event,
                config,
                thread_id=None,
            )

        assert prepared is None
        assert cache_key not in voice_handler._voice_normalization_tasks
        assert cache_key not in voice_handler._voice_normalization_cache

    @pytest.mark.asyncio
    async def test_prepare_voice_message_cancellation_does_not_cancel_shared_normalization(
        self,
        tmp_path: Path,
    ) -> None:
        """Canceling one waiter should not cancel the shared normalization task for others."""
        config = _runtime_bound_config(Config(authorization={"default_room_access": True}))
        client = AsyncMock()
        room = _matrix_room("!test:server", members=("@alice:example.com",))
        event = MagicMock(spec=nio.RoomMessageAudio)
        event.event_id = "$voice123"
        event.sender = "@alice:example.com"
        event.body = "voice.ogg"
        event.source = {"content": {"body": "voice.ogg"}}

        voice_handler._voice_normalization_cache.clear()
        voice_handler._voice_normalization_tasks.clear()
        cache_key = voice_handler._voice_cache_key(tmp_path, room.room_id, event.event_id, None)
        started = asyncio.Event()
        release = asyncio.Event()
        compute_calls = 0

        async def fake_compute(
            *_args: object,
            **_kwargs: object,
        ) -> voice_handler._NormalizedVoiceMessage:
            nonlocal compute_calls
            compute_calls += 1
            started.set()
            await release.wait()
            return voice_handler._NormalizedVoiceMessage(
                attachment_id="att-123",
                transcribed_message="🎤 hello",
            )

        with patch("mindroom.voice_handler._compute_normalized_voice_message", side_effect=fake_compute):
            first_waiter = asyncio.create_task(
                _prepare_voice_message(
                    client,
                    tmp_path,
                    room,
                    event,
                    config,
                    thread_id=None,
                ),
            )
            await started.wait()

            second_waiter = asyncio.create_task(
                _prepare_voice_message(
                    client,
                    tmp_path,
                    room,
                    event,
                    config,
                    thread_id=None,
                ),
            )
            await asyncio.sleep(0)

            first_waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first_waiter

            release.set()
            prepared = await second_waiter

        assert prepared is not None
        assert prepared.text == "🎤 hello"
        assert prepared.source["content"][ATTACHMENT_IDS_KEY] == ["att-123"]
        assert compute_calls == 1
        assert cache_key in voice_handler._voice_normalization_cache
        assert cache_key not in voice_handler._voice_normalization_tasks
