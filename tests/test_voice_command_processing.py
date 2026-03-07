"""Test audio normalization and dispatch through the shared text/media flow."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
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
from mindroom.matrix.identity import MatrixID
from mindroom.voice_handler import prepare_voice_message

if TYPE_CHECKING:
    from pathlib import Path


def _make_voice_event(
    *,
    sender: str,
    event_id: str = "$voice_event",
    body: str = "voice.ogg",
    source: dict | None = None,
) -> nio.RoomMessageAudio:
    event = MagicMock(spec=nio.RoomMessageAudio)
    event.sender = sender
    event.event_id = event_id
    event.body = body
    event.source = source or {"content": {"body": body}}
    return event


def _make_room(*user_ids: str) -> nio.MatrixRoom:
    room = MagicMock(spec=nio.MatrixRoom)
    room.room_id = "!test:example.com"
    room.canonical_alias = None
    room.users = {user_id: MagicMock() for user_id in user_ids}
    return room


def _make_visible_router_echo_scenario(
    tmp_path: Path,
    *,
    agents: dict | None = None,
    authorization: dict | None = None,
    send_response_return: str | None = "$voice_echo",
    send_response_side_effect: list[str] | None = None,
) -> tuple[AgentBot, nio.MatrixRoom, nio.RoomMessageAudio]:
    """Build a router bot + room + voice event for visible echo tests."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    configured_agents = agents or {"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}}
    config = Config(
        agents=configured_agents,
        authorization=authorization or {"default_room_access": True},
        voice={"enabled": True, "visible_router_echo": True},
    )

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot._derive_conversation_context = AsyncMock(return_value=(False, None, []))
    if send_response_side_effect is not None:
        bot._send_response = AsyncMock(side_effect=send_response_side_effect)
    else:
        bot._send_response = AsyncMock(return_value=send_response_return)

    room_user_ids = [
        "@mindroom_router:localhost",
        *[f"@mindroom_{name}:localhost" for name in configured_agents],
        "@alice:example.com",
    ]
    room = _make_room(*room_user_ids)
    event = _make_voice_event(sender="@alice:example.com")
    return bot, room, event


