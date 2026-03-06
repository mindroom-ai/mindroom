"""Test that voice transcriptions from router are processed for commands."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from agno.media import Audio

from mindroom.attachments import _attachment_id_for_event, load_attachment
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.constants import (
    ATTACHMENT_IDS_KEY,
    ORIGINAL_SENDER_KEY,
    ROUTER_AGENT_NAME,
    VOICE_PREFIX,
    VOICE_RAW_AUDIO_FALLBACK_KEY,
)


@pytest.mark.asyncio
async def test_router_processes_own_voice_transcriptions(tmp_path) -> None:  # noqa: ANN001
    """Test that router processes voice transcriptions it sends on behalf of users."""
    # Create a mock router bot
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = Config(
        authorization={"default_room_access": True},
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    # Create mock room and event
    room = MagicMock()
    room.room_id = "!test:example.com"

    # Create event that looks like voice transcription from router
    event = MagicMock()
    event.sender = "@mindroom_router:example.com"  # From router itself
    event.body = "🎤 !schedule daily"  # Voice transcription with command
    event.event_id = "test_event"
    event.source = {"content": {"body": "🎤 !schedule daily", ORIGINAL_SENDER_KEY: "@alice:example.com"}}

    # Mock the command handling and interactive handler
    with (
        patch.object(bot, "_handle_command", new_callable=AsyncMock) as mock_handle,
        patch.object(bot, "client", MagicMock()),
        patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock),
        patch("mindroom.bot.is_dm_room", return_value=False),  # Not a DM room
    ):
        await bot._on_message(room, event)

    # The command should be handled even though it's from router
    mock_handle.assert_called_once()
    command = mock_handle.call_args[0][2]
    assert command.type.value == "schedule"


@pytest.mark.asyncio
async def test_router_ignores_non_voice_self_messages(tmp_path) -> None:  # noqa: ANN001
    """Test that router still ignores its own non-voice messages."""
    # Create a mock router bot
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = MagicMock()

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    # Create mock room and event
    room = MagicMock()
    room.room_id = "!test:example.com"

    # Create event that's a regular message from router (not voice)
    event = MagicMock()
    event.sender = "@mindroom_router:example.com"  # From router itself
    event.body = "Regular message from router"  # Not a voice transcription
    event.event_id = "test_event"
    event.source = {"content": {"body": "Regular message from router"}}

    # Mock the command handling and interactive handler
    with (
        patch.object(bot, "_handle_command", new_callable=AsyncMock) as mock_handle,
        patch.object(bot, "client", MagicMock()),
        patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock),
        patch("mindroom.bot.is_dm_room", return_value=False),  # Not a DM room
    ):
        await bot._on_message(room, event)

    # Should not handle anything - router ignores its own regular messages
    mock_handle.assert_not_called()


@pytest.mark.asyncio
async def test_router_voice_transcription_includes_original_sender_metadata(tmp_path) -> None:  # noqa: ANN001
    """Router should embed the original sender so downstream permissions use user identity."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = Config(
        authorization={"default_room_access": True},
        voice={"enabled": True},
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = MagicMock()
    bot._send_response = AsyncMock(return_value="$response")
    bot._derive_conversation_context = AsyncMock(return_value=(False, "$thread", []))

    room = MagicMock()
    room.room_id = "!test:example.com"

    event = MagicMock()
    event.sender = "@alice:example.com"
    event.event_id = "$voice_event"
    event.body = "voice.ogg"
    event.source = {"content": {"body": "voice.ogg"}}

    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        mock_voice.return_value = "🎤 turn on the lights"
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_voice_message(room, event)

    bot._send_response.assert_called_once()
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert bot._send_response.call_args.kwargs["extra_content"] == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        ATTACHMENT_IDS_KEY: [expected_attachment_id],
    }


@pytest.mark.asyncio
async def test_router_voice_transcription_blocked_by_router_reply_permissions(tmp_path) -> None:  # noqa: ANN001
    """Router should not send transcription when sender is disallowed for router replies."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = Config(
        authorization={
            "default_room_access": True,
            "agent_reply_permissions": {"router": ["@alice:example.com"]},
        },
        voice={"enabled": True},
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = MagicMock()
    bot._send_response = AsyncMock(return_value="$response")

    room = MagicMock()
    room.room_id = "!test:example.com"

    event = MagicMock()
    event.sender = "@bob:example.com"
    event.event_id = "$voice_event"
    event.source = {"content": {"body": "voice.ogg"}}

    with patch("mindroom.bot.is_authorized_sender", return_value=True):
        await bot._on_voice_message(room, event)

    bot._send_response.assert_not_called()
    bot.response_tracker.mark_responded.assert_called_once_with("$voice_event")


@pytest.mark.asyncio
async def test_router_ignores_audio_events_from_internal_agents(tmp_path) -> None:  # noqa: ANN001
    """Router should not transcribe audio files posted by other MindRoom agents."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = Config(
        agents={
            "assistant": {
                "display_name": "Assistant",
            },
        },
        authorization={"default_room_access": True},
        voice={"enabled": True},
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = MagicMock()
    bot._send_response = AsyncMock(return_value="$response")

    room = MagicMock()
    room.room_id = "!test:example.com"

    event = MagicMock()
    event.sender = f"@mindroom_assistant:{config.domain}"
    event.event_id = "$agent_audio_event"
    event.body = "generated_audio.ogg"
    event.source = {"content": {"body": "generated_audio.ogg"}}

    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        await bot._on_voice_message(room, event)

    mock_voice.assert_not_called()
    mock_download_audio.assert_not_called()
    bot._send_response.assert_not_called()
    bot.response_tracker.mark_responded.assert_called_once_with("$agent_audio_event")


