"""Tests for Direct Message (DM) functionality."""

from __future__ import annotations

from pathlib import Path  # noqa: TC003
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest

from mindroom.bot import AgentBot, MultiAgentOrchestrator
from mindroom.config import AgentConfig, Config
from mindroom.matrix.client import create_dm_room
from mindroom.matrix.event_info import EventInfo
from mindroom.matrix.identity import MatrixID
from mindroom.matrix.users import AgentMatrixUser
from mindroom.thread_utils import should_agent_respond
from tests.conftest import TEST_PASSWORD


@pytest.mark.asyncio
class TestDMRoomCreation:
    """Test DM room creation utilities."""

    async def test_create_dm_room_single_agent(self) -> None:
        """Test creating a DM room with a single agent."""
        client = AsyncMock()
        client.room_create = AsyncMock(return_value=nio.RoomCreateResponse(room_id="!test_dm:localhost"))

        room_id = await create_dm_room(
            client,
            invite_user_ids=["@user:localhost", "@agent:localhost"],
            name="DM with agent",
        )

        assert room_id == "!test_dm:localhost"
        client.room_create.assert_called_once()

        # Check the room config
        call_args = client.room_create.call_args
        assert call_args.kwargs["preset"] == "trusted_private_chat"
        assert call_args.kwargs["is_direct"] is True
        assert "@user:localhost" in call_args.kwargs["invite"]
        assert "@agent:localhost" in call_args.kwargs["invite"]

    async def test_create_dm_room_multiple_agents(self) -> None:
        """Test creating a DM room with multiple agents."""
        client = AsyncMock()
        client.room_create = AsyncMock(return_value=nio.RoomCreateResponse(room_id="!group_dm:localhost"))

        room_id = await create_dm_room(
            client,
            invite_user_ids=["@user:localhost", "@agent1:localhost", "@agent2:localhost"],
            name="Group DM",
        )

        assert room_id == "!group_dm:localhost"
        assert len(client.room_create.call_args.kwargs["invite"]) == 3

    async def test_create_dm_room_failure(self) -> None:
        """Test handling DM room creation failure."""
        client = AsyncMock()
        client.room_create = AsyncMock(return_value=nio.RoomCreateError(message="Failed"))

        room_id = await create_dm_room(client, ["@user:localhost"], "Test DM")
        assert room_id is None


class TestDMResponseLogic:
    """Test agent response logic in DM rooms."""

    def test_should_respond_in_dm_mode_no_mention(self) -> None:
        """Test that agents respond in DM mode without mentions."""
        config = Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", role="Test")},
        )

        # Mock room with single agent - use the correct domain from config
        room = MagicMock()
        room.room_id = "!dm:localhost"
        # Use the actual MatrixID from config to ensure domain matches
        agent_matrix_id = config.ids["test_agent"].full_id
        room.users = {agent_matrix_id: None}

        # In DM mode, agent should respond when no one else has
        should_respond = should_agent_respond(
            agent_name="test_agent",
            am_i_mentioned=False,  # Not mentioned
            is_thread=False,
            room=room,
            thread_history=[],  # No previous messages
            config=config,
            mentioned_agents=None,  # No agents mentioned
        )

        assert should_respond is True

    def test_should_respond_in_dm_mode_when_mentioned(self) -> None:
        """Test that agents respond in DM mode when mentioned."""
        config = Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", role="Test")},
        )

        # Mock room with single agent - use the correct domain from config
        room = MagicMock()
        room.room_id = "!dm:localhost"
        # Use the actual MatrixID from config to ensure domain matches
        agent_matrix_id = config.ids["test_agent"].full_id
        room.users = {agent_matrix_id: None}

        # When mentioned, always respond
        should_respond = should_agent_respond(
            agent_name="test_agent",
            am_i_mentioned=True,  # Mentioned
            is_thread=False,
            room=room,
            thread_history=[],
            config=config,
        )

        assert should_respond is True

    def test_should_not_respond_in_dm_mode_when_other_mentioned(self) -> None:
        """Test that agents don't respond when other agents are mentioned."""
        config = Config(
            agents={
                "test_agent": AgentConfig(display_name="Test Agent", role="Test"),
                "other_agent": AgentConfig(display_name="Other Agent", role="Other"),
            },
        )

        # Mock room with multiple agents - use the correct domains from config
        room = MagicMock()
        room.room_id = "!dm:localhost"
        test_agent_id = config.ids["test_agent"].full_id
        other_agent_id = config.ids["other_agent"].full_id
        room.users = {test_agent_id: None, other_agent_id: None}

        # Another agent is mentioned, not this one
        should_respond = should_agent_respond(
            agent_name="test_agent",
            am_i_mentioned=False,
            is_thread=False,
            room=room,
            thread_history=[],
            config=config,
            mentioned_agents=[config.ids["other_agent"]],  # Other agent mentioned with correct domain
        )

        assert should_respond is False

    def test_multi_agent_dm_does_not_respond_individually(self) -> None:
        """Test that multiple agents in DM room without mentions don't respond individually."""
        config = Config(
            agents={
                "test_agent": AgentConfig(display_name="Test Agent", role="Test"),
                "other_agent": AgentConfig(display_name="Other Agent", role="Other"),
            },
        )

        # Mock room with multiple agents - use the correct domains from config
        room = MagicMock()
        room.room_id = "!dm:localhost"
        test_agent_id = config.ids["test_agent"].full_id
        other_agent_id = config.ids["other_agent"].full_id
        room.users = {test_agent_id: None, other_agent_id: None}

        # No mentions - agents should not respond individually (team formation happens at a higher level)
        should_respond_test = should_agent_respond(
            agent_name="test_agent",
            am_i_mentioned=False,
            is_thread=False,
            room=room,
            thread_history=[],
            config=config,
            mentioned_agents=None,  # No agents mentioned
        )

        should_respond_other = should_agent_respond(
            agent_name="other_agent",
            am_i_mentioned=False,
            is_thread=False,
            room=room,
            thread_history=[],
            config=config,
            mentioned_agents=None,  # No agents mentioned
        )

        # Agents should not respond individually - team formation is handled at bot level
        assert should_respond_test is False
        assert should_respond_other is False


