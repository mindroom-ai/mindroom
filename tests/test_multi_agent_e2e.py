"""End-to-end tests for the multi-agent bot system."""

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from aioresponses import aioresponses

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.matrix import AgentMatrixUser


@pytest.fixture
def mock_calculator_agent() -> AgentMatrixUser:
    """Create a mock calculator agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        user_id="@mindroom_calculator:localhost",
        display_name="CalculatorAgent",
        password="calc_pass",
        access_token="calc_token",
    )


@pytest.fixture
def mock_general_agent() -> AgentMatrixUser:
    """Create a mock general agent user."""
    return AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password="gen_pass",
        access_token="gen_token",
    )


@pytest.mark.asyncio
async def test_agent_processes_direct_mention(mock_calculator_agent: AgentMatrixUser, tmp_path: Path) -> None:
    """Test that an agent processes messages where it's directly mentioned."""
    test_room_id = "!test:example.org"
    test_user_id = "@alice:example.org"

    with patch("mindroom.bot.login_agent_user") as mock_login:
        # Mock the client
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_client.access_token = mock_calculator_agent.access_token
        mock_login.return_value = mock_client

        bot = AgentBot(mock_calculator_agent, tmp_path)
        await bot.start()

        # Create a message mentioning the calculator agent
        message_body = "@mindroom_calculator:localhost What's 15% of 200?"
        message_event = nio.RoomMessageText(
            body=message_body,
            formatted_body=message_body,
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": message_body,
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
                "event_id": "$test_event:example.org",
                "sender": test_user_id,
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        message_event.sender = test_user_id

        room = nio.MatrixRoom(test_room_id, mock_calculator_agent.user_id)

        with aioresponses() as m:
            # Mock the HTTP endpoint for sending messages
            m.put(
                re.compile(rf".*{re.escape(test_room_id)}/send/m\.room\.message/.*"),
                status=200,
                payload={"event_id": "$response_event:example.org"},
            )

            # Mock the AI response
            with patch("mindroom.bot.ai_response") as mock_ai:
                mock_ai.return_value = "15% of 200 is 30"

                # Process the message
                await bot._on_message(room, message_event)

                # Verify AI was called with correct parameters (full message body as prompt)
                mock_ai.assert_called_once_with(
                    agent_name="calculator",
                    prompt="@mindroom_calculator:localhost What's 15% of 200?",
                    session_id=test_room_id,
                    thread_history=[],
                    storage_path=tmp_path,
                )

                # Verify message was sent
                bot.client.room_send.assert_called_once()
                call_args = bot.client.room_send.call_args
                assert call_args[1]["room_id"] == test_room_id
                assert call_args[1]["content"]["body"] == "15% of 200 is 30"


@pytest.mark.asyncio
async def test_agent_ignores_other_agents(
    mock_calculator_agent: AgentMatrixUser,
    mock_general_agent: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """Test that agents ignore messages from other agents."""
    test_room_id = "!test:example.org"

    with patch("mindroom.bot.login_agent_user") as mock_login:
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client

        bot = AgentBot(mock_calculator_agent, tmp_path)
        await bot.start()

        # Create a message from another agent
        message_event = nio.RoomMessageText(
            body="Hello from general agent",
            formatted_body="Hello from general agent",
            format="org.matrix.custom.html",
            source={
                "content": {"msgtype": "m.text", "body": "Hello from general agent"},
                "event_id": "$test_event:example.org",
                "sender": mock_general_agent.user_id,
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        message_event.sender = mock_general_agent.user_id

        room = nio.MatrixRoom(test_room_id, mock_calculator_agent.user_id)

        with patch("mindroom.bot.ai_response") as mock_ai:
            await bot._on_message(room, message_event)

            # Should not process the message
            mock_ai.assert_not_called()
            bot.client.room_send.assert_not_called()


@pytest.mark.asyncio
async def test_agent_responds_in_threads_based_on_participation(
    mock_calculator_agent: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """Test that agents respond in threads based on whether other agents are participating."""
    test_room_id = "!test:example.org"
    test_user_id = "@alice:example.org"
    thread_root_id = "$thread_root:example.org"

    with patch("mindroom.bot.login_agent_user") as mock_login:
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client

        bot = AgentBot(mock_calculator_agent, tmp_path)
        await bot.start()

        # Test 1: Thread with only this agent - should respond without mention
        message_event = nio.RoomMessageText(
            body="What about 20% of 300?",
            formatted_body="What about 20% of 300?",
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": "What about 20% of 300?",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": thread_root_id,
                    },
                },
                "event_id": "$test_event:example.org",
                "sender": test_user_id,
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        message_event.sender = test_user_id

        room = nio.MatrixRoom(test_room_id, mock_calculator_agent.user_id)

        with patch("mindroom.bot.ai_response") as mock_ai, patch("mindroom.bot.fetch_thread_history") as mock_fetch:
            # Only this agent in the thread
            mock_fetch.return_value = [
                {"sender": test_user_id, "body": "What's 10% of 100?", "timestamp": 123, "event_id": "msg1"},
                {
                    "sender": mock_calculator_agent.user_id,
                    "body": "10% of 100 is 10",
                    "timestamp": 124,
                    "event_id": "msg2",
                },
            ]
            mock_ai.return_value = "20% of 300 is 60"

            await bot._on_message(room, message_event)

            # Should process the message as only agent in thread
            mock_ai.assert_called_once()
            bot.client.room_send.assert_called_once()

        # Test 2: Thread with multiple agents - should NOT respond without mention
        bot.client.room_send.reset_mock()

        with patch("mindroom.bot.ai_response") as mock_ai, patch("mindroom.bot.fetch_thread_history") as mock_fetch:
            # Multiple agents in the thread
            mock_fetch.return_value = [
                {"sender": test_user_id, "body": "What's 10% of 100?", "timestamp": 123, "event_id": "msg1"},
                {
                    "sender": mock_calculator_agent.user_id,
                    "body": "10% of 100 is 10",
                    "timestamp": 124,
                    "event_id": "msg2",
                },
                {
                    "sender": "@mindroom_general:localhost",
                    "body": "I can also help",
                    "timestamp": 125,
                    "event_id": "msg3",
                },
            ]

            await bot._on_message(room, message_event)

            # Should NOT process without mention when multiple agents
            mock_ai.assert_not_called()
            bot.client.room_send.assert_not_called()

        # Test 3: Thread with multiple agents WITH mention - should respond
        message_event_with_mention = nio.RoomMessageText(
            body="@mindroom_calculator:localhost What about 20% of 300?",
            formatted_body="@mindroom_calculator:localhost What about 20% of 300?",
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": "@mindroom_calculator:localhost What about 20% of 300?",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": thread_root_id,
                    },
                    "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                },
                "event_id": "$test_event2:example.org",
                "sender": test_user_id,
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        message_event_with_mention.sender = test_user_id

        with patch("mindroom.bot.ai_response") as mock_ai, patch("mindroom.bot.fetch_thread_history") as mock_fetch:
            mock_fetch.return_value = [
                {"sender": test_user_id, "body": "What's 10% of 100?", "timestamp": 123, "event_id": "msg1"},
                {
                    "sender": mock_calculator_agent.user_id,
                    "body": "10% of 100 is 10",
                    "timestamp": 124,
                    "event_id": "msg2",
                },
                {
                    "sender": "@mindroom_general:localhost",
                    "body": "I can also help",
                    "timestamp": 125,
                    "event_id": "msg3",
                },
            ]
            mock_ai.return_value = "20% of 300 is 60"

            await bot._on_message(room, message_event_with_mention)

            # Should process the message with explicit mention
            mock_ai.assert_called_once_with(
                agent_name="calculator",
                prompt="@mindroom_calculator:localhost What about 20% of 300?",
                session_id=f"{test_room_id}:{thread_root_id}",
                thread_history=mock_fetch.return_value,
                storage_path=tmp_path,
            )

            # Verify thread response format
            bot.client.room_send.assert_called_once()
            sent_content = bot.client.room_send.call_args[1]["content"]
            assert sent_content["m.relates_to"]["rel_type"] == "m.thread"
            assert sent_content["m.relates_to"]["event_id"] == thread_root_id


@pytest.mark.skip(reason="Test hangs during collection - needs investigation")
@pytest.mark.asyncio
async def test_orchestrator_manages_multiple_agents(tmp_path: Path) -> None:
    """Test that the orchestrator manages multiple agents correctly."""
    with patch("mindroom.bot.ensure_all_agent_users") as mock_ensure:
        # Mock agent users
        mock_agents = {
            "calculator": AgentMatrixUser(
                agent_name="calculator",
                user_id="@mindroom_calculator:localhost",
                display_name="CalculatorAgent",
                password="calc_pass",
            ),
            "general": AgentMatrixUser(
                agent_name="general",
                user_id="@mindroom_general:localhost",
                display_name="GeneralAgent",
                password="gen_pass",
            ),
        }
        mock_ensure.return_value = mock_agents

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()

        # Verify agents were created
        assert len(orchestrator.agent_bots) == 2
        assert "calculator" in orchestrator.agent_bots
        assert "general" in orchestrator.agent_bots

        # Test starting all agents
        with patch("mindroom.bot.login_agent_user") as mock_login:
            mock_client = AsyncMock()
            mock_client.add_event_callback = MagicMock()
            mock_client.sync_forever = AsyncMock(side_effect=KeyboardInterrupt)
            mock_login.return_value = mock_client

            with pytest.raises(KeyboardInterrupt):
                await orchestrator.start()

            # Verify all agents were started
            assert mock_login.call_count == 2
            assert orchestrator.running


@pytest.mark.asyncio
async def test_orchestrator_invites_agents_to_room(tmp_path: Path) -> None:
    """Test that the orchestrator can invite all agents to a room."""
    test_room_id = "!test:example.org"

    with patch("mindroom.bot.ensure_all_agent_users") as mock_ensure:
        mock_agents = {
            "calculator": AgentMatrixUser(
                agent_name="calculator",
                user_id="@mindroom_calculator:localhost",
                display_name="CalculatorAgent",
                password="calc_pass",
            ),
            "general": AgentMatrixUser(
                agent_name="general",
                user_id="@mindroom_general:localhost",
                display_name="GeneralAgent",
                password="gen_pass",
            ),
        }
        mock_ensure.return_value = mock_agents

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()

        # Test inviting agents
        mock_inviter_client = AsyncMock()
        await orchestrator.invite_agents_to_room(test_room_id, mock_inviter_client)

        # Verify invites
        assert mock_inviter_client.room_invite.call_count == 2
        invite_calls = mock_inviter_client.room_invite.call_args_list
        invited_users = {call[0][1] for call in invite_calls}
        assert invited_users == {
            "@mindroom_calculator:localhost",
            "@mindroom_general:localhost",
        }


@pytest.mark.asyncio
async def test_agent_handles_room_invite(mock_calculator_agent: AgentMatrixUser, tmp_path: Path) -> None:
    """Test that agents properly handle room invitations."""
    test_room_id = "!test:example.org"

    with patch("mindroom.bot.login_agent_user") as mock_login:
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client

        bot = AgentBot(mock_calculator_agent, tmp_path)
        await bot.start()

        # Create invite event
        mock_room = MagicMock()
        mock_room.room_id = test_room_id
        mock_room.display_name = "Test Room"
        mock_event = MagicMock(spec=nio.InviteEvent)
        mock_event.sender = "@inviter:example.org"

        await bot._on_invite(mock_room, mock_event)

        # Verify room was joined
        bot.client.join.assert_called_once_with(test_room_id)
