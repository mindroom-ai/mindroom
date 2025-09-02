"""End-to-end tests for the multi-agent bot system."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from aioresponses import aioresponses

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.config import AgentConfig, Config, ModelConfig
from mindroom.matrix.users import AgentMatrixUser

from .conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


@pytest.fixture
def mock_calculator_agent() -> AgentMatrixUser:
    """Create a mock calculator agent user."""
    # Import here to get the actual domain from environment
    from mindroom.config import Config  # noqa: PLC0415

    config = Config.from_yaml()
    return AgentMatrixUser(
        agent_name="calculator",
        user_id=f"@mindroom_calculator:{config.domain}",
        display_name="CalculatorAgent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )


@pytest.fixture
def mock_general_agent() -> AgentMatrixUser:
    """Create a mock general agent user."""
    # Import here to get the actual domain from environment
    from mindroom.config import Config  # noqa: PLC0415

    config = Config.from_yaml()
    return AgentMatrixUser(
        agent_name="general",
        user_id=f"@mindroom_general:{config.domain}",
        display_name="GeneralAgent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )


@pytest.mark.asyncio
@patch("mindroom.bot.fetch_thread_history")
async def test_agent_processes_direct_mention(
    mock_fetch_history: AsyncMock,
    mock_calculator_agent: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """Test that an agent processes messages where it's directly mentioned."""
    mock_fetch_history.return_value = []
    test_room_id = "!test:localhost"
    test_user_id = "@alice:localhost"

    with patch("mindroom.bot.login_agent_user") as mock_login:
        # Mock the client
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_client.access_token = mock_calculator_agent.access_token
        mock_login.return_value = mock_client

        config = Config.from_yaml()

        bot = AgentBot(mock_calculator_agent, tmp_path, config, rooms=[test_room_id])
        await bot.start()

        # Create a message mentioning the calculator agent
        message_body = f"@mindroom_calculator:{config.domain} What's 15% of 200?"
        message_event = nio.RoomMessageText(
            body=message_body,
            formatted_body=message_body,
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": message_body,
                    "m.mentions": {"user_ids": [f"@mindroom_calculator:{config.domain}"]},
                    "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root:localhost"},
                },
                "event_id": "$test_event:localhost",
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
                payload={"event_id": "$response_event:localhost"},
            )

            # Mock the AI response and presence check
            with (
                patch("mindroom.bot.stream_agent_response") as mock_ai,
                patch("mindroom.bot.should_use_streaming", return_value=True),
            ):

                async def mock_streaming_response() -> AsyncGenerator[str, None]:
                    yield "15% of 200 is 30"

                mock_ai.return_value = mock_streaming_response()

                # Process the message
                await bot._on_message(room, message_event)

                # Verify AI was called with correct parameters (full message body as prompt)
                mock_ai.assert_called_once_with(
                    agent_name="calculator",
                    prompt=f"@mindroom_calculator:{config.domain} What's 15% of 200?",
                    session_id=f"{test_room_id}:$thread_root:localhost",
                    thread_history=[],
                    storage_path=tmp_path,
                    config=config,
                    room_id=test_room_id,
                )

                # Verify message was sent (thinking + streaming updates)
                # With streaming: 1 thinking message + streaming updates
                assert bot.client.room_send.call_count >= 1  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_agent_ignores_other_agents(
    mock_calculator_agent: AgentMatrixUser,
    mock_general_agent: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """Test that agents ignore messages from other agents."""
    test_room_id = "!test:localhost"

    with patch("mindroom.bot.login_agent_user") as mock_login:
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client

        config = Config.from_yaml()

        bot = AgentBot(mock_calculator_agent, tmp_path, config, rooms=[test_room_id])
        await bot.start()

        # Create a message from another agent
        message_event = nio.RoomMessageText(
            body="Hello from general agent",
            formatted_body="Hello from general agent",
            format="org.matrix.custom.html",
            source={
                "content": {"msgtype": "m.text", "body": "Hello from general agent"},
                "event_id": "$test_event:localhost",
                "sender": mock_general_agent.user_id,
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        message_event.sender = mock_general_agent.user_id

        room = nio.MatrixRoom(test_room_id, mock_calculator_agent.user_id)

        with patch("mindroom.bot.stream_agent_response") as mock_ai:
            await bot._on_message(room, message_event)

            # Should not process the message
            mock_ai.assert_not_called()
            bot.client.room_send.assert_not_called()  # type: ignore[union-attr]


@pytest.mark.asyncio
@patch("mindroom.teams.Team.arun")
async def test_agent_responds_in_threads_based_on_participation(  # noqa: PLR0915
    mock_team_arun: AsyncMock,
    mock_calculator_agent: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """Test that agents respond in threads based on whether other agents are participating."""
    # Create the config first to get the actual domain
    mock_config = Config(
        agents={
            "calculator": AgentConfig(display_name="Calculator", rooms=["!test:localhost"]),
            "general": AgentConfig(display_name="General", rooms=["!test:localhost"]),
        },
        teams={},
        room_models={},
        models={"default": ModelConfig(provider="anthropic", id="claude-3-5-haiku-latest")},
    )

    # Use the actual domain from config (which comes from MATRIX_HOMESERVER env var)
    domain = mock_config.domain
    test_room_id = "!test:localhost"  # Room ID can stay as localhost
    test_user_id = f"@alice:{domain}"
    thread_root_id = f"$thread_root:{domain}"

    # Update the mock agent to use the correct domain
    mock_calculator_agent.user_id = f"@mindroom_calculator:{domain}"

    with (
        patch("mindroom.bot.login_agent_user") as mock_login,
        patch("mindroom.config.Config.from_yaml", return_value=mock_config),
        patch("mindroom.teams.get_model_instance") as mock_get_model_instance,
    ):
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client

        # Mock get_model_instance to return a mock model
        mock_model = MagicMock()
        mock_get_model_instance.return_value = mock_model

        config = Config.from_yaml()

        bot = AgentBot(mock_calculator_agent, tmp_path, config, rooms=[test_room_id], enable_streaming=False)

        # Mock orchestrator
        mock_orchestrator = MagicMock()
        mock_agent_bot = MagicMock()
        mock_agent_bot.agent = MagicMock()
        mock_orchestrator.agent_bots = {"calculator": mock_agent_bot, "general": mock_agent_bot}
        mock_orchestrator.current_config = mock_config
        mock_orchestrator.config = mock_config  # This is what teams.py uses
        bot.orchestrator = mock_orchestrator
        mock_team_arun.return_value = "Team response"

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
                "event_id": f"$test_event:{domain}",
                "sender": test_user_id,
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        message_event.sender = test_user_id

        room = nio.MatrixRoom(test_room_id, mock_calculator_agent.user_id)
        # Mock room users to include the agent
        room.users = {mock_calculator_agent.user_id: MagicMock()}

        with (
            patch("mindroom.bot.ai_response") as mock_ai,
            patch("mindroom.bot.fetch_thread_history") as mock_fetch,
            patch("mindroom.bot.is_dm_room", return_value=False),  # Not a DM room
            patch("mindroom.bot.interactive.handle_text_response", new=AsyncMock()),  # Mock interactive handler
            patch("mindroom.bot.should_use_streaming", return_value=False),  # No streaming
        ):
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

            # Mock non-streaming response
            mock_ai.return_value = "20% of 300 is 60"

            await bot._on_message(room, message_event)

            # Should process the message as only agent in thread
            mock_ai.assert_called_once()
            # With stop button: The test is mocking room_send incorrectly so only 2 succeed
            assert bot.client.room_send.call_count == 2  # type: ignore[union-attr]

        # Test 2: Thread with multiple agents - should form team and respond
        bot.client.room_send.reset_mock()  # type: ignore[union-attr]
        mock_team_arun.reset_mock()

        # Create a new message event with a different ID for Test 2
        message_event_2 = nio.RoomMessageText(
            body="What about 30% of 400?",
            formatted_body="What about 30% of 400?",
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": "What about 30% of 400?",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": thread_root_id,
                    },
                },
                "event_id": f"$test_event_2:{domain}",  # Different event ID
                "sender": test_user_id,
                "origin_server_ts": 1234567891,
                "type": "m.room.message",
            },
        )
        message_event_2.sender = test_user_id

        with (
            patch("mindroom.bot.ai_response") as mock_ai,
            patch("mindroom.bot.fetch_thread_history") as mock_fetch,
            patch("mindroom.bot.is_dm_room", return_value=False),  # Not a DM room
            patch("mindroom.bot.interactive.handle_text_response", new=AsyncMock()),  # Mock interactive handler
            patch("mindroom.bot.should_use_streaming", return_value=False),  # No streaming
        ):
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
                    "sender": f"@mindroom_general:{domain}",
                    "body": "I can also help",
                    "timestamp": 125,
                    "event_id": "msg3",
                },
            ]

            await bot._on_message(room, message_event_2)

            # Should form team and send team response when multiple agents in thread
            mock_ai.assert_not_called()
            mock_team_arun.assert_called_once()
            assert bot.client.room_send.call_count == 2  # type: ignore[union-attr]  # Team response (thinking + final)

        # Reset mocks for Test 3
        bot.client.room_send.reset_mock()  # type: ignore[union-attr]
        mock_team_arun.reset_mock()

        # Test 3: Thread with multiple agents WITH mention - should respond
        message_event_with_mention = nio.RoomMessageText(
            body=f"@mindroom_calculator:{domain} What about 20% of 300?",
            formatted_body=f"@mindroom_calculator:{domain} What about 20% of 300?",
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": f"@mindroom_calculator:{domain} What about 20% of 300?",
                    "m.relates_to": {
                        "rel_type": "m.thread",
                        "event_id": thread_root_id,
                    },
                    "m.mentions": {"user_ids": [f"@mindroom_calculator:{domain}"]},
                },
                "event_id": f"$test_event2:{domain}",
                "sender": test_user_id,
                "origin_server_ts": 1234567890,
                "type": "m.room.message",
            },
        )
        message_event_with_mention.sender = test_user_id

        with (
            patch("mindroom.bot.ai_response") as mock_ai,
            patch("mindroom.bot.fetch_thread_history") as mock_fetch,
            patch("mindroom.bot.is_dm_room", return_value=False),  # Not a DM room
            patch("mindroom.bot.interactive.handle_text_response", new=AsyncMock()),  # Mock interactive handler
            patch("mindroom.bot.should_use_streaming", return_value=False),  # No streaming
        ):
            mock_fetch.return_value = [
                {"sender": test_user_id, "body": "What's 10% of 100?", "timestamp": 123, "event_id": "msg1"},
                {
                    "sender": mock_calculator_agent.user_id,
                    "body": "10% of 100 is 10",
                    "timestamp": 124,
                    "event_id": "msg2",
                },
                {
                    "sender": f"@mindroom_general:{domain}",
                    "body": "I can also help",
                    "timestamp": 125,
                    "event_id": "msg3",
                },
            ]

            # Mock non-streaming response for mention case
            mock_ai.return_value = "20% of 300 is 60"

            await bot._on_message(room, message_event_with_mention)

            # Should process the message with explicit mention
            mock_ai.assert_called_once_with(
                agent_name="calculator",
                prompt=f"@mindroom_calculator:{domain} What about 20% of 300?",
                session_id=f"{test_room_id}:{thread_root_id}",
                thread_history=mock_fetch.return_value,
                storage_path=tmp_path,
                config=config,
                room_id=test_room_id,
            )

            # Verify thread response format (team response with mocking issue)
            assert bot.client.room_send.call_count == 2  # type: ignore[union-attr]
            sent_content = bot.client.room_send.call_args[1]["content"]  # type: ignore[union-attr]
            assert sent_content["m.relates_to"]["rel_type"] == "m.thread"
            assert sent_content["m.relates_to"]["event_id"] == thread_root_id


