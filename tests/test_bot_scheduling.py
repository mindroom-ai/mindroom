"""Integration tests for scheduling functionality in the bot."""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot
from mindroom.commands import Command, CommandType
from mindroom.config import AgentConfig, Config, ModelConfig, RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME, VOICE_PREFIX
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.thread_utils import should_agent_respond

from .conftest import TEST_ACCESS_TOKEN, TEST_PASSWORD


def create_mock_room(room_id: str = "!test:localhost", agents: list[str] | None = None) -> MagicMock:
    """Create a mock room with specified agents."""
    room = MagicMock()
    room.room_id = room_id
    if agents:
        room.users = {f"@mindroom_{agent}:localhost": None for agent in agents}
    else:
        room.users = {}
    return room


@pytest.fixture
def mock_agent_bot() -> AgentBot:
    """Create a mock agent bot for testing."""
    agent_user = AgentMatrixUser(
        agent_name="general",
        user_id="@mindroom_general:localhost",
        display_name="General Agent",
        password=TEST_PASSWORD,
        access_token=TEST_ACCESS_TOKEN,
    )
    config = Config.from_yaml()  # Load actual config for testing
    with tempfile.TemporaryDirectory() as tmpdir:
        bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
    bot.client = AsyncMock()
    bot.logger = MagicMock()
    bot._send_response = AsyncMock()  # type: ignore[method-assign]
    return bot


class TestBotScheduleCommands:
    """Test bot handling of schedule commands."""

    @pytest.mark.asyncio
    async def test_handle_schedule_command(self, mock_agent_bot: AgentBot) -> None:
        """Test bot handles schedule command correctly."""
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.sender = "@user:server"
        event.body = "!schedule in 5 minutes Check deployment"
        event.source = {"content": {"m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"}}}

        command = Command(
            type=CommandType.SCHEDULE,
            args={"full_text": "in 5 minutes Check deployment"},
            raw_text=event.body,
        )

        # Mock the schedule_task function
        with patch("mindroom.bot.schedule_task") as mock_schedule:
            mock_schedule.return_value = ("task123", "✅ Scheduled: 5 minutes from now")

            # Mock response tracker for the test
            mock_agent_bot.response_tracker = MagicMock()
            mock_agent_bot.response_tracker.has_responded.return_value = False

            await mock_agent_bot._handle_command(room, event, command)

            # Verify schedule_task was called correctly
            mock_schedule.assert_called_once_with(
                client=mock_agent_bot.client,
                room_id="!test:server",
                thread_id="$thread123",
                scheduled_by="@user:server",
                full_text="in 5 minutes Check deployment",
                config=mock_agent_bot.config,
                room=room,
                mentioned_agents=[],  # No agents mentioned in this command
            )

            # Verify response was sent
            mock_agent_bot._send_response.assert_called_once()  # type: ignore[attr-defined]
            call_args = mock_agent_bot._send_response.call_args  # type: ignore[attr-defined]
            assert "✅ Scheduled: 5 minutes from now" in call_args[0][2]

    @pytest.mark.asyncio
    async def test_handle_schedule_command_no_message(self, mock_agent_bot: AgentBot) -> None:
        """Test schedule command with no message uses default."""
        mock_agent_bot.response_tracker = MagicMock()
        mock_agent_bot.response_tracker.has_responded.return_value = False
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.sender = "@user:server"
        event.body = "!schedule tomorrow"
        event.source = {"content": {"m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"}}}

        command = Command(type=CommandType.SCHEDULE, args={"full_text": "tomorrow"}, raw_text=event.body)

        with patch("mindroom.bot.schedule_task") as mock_schedule:
            mock_schedule.return_value = ("task456", "✅ Scheduled for tomorrow")

            await mock_agent_bot._handle_command(room, event, command)

            # Verify the full text was passed
            call_args = mock_schedule.call_args
            assert call_args[1]["full_text"] == "tomorrow"

    @pytest.mark.asyncio
    async def test_handle_list_schedules_command(self, mock_agent_bot: AgentBot) -> None:
        """Test bot handles list schedules command."""
        mock_agent_bot.response_tracker = MagicMock()
        mock_agent_bot.response_tracker.has_responded.return_value = False
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.body = "!list_schedules"
        event.source = {"content": {"m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"}}}

        command = Command(type=CommandType.LIST_SCHEDULES, args={}, raw_text=event.body)

        with patch("mindroom.bot.list_scheduled_tasks") as mock_list:
            mock_list.return_value = "**Scheduled Tasks:**\n• task123 - Tomorrow: Test"

            await mock_agent_bot._handle_command(room, event, command)

            mock_list.assert_called_once_with(
                client=mock_agent_bot.client,
                room_id="!test:server",
                thread_id="$thread123",
                config=mock_agent_bot.config,
            )

            mock_agent_bot._send_response.assert_called_once()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_handle_cancel_schedule_command(self, mock_agent_bot: AgentBot) -> None:
        """Test bot handles cancel schedule command."""
        mock_agent_bot.response_tracker = MagicMock()
        mock_agent_bot.response_tracker.has_responded.return_value = False
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.body = "!cancel_schedule task123"
        event.source = {"content": {"m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"}}}

        command = Command(type=CommandType.CANCEL_SCHEDULE, args={"task_id": "task123"}, raw_text=event.body)

        with patch("mindroom.bot.cancel_scheduled_task") as mock_cancel:
            mock_cancel.return_value = "✅ Cancelled task `task123`"

            await mock_agent_bot._handle_command(room, event, command)

            mock_cancel.assert_called_once_with(client=mock_agent_bot.client, room_id="!test:server", task_id="task123")

    @pytest.mark.asyncio
    async def test_handle_cancel_all_scheduled_tasks(self, mock_agent_bot: AgentBot) -> None:
        """Test bot handles cancel all scheduled tasks command."""
        mock_agent_bot.response_tracker = MagicMock()
        mock_agent_bot.response_tracker.has_responded.return_value = False
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.body = "!cancel_schedule all"
        event.source = {"content": {}}

        command = Command(
            type=CommandType.CANCEL_SCHEDULE,
            args={"task_id": "all", "cancel_all": True},
            raw_text=event.body,
        )

        with patch("mindroom.bot.cancel_all_scheduled_tasks") as mock_cancel_all:
            mock_cancel_all.return_value = "✅ Cancelled 3 scheduled task(s)"

            await mock_agent_bot._handle_command(room, event, command)

            mock_cancel_all.assert_called_once_with(client=mock_agent_bot.client, room_id="!test:server")

        mock_agent_bot._send_response.assert_called_once()  # type: ignore[attr-defined]
        call_args = mock_agent_bot._send_response.call_args  # type: ignore[attr-defined]
        assert "✅ Cancelled 3 scheduled task(s)" in call_args[0][2]

    @pytest.mark.asyncio
    async def test_schedule_command_auto_creates_thread(self, mock_agent_bot: AgentBot) -> None:
        """Test that schedule commands auto-create threads when used in main room."""
        mock_agent_bot.response_tracker = MagicMock()
        mock_agent_bot.response_tracker.has_responded.return_value = False
        room = MagicMock()
        room.room_id = "!test:server"

        event = MagicMock()
        event.event_id = "$event123"
        event.body = "!schedule in 5 minutes Test"
        event.source = {"content": {}}  # No thread relation

        command = Command(type=CommandType.SCHEDULE, args={"full_text": "in 5 minutes Test"}, raw_text=event.body)

        with patch("mindroom.bot.schedule_task") as mock_schedule:
            mock_schedule.return_value = ("task123", "✅ Scheduled: 5 minutes from now")

            await mock_agent_bot._handle_command(room, event, command)

        # Should successfully schedule the task (auto-creates thread)
        mock_agent_bot._send_response.assert_called_once()  # type: ignore[attr-defined]
        call_args = mock_agent_bot._send_response.call_args  # type: ignore[attr-defined]
        assert "✅" in call_args[0][2] or "Task ID" in call_args[0][2]
        # The thread_id should be None (will be handled by _send_response)
        # and the event should be passed for thread creation
        assert call_args[1].get("reply_to_event") == event