@pytest.mark.asyncio
async def test_router_processes_own_voice_transcriptions(tmp_path) -> None:  # noqa: ANN001
    """Router should still handle voice-derived commands it sent on behalf of users."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=Config(authorization={"default_room_access": True}),
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = MagicMock(spec=nio.RoomMessageText)
    event.sender = "@mindroom_router:example.com"
    event.body = "🎤 !schedule daily"
    event.event_id = "test_event"
    event.source = {"content": {"body": "🎤 !schedule daily", ORIGINAL_SENDER_KEY: "@alice:example.com"}}

    with (
        patch.object(bot, "_handle_command", new_callable=AsyncMock) as mock_handle,
        patch.object(bot, "client", MagicMock()),
        patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock),
        patch("mindroom.bot.is_dm_room", return_value=False),
    ):
        await bot._on_message(room, event)

    mock_handle.assert_called_once()
    command = mock_handle.call_args[0][2]
    assert command.type.value == "schedule"


@pytest.mark.asyncio
async def test_router_ignores_non_voice_self_messages(tmp_path) -> None:  # noqa: ANN001
    """Router should still ignore its own regular text messages."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=MagicMock(),
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()

    room = _make_room("@mindroom_router:example.com", "@bob:example.com")
    event = MagicMock(spec=nio.RoomMessageText)
    event.sender = "@mindroom_router:example.com"
    event.body = "Regular message from router"
    event.event_id = "test_event"
    event.source = {"content": {"body": "Regular message from router"}}

    with (
        patch.object(bot, "_handle_command", new_callable=AsyncMock) as mock_handle,
        patch.object(bot, "client", MagicMock()),
        patch("mindroom.bot.interactive.handle_text_response", new_callable=AsyncMock),
        patch("mindroom.bot.is_dm_room", return_value=False),
    ):
        await bot._on_message(room, event)

    mock_handle.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_voice_message_includes_original_sender_and_attachment_metadata(tmp_path) -> None:  # noqa: ANN001
    """Audio normalization should preserve sender identity and attachment IDs."""
    config = Config(
        authorization={"default_room_access": True},
        voice={"enabled": True},
    )
    room = _make_room("@mindroom_router:example.com", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")
    client = MagicMock()

    with (
        patch("mindroom.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = "🎤 turn on the lights"
        prepared = await prepare_voice_message(
            client,
            tmp_path,
            room,
            event,
            config,
            sender_domain="example.com",
            thread_id=None,
        )

    assert prepared is not None
    assert prepared.text == "🎤 turn on the lights"
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert prepared.source["content"][ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert prepared.source["content"][ATTACHMENT_IDS_KEY] == [expected_attachment_id]
    assert VOICE_RAW_AUDIO_FALLBACK_KEY not in prepared.source["content"]
    attachment = load_attachment(tmp_path, expected_attachment_id)
    assert attachment is not None
    assert attachment.local_path.exists()


@pytest.mark.asyncio
async def test_prepare_voice_message_marks_raw_audio_fallback_and_thread(tmp_path) -> None:  # noqa: ANN001
    """Fallback normalization should keep thread metadata and the raw-audio flag."""
    config = Config(authorization={"default_room_access": True})
    room = _make_room("@mindroom_home:example.com", "@alice:example.com")
    event = _make_voice_event(
        sender="@alice:example.com",
        source={
            "content": {
                "body": "voice.ogg",
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root"},
            },
        },
    )
    client = MagicMock()

    with patch("mindroom.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio:
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        prepared = await prepare_voice_message(
            client,
            tmp_path,
            room,
            event,
            config,
            sender_domain="example.com",
            thread_id="$thread_root",
        )

    assert prepared is not None
    assert prepared.text == f"{VOICE_PREFIX}[Attached voice message]"
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert prepared.source["content"][ORIGINAL_SENDER_KEY] == "@alice:example.com"
    assert prepared.source["content"][VOICE_RAW_AUDIO_FALLBACK_KEY] is True
    assert prepared.source["content"][ATTACHMENT_IDS_KEY] == [expected_attachment_id]
    assert prepared.source["content"]["m.relates_to"] == {"rel_type": "m.thread", "event_id": "$thread_root"}
    attachment = load_attachment(tmp_path, expected_attachment_id)
    assert attachment is not None
    assert attachment.thread_id == "$thread_root"


@pytest.mark.asyncio
async def test_router_ignores_audio_events_from_internal_agents(tmp_path) -> None:  # noqa: ANN001
    """Audio from another agent should be ignored immediately."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:example.com"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:example.com")

    config = Config(
        agents={"assistant": {"display_name": "Assistant"}},
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
    bot._send_response = AsyncMock()

    room = _make_room(
        "@mindroom_router:example.com",
        f"@mindroom_assistant:{config.domain}",
        "@alice:example.com",
    )
    event = _make_voice_event(
        sender=f"@mindroom_assistant:{config.domain}",
        event_id="$agent_audio_event",
        body="generated_audio.ogg",
        source={"content": {"body": "generated_audio.ogg", "msgtype": "m.audio"}},
    )

    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, event)

    mock_voice.assert_not_called()
    mock_download_audio.assert_not_called()
    bot._send_response.assert_not_called()
    bot.response_tracker.mark_responded.assert_called_once_with("$agent_audio_event")


@pytest.mark.asyncio
async def test_agent_handles_audio_without_router_when_voice_disabled(tmp_path) -> None:  # noqa: ANN001
    """A single agent should answer audio directly when no router is present."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_home:localhost"
    agent_user.agent_name = "home"
    agent_user.matrix_id = MatrixID.parse("@mindroom_home:localhost")

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=Config(
            agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
            authorization={"default_room_access": True},
        ),
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    bot._generate_response = AsyncMock(return_value="$response")
    bot._derive_conversation_context = AsyncMock(return_value=(True, "$voice_event", []))

    room = _make_room("@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = None
        await bot._on_media_message(room, event)

    bot._generate_response.assert_called_once()
    call_kwargs = bot._generate_response.call_args.kwargs
    expected_attachment_id = _attachment_id_for_event("$voice_event")
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}[Attached voice message]")
    assert call_kwargs["attachment_ids"] == [expected_attachment_id]
    assert list(call_kwargs["media"].audio)
    bot.response_tracker.mark_responded.assert_called_once_with("$voice_event", "$response")


@pytest.mark.asyncio
async def test_agent_handles_audio_with_router_present_in_single_agent_room(tmp_path) -> None:  # noqa: ANN001
    """Router presence should not block the only visible agent from answering audio."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_home:localhost"
    agent_user.agent_name = "home"
    agent_user.matrix_id = MatrixID.parse("@mindroom_home:localhost")

    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=Config(
            agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
            authorization={"default_room_access": True},
        ),
        rooms=["!test:example.com"],
    )
    bot.response_tracker = MagicMock()
    bot.response_tracker.has_responded.return_value = False
    bot.logger = MagicMock()
    bot.client = AsyncMock()
    bot.client.rooms = {}
    bot.client.user_id = "@mindroom_home:localhost"
    bot._generate_response = AsyncMock(return_value="$response")
    bot._derive_conversation_context = AsyncMock(return_value=(True, "$voice_event", []))

    room = _make_room("@mindroom_router:localhost", "@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = None
        await bot._on_media_message(room, event)

    mock_download_audio.assert_called_once()
    bot._generate_response.assert_called_once()


@pytest.mark.asyncio
async def test_router_and_agent_share_audio_normalization_when_router_is_present(tmp_path) -> None:  # noqa: ANN001
    """Router-present rooms should still normalize one audio event only once."""
    config = Config(
        agents={"home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]}},
        authorization={"default_room_access": True},
        voice={"enabled": True},
    )

    bots: list[AgentBot] = []
    for agent_name in (ROUTER_AGENT_NAME, "home"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot.logger = MagicMock()
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = agent_user.user_id
        bot._send_response = AsyncMock(return_value="$router_response")
        bot._generate_response = AsyncMock(return_value=f"${agent_name}_response")
        bot._derive_conversation_context = AsyncMock(return_value=(True, "$voice_event", []))
        bots.append(bot)

    room = _make_room("@mindroom_router:localhost", "@mindroom_home:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}turn on the lights"
        for bot in bots:
            await bot._on_media_message(room, event)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    bots[0]._send_response.assert_not_called()
    assert bots[1]._generate_response.await_count == 1


@pytest.mark.asyncio
async def test_router_posts_visible_voice_echo_when_enabled(tmp_path) -> None:  # noqa: ANN001
    """Router can optionally post the normalized voice text for user visibility."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@home turn on the lights"
        await bot._on_media_message(room, event)

    bot._send_response.assert_called_once()
    call_kwargs = bot._send_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["response_text"] == f"{VOICE_PREFIX}@home turn on the lights"
    assert call_kwargs["thread_id"] == "$voice_event"
    assert call_kwargs["skip_mentions"] is True


@pytest.mark.asyncio
async def test_router_visible_voice_echo_is_deduplicated_on_redelivery(tmp_path) -> None:  # noqa: ANN001
    """Visible router echoes should be sent once even if the same audio event is redelivered."""
    bot, room, event = _make_visible_router_echo_scenario(tmp_path)

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@home turn on the lights"
        await bot._on_media_message(room, event)
        await bot._on_media_message(room, event)

    bot._send_response.assert_called_once()
    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert bot.response_tracker.has_responded(event.event_id)
    assert bot.response_tracker.get_response_event_id(event.event_id) == "$voice_echo"


@pytest.mark.asyncio
async def test_router_visible_voice_echo_respects_reply_permissions(tmp_path) -> None:  # noqa: ANN001
    """Router should not post visible echoes when it cannot reply to the sender."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        authorization={
            "default_room_access": True,
            "agent_reply_permissions": {ROUTER_AGENT_NAME: ["@bob:example.com"]},
        },
    )

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
    ):
        await bot._on_media_message(room, event)

    bot._send_response.assert_not_called()
    mock_download_audio.assert_not_awaited()
    mock_voice.assert_not_awaited()
    assert bot.response_tracker.has_responded(event.event_id)
    assert bot.response_tracker.get_response_event_id(event.event_id) is None


@pytest.mark.asyncio
async def test_router_visible_voice_echo_keeps_multi_agent_handoff(tmp_path) -> None:  # noqa: ANN001
    """Visible router echoes should not replace the normal multi-agent handoff."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
        },
        send_response_side_effect=["$voice_echo", "$route"],
    )

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.suggest_agent_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)

    assert bot._send_response.await_count == 2
    echo_call = bot._send_response.call_args_list[0].kwargs
    handoff_call = bot._send_response.call_args_list[1].kwargs
    assert echo_call["reply_to_event_id"] == "$voice_event"
    assert echo_call["response_text"] == f"{VOICE_PREFIX}summarize this audio"
    assert echo_call["skip_mentions"] is True
    assert "extra_content" not in echo_call
    assert handoff_call["reply_to_event_id"] == "$voice_event"
    assert handoff_call["response_text"] == "@home could you help with this?"
    assert handoff_call["extra_content"] == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        ATTACHMENT_IDS_KEY: [_attachment_id_for_event("$voice_event")],
    }


@pytest.mark.asyncio
async def test_router_visible_voice_echo_is_not_duplicated_when_handoff_retries(tmp_path) -> None:  # noqa: ANN001
    """A failed handoff retry should reuse the prior visible echo instead of reposting it."""
    bot, room, event = _make_visible_router_echo_scenario(
        tmp_path,
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
        },
        send_response_side_effect=["$voice_echo", None, "$route"],
    )

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.suggest_agent_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)

        assert not bot.response_tracker.has_responded(event.event_id)
        assert bot.response_tracker.get_visible_echo_event_id(event.event_id) == "$voice_echo"

        await bot._on_media_message(room, event)

    response_texts = [call.kwargs["response_text"] for call in bot._send_response.call_args_list]
    assert response_texts == [
        f"{VOICE_PREFIX}summarize this audio",
        "@home could you help with this?",
        "@home could you help with this?",
    ]
    assert bot.response_tracker.has_responded(event.event_id)
    assert bot.response_tracker.get_visible_echo_event_id(event.event_id) == "$voice_echo"


