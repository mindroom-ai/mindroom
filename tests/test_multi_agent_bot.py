"""Tests for the multi-agent bot system."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.matrix_agent_manager import AgentMatrixUser


@pytest.fixture
def mock_agent_user() -> AgentMatrixUser:
    """Create a mock agent user."""
    return AgentMatrixUser(
        agent_name="calculator",
        user_id="@mindroom_calculator:localhost",
        display_name="CalculatorAgent",
        password="test_password",
        access_token="test_token",
    )


@pytest.fixture
def mock_agent_users() -> dict[str, AgentMatrixUser]:
    """Create mock agent users for testing."""
    return {
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


class TestAgentBot:
    """Test cases for AgentBot class."""

    @pytest.mark.asyncio
    async def test_agent_bot_initialization(self, mock_agent_user: AgentMatrixUser) -> None:
        """Test AgentBot initialization."""
        bot = AgentBot(mock_agent_user)
        assert bot.agent_user == mock_agent_user
        assert bot.agent_name == "calculator"
        assert bot.client is None
        assert not bot.running
        assert bot.rooms == []

        # Test with rooms
        rooms = ["!room1:localhost", "!room2:localhost"]
        bot_with_rooms = AgentBot(mock_agent_user, rooms=rooms)
        assert bot_with_rooms.rooms == rooms

    @pytest.mark.asyncio
    @patch("mindroom.bot.login_agent_user")
    async def test_agent_bot_start(
        self,
        mock_login: AsyncMock,
        mock_agent_user: AgentMatrixUser,
    ) -> None:
        """Test starting an agent bot."""
        # Mock the client
        mock_client = AsyncMock()
        # add_event_callback is a sync method, not async
        mock_client.add_event_callback = MagicMock()
        mock_login.return_value = mock_client

        bot = AgentBot(mock_agent_user)
        await bot.start()

        # Verify login was called
        mock_login.assert_called_once_with(mock_agent_user)

        # Verify event callbacks were added
        assert mock_client.add_event_callback.call_count == 2
        assert bot.running
        assert bot.client == mock_client

    @pytest.mark.asyncio
    @patch("mindroom.bot.login_agent_user")
    async def test_agent_bot_auto_join_rooms(
        self,
        mock_login: AsyncMock,
        mock_agent_user: AgentMatrixUser,
    ) -> None:
        """Test that agent bot auto-joins configured rooms on start."""
        # Mock the client
        mock_client = AsyncMock()
        # add_event_callback is a sync method, not async
        mock_client.add_event_callback = MagicMock()
        mock_login.return_value = mock_client

        # Mock join responses
        mock_client.join.side_effect = [
            AsyncMock(spec=nio.JoinResponse),
            AsyncMock(spec=nio.JoinResponse),
        ]

        rooms = ["!room1:localhost", "!room2:localhost"]
        bot = AgentBot(mock_agent_user, rooms=rooms)
        await bot.start()

        # Verify rooms were joined
        assert mock_client.join.call_count == 2
        mock_client.join.assert_any_call("!room1:localhost")
        mock_client.join.assert_any_call("!room2:localhost")

    @pytest.mark.asyncio
    async def test_agent_bot_stop(self, mock_agent_user: AgentMatrixUser) -> None:
        """Test stopping an agent bot."""
        bot = AgentBot(mock_agent_user)
        bot.client = AsyncMock()
        bot.running = True

        await bot.stop()

        assert not bot.running
        bot.client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_bot_on_invite(self, mock_agent_user: AgentMatrixUser) -> None:
        """Test agent bot handling room invitations."""
        bot = AgentBot(mock_agent_user)
        bot.client = AsyncMock()

        # Create mock room and event
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"
        mock_room.display_name = "Test Room"
        mock_event = MagicMock()

        await bot._on_invite(mock_room, mock_event)

        bot.client.join.assert_called_once_with("!test:localhost")

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.fetch_thread_history")
    async def test_agent_bot_on_message_mentioned(
        self,
        mock_fetch_history: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_agent_user: AgentMatrixUser,
    ) -> None:
        """Test agent bot responding to messages where it's mentioned."""
        mock_fetch_history.return_value = []
        mock_ai_response.return_value = "Calculator response"

        bot = AgentBot(mock_agent_user)
        bot.client = AsyncMock()

        # Create mock room and event
        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

        mock_event = MagicMock()
        mock_event.sender = "@user:localhost"
        mock_event.body = "@mindroom_calculator:localhost: What's 2+2?"  # Include full user ID
        mock_event.event_id = "event123"
        mock_event.source = {
            "content": {
                "body": "@mindroom_calculator:localhost: What's 2+2?",
                "m.mentions": {"user_ids": ["@mindroom_calculator:localhost"]},
            }
        }

        await bot._on_message(mock_room, mock_event)

        # Verify AI response was called with the full message body as prompt
        mock_ai_response.assert_called_once_with(
            "calculator",
            "@mindroom_calculator:localhost: What's 2+2?",
            "!test:localhost",
            thread_history=[],
        )

        # Verify message was sent
        bot.client.room_send.assert_called_once()
        call_args = bot.client.room_send.call_args
        assert call_args[1]["room_id"] == "!test:localhost"
        assert call_args[1]["message_type"] == "m.room.message"
        assert call_args[1]["content"]["body"] == "Calculator response"

    @pytest.mark.asyncio
    async def test_agent_bot_ignores_own_messages(self, mock_agent_user: AgentMatrixUser) -> None:
        """Test that agent bot ignores its own messages."""
        bot = AgentBot(mock_agent_user)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_calculator:localhost"
        mock_event.body = "My own message"
        mock_event.source = {"content": {}}

        await bot._on_message(mock_room, mock_event)

        # Should not process or respond
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    async def test_agent_bot_ignores_other_agents(self, mock_agent_user: AgentMatrixUser) -> None:
        """Test that agent bot ignores messages from other agents."""
        bot = AgentBot(mock_agent_user)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_event = MagicMock()
        mock_event.sender = "@mindroom_general:localhost"
        mock_event.body = "Message from another agent"
        mock_event.source = {"content": {}}

        await bot._on_message(mock_room, mock_event)

        # Should not process or respond
        bot.client.room_send.assert_not_called()

    @pytest.mark.asyncio
    @patch("mindroom.bot.ai_response")
    @patch("mindroom.bot.fetch_thread_history")
    async def test_agent_bot_thread_response(
        self,
        mock_fetch_history: AsyncMock,
        mock_ai_response: AsyncMock,
        mock_agent_user: AgentMatrixUser,
    ) -> None:
        """Test agent bot responding to all messages in threads."""
        mock_fetch_history.return_value = [
            {"sender": "@user:localhost", "body": "Previous message", "timestamp": 123, "event_id": "prev1"},
        ]
        mock_ai_response.return_value = "Thread response"

        bot = AgentBot(mock_agent_user)
        bot.client = AsyncMock()

        mock_room = MagicMock()
        mock_room.room_id = "!test:localhost"

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

        # Should respond even without mention
        mock_ai_response.assert_called_once()
        bot.client.room_send.assert_called_once()