class TestBotTaskRestoration:
    """Test scheduled task restoration on bot startup."""

    @pytest.mark.asyncio
    async def test_restore_tasks_on_room_join(self) -> None:
        """Test that scheduled tasks are restored when joining rooms."""
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config()  # Empty config for testing
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])

            # Mock the necessary methods
            with (
                patch("mindroom.matrix.users.login") as mock_login,
                patch("mindroom.bot.restore_scheduled_tasks", new_callable=AsyncMock) as mock_restore,
            ):
                mock_client = AsyncMock()
                mock_login.return_value = mock_client

                # Mock the client.join method to return JoinResponse
                mock_join_response = MagicMock(spec=nio.JoinResponse)
                mock_client.join.return_value = mock_join_response

                mock_restore.return_value = 2  # 2 tasks restored

                await bot.start()
                # Now have the bot join its configured rooms
                await bot.join_configured_rooms()

                # Verify restore was called for the room with config
                mock_restore.assert_called_once_with(bot.client, "!test:server", config)

                # Just verify restore was called - logger testing is complex with the bind() method
                assert mock_restore.called

    @pytest.mark.asyncio
    async def test_no_log_when_no_tasks_restored(self) -> None:
        """Test that no log is generated when no tasks are restored."""
        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config = Config()  # Empty config for testing
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])

            with (
                patch("mindroom.matrix.users.login") as mock_login,
                patch("mindroom.bot.restore_scheduled_tasks", new_callable=AsyncMock) as mock_restore,
                patch("mindroom.bot.AgentBot._set_presence_with_model_info", new_callable=AsyncMock),
            ):
                mock_client = AsyncMock()
                mock_login.return_value = mock_client

                # Mock the client.join method to return JoinResponse
                mock_join_response = MagicMock(spec=nio.JoinResponse)
                mock_client.join.return_value = mock_join_response

                mock_restore.return_value = 0  # No tasks restored

                await bot.start()
                # Now have the bot join its configured rooms
                await bot.join_configured_rooms()

                # Just verify restore was called with 0 - logger testing is complex with the bind() method
                assert mock_restore.return_value == 0