@pytest.mark.asyncio
async def test_router_routes_transcribed_audio_when_multiple_agents_are_present(tmp_path) -> None:  # noqa: ANN001
    """Router should route normalized audio like any other synthetic text input."""
    agent_user = MagicMock()
    agent_user.user_id = "@mindroom_router:localhost"
    agent_user.agent_name = ROUTER_AGENT_NAME
    agent_user.matrix_id = MatrixID.parse("@mindroom_router:localhost")

    config = Config(
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
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
    bot.client = AsyncMock()
    bot._send_response = AsyncMock(return_value="$response")
    bot._derive_conversation_context = AsyncMock(return_value=(False, None, []))

    room = _make_room(
        "@mindroom_router:localhost",
        "@mindroom_home:localhost",
        "@mindroom_research:localhost",
        "@alice:example.com",
    )
    event = _make_voice_event(sender="@alice:example.com")

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.suggest_agent_for_message", new_callable=AsyncMock, return_value="home"),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        await bot._on_media_message(room, event)

    bot._send_response.assert_called_once()
    call_kwargs = bot._send_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["response_text"] == "@home could you help with this?"
    assert call_kwargs["extra_content"] == {
        ORIGINAL_SENDER_KEY: "@alice:example.com",
        ATTACHMENT_IDS_KEY: [_attachment_id_for_event("$voice_event")],
    }
    bot.response_tracker.mark_responded.assert_called_once_with("$voice_event")


@pytest.mark.asyncio
async def test_transcribed_mentions_target_the_mentioned_agent_when_router_absent(tmp_path) -> None:  # noqa: ANN001
    """A transcript mention should make the mentioned agent respond directly."""
    config = Config(
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
        },
        authorization={"default_room_access": True},
        voice={"enabled": True},
    )

    room = _make_room("@mindroom_home:localhost", "@mindroom_research:localhost", "@alice:example.com")
    event = _make_voice_event(sender="@alice:example.com")

    bots: list[AgentBot] = []
    for agent_name in ("home", "research"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot.logger = MagicMock()
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = f"@mindroom_{agent_name}:localhost"
        bot._generate_response = AsyncMock(return_value=f"${agent_name}_response")
        bot._derive_conversation_context = AsyncMock(return_value=(True, "$voice_event", []))
        bots.append(bot)

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}@research summarize this audio"
        for bot in bots:
            await bot._on_media_message(room, event)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert bots[0]._generate_response.await_count == 0
    assert bots[1]._generate_response.await_count == 1
    call_kwargs = bots[1]._generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}@research summarize this audio")
    assert call_kwargs["attachment_ids"] == [_attachment_id_for_event("$voice_event")]