@pytest.mark.asyncio
async def test_router_processes_audio_events_from_non_agent_internal_user(tmp_path) -> None:  # noqa: ANN001
    """Router should process voice audio from the internal user account (non-agent)."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = Config(
        authorization={"default_room_access": True},
        voice={"enabled": True},
        mindroom_user={"username": "mindroom_user", "display_name": "MindRoomUser"},
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = MagicMock()
    bot._send_response = AsyncMock(return_value="$response")
    bot._derive_conversation_context = AsyncMock(return_value=(False, "$thread", []))

    room = MagicMock()
    room.room_id = "!test:example.com"

    event = MagicMock()
    event.sender = config.get_mindroom_user_id()
    event.event_id = "$mindroom_user_audio_event"
    event.body = "voice.ogg"
    event.source = {"content": {"body": "voice.ogg"}}

    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        mock_voice.return_value = "🎤 hello from internal user"
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_voice_message(room, event)

    mock_voice.assert_called_once()
    mock_download_audio.assert_called_once()
    bot._send_response.assert_called_once()
    bot.response_tracker.mark_responded.assert_called_once_with("$mindroom_user_audio_event", "$response")


@pytest.mark.asyncio
async def test_router_voice_transcription_falls_back_to_raw_audio(tmp_path) -> None:  # noqa: ANN001
    """Router relays raw audio metadata when transcription is unavailable."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = Config(
        authorization={"default_room_access": True},
        voice={"enabled": True},
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = MagicMock()
    bot._send_response = AsyncMock(return_value="$response")
    bot._derive_conversation_context = AsyncMock(return_value=(False, "$thread", []))

    room = MagicMock()
    room.room_id = "!test:example.com"

    event = MagicMock()
    event.sender = "@alice:example.com"
    event.event_id = "$voice_event"
    event.body = "voice.ogg"
    event.source = {"content": {"body": "voice.ogg"}}

    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        mock_voice.return_value = None
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_voice_message(room, event)

    bot._send_response.assert_called_once()
    assert bot._send_response.call_args.kwargs["response_text"] == f"{VOICE_PREFIX}[Attached voice message]"
    extra_content = bot._send_response.call_args.kwargs["extra_content"]
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert extra_content[ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert extra_content[VOICE_RAW_AUDIO_FALLBACK_KEY] is True
    assert extra_content[ATTACHMENT_IDS_KEY] == [expected_attachment_id]
    attachment = load_attachment(tmp_path, expected_attachment_id)
    assert attachment is not None
    assert attachment.local_path.exists()
    assert attachment.local_path.is_file()


@pytest.mark.asyncio
async def test_router_voice_disabled_still_relays_raw_audio_in_thread(tmp_path) -> None:  # noqa: ANN001
    """Voice-disabled configs should still relay threaded audio as an attachment fallback."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME

    config = Config(
        authorization={"default_room_access": True},
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = MagicMock()
    bot._send_response = AsyncMock(return_value="$response")
    bot._derive_conversation_context = AsyncMock(return_value=(True, "$thread_root", []))

    room = MagicMock()
    room.room_id = "!test:example.com"

    event = MagicMock()
    event.sender = "@alice:example.com"
    event.event_id = "$voice_event"
    event.body = "voice.ogg"
    event.source = {
        "content": {
            "body": "voice.ogg",
            "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
        },
    }

    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_voice_message(room, event)

    mock_voice.assert_not_called()
    mock_download_audio.assert_called_once()
    bot._send_response.assert_called_once()
    call_kwargs = bot._send_response.call_args.kwargs
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["thread_id"] == "$thread_root"
    assert call_kwargs["response_text"] == f"{VOICE_PREFIX}[Attached voice message]"
    assert call_kwargs["extra_content"] == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        VOICE_RAW_AUDIO_FALLBACK_KEY: True,
        ATTACHMENT_IDS_KEY: [expected_attachment_id],
    }
    attachment = load_attachment(tmp_path, expected_attachment_id)
    assert attachment is not None
    assert attachment.local_path.exists()
    assert attachment.local_path.is_file()
    bot.response_tracker.mark_responded.assert_called_once_with("$voice_event", "$response")
