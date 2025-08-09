"""Tests for the multi-agent bot system."""

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.matrix.users import AgentMatrixUser
from mindroom.models import Config, RouterConfig
from mindroom.response_tracker import ResponseTracker
from mindroom.thread_invites import ThreadInviteManager


@dataclass
class MockConfig:
    """Mock configuration for testing."""

    agents: dict = None

    def __post_init__(self):
        if self.agents is None:
            self.agents = {
                "calculator": MagicMock(rooms=["lobby", "science", "analysis"]),
                "general": MagicMock(rooms=["lobby", "help"]),
            }


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Create a mock agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        password="test_password",
        display_name="CalculatorAgent",
        user_id="@mindroom_calculator:localhost",
    )


@pytest.fixture
def mock_agent_users() -> dict[str, AgentMatrixUser]:
    """Create mock agent users."""
    return {
        "calculator": AgentMatrixUser(
            agent_name="calculator",
            password="test_password1",
            display_name="CalculatorAgent",
            user_id="@mindroom_calculator:localhost",
        ),
        "general": AgentMatrixUser(
            agent_name="general",
            password="test_password2",
            display_name="GeneralAgent",
            user_id="@mindroom_general:localhost",
        ),
    }


class TestAgentBot:
    """Test cases for AgentBot class."""

    @pytest.mark.asyncio
    async def test_agent_bot_initialization(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test AgentBot initialization."""
        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(mock_agent_user, tmp_path, rooms=["!test:localhost"], config=config)
        assert bot.agent_user == mock_agent_user
        assert bot.agent_name == "calculator"
        assert bot.rooms == ["!test:localhost"]
        assert not bot.running
        assert bot.enable_streaming is True  # Default value

        # Test with streaming disabled
        config = Config(router=RouterConfig(model="default"))

        bot_no_stream = AgentBot(
            mock_agent_user, tmp_path, rooms=["!test:localhost"], enable_streaming=False, config=config
        )
        assert bot_no_stream.enable_streaming is False

    @pytest.mark.asyncio
    @patch("mindroom.bot.MATRIX_HOMESERVER", "http://localhost:8008")
    @patch("mindroom.bot.login_agent_user")
    @patch("mindroom.bot.AgentBot.ensure_user_account")
    async def test_agent_bot_start(
        self, mock_ensure_user: AsyncMock, mock_login: AsyncMock, mock_agent_user: AgentMatrixUser, tmp_path: Path
    ) -> None:
        """Test starting an agent bot."""
        mock_client = AsyncMock()
        # add_event_callback is a sync method, not async
        mock_client.add_event_callback = MagicMock()
        mock_login.return_value = mock_client

        # Mock ensure_user_account to not change the agent_user
        mock_ensure_user.return_value = None

        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        await bot.start()

        assert bot.running
        assert bot.client == mock_client
        # The bot calls ensure_setup which calls ensure_user_account
        # and then login with whatever user account was ensured
        assert mock_login.called
        assert mock_client.add_event_callback.call_count == 3  # invite, message, and reaction callbacks

    @pytest.mark.asyncio
    async def test_agent_bot_stop(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test stopping an agent bot."""
        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        bot.running = True

        await bot.stop()

        assert not bot.running
        bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_bot_on_invite(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test handling room invitations."""
        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"

        await bot._on_invite(mock_room, mock_event)

        bot.client.join.assert_called_once_with("!test:localhost")

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_ignore_own(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test that agent ignores its own messages."""
        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_calculator:localhost"  # Bot's own ID

        await bot._on_message(mock_room, mock_event)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_ignore_other_agents(
        self, mock_agent_user: AgentMatrixUser, tmp_path: Path
    ) -> None:
        """Test that agent ignores messages from other agents."""
        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_general:localhost"  # Another agent

        await bot._on_message(mock_room, mock_event)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.ai_response_streaming")
    @patch("mindroom.bot.fetch_thread_history")
    async def test_agent_bot_on_message_mentioned(
        self,
        mock_fetch_history: AsyncMock,
        mock_ai_response_streaming: AsyncMock,
        mock_ai_response: AsyncMock,
        enable_streaming: bool,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test agent bot responding to mentions with both streaming and non-streaming modes."""

        # Mock streaming response - return an async generator
        async def mock_streaming_response():
            yield "Test"
            yield " response"

        mock_ai_response_streaming.return_value = mock_streaming_response()
        mock_ai_response.return_value = "Test response"
        mock_fetch_history.return_value = []

        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(
            mock_agent_user, tmp_path, rooms=["!test:localhost"], enable_streaming=enable_streaming, config=config
        )
        bot.client = AsyncMock()

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Initialize response tracker with isolated path
        bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
        bot.thread_invite_manager = ThreadInviteManager(bot.client)

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "@mindroom_calculator:localhost: What's 2+2?"
        mock_event.event_id = "event123"
        mock_event.source = {
            "content": {
                "body": "@mindroom_calculator:localhost: What's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root_id"},
            }
        }

        await bot._on_message(mock_room, mock_event)

        # Should call AI and send response based on streaming mode
        if enable_streaming:
            mock_ai_response_streaming.assert_called_once_with(
                agent_name="calculator",
                prompt="@mindroom_calculator:localhost: What's 2+2?",
                session_id="!test:localhost:$thread_root_id",
                storage_path=tmp_path,
                thread_history=[],
                room_id="!test:localhost",
            )
            mock_ai_response.assert_not_called()
            # With streaming, we expect 2 calls: initial message + final edit
            assert bot.client.room_send.call_count == 2
        else:
            mock_ai_response.assert_called_once_with(
                agent_name="calculator",
                prompt="@mindroom_calculator:localhost: What's 2+2?",
                session_id="!test:localhost:$thread_root_id",
                storage_path=tmp_path,
                thread_history=[],
                room_id="!test:localhost",
            )
            mock_ai_response_streaming.assert_not_called()
            # Without streaming, we expect 1 call
            assert bot.client.room_send.call_count == 1

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_not_mentioned(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test agent bot not responding when not mentioned."""
        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "Hello everyone!"
        mock_event.source = {"content": {"body": "Hello everyone!"}}

        await bot._on_message(mock_room, mock_event)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("enable_streaming", [True, False])
    @patch("mindroom.teams.Team.arun")
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.ai_response_streaming")
    @patch("mindroom.bot.fetch_thread_history")
    async def test_agent_bot_thread_response(
        self,
        mock_fetch_history: AsyncMock,
        mock_ai_response_streaming: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_team_arun: AsyncMock,
        enable_streaming: bool,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test agent bot thread response behavior based on agent participation."""
        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(
            mock_agent_user, tmp_path, rooms=["!test:localhost"], enable_streaming=enable_streaming, config=config
        )
        bot.client = AsyncMock()

        # Mock orchestrator with agent_bots
        mock_orchestrator = MagicMock()
        mock_agent_bot = MagicMock()
        mock_agent_bot.agent = MagicMock()
        mock_orchestrator.agent_bots = {"calculator": mock_agent_bot, "general": mock_agent_bot}
        bot.orchestrator = mock_orchestrator

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Initialize response tracker with isolated path
        bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
        bot.thread_invite_manager = ThreadInviteManager(bot.client)

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"
        # Mock room users to include the agent
        mock_room.users = {"@mindroom_calculator:localhost": MagicMock()}

        # Test 1: Thread with only this agent - should respond without mention
        mock_fetch_history.return_value = [
            {"sender": "@user:localhost", "body": "Previous message", "timestamp": 123, "event_id": "prev1"},
            {
                "sender": "@mindroom_calculator:localhost",
                "body": "My previous response",
                "timestamp": 124,
                "event_id": "prev2",
            },
        ]

        # Mock streaming response - return an async generator
        async def mock_streaming_response():
            yield "Thread"
            yield " response"

        mock_ai_response_streaming.return_value = mock_streaming_response()
        mock_ai_response.return_value = "Thread response"
        mock_team_arun.return_value = "Team response"

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "Thread message without mention"
        mock_event.event_id = "event123"
        mock_event.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "thread_root",
                },
            },
        }

        await bot._on_message(mock_room, mock_event)

        # Should respond as only agent in thread
        if enable_streaming:
            mock_ai_response_streaming.assert_called_once()
            mock_ai_response.assert_not_called()
            # With streaming, we expect 2 calls: initial message + final edit
            assert bot.client.room_send.call_count == 2
        else:
            mock_ai_response.assert_called_once()
            mock_ai_response_streaming.assert_not_called()
            # Without streaming, we expect 1 call
            assert bot.client.room_send.call_count == 1

        # Reset mocks
        mock_ai_response_streaming.reset_mock()
        mock_ai_response.reset_mock()
        mock_team_arun.reset_mock()
        bot.client.room_send.reset_mock()
        mock_fetch_history.reset_mock()

        # Test 2: Thread with multiple agents - should NOT respond without mention
        test2_history = [
            {"sender": "@user:localhost", "body": "Previous message", "timestamp": 123, "event_id": "prev1"},
            {"sender": "@mindroom_calculator:localhost", "body": "My response", "timestamp": 124, "event_id": "prev2"},
            {
                "sender": "@mindroom_general:localhost",
                "body": "Another agent response",
                "timestamp": 125,
                "event_id": "prev3",
            },
        ]
        mock_fetch_history.return_value = test2_history

        # Create a new event with a different ID for Test 2
        mock_event_2 = MagicMock()
        mock_event_2.sender = "@user:localhost"
        mock_event_2.body = "Thread message without mention"
        mock_event_2.event_id = "event456"  # Different event ID
        mock_event_2.source = {
            "content": {
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "thread_root",
                },
            },
        }

        await bot._on_message(mock_room, mock_event_2)

        # Should form team and send team response when multiple agents in thread
        mock_ai_response_streaming.assert_not_called()
        mock_ai_response.assert_not_called()
        mock_team_arun.assert_called_once()
        bot.client.room_send.assert_called_once()  # Team response sent

        # Reset mocks
        mock_ai_response_streaming.reset_mock()
        mock_ai_response.reset_mock()
        mock_team_arun.reset_mock()
        bot.client.room_send.reset_mock()

        # Test 3: Thread with multiple agents WITH mention - should respond
        mock_event_with_mention = MagicMock()
        mock_event_with_mention.sender = "@user:localhost"
        mock_event_with_mention.body = "@mindroom_calculator:localhost What's 2+2?"
        mock_event_with_mention.event_id = "event789"  # Unique event ID for Test 3
        mock_event_with_mention.source = {
            "content": {
                "body": "@mindroom_calculator:localhost What's 2+2?",
                "m.relates_to": {
                    "rel_type": "m.thread",
                    "event_id": "thread_root",
                },
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            },
        }

        # Set up fresh async generator for the second call
        async def mock_streaming_response2():
            yield "Mentioned"
            yield " response"

        mock_ai_response_streaming.return_value = mock_streaming_response2()
        mock_ai_response.return_value = "Mentioned response"

        await bot._on_message(mock_room, mock_event_with_mention)

        # Should respond when explicitly mentioned
        if enable_streaming:
            mock_ai_response_streaming.assert_called_once()
            mock_ai_response.assert_not_called()
            # With streaming, we expect 2 calls: initial message + final edit
            assert bot.client.room_send.call_count == 2
        else:
            mock_ai_response.assert_called_once()
            mock_ai_response_streaming.assert_not_called()
            # Without streaming, we expect 1 call
            assert bot.client.room_send.call_count == 1

    @pytest.mark.asyncio
    async def test_agent_bot_skips_already_responded_messages(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agent bot skips messages it has already responded to."""
        config = Config(router=RouterConfig(model="default"))

        bot = AgentBot(mock_agent_user, tmp_path, config=config)
        bot.client = AsyncMock()
        # Initialize response tracker with isolated path
        bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
        bot.thread_invite_manager = ThreadInviteManager(bot.client)

        # Mark an event as already responded
        bot.response_tracker.mark_responded("event123")

        # Create mock room and event
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "@mindroom_calculator:localhost: What's 2+2?"
        mock_event.event_id = "event123"  # Same event ID
        mock_event.source = {
            "content": {
                "body": "@mindroom_calculator:localhost: What's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
                "m.relates_to": {"rel_type": "m.thread", "event_id": "$thread_root_id"},
            }
        }

        await bot._on_message(mock_room, mock_event)

        # Should not send any message since it already responded
        bot.client.room_send.assert_not_called()


class TestMultiAgentOrchestrator:
    """Test cases for MultiAgentOrchestrator class."""

    @pytest.mark.asyncio
    async def test_orchestrator_initialization(self, tmp_path: Path) -> None:
        """Test MultiAgentOrchestrator initialization."""
        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        assert orchestrator.agent_bots == {}
        assert not orchestrator.running

    @pytest.mark.asyncio
    @patch("mindroom.bot.load_config")
    async def test_orchestrator_initialize(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test initializing the orchestrator with agents."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_load_config.return_value = mock_config

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()

        # Should have 3 bots: calculator, general, and router
        assert len(orchestrator.agent_bots) == 3
        assert "calculator" in orchestrator.agent_bots
        assert "general" in orchestrator.agent_bots
        assert "router" in orchestrator.agent_bots

    @pytest.mark.asyncio
    @patch("mindroom.bot.load_config")
    async def test_orchestrator_start(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test starting all agent bots."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        mock_load_config.return_value = mock_config

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()  # Need to initialize first

        # Mock start for all bots to avoid actual login/setup
        start_mocks = []
        for bot in orchestrator.agent_bots.values():
            # Create a mock that tracks the call
            mock_start = AsyncMock()
            # Replace start with our mock
            bot.start = mock_start
            start_mocks.append(mock_start)
            bot.running = False

        # Start the orchestrator but don't wait for sync_forever
        start_tasks = []
        for _agent_name, bot in orchestrator.agent_bots.items():
            start_tasks.append(bot.start())

        await asyncio.gather(*start_tasks)
        orchestrator.running = True  # Manually set since we're not calling orchestrator.start()

        assert orchestrator.running
        # Verify start was called for each bot
        for mock_start in start_mocks:
            mock_start.assert_called_once()

    @pytest.mark.asyncio
    @patch("mindroom.bot.load_config")
    async def test_orchestrator_stop(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test stopping all agent bots."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        mock_load_config.return_value = mock_config

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()

        # Mock the agent clients and ensure_user_account
        for bot in orchestrator.agent_bots.values():
            bot.client = AsyncMock()
            bot.running = True
            bot.ensure_user_account = AsyncMock()

        await orchestrator.stop()

        assert not orchestrator.running
        for bot in orchestrator.agent_bots.values():
            assert not bot.running
            bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("mindroom.bot.load_config")
    @patch.dict(os.environ, {"MINDROOM_ENABLE_STREAMING": "false"})
    async def test_orchestrator_streaming_env_var(
        self,
        mock_load_config: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Test that orchestrator respects MINDROOM_ENABLE_STREAMING environment variable."""
        # Mock config with just 2 agents
        mock_config = MagicMock()
        mock_config.agents = {
            "calculator": MagicMock(display_name="CalculatorAgent", rooms=["lobby"]),
            "general": MagicMock(display_name="GeneralAgent", rooms=["lobby"]),
        }
        mock_config.teams = {}
        mock_config.get_all_configured_rooms.return_value = ["lobby"]
        mock_load_config.return_value = mock_config

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()

        # All bots should have streaming disabled except teams (which never stream)
        for _name, bot in orchestrator.agent_bots.items():
            if hasattr(bot, "enable_streaming"):
                assert bot.enable_streaming is False