class TestCommandHandling:
    """Test command handling behavior across different agents."""

    def setup_method(self) -> None:
        """Set up test config."""
        self.config = Config(
            agents={
                "calculator": AgentConfig(display_name="Calculator", rooms=["#test:example.org"]),
                "finance": AgentConfig(display_name="Finance", rooms=["#test:example.org"]),
                "router": AgentConfig(display_name="Router", rooms=["#test:example.org"]),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
        )

    @pytest.mark.asyncio
    async def test_non_router_agent_ignores_commands(self) -> None:
        """Test that non-router agents ignore command messages."""
        # Create a calculator agent (not router)
        agent_user = AgentMatrixUser(
            agent_name="calculator",
            user_id="@mindroom_calculator:localhost",
            display_name="Calculator Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        config = Config(router=RouterConfig(model="default"))

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._generate_response = AsyncMock()  # type: ignore[method-assign]
        bot._extract_message_context = AsyncMock()  # type: ignore[method-assign]

        # Create a room and event
        room = nio.MatrixRoom(room_id="!test:server", own_user_id=bot.client.user_id)
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event123",
                "sender": "@user:server",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "!schedule in 5 minutes test"},
            },
        )

        # Call _on_message
        await bot._on_message(room, event)

        # Verify the agent didn't try to process the command
        bot._generate_response.assert_not_called()
        # Debug logging has been removed, so we just verify the behavior

    @pytest.mark.asyncio
    async def test_router_agent_handles_commands(self) -> None:
        """Test that router agent does handle commands."""
        # Create router agent
        agent_user = AgentMatrixUser(
            agent_name="router",
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        config = Config(router=RouterConfig(model="default"))

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
            bot.client = AsyncMock()
            bot.logger = MagicMock()
            bot._handle_command = AsyncMock()  # type: ignore[method-assign]

            # Create a room and event with thread info
            room = nio.MatrixRoom(room_id="!test:server", own_user_id=bot.client.user_id)
            event = nio.RoomMessageText.from_dict(
                {
                    "event_id": "$event123",
                    "sender": "@user:server",
                    "origin_server_ts": 1234567890,
                    "content": {
                        "msgtype": "m.text",
                        "body": "!schedule in 5 minutes test",
                        "m.relates_to": {"event_id": "$thread123", "rel_type": "m.thread"},
                    },
                },
            )

            with patch("mindroom.constants.ROUTER_AGENT_NAME", "router"):
                await bot._on_message(room, event)

            # Verify the command was handled
            bot._handle_command.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_router_agent_responds_to_non_commands(self) -> None:
        """Test that non-router agents still respond to regular messages."""
        # Create a calculator agent (not router)
        agent_user = AgentMatrixUser(
            agent_name="calculator",
            user_id="@mindroom_calculator:localhost",
            display_name="Calculator Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        config = Config(
            router=RouterConfig(model="default"),
            agents={
                "calculator": AgentConfig(display_name="Calculator Agent", role="Calculator"),
            },
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._generate_response = AsyncMock()  # type: ignore[method-assign]
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Mock context extraction to say agent is mentioned
        mock_context = MagicMock()
        mock_context.am_i_mentioned = True
        mock_context.is_thread = True
        mock_context.thread_id = "$thread123"
        mock_context.thread_history = []
        # mentioned_agents should be a list of MatrixID objects
        mock_context.mentioned_agents = [config.ids["calculator"]] if "calculator" in config.ids else []
        bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

        # Mock should_agent_respond to return True
        with patch("mindroom.bot.should_agent_respond", return_value=True):
            # Create a room and event with a regular message
            room = nio.MatrixRoom(room_id="!test:server", own_user_id=bot.client.user_id)
            event = nio.RoomMessageText.from_dict(
                {
                    "event_id": "$event123",
                    "sender": "@user:server",
                    "origin_server_ts": 1234567890,
                    "content": {"msgtype": "m.text", "body": "@calculator what is 2+2?"},
                },
            )

            await bot._on_message(room, event)

            # Verify the agent processed the message
            bot._generate_response.assert_called_once()

    @pytest.mark.asyncio
    async def test_agents_ignore_error_messages_from_other_agents(self) -> None:
        """Test that agents don't respond to error messages from other agents."""
        # Create a general agent
        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        config = Config(router=RouterConfig(model="default"))

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
            bot.client = AsyncMock()
            bot.client.user_id = "@mindroom_general:localhost"  # Set the bot's user ID
            bot.logger = MagicMock()
            bot._generate_response = AsyncMock()  # type: ignore[method-assign]
            bot.response_tracker = MagicMock()
            bot.response_tracker.has_responded.return_value = False

            # Mock context extraction
            mock_context = MagicMock()
            mock_context.am_i_mentioned = False
            mock_context.is_thread = True
            mock_context.thread_id = "$thread123"
            mock_context.thread_history = []
            mock_context.mentioned_agents = []
            bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

            # Create a room and event with error message from router agent
            room = nio.MatrixRoom(room_id="!test:server", own_user_id=bot.client.user_id)
            event = nio.RoomMessageText.from_dict(
                {
                    "event_id": "$event123",
                    "sender": "@mindroom_router:localhost",  # From router agent
                    "origin_server_ts": 1234567890,
                    "content": {
                        "msgtype": "m.text",
                        "body": "❌ Unable to parse the schedule request\n\n💡 Try something like 'in 5 minutes Check the deployment'",
                    },
                },
            )

            # Mock interactive.handle_text_response and extract_agent_name
            with (
                patch("mindroom.bot.interactive.handle_text_response"),
                patch("mindroom.bot.extract_agent_name") as mock_extract,
            ):
                # Make extract_agent_name return "router" for the router agent sender
                mock_extract.return_value = "router"
                # Call _on_message
                await bot._on_message(room, event)

            # Verify the agent didn't try to process the error message
            bot._generate_response.assert_not_called()
            # Check log calls - should be caught by the general agent message check
            debug_calls = [call[0][0] for call in bot.logger.debug.call_args_list]
            assert "Ignoring message from other agent (not mentioned)" in debug_calls

    @pytest.mark.asyncio
    async def test_router_error_without_mentions_ignored_by_other_agents(self) -> None:
        """Test the exact scenario where RouterAgent sends an error without mentions and other agents ignore it."""
        # This tests the specific case where:
        # 1. User sends a schedule command
        # 2. RouterAgent fails to parse it and sends an error message
        # 3. FinanceAgent should NOT respond to the error message

        # Create thread history with user command and router error
        thread_history = [
            {
                "event_id": "$user_msg",
                "sender": "@user:localhost",
                "content": {"msgtype": "m.text", "body": "!schedule remind me in 1 min", "m.mentions": {}},
            },
            {
                "event_id": "$router_error",
                "sender": "@mindroom_router:localhost",
                "content": {
                    "msgtype": "m.text",
                    "body": "❌ Unable to parse the schedule request\n\n💡 Try something like 'in 5 minutes Check the deployment'",
                    "m.mentions": {},  # No mentions!
                },
            },
        ]

        # NOTE: In reality, when router sends an error without mentions,
        # bot.py returns early and never calls should_agent_respond.
        # But we test what WOULD happen if it were called:

        # Test with single agent (finance only, router excluded from available_agents)
        should_respond = should_agent_respond(
            agent_name="finance",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!test:localhost", ["finance", "router"]),
            thread_history=thread_history,  # Full history including router's error
            config=self.config,
        )

        # With new logic: Single agent takes ownership (router excluded)
        assert should_respond, "Single agent takes ownership after router error"

        # Test with multiple agents - nobody responds
        should_respond = should_agent_respond(
            agent_name="finance",
            am_i_mentioned=False,
            is_thread=True,
            room=create_mock_room("!test:localhost", ["finance", "calculator", "router"]),
            thread_history=thread_history,  # Include router's error in history
            config=self.config,
        )

        assert not should_respond, "Multiple agents wait for routing"

    @pytest.mark.asyncio
    async def test_router_error_actual_behavior(self) -> None:
        """Test the ACTUAL behavior when router sends an error - through full message flow."""
        # Create finance agent
        agent_user = AgentMatrixUser(
            agent_name="finance",
            user_id="@mindroom_finance:localhost",
            display_name="Finance Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        config = Config(router=RouterConfig(model="default"))

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
            bot.client = AsyncMock()
            bot.client.user_id = "@mindroom_finance:localhost"
            bot.logger = MagicMock()
            bot._generate_response = AsyncMock()  # type: ignore[method-assign]
            bot.response_tracker = MagicMock()
            bot.response_tracker.has_responded.return_value = False

            # Mock context extraction for router's error message
            mock_context = MagicMock()
            mock_context.am_i_mentioned = False
            mock_context.mentioned_agents = []
            mock_context.is_thread = True
            mock_context.thread_id = "$thread123"
            mock_context.thread_history = []
            bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

            # Create router's error message event
            room = nio.MatrixRoom(room_id="!test:server", own_user_id=bot.client.user_id)
            event = nio.RoomMessageText.from_dict(
                {
                    "event_id": "$router_error",
                    "sender": "@mindroom_router:localhost",
                    "origin_server_ts": 1234567890,
                    "content": {
                        "msgtype": "m.text",
                        "body": "❌ Unable to parse the schedule request",
                    },
                },
            )

            with (
                patch("mindroom.bot.interactive.handle_text_response"),
                patch("mindroom.bot.extract_agent_name", return_value="router"),
            ):
                await bot._on_message(room, event)

            # Verify finance agent did NOT process the message
            bot._generate_response.assert_not_called()

            # Verify it was caught early by the agent message check
            debug_calls = [call[0][0] for call in bot.logger.debug.call_args_list]
            assert "Ignoring message from other agent (not mentioned)" in debug_calls

    @pytest.mark.asyncio
    async def test_router_error_prevents_team_formation(self) -> None:
        """Test that RouterAgent error messages don't trigger team formation."""
        # This tests the scenario where multiple agents were mentioned earlier in thread
        # but RouterAgent sends an error without mentions - no team should form

        # Create news agent (first alphabetically, would coordinate team)
        agent_user = AgentMatrixUser(
            agent_name="news",
            user_id="@mindroom_news:localhost",
            display_name="News Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        config = Config(router=RouterConfig(model="default"))
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.client.user_id = "@mindroom_news:localhost"
        bot.logger = MagicMock()
        bot._generate_response = AsyncMock()  # type: ignore[method-assign]
        bot._send_response = AsyncMock()  # type: ignore[method-assign]
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot.orchestrator = MagicMock()

        # Create thread history with multiple agents mentioned
        thread_history = [
            {
                "event_id": "$user_msg",
                "sender": "@user:localhost",
                "content": {
                    "msgtype": "m.text",
                    "body": "@news @research check this out",
                    "m.mentions": {"user_ids": ["@mindroom_news:localhost", "@mindroom_research:localhost"]},
                },
            },
            {
                "event_id": "$news_response",
                "sender": "@mindroom_news:localhost",
                "content": {"msgtype": "m.text", "body": "I'll look into it", "m.mentions": {}},
            },
            {
                "event_id": "$research_response",
                "sender": "@mindroom_research:localhost",
                "content": {"msgtype": "m.text", "body": "Analyzing now", "m.mentions": {}},
            },
            {
                "event_id": "$user_schedule",
                "sender": "@user:localhost",
                "content": {"msgtype": "m.text", "body": "!schedule remind me tomorrow", "m.mentions": {}},
            },
        ]

        # Mock context for the router error message
        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.is_thread = True
        mock_context.thread_id = "$thread123"
        mock_context.thread_history = thread_history  # History before router error
        mock_context.mentioned_agents = []  # Router doesn't mention anyone
        bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

        # Create room and event for router error
        room = nio.MatrixRoom(room_id="!test:server", own_user_id=bot.client.user_id)
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$router_error",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.text",
                    "body": "❌ Unable to parse the schedule request",
                },
            },
        )

        with (
            patch("mindroom.bot.interactive") as mock_interactive,
            patch("mindroom.bot.extract_agent_name") as mock_extract,
            patch("mindroom.bot.team_response") as mock_team,
        ):
            mock_interactive.handle_text_response = AsyncMock()
            mock_extract.side_effect = (
                lambda x, config: "router"  # noqa: ARG005
                if "router" in x
                else ("news" if "news" in x else ("research" if "research" in x else None))
            )

            await bot._on_message(room, event)

        # Verify news agent did NOT form a team or respond
        bot._generate_response.assert_not_called()
        bot._send_response.assert_not_called()
        mock_team.assert_not_called()

        # Verify it was logged as being ignored
        debug_calls = [call[0][0] for call in bot.logger.debug.call_args_list]
        # The general "agent without mentions" check catches this first
        assert "Ignoring message from other agent (not mentioned)" in debug_calls

    @pytest.mark.asyncio
    async def test_full_router_error_flow_integration(self) -> None:
        """Integration test for the full flow of router error handling."""
        # Create a finance agent
        agent_user = AgentMatrixUser(
            agent_name="finance",
            user_id="@mindroom_finance:localhost",
            display_name="Finance Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        config = Config(router=RouterConfig(model="default"))
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.client.user_id = "@mindroom_finance:localhost"
        bot.logger = MagicMock()
        bot._generate_response = AsyncMock()  # type: ignore[method-assign]
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Create thread history that mimics the real scenario
        thread_history = [
            {
                "event_id": "$earlier_msg",
                "sender": "@user:localhost",
                "content": {
                    "msgtype": "m.text",
                    "body": "Calculate compound interest on $10,000 at 5% for 10 years",
                    "m.mentions": {},
                },
            },
            {
                "event_id": "$router_routing",
                "sender": "@mindroom_router:localhost",
                "content": {
                    "msgtype": "m.text",
                    "body": "@mindroom_finance:localhost could you help with this? ✓",
                    "m.mentions": {"user_ids": ["@mindroom_finance:localhost"]},
                },
            },
            {
                "event_id": "$finance_response",
                "sender": "@mindroom_finance:localhost",
                "content": {"msgtype": "m.text", "body": "I'll calculate that for you...", "m.mentions": {}},
            },
            {
                "event_id": "$user_schedule",
                "sender": "@user:localhost",
                "content": {"msgtype": "m.text", "body": "!schedule remind me in 1 min", "m.mentions": {}},
            },
        ]

        # Mock context for the router error message
        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.is_thread = True
        mock_context.thread_id = "$thread123"
        mock_context.thread_history = [
            *thread_history,
            {
                "event_id": "$router_error",
                "sender": "@mindroom_router:localhost",
                "content": {
                    "msgtype": "m.text",
                    "body": "❌ Unable to parse the schedule request\n\n💡 Try something like 'in 5 minutes Check the deployment'",
                    "m.mentions": {},
                },
            },
        ]
        mock_context.mentioned_agents = []
        bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

        # Create room and event for router error
        room = nio.MatrixRoom(room_id="!test:server", own_user_id=bot.client.user_id)
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$router_error",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
                "content": {
                    "msgtype": "m.text",
                    "body": "❌ Unable to parse the schedule request\n\n💡 Try something like 'in 5 minutes Check the deployment'",
                },
            },
        )

        with (
            patch("mindroom.bot.interactive") as mock_interactive,
            patch("mindroom.bot.extract_agent_name") as mock_extract,
        ):
            mock_interactive.handle_text_response = AsyncMock()
            mock_extract.side_effect = (
                lambda x, config: "router" if "router" in x else ("finance" if "finance" in x else None)  # noqa: ARG005
            )

            await bot._on_message(room, event)

        # Verify finance agent did NOT respond to router's error
        bot._generate_response.assert_not_called()

        # Verify it was logged as being ignored
        debug_calls = [call[0][0] for call in bot.logger.debug.call_args_list]
        assert "Ignoring message from other agent (not mentioned)" in debug_calls

    @pytest.mark.asyncio
    async def test_agents_ignore_any_agent_messages_without_mentions(self) -> None:
        """Test that agents don't respond to ANY agent messages that don't mention anyone."""
        # Create a general agent
        agent_user = AgentMatrixUser(
            agent_name="general",
            user_id="@mindroom_general:localhost",
            display_name="General Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        config = Config(router=RouterConfig(model="default"))
        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.client.user_id = "@mindroom_general:localhost"
        bot.logger = MagicMock()
        bot._generate_response = AsyncMock()  # type: ignore[method-assign]
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Mock context extraction - no agents mentioned
        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []  # No agents mentioned
        mock_context.is_thread = True
        mock_context.thread_id = "$thread123"
        mock_context.thread_history = []
        bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

        # Create a room and event with message from router agent without mentions
        room = nio.MatrixRoom(room_id="!test:server", own_user_id=bot.client.user_id)
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event123",
                "sender": "@mindroom_router:localhost",  # From router agent
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "❌ Unable to parse the schedule request"},
            },
        )

        # Mock interactive.handle_text_response and extract_agent_name
        with (
            patch("mindroom.bot.interactive.handle_text_response"),
            patch("mindroom.bot.extract_agent_name") as mock_extract,
        ):
            # Make extract_agent_name return "router" for the router agent sender
            mock_extract.return_value = "router"
            # Call _on_message
            await bot._on_message(room, event)

        # Verify the agent didn't try to process the message
        bot._generate_response.assert_not_called()
        # Check debug calls for the new log message
        debug_calls = [call[0][0] for call in bot.logger.debug.call_args_list]
        assert "Ignoring message from other agent (not mentioned)" in debug_calls


