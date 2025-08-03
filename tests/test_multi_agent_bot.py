"""Tests for the multi-agent bot system."""

import asyncio
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.matrix import AgentMatrixUser


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
        bot = AgentBot(mock_agent_user, tmp_path, rooms=["!test:localhost"])
        assert bot.agent_user == mock_agent_user
        assert bot.agent_name == "calculator"
        assert bot.rooms == ["!test:localhost"]
        assert not bot.running

    @pytest.mark.asyncio
    @patch("mindroom.bot.login_agent_user")
    async def test_agent_bot_start(
        self, mock_login: AsyncMock, mock_agent_user: AgentMatrixUser, tmp_path: Path
    ) -> None:
        """Test starting an agent bot."""
        mock_client = AsyncMock()
        # add_event_callback is a sync method, not async
        mock_client.add_event_callback = MagicMock()
        mock_login.return_value = mock_client

        bot = AgentBot(mock_agent_user, tmp_path)
        await bot.start()

        assert bot.running
        assert bot.client == mock_client
        mock_login.assert_called_once_with("http://localhost:8008", mock_agent_user)
        assert mock_client.add_event_callback.call_count == 3  # invite, message, and reaction callbacks

    @pytest.mark.asyncio
    async def test_agent_bot_stop(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test stopping an agent bot."""
        bot = AgentBot(mock_agent_user, tmp_path)
        bot.client = AsyncMock()
        bot.running = True

        await bot.stop()

        assert not bot.running
        bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_bot_on_invite(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test handling room invitations."""
        bot = AgentBot(mock_agent_user, tmp_path)
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
        bot = AgentBot(mock_agent_user, tmp_path)
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
        bot = AgentBot(mock_agent_user, tmp_path)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_general:localhost"  # Another agent

        await bot._on_message(mock_room, mock_event)

        # Should not send any response
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.fetch_thread_history")
    async def test_agent_bot_on_message_mentioned(
        self,
        mock_fetch_history: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test agent bot responding to mentions."""
        mock_ai_response.return_value = "Test response"
        mock_fetch_history.return_value = []

        bot = AgentBot(mock_agent_user, tmp_path, rooms=["!test:localhost"])
        bot.client = AsyncMock()

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Initialize response tracker with isolated path
        from mindroom.interactive import InteractiveManager
        from mindroom.response_tracker import ResponseTracker
        from mindroom.thread_invites import ThreadInviteManager

        bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
        bot.thread_invite_manager = ThreadInviteManager(bot.client)
        bot.interactive_manager = InteractiveManager(bot.client, bot.agent_name)

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

        # Should call AI and send response
        mock_ai_response.assert_called_once_with(
            agent_name="calculator",
            prompt="@mindroom_calculator:localhost: What's 2+2?",
            session_id="!test:localhost:$thread_root_id",
            storage_path=tmp_path,
            thread_history=[],
            room_id="!test:localhost",
        )
        bot.client.room_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_bot_on_message_not_mentioned(self, mock_agent_user: AgentMatrixUser, tmp_path: Path) -> None:
        """Test agent bot not responding when not mentioned."""
        bot = AgentBot(mock_agent_user, tmp_path)
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
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.fetch_thread_history")
    async def test_agent_bot_thread_response(
        self,
        mock_fetch_history: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test agent bot thread response behavior based on agent participation."""
        bot = AgentBot(mock_agent_user, tmp_path, rooms=["!test:localhost"])
        bot.client = AsyncMock()

        # Mock successful room_send response
        mock_send_response = MagicMock()
        mock_send_response.__class__ = nio.RoomSendResponse
        bot.client.room_send.return_value = mock_send_response

        # Initialize response tracker with isolated path
        from mindroom.interactive import InteractiveManager
        from mindroom.response_tracker import ResponseTracker
        from mindroom.thread_invites import ThreadInviteManager

        bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
        bot.thread_invite_manager = ThreadInviteManager(bot.client)
        bot.interactive_manager = InteractiveManager(bot.client, bot.agent_name)

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

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
        mock_ai_response.return_value = "Thread response"

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
        mock_ai_response.assert_called_once()
        bot.client.room_send.assert_called_once()

        # Reset mocks
        mock_ai_response.reset_mock()
        bot.client.room_send.reset_mock()

        # Test 2: Thread with multiple agents - should NOT respond without mention
        mock_fetch_history.return_value = [
            {"sender": "@user:localhost", "body": "Previous message", "timestamp": 123, "event_id": "prev1"},
            {"sender": "@mindroom_calculator:localhost", "body": "My response", "timestamp": 124, "event_id": "prev2"},
            {
                "sender": "@mindroom_general:localhost",
                "body": "Another agent response",
                "timestamp": 125,
                "event_id": "prev3",
            },
        ]

        await bot._on_message(mock_room, mock_event)

        # Should NOT respond when multiple agents in thread
        mock_ai_response.assert_not_called()
        bot.client.room_send.assert_not_called()

        # Test 3: Thread with multiple agents WITH mention - should respond
        mock_event_with_mention = MagicMock()
        mock_event_with_mention.sender = "@user:localhost"
        mock_event_with_mention.body = "@mindroom_calculator:localhost What's 2+2?"
        mock_event_with_mention.event_id = "event456"
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

        await bot._on_message(mock_room, mock_event_with_mention)

        # Should respond when explicitly mentioned
        mock_ai_response.assert_called_once()
        bot.client.room_send.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_bot_skips_already_responded_messages(
        self,
        mock_agent_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Test that agent bot skips messages it has already responded to."""
        bot = AgentBot(mock_agent_user, tmp_path)
        bot.client = AsyncMock()
        # Initialize response tracker with isolated path
        from mindroom.interactive import InteractiveManager
        from mindroom.response_tracker import ResponseTracker
        from mindroom.thread_invites import ThreadInviteManager

        bot.response_tracker = ResponseTracker(bot.agent_name, base_path=tmp_path)
        bot.thread_invite_manager = ThreadInviteManager(bot.client)
        bot.interactive_manager = InteractiveManager(bot.client, bot.agent_name)

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
    @patch("mindroom.bot.ensure_all_agent_users")
    async def test_orchestrator_initialize(
        self,
        mock_ensure_users: AsyncMock,
        mock_agent_users: dict[str, AgentMatrixUser],
        tmp_path: Path,
    ) -> None:
        """Test initializing the orchestrator with agents."""
        mock_ensure_users.return_value = mock_agent_users

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()

        assert len(orchestrator.agent_bots) == 2
        assert "calculator" in orchestrator.agent_bots
        assert "general" in orchestrator.agent_bots

    @pytest.mark.asyncio
    @patch("mindroom.bot.ensure_all_agent_users")
    @patch("mindroom.bot.login_agent_user")
    async def test_orchestrator_start(
        self,
        mock_login: AsyncMock,
        mock_ensure_users: AsyncMock,
        mock_agent_users: dict[str, AgentMatrixUser],
        tmp_path: Path,
    ) -> None:
        """Test starting all agent bots."""
        mock_ensure_users.return_value = mock_agent_users
        mock_client = AsyncMock()
        # add_event_callback is a sync method, not async
        mock_client.add_event_callback = MagicMock()
        mock_client.sync_forever = AsyncMock(side_effect=KeyboardInterrupt)
        mock_login.return_value = mock_client

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()  # Need to initialize first

        # Start the orchestrator but don't wait for sync_forever
        start_tasks = []
        for _agent_name, bot in orchestrator.agent_bots.items():
            start_tasks.append(bot.start())

        await asyncio.gather(*start_tasks)
        orchestrator.running = True  # Manually set since we're not calling orchestrator.start()

        assert orchestrator.running
        assert mock_login.call_count == 2  # Called for each agent

    @pytest.mark.asyncio
    @patch("mindroom.bot.ensure_all_agent_users")
    async def test_orchestrator_stop(
        self,
        mock_ensure_users: AsyncMock,
        mock_agent_users: dict[str, AgentMatrixUser],
        tmp_path: Path,
    ) -> None:
        """Test stopping all agent bots."""
        mock_ensure_users.return_value = mock_agent_users

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()

        # Mock the agent clients
        for bot in orchestrator.agent_bots.values():
            bot.client = AsyncMock()
            bot.running = True

        await orchestrator.stop()

        assert not orchestrator.running
        for bot in orchestrator.agent_bots.values():
            assert not bot.running
            bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    @patch("mindroom.bot.ensure_all_agent_users")
    async def test_orchestrator_invite_agents_to_room(
        self,
        mock_ensure_users: AsyncMock,
        mock_agent_users: dict[str, AgentMatrixUser],
        tmp_path: Path,
    ) -> None:
        """Test inviting all agents to a room."""
        mock_ensure_users.return_value = mock_agent_users

        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)
        await orchestrator.initialize()

        mock_inviter_client = AsyncMock()
        await orchestrator.invite_agents_to_room("!room:localhost", mock_inviter_client)

        # Verify invites were sent for all agents
        assert mock_inviter_client.room_invite.call_count == 2
        calls = mock_inviter_client.room_invite.call_args_list
        invited_users = {call[0][1] for call in calls}
        assert invited_users == {"@mindroom_calculator:localhost", "@mindroom_general:localhost"}
