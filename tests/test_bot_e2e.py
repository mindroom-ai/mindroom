"""Proper end-to-end tests for the minimal Matrix AI bot.

These tests verify the bot's actual behavior using HTTP mocking via aioresponses.
We test the full flow from receiving events to making HTTP calls.
"""

import re
from unittest.mock import patch

import pytest
from aioresponses import aioresponses
from nio import (
    InviteMemberEvent,
    MatrixRoom,
    RoomMessageText,
)

from mindroom.bot import Bot

from .test_helpers import mock_room_messages_empty


@pytest.mark.asyncio
async def test_bot_processes_message_and_sends_response() -> None:
    """Test that the bot processes a message and sends a response via HTTP.

    This is a true end-to-end test that verifies:
    1. Bot receives a message event
    2. Bot parses the mention and extracts the prompt
    3. Bot calls the AI service (mocked)
    4. Bot sends the response via actual HTTP call (mocked)
    """
    homeserver = "https://matrix.example.org"
    bot_user_id = "@bot:example.org"
    test_room_id = "!test:example.org"
    test_user_id = "@alice:example.org"

    with (
        patch("mindroom.matrix.MATRIX_HOMESERVER", homeserver),
        patch("mindroom.matrix.MATRIX_USER_ID", bot_user_id),
        patch("mindroom.matrix.MATRIX_PASSWORD", "password"),
        patch("os.path.exists", return_value=True),
    ):
        bot = Bot()

        # Set up the bot as already logged in
        bot.client.access_token = "test_token"
        bot.client.user_id = bot_user_id
        bot.client.user = "bot"

        # Create a message event
        message_body = f"{bot_user_id} Hello, can you help me?"
        message_event = RoomMessageText(
            body=message_body,
            formatted_body=message_body,
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": message_body,
                },
                "event_id": "$test_event:example.org",
                "sender": test_user_id,
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        message_event.sender = test_user_id

        room = MatrixRoom(test_room_id, bot_user_id)

        try:
            with aioresponses() as m:
                # Mock the HTTP endpoint for sending messages
                # nio appends a transaction ID to the URL
                m.put(
                    re.compile(rf".*{re.escape(test_room_id)}/send/m\.room\.message/.*"),
                    status=200,
                    payload={"event_id": "$response_event:example.org"},
                )

                # Mock the AI response
                with patch("mindroom.bot.ai_response") as mock_ai:
                    mock_ai.return_value = "I'd be happy to help!"

                    # Process the message
                    await bot._on_message(room, message_event)

                    # Verify AI was called with correct parameters
                    mock_ai.assert_called_once_with(
                        "general",  # Default agent
                        "Hello, can you help me?",
                        test_room_id,
                        thread_history=[],  # No thread history for non-thread message
                    )

                    # Verify HTTP call was made
                    assert len(m.requests) == 1

                    # Check the URL
                    key = next(iter(m.requests.keys()))
                    method, url = key
                    assert method == "PUT"
                    assert test_room_id in str(url)
                    assert "send/m.room.message" in str(url)

        finally:
            await bot.client.close()


@pytest.mark.asyncio
async def test_bot_handles_room_invite() -> None:
    """Test that the bot accepts room invites."""
    homeserver = "https://matrix.example.org"
    bot_user_id = "@bot:example.org"
    test_room_id = "!test:example.org"
    inviter_id = "@inviter:example.org"

    with (
        patch("mindroom.matrix.MATRIX_HOMESERVER", homeserver),
        patch("mindroom.matrix.MATRIX_USER_ID", bot_user_id),
        patch("mindroom.matrix.MATRIX_PASSWORD", "password"),
        patch("os.path.exists", return_value=True),
    ):
        bot = Bot()

        # Set up as logged in
        bot.client.access_token = "test_token"
        bot.client.user_id = bot_user_id

        # Create invite event
        invite_event = InviteMemberEvent(
            source={
                "type": "m.room.member",
                "state_key": bot_user_id,
                "content": {"membership": "invite"},
                "sender": inviter_id,
            },
            sender=inviter_id,
            state_key=bot_user_id,
            membership="invite",
            prev_membership=None,
            content={"membership": "invite"},
            prev_content=None,
        )

        room = MatrixRoom(test_room_id, bot_user_id)

        # Mock the join method
        join_called = False
        join_room_id = None

        async def mock_join(room_id):
            nonlocal join_called, join_room_id
            join_called = True
            join_room_id = room_id
            from nio import JoinResponse

            return JoinResponse(room_id=room_id)

        bot.client.join = mock_join

        try:
            # Process the invite
            await bot._on_invite(room, invite_event)

            # Verify join was called with correct room ID
            assert join_called, "Bot did not attempt to join the room"
            assert join_room_id == test_room_id, f"Bot tried to join {join_room_id} instead of {test_room_id}"

        finally:
            await bot.client.close()


