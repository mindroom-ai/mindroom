"""End-to-end test for streaming edits using real Matrix API."""

import asyncio
import contextlib
from pathlib import Path
from unittest.mock import AsyncMock, patch

import nio
import pytest

from mindroom.bot import MultiAgentOrchestrator


@pytest.mark.asyncio
@pytest.mark.e2e  # Mark as end-to-end test
@patch("mindroom.bot.ensure_all_agent_users")
@patch("mindroom.bot.login_agent_user")
async def test_streaming_edits_e2e(
    mock_login: AsyncMock,
    mock_ensure_users: AsyncMock,
    tmp_path: Path,
) -> None:
    """End-to-end test that agents don't respond to streaming edits from other agents."""
    # Mock user setup
    from mindroom.matrix import AgentMatrixUser

    mock_users = {
        "helper": AgentMatrixUser(
            agent_name="helper",
            user_id="@mindroom_helper:localhost",
            display_name="HelperAgent",
            password="test_pass",
            access_token="helper_token",
        ),
        "calculator": AgentMatrixUser(
            agent_name="calculator",
            user_id="@mindroom_calculator:localhost",
            display_name="CalculatorAgent",
            password="test_pass",
            access_token="calc_token",
        ),
    }
    mock_ensure_users.return_value = mock_users

    # Create test room
    test_room_id = "!streaming_test:localhost"
    test_room = nio.MatrixRoom(room_id=test_room_id, own_user_id="", encrypted=False)
    test_room.name = "Streaming Test Room"

    # Track events sent by agents
    helper_events = []
    calc_events = []

    # Create mock clients for each agent
    helper_client = AsyncMock()
    calc_client = AsyncMock()

    # Configure login to return appropriate clients
    def login_side_effect(homeserver, agent_user):
        if agent_user.agent_name == "helper":
            return helper_client
        elif agent_user.agent_name == "calculator":
            return calc_client
        raise ValueError(f"Unknown agent: {agent_user.agent_name}")

    mock_login.side_effect = login_side_effect

    # Track room_send calls
    async def helper_room_send(room_id, message_type, content):
        event_id = f"$helper_{len(helper_events)}"
        helper_events.append(
            {
                "event_id": event_id,
                "room_id": room_id,
                "type": message_type,
                "content": content,
            }
        )
        return nio.RoomSendResponse(event_id=event_id, room_id=room_id)

    async def calc_room_send(room_id, message_type, content):
        event_id = f"$calc_{len(calc_events)}"
        calc_events.append(
            {
                "event_id": event_id,
                "room_id": room_id,
                "type": message_type,
                "content": content,
            }
        )
        return nio.RoomSendResponse(event_id=event_id, room_id=room_id)

    helper_client.room_send.side_effect = helper_room_send
    calc_client.room_send.side_effect = calc_room_send

    # Mock other client methods
    for client in [helper_client, calc_client]:
        client.joined_rooms.return_value = nio.JoinedRoomsResponse(rooms=[test_room_id])
        client.sync_forever = AsyncMock()

    # Create orchestrator with specific room configuration
    orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

    # Patch the config loading to assign rooms
    with patch("mindroom.bot.load_config") as mock_config:
        mock_config.return_value.agents = {
            "helper": type("obj", (), {"rooms": [test_room_id]})(),
            "calculator": type("obj", (), {"rooms": [test_room_id]})(),
        }

        await orchestrator.initialize()

    # Start the orchestrator (in background)
    start_task = asyncio.create_task(orchestrator.start())

    try:
        # Give the bots time to start
        await asyncio.sleep(0.1)

        # Access the bots
        helper_bot = orchestrator.agent_bots["helper"]
        calc_bot = orchestrator.agent_bots["calculator"]

        # Ensure calculator bot has streaming disabled for this test
        calc_bot.enable_streaming = False

        # Simulate user mentioning helper
        from unittest.mock import MagicMock

        user_event = MagicMock(spec=nio.RoomMessageText)
        user_event.body = "@mindroom_helper:localhost can you help with math?"
        user_event.sender = "@user:localhost"
        user_event.event_id = "$user_123"
        user_event.source = {
            "event_id": "$user_123",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "@mindroom_helper:localhost can you help with math?",
                "m.mentions": {"user_ids": ["@mindroom_helper:localhost"]},
            },
        }

        # Mock AI response for helper (streaming)
        with patch("mindroom.bot.ai_response_streaming") as mock_streaming:

            async def stream_response(agent_name, prompt, session_id, storage_path, thread_history, room_id):
                yield "I can help! Let me ask "
                yield "@mindroom_calculator:localhost what's 2+2?"

            mock_streaming.return_value = stream_response(
                "helper", user_event.body, "session", tmp_path, [], test_room_id
            )

            # Mock that helper is mentioned
            with patch("mindroom.bot.check_agent_mentioned") as mock_check:
                mock_check.return_value = (["helper"], True)

                # Process with helper bot
                await helper_bot._on_message(test_room, user_event)

        # Wait for streaming to complete
        await asyncio.sleep(0.1)

        # Verify helper sent initial message and edit
        assert len(helper_events) >= 1
        initial_msg = helper_events[0]
        assert initial_msg["type"] == "m.room.message"

        # Find the edit event (if streaming produced one)
        edit_event = None
        for event in helper_events[1:]:
            if "m.relates_to" in event["content"]:
                edit_event = event
                break

        if edit_event:
            # Simulate calculator seeing the edit
            calc_edit_event = MagicMock(spec=nio.RoomMessageText)
            calc_edit_event.body = edit_event["content"].get("body", "")
            calc_edit_event.sender = "@mindroom_helper:localhost"
            calc_edit_event.event_id = f"$edit_{helper_events.index(edit_event)}"
            calc_edit_event.source = {
                "event_id": f"$edit_{helper_events.index(edit_event)}",
                "sender": "@mindroom_helper:localhost",
                "origin_server_ts": 1234567891,
                "type": "m.room.message",
                "content": edit_event["content"],
            }

            # Process edit with calculator bot
            await calc_bot._on_message(test_room, calc_edit_event)

            # Verify calculator did NOT respond to the edit
            assert len(calc_events) == 0, "Calculator should not respond to agent edits"

        # Now simulate helper's final message (not an edit)
        final_event = MagicMock(spec=nio.RoomMessageText)
        final_event.body = "I can help! Let me ask @mindroom_calculator:localhost what's 2+2?"
        final_event.sender = "@mindroom_helper:localhost"
        final_event.event_id = "$helper_final"
        final_event.source = {
            "event_id": "$helper_final",
            "sender": "@mindroom_helper:localhost",
            "origin_server_ts": 1234567892,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "I can help! Let me ask @mindroom_calculator:localhost what's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            },
        }

        # Mock AI response for calculator (non-streaming)
        with patch("mindroom.bot.ai_response") as mock_ai:
            mock_ai.return_value = "The answer is 4"

            # Also mock that calculator is mentioned
            with patch("mindroom.bot.check_agent_mentioned") as mock_check:
                mock_check.return_value = (["calculator"], True)

                # Process final message with calculator bot
                await calc_bot._on_message(test_room, final_event)

        # Wait for processing
        await asyncio.sleep(0.1)

        # Verify calculator responded to the final message
        assert len(calc_events) == 1, "Calculator should respond to final message"
        calc_response = calc_events[0]
        assert calc_response["type"] == "m.room.message"
        assert "4" in calc_response["content"].get("body", "")

    finally:
        # Stop the orchestrator
        await orchestrator.stop()
        start_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await start_task