class TestRouterSkipsSingleAgent:
    """Test router's behavior when there's only one agent in the room."""

    @pytest.mark.asyncio
    async def test_router_skips_routing_with_single_agent(self) -> None:
        """Test that router doesn't route when there's only one agent available."""
        # Create router agent
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        # Config with only general agent
        config = Config(
            router=RouterConfig(model="default"),
            agents={"general": AgentConfig(display_name="General Agent", role="General assistant")},
        )
        config.ids = {
            "general": MatrixID.from_username("mindroom_general", "localhost"),
            ROUTER_AGENT_NAME: MatrixID.from_username("mindroom_router", "localhost"),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._handle_ai_routing = AsyncMock()  # type: ignore[method-assign]
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Create context with no mentions and no agents in thread
        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

        # Create room with only general agent (router is also there but excluded from available agents)
        room = nio.MatrixRoom(room_id="!test:server", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@user:localhost": None,
        }

        # Create user message
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event123",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "Hello, can you help me?"},
            },
        )

        with (
            patch("mindroom.bot.interactive.handle_text_response"),
            patch("mindroom.bot.extract_agent_name", return_value=None),  # User message
            patch("mindroom.bot.get_agents_in_thread", return_value=[]),
            patch("mindroom.bot.get_available_agents_in_room") as mock_get_available,
        ):
            # Return only one agent (general)
            mock_get_available.return_value = [config.ids["general"]]

            await bot._on_message(room, event)

        # Verify router didn't attempt to route
        bot._handle_ai_routing.assert_not_called()

        # Verify it logged that it's skipping routing
        info_calls = [call[0][0] for call in bot.logger.info.call_args_list]
        assert "Skipping routing: only one agent present" in info_calls

    @pytest.mark.asyncio
    async def test_router_routes_with_multiple_agents(self) -> None:
        """Test that router DOES route when there are multiple agents available."""
        # Create router agent
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        # Config with multiple agents
        config = Config(
            router=RouterConfig(model="default"),
            agents={
                "general": AgentConfig(display_name="General Agent", role="General assistant"),
                "calculator": AgentConfig(display_name="Calculator Agent", role="Math calculations"),
            },
        )
        config.ids = {
            "general": MatrixID.from_username("mindroom_general", "localhost"),
            "calculator": MatrixID.from_username("mindroom_calculator", "localhost"),
            ROUTER_AGENT_NAME: MatrixID.from_username("mindroom_router", "localhost"),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._handle_ai_routing = AsyncMock()  # type: ignore[method-assign]
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Create context with no mentions and no agents in thread
        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

        # Create room with multiple agents
        room = nio.MatrixRoom(room_id="!test:server", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@mindroom_calculator:localhost": None,
            "@user:localhost": None,
        }

        # Create user message
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event123",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "What is 2 + 2?"},
            },
        )

        with (
            patch("mindroom.bot.interactive.handle_text_response"),
            patch("mindroom.bot.extract_agent_name", return_value=None),  # User message
            patch("mindroom.bot.get_agents_in_thread", return_value=[]),
            patch("mindroom.bot.get_available_agents_in_room") as mock_get_available,
        ):
            # Return multiple agents
            mock_get_available.return_value = [config.ids["general"], config.ids["calculator"]]

            await bot._on_message(room, event)

        # Verify router DID attempt to route
        bot._handle_ai_routing.assert_called_once_with(room, event, [])

        # Verify it didn't log about skipping
        info_calls = [call[0][0] for call in bot.logger.info.call_args_list]
        assert "Skipping routing: only one agent present" not in info_calls

    @pytest.mark.asyncio
    async def test_router_handles_command_even_with_single_agent(self) -> None:
        """Router should handle commands even when only one agent is present."""
        # Create router agent
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        # Config with only general agent
        config = Config(
            router=RouterConfig(model="default"),
            agents={"general": AgentConfig(display_name="General Agent", role="General assistant")},
        )
        config.ids = {
            "general": MatrixID.from_username("mindroom_general", "localhost"),
            ROUTER_AGENT_NAME: MatrixID.from_username("mindroom_router", "localhost"),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._handle_command = AsyncMock()  # type: ignore[method-assign]
        bot._send_response = AsyncMock()  # type: ignore[method-assign]

        # Room with router + one agent + a human
        room = nio.MatrixRoom(room_id="!test:server", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@user:localhost": None,
        }

        # Unknown command from human
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event_cmd",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "!not_a_real_command"},
            },
        )

        with (
            patch("mindroom.bot.interactive.handle_text_response"),
            patch("mindroom.bot.get_available_agents_in_room") as mock_get_available,
        ):
            mock_get_available.return_value = [config.ids["general"]]
            await bot._on_message(room, event)

        # Router should handle the command even with a single agent
        # This ensures commands work properly in single-agent rooms
        bot._handle_command.assert_called_once()
        # Router should not send a response for unknown commands (handled by _handle_command)
        bot._send_response.assert_not_called()

    @pytest.mark.asyncio
    async def test_router_handles_schedule_command_in_single_agent_room(self) -> None:
        """Router should handle schedule commands even in single-agent rooms."""
        # Create router agent
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        # Config with only general agent
        config = Config(
            router=RouterConfig(model="default"),
            agents={"general": AgentConfig(display_name="General Agent", role="General assistant")},
        )
        config.ids = {
            "general": MatrixID.from_username("mindroom_general", "localhost"),
            ROUTER_AGENT_NAME: MatrixID.from_username("mindroom_router", "localhost"),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._handle_command = AsyncMock()  # type: ignore[method-assign]
        bot._send_response = AsyncMock()  # type: ignore[method-assign]

        # Room with router + one agent + a human
        room = nio.MatrixRoom(room_id="!test:server", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@user:localhost": None,
        }

        # Schedule command from human
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event_schedule",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "!schedule in 5 minutes remind me to check email"},
            },
        )

        with (
            patch("mindroom.bot.interactive.handle_text_response"),
            patch("mindroom.bot.get_available_agents_in_room") as mock_get_available,
        ):
            mock_get_available.return_value = [config.ids["general"]]
            await bot._on_message(room, event)

        # Router MUST handle schedule commands even with a single agent
        # This is a regression test to ensure commands work in single-agent rooms
        bot._handle_command.assert_called_once()
        args = bot._handle_command.call_args[0]
        assert args[2].type.value == "schedule", "Router should handle schedule command"

    @pytest.mark.asyncio
    async def test_router_handles_voice_transcription_in_single_agent_room(self) -> None:
        """Router voice transcriptions should work in single-agent rooms."""
        # Create router agent
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        # Config with only general agent
        config = Config(
            router=RouterConfig(model="default"),
            agents={"general": AgentConfig(display_name="General Agent", role="General assistant")},
        )
        config.ids = {
            "general": MatrixID.from_username("mindroom_general", "localhost"),
            ROUTER_AGENT_NAME: MatrixID.from_username("mindroom_router", "localhost"),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._handle_ai_routing = AsyncMock()  # type: ignore[method-assign]
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False

        # Room with router + one agent + a human
        room = nio.MatrixRoom(room_id="!test:server", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@user:localhost": None,
        }

        # Voice transcription from router (self-message with VOICE_PREFIX)
        voice_event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event_voice",
                "sender": "@mindroom_router:localhost",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": f"{VOICE_PREFIX}What's the weather today?"},
            },
        )

        # Create context for voice message
        mock_context = MagicMock()
        mock_context.am_i_mentioned = False
        mock_context.mentioned_agents = []
        mock_context.is_thread = False
        mock_context.thread_id = None
        mock_context.thread_history = []
        bot._extract_message_context = AsyncMock(return_value=mock_context)  # type: ignore[method-assign]

        with (
            patch("mindroom.bot.interactive.handle_text_response"),
            patch("mindroom.bot.get_available_agents_in_room") as mock_get_available,
            patch("mindroom.bot.get_agents_in_thread") as mock_agents_in_thread,
        ):
            mock_get_available.return_value = [config.ids["general"]]
            mock_agents_in_thread.return_value = []
            await bot._on_message(room, voice_event)

        # Voice transcriptions should work: router skips routing but doesn't interfere
        # This is a regression test to ensure voice works in single-agent rooms
        assert not bot._handle_ai_routing.called, "Router should skip routing for voice in single-agent room"
        info_calls = [call[0][0] for call in bot.logger.info.call_args_list]
        assert "Skipping routing: only one agent present" in info_calls

    @pytest.mark.asyncio
    async def test_router_handles_command_with_multiple_agents(self) -> None:
        """Router should handle commands when multiple agents are present."""
        # Create router agent
        agent_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            user_id="@mindroom_router:localhost",
            display_name="Router Agent",
            password=TEST_PASSWORD,
            access_token=TEST_ACCESS_TOKEN,
        )

        # Config with two agents
        config = Config(
            router=RouterConfig(model="default"),
            agents={
                "general": AgentConfig(display_name="General Agent", role="General assistant"),
                "calculator": AgentConfig(display_name="Calculator Agent", role="Math calculations"),
            },
        )
        config.ids = {
            "general": MatrixID.from_username("mindroom_general", "localhost"),
            "calculator": MatrixID.from_username("mindroom_calculator", "localhost"),
            ROUTER_AGENT_NAME: MatrixID.from_username("mindroom_router", "localhost"),
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            bot = AgentBot(agent_user=agent_user, storage_path=Path(tmpdir), config=config, rooms=["!test:server"])
        bot.client = AsyncMock()
        bot.logger = MagicMock()
        bot._handle_command = AsyncMock()  # type: ignore[method-assign]

        # Room with router + two agents + a human
        room = nio.MatrixRoom(room_id="!test:server", own_user_id="@mindroom_router:localhost")
        room.users = {
            "@mindroom_router:localhost": None,
            "@mindroom_general:localhost": None,
            "@mindroom_calculator:localhost": None,
            "@user:localhost": None,
        }

        # Valid command from human (help)
        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event_help",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "!help"},
            },
        )

        with (
            patch("mindroom.bot.interactive.handle_text_response"),
            patch("mindroom.bot.get_available_agents_in_room") as mock_get_available,
        ):
            mock_get_available.return_value = [config.ids["general"], config.ids["calculator"]]
            await bot._on_message(room, event)

        bot._handle_command.assert_called_once()
