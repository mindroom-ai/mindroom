"""End-to-end tests for the multi-agent bot system."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.config.agent import AgentConfig
from mindroom.config.auth import AuthorizationConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig
from mindroom.constants import STREAM_STATUS_KEY, RuntimePaths, resolve_runtime_paths
from mindroom.matrix.cache.thread_history_result import thread_history_result
from mindroom.matrix.client import ResolvedVisibleMessage
from mindroom.matrix.users import AgentMatrixUser
from mindroom.media_inputs import MediaInputs
from mindroom.orchestrator import MultiAgentOrchestrator
from mindroom.teams import TeamMode
from tests.conftest import (
    TEST_ACCESS_TOKEN,
    TEST_PASSWORD,
    bind_runtime_paths,
    install_runtime_cache_support,
    make_matrix_client_mock,
    patch_response_runner_module,
    runtime_paths_for,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path


def _runtime_paths(storage_path: Path) -> RuntimePaths:
    config_path = storage_path / "config.yaml"
    config_path.write_text("router:\n  model: default\n", encoding="utf-8")
    return resolve_runtime_paths(config_path=config_path, storage_path=storage_path, process_env={})


def _make_config(storage_path: Path) -> Config:
    config = bind_runtime_paths(
        Config(
            agents={
                "calculator": AgentConfig(display_name="CalculatorAgent", rooms=["!test:localhost"]),
                "general": AgentConfig(display_name="GeneralAgent", rooms=["!test:localhost"]),
            },
            teams={},
            models={"default": ModelConfig(provider="test", id="test-model")},
            authorization=AuthorizationConfig(default_room_access=True),
        ),
        _runtime_paths(storage_path),
    )
    config.memory.backend = "file"
    return config


def _visible_message(*, sender: str, body: str, event_id: str, timestamp: int) -> ResolvedVisibleMessage:
    """Build one typed visible message for thread-history tests."""
    return ResolvedVisibleMessage.synthetic(
        sender=sender,
        body=body,
        event_id=event_id,
        timestamp=timestamp,
    )


@pytest.fixture
def mock_calculator_agent() -> AgentMatrixUser:
    """Create a mock calculator agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        user_id="@mindroom_calculator:localhost",
        display_name="CalculatorAgent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )


@pytest.fixture
def mock_general_agent() -> AgentMatrixUser:
    """Create a mock general agent user."""
    return AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="GeneralAgent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )


@pytest.mark.asyncio
@patch("mindroom.conversation_resolver.ConversationResolver.fetch_thread_history")
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
        mock_client.add_response_callback = MagicMock()
        mock_client.user_id = mock_calculator_agent.user_id
        mock_client.access_token = mock_calculator_agent.access_token
        mock_login.return_value = mock_client

        config = _make_config(tmp_path)

        bot = AgentBot(mock_calculator_agent, tmp_path, config, runtime_paths_for(config), rooms=[test_room_id])
        bot.client = mock_client
        install_runtime_cache_support(bot)
        bot._conversation_cache.get_thread_history = AsyncMock(return_value=[])
        bot._conversation_cache.get_thread_snapshot = AsyncMock(
            return_value=thread_history_result([], is_full_history=False),
        )
        bot.running = True

        # Create a message mentioning the calculator agent
        message_body = f"@mindroom_calculator:{config.get_domain(runtime_paths_for(config))} What's 15% of 200?"
        message_event = nio.RoomMessageText(
            body=message_body,
            formatted_body=message_body,
            format="org.matrix.custom.html",
            source={
                "content": {
                    "msgtype": "m.text",
                    "body": message_body,
                    "m.mentions": {
                        "user_ids": [f"@mindroom_calculator:{config.get_domain(runtime_paths_for(config))}"],
                    },
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

        async def mock_streaming_response() -> AsyncGenerator[str, None]:
            yield "15% of 200 is 30"

        mock_ai = AsyncMock(return_value=mock_streaming_response())
        with (
            patch(
                "mindroom.delivery_gateway.send_streaming_response",
                new_callable=AsyncMock,
            ) as mock_send_streaming_response,
            patch_response_runner_module(
                stream_agent_response=mock_ai,
                should_use_streaming=AsyncMock(return_value=True),
            ),
        ):
            mock_send_streaming_response.return_value = ("$response", "15% of 200 is 30")
            await bot._on_message(room, message_event)

        # Verify AI was called with correct parameters (full message body as prompt)
        mock_ai.assert_called_once()
        ai_kwargs = mock_ai.call_args.kwargs
        assert ai_kwargs["agent_name"] == "calculator"
        assert ai_kwargs["prompt"].startswith("[")
        assert ai_kwargs["prompt"].endswith(
            f"@mindroom_calculator:{config.get_domain(runtime_paths_for(config))} What's 15% of 200?",
        )
        assert ai_kwargs["session_id"] == f"{test_room_id}:$thread_root:localhost"
        assert ai_kwargs["thread_history"] == []
        assert ai_kwargs["runtime_paths"].storage_root == runtime_paths_for(config).storage_root
        assert ai_kwargs["config"] == config
        assert ai_kwargs["room_id"] == test_room_id
        assert ai_kwargs["knowledge"] is None
        assert ai_kwargs["user_id"] == test_user_id
        assert ai_kwargs["media"] == MediaInputs()
        assert ai_kwargs["reply_to_event_id"] == "$test_event:localhost"
        assert ai_kwargs["show_tool_calls"] is True
        assert ai_kwargs["run_metadata_collector"] == {}

        mock_send_streaming_response.assert_awaited_once()
        send_args = mock_send_streaming_response.await_args.args
        assert send_args[1] == test_room_id
        assert send_args[2] == "$test_event:localhost"
        assert send_args[3] == "$thread_root:localhost"


@pytest.mark.asyncio
async def test_agent_ignores_other_agents(
    mock_calculator_agent: AgentMatrixUser,
    mock_general_agent: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """Test that agents ignore messages from other agents."""
    test_room_id = "!test:localhost"

    with patch("mindroom.bot.login_agent_user") as mock_login:
        mock_client = make_matrix_client_mock(user_id=mock_calculator_agent.user_id)
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client

        config = _make_config(tmp_path)

        bot = AgentBot(mock_calculator_agent, tmp_path, config, runtime_paths_for(config), rooms=[test_room_id])
        install_runtime_cache_support(bot)
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

        mock_ai = AsyncMock()
        with patch("mindroom.response_runner.stream_agent_response", new=mock_ai):
            await bot._on_message(room, message_event)

            # Should not process the message
            mock_ai.assert_not_called()
            bot.client.room_send.assert_not_called()


@pytest.mark.asyncio
@patch("mindroom.teams.get_agent_knowledge")
@patch("mindroom.teams.create_agent")
@patch("mindroom.teams.Team.arun")
async def test_agent_responds_in_threads_based_on_participation(  # noqa: PLR0915
    mock_team_arun: AsyncMock,
    mock_create_agent: MagicMock,
    mock_get_agent_knowledge: MagicMock,
    mock_calculator_agent: AgentMatrixUser,
    tmp_path: Path,
) -> None:
    """Test that agents respond in threads based on whether other agents are participating."""
    # Create the config first to get the actual domain
    mock_config = _make_config(tmp_path)
    mock_config.models = {"default": ModelConfig(provider="anthropic", id="claude-3-5-haiku-latest")}
    mock_get_agent_knowledge.return_value = None
    fake_member = MagicMock()
    fake_member.name = "MockAgent"
    fake_member.instructions = []
    mock_create_agent.return_value = fake_member

    # Use the actual domain from config (which comes from MATRIX_HOMESERVER env var)
    domain = mock_config.get_domain(runtime_paths_for(mock_config))
    test_room_id = "!test:localhost"  # Room ID can stay as localhost
    test_user_id = f"@alice:{domain}"
    thread_root_id = f"$thread_root:{domain}"

    # Update the mock agent to use the correct domain
    mock_calculator_agent.user_id = f"@mindroom_calculator:{domain}"

    with (
        patch("mindroom.bot.login_agent_user") as mock_login,
        patch("mindroom.config.main.Config.from_yaml", return_value=mock_config),
        patch("mindroom.teams._select_team_mode", new=AsyncMock()) as mock_select_mode,
    ):
        mock_client = make_matrix_client_mock(user_id=mock_calculator_agent.user_id)
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client
        mock_select_mode.return_value = TeamMode.COLLABORATE

        config = _make_config(tmp_path)

        bot = AgentBot(
            mock_calculator_agent,
            tmp_path,
            config,
            runtime_paths_for(config),
            rooms=[test_room_id],
            enable_streaming=False,
        )
        install_runtime_cache_support(bot)

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
        # Thread team resolution depends on visible room membership, not only thread history.
        room.users = {
            mock_calculator_agent.user_id: MagicMock(),
            f"@mindroom_general:{domain}": MagicMock(),
        }

        with (
            patch.object(bot._conversation_cache, "get_thread_snapshot") as mock_fetch_snapshot,
            patch.object(bot._conversation_cache, "get_thread_history") as mock_fetch,
            patch("mindroom.turn_controller.is_dm_room", return_value=False),  # Not a DM room
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
        ):
            # Only this agent in the thread
            thread_history = [
                _visible_message(sender=test_user_id, body="What's 10% of 100?", timestamp=123, event_id="msg1"),
                _visible_message(
                    sender=mock_calculator_agent.user_id,
                    body="10% of 100 is 10",
                    timestamp=124,
                    event_id="msg2",
                ),
            ]
            mock_fetch.return_value = thread_history
            mock_fetch_snapshot.return_value = thread_history_result(thread_history, is_full_history=False)

            mock_ai = AsyncMock(return_value="20% of 300 is 60")
            with patch_response_runner_module(
                ai_response=mock_ai,
                should_use_streaming=AsyncMock(return_value=False),
            ):
                await bot._on_message(room, message_event)

            # Should process the message as only agent in thread
            mock_ai.assert_called_once()
            # With stop button support: placeholder + reaction + final
            assert bot.client.room_send.call_count >= 2

        # Test 2: Thread with multiple agents - should form team and respond
        bot.client.room_send.reset_mock()
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
            patch.object(bot._conversation_cache, "get_thread_snapshot") as mock_fetch_snapshot,
            patch.object(bot._conversation_cache, "get_thread_history") as mock_fetch,
            patch("mindroom.turn_controller.is_dm_room", return_value=False),  # Not a DM room
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
        ):
            # Multiple agents in the thread
            thread_history = [
                _visible_message(sender=test_user_id, body="What's 10% of 100?", timestamp=123, event_id="msg1"),
                _visible_message(
                    sender=mock_calculator_agent.user_id,
                    body="10% of 100 is 10",
                    timestamp=124,
                    event_id="msg2",
                ),
                _visible_message(
                    sender=f"@mindroom_general:{domain}",
                    body="I can also help",
                    timestamp=125,
                    event_id="msg3",
                ),
            ]
            mock_fetch.return_value = thread_history
            mock_fetch_snapshot.return_value = thread_history_result(thread_history, is_full_history=False)
            bot.client.room_send.side_effect = [
                nio.RoomSendResponse.from_dict({"event_id": "$placeholder"}, test_room_id),
                nio.RoomSendResponse.from_dict({"event_id": "$edit"}, test_room_id),
            ]

            mock_ai = AsyncMock()
            mock_team_response = AsyncMock(return_value="Team response")
            with patch_response_runner_module(
                ai_response=mock_ai,
                team_response=mock_team_response,
                should_use_streaming=AsyncMock(return_value=False),
            ):
                await bot._on_message(room, message_event_2)

            # Should form team and send team response when multiple agents in thread
            mock_ai.assert_not_called()
            mock_team_response.assert_awaited_once()
            assert bot.client.room_send.call_count == 2
            placeholder_content = bot.client.room_send.call_args_list[0].kwargs["content"]
            final_content = bot.client.room_send.call_args_list[1].kwargs["content"]
            assert placeholder_content[STREAM_STATUS_KEY] == "pending"
            assert placeholder_content["body"].startswith("🤝 Team Response: Thinking...")
            assert final_content["m.relates_to"]["rel_type"] == "m.replace"
            assert final_content["m.relates_to"]["event_id"] == "$placeholder"
            assert final_content["m.new_content"]["body"] != placeholder_content["body"]
            bot.client.room_send.side_effect = None

        # Reset mocks for Test 3
        bot.client.room_send.reset_mock()
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
            patch.object(bot._conversation_cache, "get_thread_snapshot") as mock_fetch_snapshot,
            patch.object(bot._conversation_cache, "get_thread_history") as mock_fetch,
            patch("mindroom.turn_controller.is_dm_room", return_value=False),  # Not a DM room
            patch("mindroom.turn_controller.interactive.handle_text_response", new=AsyncMock(return_value=None)),
        ):
            thread_history = [
                _visible_message(sender=test_user_id, body="What's 10% of 100?", timestamp=123, event_id="msg1"),
                _visible_message(
                    sender=mock_calculator_agent.user_id,
                    body="10% of 100 is 10",
                    timestamp=124,
                    event_id="msg2",
                ),
                _visible_message(
                    sender=f"@mindroom_general:{domain}",
                    body="I can also help",
                    timestamp=125,
                    event_id="msg3",
                ),
            ]
            mock_fetch.return_value = thread_history
            mock_fetch_snapshot.return_value = thread_history_result(thread_history, is_full_history=False)

            mock_ai = AsyncMock(return_value="20% of 300 is 60")
            with patch_response_runner_module(
                ai_response=mock_ai,
                should_use_streaming=AsyncMock(return_value=False),
            ):
                await bot._on_message(room, message_event_with_mention)

            # Should process the message with explicit mention
            mock_ai.assert_called_once()
            ai_kwargs = mock_ai.call_args.kwargs
            assert ai_kwargs["agent_name"] == "calculator"
            assert ai_kwargs["prompt"].startswith("[")
            assert ai_kwargs["prompt"].endswith(f"@mindroom_calculator:{domain} What about 20% of 300?")
            assert ai_kwargs["session_id"] == f"{test_room_id}:{thread_root_id}"
            assert ai_kwargs["thread_history"][0].body.startswith("[")
            assert ai_kwargs["thread_history"][0].body.endswith("What's 10% of 100?")
            assert ai_kwargs["thread_history"][1].body == "10% of 100 is 10"
            assert ai_kwargs["thread_history"][2].body == "I can also help"
            assert ai_kwargs["runtime_paths"].storage_root == runtime_paths_for(config).storage_root
            assert ai_kwargs["config"] == config
            assert ai_kwargs["room_id"] == test_room_id
            assert ai_kwargs["knowledge"] is None
            assert ai_kwargs["user_id"] == test_user_id
            assert ai_kwargs["media"] == MediaInputs()
            assert ai_kwargs["reply_to_event_id"] == f"$test_event2:{domain}"
            assert ai_kwargs["show_tool_calls"] is True
            assert ai_kwargs["tool_trace_collector"] == []
            assert ai_kwargs["run_metadata_collector"] == {}

            # Verify a response stays in the target thread.
            sent_contents = [call.kwargs["content"] for call in bot.client.room_send.call_args_list]
            assert len(sent_contents) >= 2
            assert any(
                content.get("m.relates_to", {}).get("rel_type") == "m.thread"
                and content.get("m.relates_to", {}).get("event_id") == thread_root_id
                for content in sent_contents
            )


@pytest.mark.asyncio
@pytest.mark.requires_matrix  # Requires real Matrix server for multi-agent orchestration
@pytest.mark.timeout(10)  # Add timeout to prevent hanging on real server connection
async def test_orchestrator_manages_multiple_agents(tmp_path: Path) -> None:
    """Test that the orchestrator manages multiple agents correctly."""
    with patch("mindroom.matrix.users._ensure_all_agent_users") as mock_ensure:
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
        with patch("mindroom.config.main.Config.from_yaml") as mock_from_yaml:
            mock_config = MagicMock()
            mock_config.agents = {
                "calculator": MagicMock(display_name="CalculatorAgent", rooms=["room1"]),
                "general": MagicMock(display_name="GeneralAgent", rooms=["room1"]),
            }
            mock_config.teams = {}
            mock_from_yaml.return_value = mock_config

            with patch("mindroom.orchestrator.MultiAgentOrchestrator._ensure_user_account", new=AsyncMock()):
                orchestrator = MultiAgentOrchestrator(runtime_paths=_runtime_paths(tmp_path))
                await orchestrator.initialize()

                # Verify agents were created (2 agents + 1 router)
                assert len(orchestrator.agent_bots) == 3
                assert "calculator" in orchestrator.agent_bots
                assert "general" in orchestrator.agent_bots
                assert "router" in orchestrator.agent_bots

        # Test that agents can be started
        with (
            patch("mindroom.bot.login_agent_user") as mock_login,
            patch("mindroom.bot.AgentBot.ensure_user_account", new=AsyncMock()),
        ):
            mock_client = AsyncMock()
            mock_client.add_event_callback = MagicMock()
            mock_client.add_response_callback = MagicMock()
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
        mock_client = make_matrix_client_mock(user_id=mock_calculator_agent.user_id)
        mock_client.user_id = mock_calculator_agent.user_id
        mock_login.return_value = mock_client

        config = _make_config(tmp_path)

        bot = AgentBot(mock_calculator_agent, tmp_path, config, runtime_paths_for(config), rooms=[initial_room])
        install_runtime_cache_support(bot)
        await bot.start()

        # Create invite event for a different room
        mock_room = MagicMock()
        mock_room.room_id = invite_room
        mock_room.display_name = "Invite Room"
        mock_event = MagicMock(spec=nio.InviteEvent)
        mock_event.sender = "@inviter:localhost"

        await bot._on_invite(mock_room, mock_event)

        # Verify new room was joined (not the initial room)
        bot.client.join.assert_called_with(invite_room)