@pytest.mark.asyncio
async def test_caption_mentions_still_target_agent_when_stt_drops_the_mention(tmp_path) -> None:  # noqa: ANN001
    """Inherited audio-caption mentions should still target the agent when STT omits them."""
    config = Config(
        agents={
            "home": {"display_name": "HomeAssistant", "rooms": ["!test:example.com"]},
            "research": {"display_name": "ResearchAgent", "rooms": ["!test:example.com"]},
        },
        authorization={"default_room_access": True},
        voice={"enabled": True},
    )

    room = _make_room("@mindroom_home:localhost", "@mindroom_research:localhost", "@alice:example.com")
    event = _make_voice_event(
        sender="@alice:example.com",
        body="For @research voice note",
        source={
            "content": {
                "body": "For @research voice note",
                "filename": "voice.ogg",
                "m.mentions": {"user_ids": ["@mindroom_research:localhost"]},
            },
        },
    )

    bots: list[AgentBot] = []
    for agent_name in ("home", "research"):
        agent_user = MagicMock()
        agent_user.user_id = f"@mindroom_{agent_name}:localhost"
        agent_user.agent_name = agent_name
        agent_user.matrix_id = MatrixID.parse(f"@mindroom_{agent_name}:localhost")
        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=["!test:example.com"],
        )
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot.logger = MagicMock()
        bot.client = AsyncMock()
        bot.client.rooms = {}
        bot.client.user_id = f"@mindroom_{agent_name}:localhost"
        bot._generate_response = AsyncMock(return_value=f"${agent_name}_response")
        bot._derive_conversation_context = AsyncMock(return_value=(True, "$voice_event", []))
        bots.append(bot)

    with (
        patch("mindroom.bot.voice_handler.download_audio", new_callable=AsyncMock) as mock_download_audio,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch("mindroom.bot.is_dm_room", new_callable=AsyncMock, return_value=False),
    ):
        mock_download_audio.return_value = Audio(content=b"voice-bytes", mime_type="audio/ogg")
        mock_voice.return_value = f"{VOICE_PREFIX}summarize this audio"
        for bot in bots:
            await bot._on_media_message(room, event)

    assert mock_download_audio.await_count == 1
    assert mock_voice.await_count == 1
    assert bots[0]._generate_response.await_count == 0
    assert bots[1]._generate_response.await_count == 1
    call_kwargs = bots[1]._generate_response.call_args.kwargs
    assert call_kwargs["reply_to_event_id"] == "$voice_event"
    assert call_kwargs["prompt"].startswith(f"{VOICE_PREFIX}summarize this audio")
    assert call_kwargs["attachment_ids"] == [_attachment_id_for_event("$voice_event")]