@pytest.mark.asyncio
class TestDMMessageContext:
    """Test message context extraction for DMs."""

    async def test_extract_dm_context(self, tmp_path: Path) -> None:
        """Test extracting message context in DM mode."""
        config = Config()

        # Create a bot with mocked components
        # Use the correct MatrixID from config
        test_agent_matrix_id = (
            config.ids["test_agent"] if "test_agent" in config.ids else MatrixID.parse("@mindroom_test_agent:localhost")
        )
        agent_user = AgentMatrixUser(
            agent_name="test_agent",
            user_id=test_agent_matrix_id.full_id,
            display_name="Test Agent",
            password=TEST_PASSWORD,
        )
        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=[],  # Not configured for any rooms
        )

        # Mock the client
        bot.client = AsyncMock()

        # Mock fetch_thread_history - DMs now use threads like regular rooms
        with patch("mindroom.bot.fetch_thread_history", return_value=[]) as mock_fetch:
            # Create a test event
            room = MagicMock()
            room.room_id = "!dm:localhost"
            room.name = "DM Room"

            event = MagicMock()
            event.source = {
                "content": {
                    "body": "Hello agent",
                    "m.mentions": {},
                },
            }
            event.event_id = "test_event"

            # Extract context
            context = await bot._extract_message_context(room, event)

            # DMs now use threads like regular rooms - no special fetch
            # fetch_thread_history should NOT be called for non-thread messages
            mock_fetch.assert_not_called()
            assert not context.is_thread
            assert context.thread_history == []