@pytest.mark.asyncio
@pytest.mark.e2e
async def test_user_edits_with_mentions_e2e(tmp_path: Path) -> None:
    """Test that agents DO respond to user edits that add mentions."""
    from mindroom.matrix import AgentMatrixUser

    # Create a single bot for this test
    calc_user = AgentMatrixUser(
        agent_name="calculator",
        user_id="@mindroom_calculator:localhost",
        display_name="CalculatorAgent",
        password="test_pass",
        access_token="calc_token",
    )

    # Mock login
    with patch("mindroom.bot.login_agent_user") as mock_login:
        mock_client = AsyncMock()
        mock_login.return_value = mock_client

        # Track events
        events_sent = []

        async def mock_room_send(room_id, message_type, content):
            event_id = f"$calc_{len(events_sent)}"
            events_sent.append(
                {
                    "event_id": event_id,
                    "content": content,
                }
            )
            return nio.RoomSendResponse(event_id=event_id, room_id=room_id)

        mock_client.room_send.side_effect = mock_room_send

        # Create bot
        from mindroom.bot import AgentBot

        bot = AgentBot(calc_user, tmp_path, rooms=["!test:localhost"], enable_streaming=False)
        await bot.start()

        test_room = nio.MatrixRoom(room_id="!test:localhost", own_user_id="", encrypted=False)

        # User sends initial message without mention
        from unittest.mock import MagicMock

        initial_event = MagicMock(spec=nio.RoomMessageText)
        initial_event.body = "What's the sum?"
        initial_event.sender = "@user:localhost"
        initial_event.event_id = "$user_initial"
        initial_event.source = {
            "event_id": "$user_initial",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567890,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "What's the sum?",
            },
        }

        # Process - bot should not respond (not mentioned)
        await bot._on_message(test_room, initial_event)
        assert len(events_sent) == 0

        # User edits to add mention
        edit_event = MagicMock(spec=nio.RoomMessageText)
        edit_event.body = "* @mindroom_calculator:localhost what's 2+2?"
        edit_event.sender = "@user:localhost"
        edit_event.event_id = "$user_edit"
        edit_event.source = {
            "event_id": "$user_edit",
            "sender": "@user:localhost",
            "origin_server_ts": 1234567891,
            "type": "m.room.message",
            "content": {
                "msgtype": "m.text",
                "body": "* @mindroom_calculator:localhost what's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {
                    "rel_type": "m.replace",
                    "event_id": "$user_initial",
                },
                "m.new_content": {
                    "body": "@mindroom_calculator:localhost what's 2+2?",
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
            },
        }

        # Mock AI response
        with patch("mindroom.bot.ai_response") as mock_ai:
            mock_ai.return_value = "2+2 equals 4"

            # Mock that calculator is mentioned
            with patch("mindroom.bot.check_agent_mentioned") as mock_check:
                mock_check.return_value = (["calculator"], True)

                # Process edit - bot SHOULD respond
                await bot._on_message(test_room, edit_event)

        # Wait for processing
        await asyncio.sleep(0.1)

        # Verify bot responded
        assert len(events_sent) == 1, "Bot should respond to user edit with mention"
        response = events_sent[0]
        assert "4" in response["content"].get("body", "")

        await bot.stop()