@pytest.mark.asyncio
async def test_bot_preserves_thread_context() -> None:
    """Test that bot preserves thread context when responding."""
    homeserver = "https://matrix.example.org"
    bot_user_id = "@bot:example.org"
    test_room_id = "!test:example.org"
    thread_root_id = "$thread_root:example.org"

    with (
        patch("mindroom.matrix.MATRIX_HOMESERVER", homeserver),
        patch("mindroom.matrix.MATRIX_USER_ID", bot_user_id),
        patch("mindroom.matrix.MATRIX_PASSWORD", "password"),
        patch("os.path.exists", return_value=True),
    ):
        bot = Bot()
        bot.client.access_token = "test_token"
        bot.client.user_id = bot_user_id
        bot.client.user = "bot"

        # Create threaded message
        thread_message = RoomMessageText(
            body=f"{bot_user_id} What is Python?",
            formatted_body=f"{bot_user_id} What is Python?",
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": f"{bot_user_id} What is Python?",
                    "m.relates_to": {
                        "event_id": thread_root_id,
                        "rel_type": "m.thread",
                        "is_falling_back": True,
                        "m.in_reply_to": {"event_id": thread_root_id},
                    },
                },
                "event_id": "$thread_msg:example.org",
                "sender": "@user:example.org",
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        thread_message.sender = "@user:example.org"

        room = MatrixRoom(test_room_id, bot_user_id)

        sent_content = None

        # Capture the request data
        async def capture_request(url, **kwargs):
            nonlocal sent_content
            import json

            if "data" in kwargs and isinstance(kwargs["data"], str):
                sent_content = json.loads(kwargs["data"])
            else:
                sent_content = kwargs.get("json", {})
            # Return a CallbackResult with the response data
            from aioresponses import CallbackResult

            return CallbackResult(
                status=200,
                payload={"event_id": "$response:example.org"},
            )

        try:
            with aioresponses() as m:
                # Mock with callback to capture request
                m.put(
                    re.compile(rf".*{re.escape(test_room_id)}/send/m\.room\.message/.*"),
                    status=200,
                    callback=capture_request,
                )

                with patch("mindroom.bot.ai_response") as mock_ai:
                    mock_ai.return_value = "Python is a programming language."

                    # Mock room_messages to return empty thread history
                    mock_room_messages_empty(bot)

                    await bot._on_message(room, thread_message)

                    # Verify thread context is preserved
                    assert sent_content is not None
                    assert "m.relates_to" in sent_content
                    assert sent_content["m.relates_to"]["event_id"] == thread_root_id
                    assert sent_content["m.relates_to"]["rel_type"] == "m.thread"

        finally:
            await bot.client.close()


@pytest.mark.asyncio
async def test_bot_ignores_own_messages() -> None:
    """Test that bot ignores its own messages."""
    homeserver = "https://matrix.example.org"
    bot_user_id = "@bot:example.org"
    test_room_id = "!test:example.org"

    with (
        patch("mindroom.matrix.MATRIX_HOMESERVER", homeserver),
        patch("mindroom.matrix.MATRIX_USER_ID", bot_user_id),
        patch("mindroom.matrix.MATRIX_PASSWORD", "password"),
        patch("os.path.exists", return_value=True),
    ):
        bot = Bot()
        bot.client.access_token = "test_token"
        bot.client.user_id = bot_user_id

        # Create message from bot itself
        self_message = RoomMessageText(
            body="I am the bot",
            formatted_body="I am the bot",
            format="org.matrix.custom.html",
            source={
                "content": {"msgtype": "m.text", "body": "I am the bot"},
                "event_id": "$self_msg:example.org",
                "sender": bot_user_id,  # From bot itself
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        self_message.sender = bot_user_id

        room = MatrixRoom(test_room_id, bot_user_id)

        try:
            with aioresponses() as m:  # noqa: SIM117
                # No HTTP mocks needed - bot should ignore the message

                with patch("mindroom.bot.ai_response") as mock_ai:
                    await bot._on_message(room, self_message)

                    # Verify AI was NOT called
                    mock_ai.assert_not_called()

                    # Verify no HTTP calls were made
                    assert len(m.requests) == 0

        finally:
            await bot.client.close()


@pytest.mark.asyncio
async def test_bot_routes_to_different_agents() -> None:
    """Test that bot routes to different agents based on mentions."""
    homeserver = "https://matrix.example.org"
    bot_user_id = "@bot:example.org"
    test_room_id = "!test:example.org"

    test_cases = [
        ("@calculator: 2 + 2", "calculator", "2 + 2"),
        ("@research: quantum physics", "research", "quantum physics"),
        (f"{bot_user_id} hello", "general", "hello"),
    ]

    with (
        patch("mindroom.matrix.MATRIX_HOMESERVER", homeserver),
        patch("mindroom.matrix.MATRIX_USER_ID", bot_user_id),
        patch("mindroom.matrix.MATRIX_PASSWORD", "password"),
        patch("os.path.exists", return_value=True),
    ):
        bot = Bot()
        bot.client.access_token = "test_token"
        bot.client.user_id = bot_user_id
        bot.client.user = "bot"

        room = MatrixRoom(test_room_id, bot_user_id)

        try:
            for message_body, expected_agent, expected_prompt in test_cases:
                with aioresponses() as m:
                    # Mock message send
                    m.put(
                        re.compile(rf".*{re.escape(test_room_id)}/send/m\.room\.message/.*"),
                        status=200,
                        payload={"event_id": "$resp:example.org"},
                    )

                    message = RoomMessageText(
                        body=message_body,
                        formatted_body=message_body,
                        format="org.matrix.custom.html",
                        source={
                            "content": {"msgtype": "m.text", "body": message_body},
                            "event_id": f"$msg_{expected_agent}:example.org",
                            "sender": "@user:example.org",
                            "origin_server_ts": 1234567890,
                            "type": "m.room.message",
                        },
                    )
                    message.sender = "@user:example.org"

                    with patch("mindroom.bot.ai_response") as mock_ai:
                        mock_ai.return_value = f"Response from {expected_agent}"

                        await bot._on_message(room, message)

                        # Verify correct agent routing
                        mock_ai.assert_called_once_with(
                            expected_agent,
                            expected_prompt,
                            test_room_id,
                            thread_history=[],  # No thread history for non-thread messages
                        )

                        # Verify HTTP call
                        assert len(m.requests) == 1

        finally:
            await bot.client.close()