@pytest.mark.asyncio
class TestDMIntegration:
    """Integration tests for DM functionality."""

    async def test_agent_accepts_dm_invites(self, tmp_path: Path) -> None:
        """Test that agents accept DM invitations when configured."""
        config = Config()

        # Use the correct MatrixID from config
        test_agent_matrix_id = (
            config.ids["test_agent"] if "test_agent" in config.ids else MatrixID.parse("@mindroom_test_agent:localhost")
        )
        agent_user = AgentMatrixUser(
            agent_name="test_agent",
            user_id=test_agent_matrix_id.full_id,
            display_name="Test Agent",
            password=TEST_PASSWORD,
        )

        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=[],
        )

        bot.client = AsyncMock()
        bot.logger = MagicMock()

        # Mock join_room to return success
        with patch("mindroom.bot.join_room", return_value=True) as mock_join:
            room = MagicMock()
            room.room_id = "!dm:localhost"
            event = MagicMock()
            event.sender = "@user:localhost"

            await bot._on_invite(room, event)

            mock_join.assert_called_once()
            bot.logger.info.assert_any_call("Joined room", room_id="!dm:localhost")

    async def test_dm_response_flow(self, tmp_path: Path) -> None:
        """Test the complete flow of responding in a DM."""
        # This is a more complex integration test
        orchestrator = MultiAgentOrchestrator(storage_path=tmp_path)

        config = Config()
        config.agents = {"researcher": MagicMock()}

        # Create and configure a bot
        # Use the correct MatrixID from config
        researcher_matrix_id = (
            config.ids["researcher"] if "researcher" in config.ids else MatrixID.parse("@mindroom_researcher:localhost")
        )
        agent_user = AgentMatrixUser(
            agent_name="researcher",
            user_id=researcher_matrix_id.full_id,
            display_name="Researcher",
            password=TEST_PASSWORD,
        )

        # Important: bot is NOT configured for the DM room
        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=[],  # Empty rooms list - not configured for any room
        )

        bot.client = AsyncMock()
        bot.client.user_id = (
            config.ids["researcher"].full_id if "researcher" in config.ids else "@mindroom_researcher:localhost"
        )
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot.response_tracker.has_responded = MagicMock(return_value=False)
        bot.orchestrator = orchestrator

        # Mock helper functions
        async def mock_handle(*args: object, **kwargs: object) -> None:
            pass

        with (
            patch("mindroom.bot.fetch_thread_history", return_value=[]),
            patch("mindroom.bot.check_agent_mentioned", return_value=([], False)),
            patch("mindroom.matrix.event_info.EventInfo.from_event") as mock_thread_info,
            patch("mindroom.bot._should_skip_mentions", return_value=False),
            patch("mindroom.bot.extract_agent_name", return_value=None),  # User is not an agent
            patch("mindroom.bot.is_dm_room", return_value=True),  # This is a DM room
            patch.object(bot, "_generate_response") as mock_generate,
            patch("mindroom.bot.interactive.handle_text_response", new=mock_handle),
        ):
            # Mock thread info to return no thread
            mock_thread_info.return_value = EventInfo(
                is_thread=False,
                thread_id=None,
                can_be_thread_root=True,
                safe_thread_root=None,
                has_relations=False,
                relation_type=None,
                is_edit=False,
                original_event_id=None,
                is_reply=False,
                reply_to_event_id=None,
                is_reaction=False,
                reaction_key=None,
                reaction_target_event_id=None,
                relates_to_event_id=None,
            )

            # Create a test message event
            room = MagicMock()
            room.room_id = "!dm:localhost"
            room.name = "DM with researcher"
            # Use the correct MatrixID from config
            researcher_id = (
                config.ids["researcher"].full_id if "researcher" in config.ids else "@mindroom_researcher:localhost"
            )
            room.users = {researcher_id: None}  # Single agent in room

            event = MagicMock(spec=nio.RoomMessageText)
            event.body = "Hello researcher, can you help?"
            event.sender = "@user:localhost"
            event.event_id = "test_event"
            event.source = {
                "content": {
                    "body": "Hello researcher, can you help?",
                    "m.mentions": {},
                },
            }

            # Process the message
            await bot._on_message(room, event)

            # Verify the bot decided to respond even though not configured for the room
            mock_generate.assert_called_once()
            call_args = mock_generate.call_args
            assert call_args.kwargs["room_id"] == "!dm:localhost"
            assert call_args.kwargs["prompt"] == "Hello researcher, can you help?"

    async def test_agent_processes_dm_messages_when_not_configured_for_room(self, tmp_path: Path) -> None:
        """Test that agents process messages in DM rooms even when not configured for them."""
        config = Config(
            agents={"test_agent": AgentConfig(display_name="Test Agent", role="Test")},
        )

        # Use the correct MatrixID from config
        test_agent_matrix_id = config.ids["test_agent"]
        agent_user = AgentMatrixUser(
            agent_name="test_agent",
            user_id=test_agent_matrix_id.full_id,
            display_name="Test Agent",
            password=TEST_PASSWORD,
        )

        # Agent is NOT configured for any rooms
        bot = AgentBot(
            agent_user=agent_user,
            storage_path=tmp_path,
            config=config,
            rooms=[],  # Empty - not configured for any room
        )

        bot.client = AsyncMock()
        bot.client.user_id = config.ids["test_agent"].full_id
        bot.response_tracker = MagicMock()
        bot.response_tracker.has_responded.return_value = False
        bot.response_tracker.has_responded = MagicMock(return_value=False)
        bot.logger = MagicMock()

        async def mock_handle(*args: object, **kwargs: object) -> None:
            pass

        with (
            patch("mindroom.bot.fetch_thread_history", return_value=[]),
            patch("mindroom.bot.check_agent_mentioned", return_value=([], False)),
            patch("mindroom.matrix.event_info.EventInfo.from_event") as mock_thread_info,
            patch("mindroom.bot._should_skip_mentions", return_value=False),
            patch("mindroom.bot.extract_agent_name", return_value=None),
            patch("mindroom.bot.is_dm_room", return_value=True),  # This is a DM room
            patch.object(bot, "_generate_response") as mock_generate,
            patch("mindroom.bot.interactive.handle_text_response", new=mock_handle),
        ):
            # Mock thread info to return no thread
            mock_thread_info.return_value = EventInfo(
                is_thread=False,
                thread_id=None,
                can_be_thread_root=True,
                safe_thread_root=None,
                has_relations=False,
                relation_type=None,
                is_edit=False,
                original_event_id=None,
                is_reply=False,
                reply_to_event_id=None,
                is_reaction=False,
                reaction_key=None,
                reaction_target_event_id=None,
                relates_to_event_id=None,
            )

            # Create a test message event in a DM room
            room = MagicMock()
            room.room_id = "!dm:localhost"  # This room is NOT in bot.rooms
            room.name = "DM Room"
            # Use the correct MatrixID from config
            test_agent_id = config.ids["test_agent"].full_id
            room.users = {test_agent_id: None}  # Single agent in room

            event = MagicMock(spec=nio.RoomMessageText)
            event.body = "Hello agent!"
            event.sender = "@user:localhost"
            event.event_id = "test_event"
            event.source = {
                "content": {
                    "body": "Hello agent!",
                    "m.mentions": {},
                },
            }

            # Process the message
            await bot._on_message(room, event)

            # Verify the bot decided to respond in the DM room
            mock_generate.assert_called_once()
            call_args = mock_generate.call_args
            assert call_args[1]["room_id"] == "!dm:localhost"
            assert call_args[1]["prompt"] == "Hello agent!"