@pytest.mark.asyncio
@pytest.mark.requires_matrix  # Requires real Matrix server for multi-agent orchestration
@pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
async def test_orchestrator_manages_multiple_agents(tmp_path: Path) -> None:
    """Test that the orchestrator manages multiple agents correctly."""
    with patch("mindroom.matrix.users.ensure_all_agent_users") as mock_ensure:
        # Mock agent users
        mock_agents = {
            "calculator": AgentMatrixUser(
                agent_name="calculator",
                user_id="@mindroom_calculator:localhost",
                display_name="CalculatorAgent",
                password=TEST_PASSWORD,
            ),
            "general": AgentMatrixUser(
                agent_name="general",
                user_id="@mindroom_general:localhost",
                display_name="GeneralAgent",
                password=TEST_PASSWORD,
            ),
        }
        mock_ensure.return_value = mock_agents

        # Mock the config loading
        with patch("mindroom.config.Config.from_yaml") as mock_from_yaml:
            mock_config = MagicMock()
            mock_config.agents = {
                "calculator": MagicMock(display_name="CalculatorAgent", rooms=["room1"]),
                "general": MagicMock(display_name="GeneralAgent", rooms=["room1"]),
            }
            mock_config.teams = {}
            mock_from_yaml.return_value = mock_config

            orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
            await orchestrator.initialize()

            # Verify agents were created (2 agents + 1 router)
            assert len(orchestrator.agent_bots) == 3
            assert "calculator" in orchestrator.agent_bots
            assert "general" in orchestrator.agent_bots
            assert "router" in orchestrator.agent_bots

        # Test that agents can be started
        with patch("mindroom.bot.login_agent_user") as mock_login:
            mock_client = AsyncMock()
            mock_client.add_event_callback = MagicMock()
            mock_client.user_id = "@mindroom_calculator:localhost"
            mock_client.join = AsyncMock(return_value=nio.JoinResponse(room_id="!test:localhost"))
            # Don't run sync_forever, just verify setup
            mock_client.sync_forever = AsyncMock()
            mock_login.return_value = mock_client

            # Manually start agents without running sync_forever
            for bot in orchestrator.agent_bots.values():
                await bot.start()

            # Verify all agents were started (2 agents + 1 router = 3)
            assert mock_login.call_count == 3
            assert all(bot.running for bot in orchestrator.agent_bots.values())
            assert all(bot.client is not None for bot in orchestrator.agent_bots.values())


@pytest.mark.asyncio
async def test_agent_handles_room_invite(mock_calculator_agent: AgentMatrixUser, tmp_path: Path) -> None:
    """Test that agents properly handle room invitations."""
    initial_room = "!initial:localhost"
    invite_room = "!invite:localhost"

    with patch("mindroom.bot.login_agent_user") as mock_login:
        mock_client = AsyncMock()
        mock_client.add_event_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client

        config = Config.from_yaml()

        bot = AgentBot(mock_calculator_agent, tmp_path, config, rooms=[initial_room])
        await bot.start()

        # Create invite event for a different room
        mock_room = MagicMock()
        mock_room.room_id = invite_room
        mock_room.display_name = "Invite Room"
        mock_event = MagicMock(spec=nio.InviteEvent)
        mock_event.sender = "@inviter:localhost"

        await bot._on_invite(mock_room, mock_event)

        # Verify new room was joined (not the initial room)
        bot.client.join.assert_called_with(invite_room)  # type: ignore[union-attr]