class TestMultiAgentOrchestrator:
    """Test cases for MultiAgentOrchestrator class."""

    @pytest.mark.asyncio
    async def test_orchestrator_initialization(self) -> None:
        """Test MultiAgentOrchestrator initialization."""
        orchestrator = MultiAgentOrchestrator()
        assert orchestrator.agent_bots == {}
        assert not orchestrator.running

    @pytest.mark.asyncio
    @patch("mindroom.bot.ensure_all_agent_users")
    async def test_orchestrator_initialize(
        self,
        mock_ensure_users: AsyncMock,
        mock_agent_users: dict[str, AgentMatrixUser],
    ) -> None:
        """Test initializing the orchestrator with agents."""
        mock_ensure_users.return_value = mock_agent_users

        orchestrator = MultiAgentOrchestrator()
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
    ) -> None:
        """Test starting all agent bots."""
        mock_ensure_users.return_value = mock_agent_users
        mock_client = AsyncMock()
        # add_event_callback is a sync method, not async
        mock_client.add_event_callback = MagicMock()
        mock_client.sync_forever = AsyncMock(side_effect=KeyboardInterrupt)
        mock_login.return_value = mock_client

        orchestrator = MultiAgentOrchestrator()
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
    ) -> None:
        """Test stopping all agent bots."""
        mock_ensure_users.return_value = mock_agent_users

        orchestrator = MultiAgentOrchestrator()
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
    ) -> None:
        """Test inviting all agents to a room."""
        mock_ensure_users.return_value = mock_agent_users

        orchestrator = MultiAgentOrchestrator()
        await orchestrator.initialize()

        mock_inviter_client = AsyncMock()
        await orchestrator.invite_agents_to_room("!room:localhost", mock_inviter_client)

        # Verify invites were sent for all agents
        assert mock_inviter_client.room_invite.call_count == 2
        calls = mock_inviter_client.room_invite.call_args_list
        invited_users = {call[0][1] for call in calls}
        assert invited_users == {"@mindroom_calculator:localhost", "@mindroom_general:localhost"}
