"""Test that agent responses are regenerated when user edits their message."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config import Config
from mindroom.matrix.users import AgentMatrixUser
from mindroom.response_tracker import ResponseTracker


@pytest.mark.asyncio
async def test_bot_regenerates_response_on_edit(tmp_path: Path) -> None:
    """Test that the bot regenerates its response when a user edits their message."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.domain = "example.com"

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create an original message event
    original_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "@test_agent what is 2+2?",
                "msgtype": "m.text",
            },
            "event_id": "$original:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    original_event.source = {
        "content": {
            "body": "@test_agent what is 2+2?",
            "msgtype": "m.text",
        },
        "event_id": "$original:example.com",
        "sender": "@user:example.com",
    }

    # Simulate that the bot has already responded to the original message
    response_event_id = "$response:example.com"
    bot.response_tracker.mark_responded(original_event.event_id, response_event_id)

    # Create an edit event
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent what is 3+3?",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent what is 3+3?",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent what is 3+3?",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent what is 3+3?",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    # Mock the methods needed for regeneration
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
        patch("mindroom.bot.should_agent_respond") as mock_should_respond,
        patch("mindroom.bot.should_use_streaming", new_callable=AsyncMock) as mock_streaming,
        patch("mindroom.bot.ai_response", new_callable=AsyncMock) as mock_ai_response,
    ):
        # Setup mocks
        mock_context.return_value = MagicMock(
            am_i_mentioned=True,
            is_thread=False,
            thread_id=None,
            thread_history=[],
            mentioned_agents=["test_agent"],
        )
        mock_should_respond.return_value = True
        mock_streaming.return_value = False  # Use non-streaming for simpler test
        mock_ai_response.return_value = "The answer is 6"

        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot attempted to regenerate the response
        mock_context.assert_called_once()
        mock_should_respond.assert_called_once()
        mock_ai_response.assert_called_once()

        # Verify that the bot edited the existing response message
        mock_edit.assert_called_once_with(
            room.room_id,
            response_event_id,
            "The answer is 6",
            None,  # thread_id
        )

        # Verify that the response tracker still maps to the same response
        assert bot.response_tracker.get_response_event_id(original_event.event_id) == response_event_id


