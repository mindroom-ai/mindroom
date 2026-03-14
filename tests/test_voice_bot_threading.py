"""Test that direct audio responses preserve thread structure."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from agno.media import Audio

from mindroom.attachments import _attachment_id_for_event, load_attachment
from mindroom.bot import AgentBot
from mindroom.config.main import Config
from mindroom.matrix.users import AgentMatrixUser
from tests.conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD, bind_runtime_paths


@pytest.fixture
def mock_home_bot() -> AgentBot:
    """Create a single-agent bot for audio threading tests."""
    agent_user = AgentMatrixUser(
        agent_name="home",
        user_id="@mindroom_home:localhost",
        display_name="HomeAssistant",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )
    config = Config(
        agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:server"]}},
        authorization={"default_room_access": True},
    )
    config = bind_runtime_paths(config)
    with tempfile.TemporaryDirectory() as tmpdir:
        bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    bot.logger = MagicMock()
    bot._generate_response = AsyncMock(return_value="$response")
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    return bot


def _make_voice_event(
    *,
    event_id: str,
    source: dict,
) -> nio.RoomMessageAudio:
    voice_event = MagicMock(spec=nio.RoomMessageAudio)
    voice_event.event_id = event_id
    voice_event.sender = "@user:example.com"
    voice_event.body = "voice.ogg"
    voice_event.source = source
    return voice_event


@pytest.mark.asyncio
async def test_voice_message_in_main_room_creates_thread(mock_home_bot: AgentBot) -> None:
    """Audio in the main room should reply in a thread rooted at the audio event."""
    bot = mock_home_bot
    bot._derive_conversation_context = AsyncMock(return_value=(True, "$voice123", []))

    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }

    voice_event = _make_voice_event(event_id="$voice123", source={"content": {}})

    with (
        patch("mindroom.bot.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler._handle_voice_message", return_value="🎤 what is the weather"),
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_media_message(room, voice_event)

    bot._generate_response.assert_called_once()
    call_kwargs = bot._generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice123"
    assert call_kwargs["thread_id"] == "$voice123"
    assert call_kwargs["prompt"].startswith("🎤 what is the weather")


@pytest.mark.asyncio
async def test_voice_message_in_thread_continues_thread(mock_home_bot: AgentBot) -> None:
    """Audio in an existing thread should keep using that thread root."""
    bot = mock_home_bot
    bot._derive_conversation_context = AsyncMock(return_value=(True, "$thread_root", []))

    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }

    voice_event = _make_voice_event(
        event_id="$voice456",
        source={
            "content": {
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )

    with (
        patch("mindroom.bot.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler._handle_voice_message", return_value="🎤 show me the forecast"),
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_media_message(room, voice_event)

    bot._generate_response.assert_called_once()
    call_kwargs = bot._generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice456"
    assert call_kwargs["thread_id"] == "$thread_root"
    assert call_kwargs["prompt"].startswith("🎤 show me the forecast")
    attachment = load_attachment(bot.storage_path, _attachment_id_for_event("$voice456"))
    assert attachment is not None
    assert attachment.thread_id == "$thread_root"


@pytest.mark.asyncio
async def test_voice_plain_reply_to_thread_message_uses_thread_root(mock_home_bot: AgentBot) -> None:
    """Plain replies into a thread should resolve to the thread root before dispatch."""
    bot = mock_home_bot
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:server"
    room.canonical_alias = None
    room.users = {
        "@mindroom_home:localhost": MagicMock(),
        "@user:example.com": MagicMock(),
    }

    voice_event = _make_voice_event(
        event_id="$voice789",
        source={"content": {"m.relates_to": {"m.in_reply_to": {"event_id": "$thread_msg"}}}},
    )

    bot.client.room_get_event = AsyncMock(
        return_value=nio.RoomGetEventResponse.from_dict(
            {
                "content": {
                    "body": "Earlier thread message",
                    "msgtype": "m.text",
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
                },
                "event_id": "$thread_msg",
                "sender": "@mindroom_general:localhost",
                "origin_server_ts": 1234567890,
                "room_id": "!test:server",
                "type": "m.room.message",
            },
        ),
    )
    bot._derive_conversation_context = AsyncMock(return_value=(True, "$thread_root", []))

    with (
        patch("mindroom.bot.fetch_thread_history", AsyncMock(return_value=[])),
        patch("mindroom.bot.voice_handler._download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler._handle_voice_message", return_value="🎤 continue the same thread"),
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        await bot._on_media_message(room, voice_event)

    bot._generate_response.assert_called_once()
    call_kwargs = bot._generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice789"
    assert call_kwargs["thread_id"] == "$thread_root"
    assert call_kwargs["prompt"].startswith("🎤 continue the same thread")
    attachment = load_attachment(bot.storage_path, _attachment_id_for_event("$voice789"))
    assert attachment is not None
    assert attachment.thread_id == "$thread_root"