@pytest.mark.asyncio
async def test_bot_ignores_edit_without_previous_response(tmp_path: Path) -> None:
    """Test that the bot ignores edits if it didn't respond to the original message."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.domain = "example.com"

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create an edit event for a message we never responded to
    edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* @test_agent help",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "@test_agent help",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$unknown:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    edit_event.source = {
        "content": {
            "body": "* @test_agent help",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "@test_agent help",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$unknown:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@user:example.com",
    }

    # Mock the methods
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
    ):
        # Process the edit event
        await bot._on_message(room, edit_event)

        # Verify that the bot did NOT attempt to regenerate
        mock_context.assert_not_called()
        mock_edit.assert_not_called()


@pytest.mark.asyncio
async def test_bot_ignores_agent_edits(tmp_path: Path) -> None:
    """Test that the bot ignores edit events from other agents (e.g., streaming edits)."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config with multiple agents
    config = Mock()
    config.agents = {
        "test_agent": Mock(),
        "helper_agent": Mock(),
    }
    config.domain = "example.com"

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Simulate that the bot has responded to some message
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

    # Test 1: Bot's own edit
    own_edit_event = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Updated response",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Updated response",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit:example.com",
            "sender": "@test_agent:example.com",  # Bot's own edit
            "origin_server_ts": 1000001,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    own_edit_event.source = {
        "content": {
            "body": "* Updated response",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "Updated response",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit:example.com",
        "sender": "@test_agent:example.com",
    }

    # Test 2: Another agent's edit
    other_agent_edit = nio.RoomMessageText.from_dict(
        {
            "content": {
                "body": "* Hey @test_agent what's up",
                "msgtype": "m.text",
                "m.new_content": {
                    "body": "Hey @test_agent what's up",
                    "msgtype": "m.text",
                },
                "m.relates_to": {
                    "event_id": "$original:example.com",
                    "rel_type": "m.replace",
                },
            },
            "event_id": "$edit2:example.com",
            "sender": "@mindroom_helper_agent:example.com",  # Another agent's edit
            "origin_server_ts": 1000002,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    other_agent_edit.source = {
        "content": {
            "body": "* Hey @test_agent what's up",
            "msgtype": "m.text",
            "m.new_content": {
                "body": "Hey @test_agent what's up",
                "msgtype": "m.text",
            },
            "m.relates_to": {
                "event_id": "$original:example.com",
                "rel_type": "m.replace",
            },
        },
        "event_id": "$edit2:example.com",
        "sender": "@mindroom_helper_agent:example.com",
    }

    # Mock the methods
    with (
        patch.object(bot, "_extract_message_context", new_callable=AsyncMock) as mock_context,
        patch.object(bot, "_edit_message", new_callable=AsyncMock) as mock_edit,
    ):
        # Process the bot's own edit event
        await bot._on_message(room, own_edit_event)

        # Process another agent's edit event
        await bot._on_message(room, other_agent_edit)

        # Verify that the bot did NOT attempt to regenerate for either edit
        mock_context.assert_not_called()
        mock_edit.assert_not_called()


@pytest.mark.asyncio
async def test_response_tracker_mapping_persistence(tmp_path: Path) -> None:
    """Test that ResponseTracker correctly persists and retrieves user-to-response mappings."""
    # Create a response tracker
    tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mark some responses
    user_event_1 = "$user1:example.com"
    response_event_1 = "$response1:example.com"
    tracker.mark_responded(user_event_1, response_event_1)

    user_event_2 = "$user2:example.com"
    response_event_2 = "$response2:example.com"
    tracker.mark_responded(user_event_2, response_event_2)

    # Verify mappings are stored
    assert tracker.get_response_event_id(user_event_1) == response_event_1
    assert tracker.get_response_event_id(user_event_2) == response_event_2
    assert tracker.get_response_event_id("$unknown:example.com") is None

    # Create a new tracker instance to test persistence
    tracker2 = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Verify mappings were loaded from disk
    assert tracker2.get_response_event_id(user_event_1) == response_event_1
    assert tracker2.get_response_event_id(user_event_2) == response_event_2


@pytest.mark.asyncio
async def test_on_reaction_tracks_response_event_id(tmp_path: Path) -> None:
    """Test that _on_reaction properly tracks the response event ID."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.domain = "example.com"
    config.authorization = Mock()
    config.authorization.is_authorized = Mock(return_value=True)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a reaction event
    reaction_event = nio.ReactionEvent.from_dict(
        {
            "content": {
                "m.relates_to": {
                    "event_id": "$question:example.com",
                    "key": "1️⃣",
                    "rel_type": "m.annotation",
                },
            },
            "event_id": "$reaction:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.reaction",
            "room_id": "!test:example.com",
        },
    )
    reaction_event.reacts_to = "$question:example.com"
    reaction_event.key = "1️⃣"

    # Mock interactive.handle_reaction to return a result
    with (
        patch("mindroom.bot.interactive.handle_reaction", new_callable=AsyncMock) as mock_handle_reaction,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(bot, "_send_response", new_callable=AsyncMock) as mock_send_response,
        patch.object(bot, "_generate_response", new_callable=AsyncMock) as mock_generate_response,
        patch("mindroom.bot.fetch_thread_history", new_callable=AsyncMock) as mock_fetch_history,
        patch("mindroom.bot.has_user_responded_after_message", return_value=False),
    ):
        # Setup mocks
        mock_handle_reaction.return_value = ("Option 1", "thread_id")  # selected_value, thread_id
        mock_send_response.return_value = "$ack_event:example.com"  # Acknowledgment event ID
        mock_generate_response.return_value = (
            "$response_event:example.com"  # Response event ID (same as ack since we edit)
        )
        mock_fetch_history.return_value = []

        # Process the reaction event
        await bot._on_reaction(room, reaction_event)

        # Verify that the bot tracked the response correctly
        assert bot.response_tracker.has_responded("$question:example.com")
        assert bot.response_tracker.get_response_event_id("$question:example.com") == "$response_event:example.com"

        # Verify the methods were called with correct parameters
        mock_handle_reaction.assert_called_once()
        mock_send_response.assert_called_once()
        mock_generate_response.assert_called_once()

        # Verify that _generate_response was called with the acknowledgment event ID for editing
        call_kwargs = mock_generate_response.call_args.kwargs
        assert call_kwargs["existing_event_id"] == "$ack_event:example.com"


@pytest.mark.asyncio
async def test_on_voice_message_tracks_response_event_id(tmp_path: Path) -> None:
    """Test that _on_voice_message properly tracks the response event ID."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config with voice enabled
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.domain = "example.com"
    config.voice = Mock()
    config.voice.enabled = True
    config.authorization = Mock()
    config.authorization.is_authorized = Mock(return_value=True)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a voice message event
    voice_event = nio.RoomMessageAudio.from_dict(
        {
            "content": {
                "body": "voice_message.ogg",
                "msgtype": "m.audio",
                "url": "mxc://example.com/voice123",
                "org.matrix.msc1767.audio": {
                    "duration": 5000,
                    "waveform": [0, 100, 200],
                },
                "org.matrix.msc3245.voice": {},
            },
            "event_id": "$voice:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    voice_event.source = {
        "content": {
            "body": "voice_message.ogg",
            "msgtype": "m.audio",
            "url": "mxc://example.com/voice123",
            "org.matrix.msc1767.audio": {
                "duration": 5000,
                "waveform": [0, 100, 200],
            },
            "org.matrix.msc3245.voice": {},
        },
        "event_id": "$voice:example.com",
        "sender": "@user:example.com",
    }

    # Mock voice_handler.handle_voice_message to return a transcription
    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(bot, "_send_response", new_callable=AsyncMock) as mock_send_response,
    ):
        # Setup mocks
        mock_handle_voice.return_value = "This is the transcribed message from voice"
        mock_send_response.return_value = "$response:example.com"

        # Process the voice event
        await bot._on_voice_message(room, voice_event)

        # Verify that the bot tracked the response correctly
        assert bot.response_tracker.has_responded("$voice:example.com")
        assert bot.response_tracker.get_response_event_id("$voice:example.com") == "$response:example.com"

        # Verify the methods were called
        mock_handle_voice.assert_called_once_with(bot.client, room, voice_event, config)
        mock_send_response.assert_called_once()


@pytest.mark.asyncio
async def test_on_voice_message_no_transcription_still_marks_responded(tmp_path: Path) -> None:
    """Test that _on_voice_message marks as responded even when no transcription is produced."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config with voice enabled
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.domain = "example.com"
    config.voice = Mock()
    config.voice.enabled = True
    config.authorization = Mock()
    config.authorization.is_authorized = Mock(return_value=True)

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a voice message event
    voice_event = nio.RoomMessageAudio.from_dict(
        {
            "content": {
                "body": "voice_message.ogg",
                "msgtype": "m.audio",
                "url": "mxc://example.com/voice123",
                "org.matrix.msc1767.audio": {
                    "duration": 5000,
                    "waveform": [0, 100, 200],
                },
                "org.matrix.msc3245.voice": {},
            },
            "event_id": "$voice:example.com",
            "sender": "@user:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    voice_event.source = {
        "content": {
            "body": "voice_message.ogg",
            "msgtype": "m.audio",
            "url": "mxc://example.com/voice123",
            "org.matrix.msc1767.audio": {
                "duration": 5000,
                "waveform": [0, 100, 200],
            },
            "org.matrix.msc3245.voice": {},
        },
        "event_id": "$voice:example.com",
        "sender": "@user:example.com",
    }

    # Mock voice_handler.handle_voice_message to return None (no transcription)
    with (
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
        patch("mindroom.bot.is_authorized_sender", return_value=True),
        patch.object(bot, "_send_response", new_callable=AsyncMock) as mock_send_response,
    ):
        # Setup mocks
        mock_handle_voice.return_value = None  # No transcription

        # Process the voice event
        await bot._on_voice_message(room, voice_event)

        # Verify that the bot marked as responded even without a transcription
        assert bot.response_tracker.has_responded("$voice:example.com")
        # Should not have a response event ID since no response was sent
        assert bot.response_tracker.get_response_event_id("$voice:example.com") is None

        # Verify voice handler was called but _send_response was not
        mock_handle_voice.assert_called_once_with(bot.client, room, voice_event, config)
        mock_send_response.assert_not_called()


@pytest.mark.asyncio
async def test_unauthorized_user_cannot_edit_regenerate(tmp_path: Path) -> None:
    """Test that unauthorized users cannot trigger response regeneration through edits."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config with authorization
    config = Config(
        agents={"test_agent": {"display_name": "Test Agent", "role": "Test agent", "rooms": ["!test:example.com"]}},
        authorization={
            "global_users": ["@authorized:example.com"],
            "room_permissions": {},
            "default_room_access": False,
        },
    )
    config.domain = "example.com"

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    room = Mock(spec=nio.MatrixRoom)
    room.room_id = "!test:example.com"
    room.is_direct = False

    # Original message from authorized user
    original_event = Mock(spec=nio.RoomMessageText)
    original_event.event_id = "$original:example.com"
    original_event.sender = "@authorized:example.com"
    original_event.body = "Original question"
    original_event.source = {"event_id": "$original:example.com"}

    # Store that we responded to the original
    bot.response_tracker.mark_responded("$original:example.com", "$response:example.com")

    # Edit from unauthorized user (trying to regenerate)
    edit_event = Mock(spec=nio.RoomMessageText)
    edit_event.event_id = "$edit:example.com"
    edit_event.sender = "@unauthorized:example.com"
    edit_event.body = "Edited question"
    edit_event.source = {
        "event_id": "$edit:example.com",
        "content": {
            "m.relates_to": {
                "rel_type": "m.replace",
                "event_id": "$original:example.com",
            },
        },
    }

    # Test that authorization check works
    with (
        patch("mindroom.bot.is_authorized_sender", return_value=False) as mock_is_auth,
        patch.object(bot, "_handle_message_edit") as mock_handle_edit,
    ):
        await bot._on_message(room, edit_event)
        # Verify authorization was checked
        mock_is_auth.assert_called_once_with(edit_event.sender, config, room.room_id)
        # Should not handle edit for unauthorized user
        mock_handle_edit.assert_not_called()


@pytest.mark.asyncio
async def test_on_voice_message_unauthorized_sender_marks_responded(tmp_path: Path) -> None:
    """Test that _on_voice_message marks as responded for unauthorized senders."""
    # Create a mock agent user
    agent_user = AgentMatrixUser(
        agent_name="test_agent",
        user_id="@test_agent:example.com",
        display_name="Test Agent",
        password="test_password",  # noqa: S106
    )

    # Create a minimal mock config with voice enabled
    config = Mock()
    config.agents = {"test_agent": Mock()}
    config.domain = "example.com"
    config.voice = Mock()
    config.voice.enabled = True

    # Create the bot
    bot = AgentBot(
        agent_user=agent_user,
        storage_path=tmp_path,
        config=config,
        rooms=["!test:example.com"],
    )

    # Mock the client
    bot.client = AsyncMock(spec=nio.AsyncClient)
    bot.client.user_id = "@test_agent:example.com"

    # Create real ResponseTracker with the test path
    bot.response_tracker = ResponseTracker(agent_name="test_agent", base_path=tmp_path)

    # Mock logger
    bot.logger = MagicMock()

    # Create a room
    room = nio.MatrixRoom(room_id="!test:example.com", own_user_id="@test_agent:example.com")

    # Create a voice message event from unauthorized sender
    voice_event = nio.RoomMessageAudio.from_dict(
        {
            "content": {
                "body": "voice_message.ogg",
                "msgtype": "m.audio",
                "url": "mxc://example.com/voice123",
                "org.matrix.msc1767.audio": {
                    "duration": 5000,
                    "waveform": [0, 100, 200],
                },
                "org.matrix.msc3245.voice": {},
            },
            "event_id": "$voice:example.com",
            "sender": "@unauthorized:example.com",
            "origin_server_ts": 1000000,
            "type": "m.room.message",
            "room_id": "!test:example.com",
        },
    )
    voice_event.source = {
        "content": {
            "body": "voice_message.ogg",
            "msgtype": "m.audio",
            "url": "mxc://example.com/voice123",
            "org.matrix.msc1767.audio": {
                "duration": 5000,
                "waveform": [0, 100, 200],
            },
            "org.matrix.msc3245.voice": {},
        },
        "event_id": "$voice:example.com",
        "sender": "@unauthorized:example.com",
    }

    # Mock is_authorized_sender to return False
    with (
        patch("mindroom.bot.is_authorized_sender", return_value=False) as mock_is_authorized,
        patch("mindroom.bot.voice_handler.handle_voice_message", new_callable=AsyncMock) as mock_handle_voice,
    ):
        # Process the voice event
        await bot._on_voice_message(room, voice_event)

        # Verify that the bot marked as responded even for unauthorized sender
        assert bot.response_tracker.has_responded("$voice:example.com")
        # Should not have a response event ID since no response was sent
        assert bot.response_tracker.get_response_event_id("$voice:example.com") is None

        # Verify authorization was checked but voice handler was not called
        mock_is_authorized.assert_called_once()
        mock_handle_voice.assert_not_called()
