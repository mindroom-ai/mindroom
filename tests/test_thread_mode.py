"""Tests for thread_mode: room configuration and behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom.bot import AgentBot
from mindroom.commands import Command, CommandType
from mindroom.config.agent import AgentConfig, TeamConfig
from mindroom.config.main import Config
from mindroom.config.models import ModelConfig, RouterConfig
from mindroom.constants import ROUTER_AGENT_NAME
from mindroom.matrix.users import AgentMatrixUser
from mindroom.streaming import StreamingResponse, send_streaming_response
from mindroom.thread_utils import create_session_id

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

from .conftest import TEST_PASSWORD


@pytest.fixture
def room_mode_config() -> Config:
    """Config with one agent in room mode and one in default thread mode."""
    return Config(
        agents={
            "assistant": AgentConfig(
                display_name="Assistant",
                rooms=["!room:localhost"],
                thread_mode="room",
            ),
            "coder": AgentConfig(
                display_name="Coder",
                rooms=["!room:localhost"],
            ),
        },
        teams={},
        room_models={},
        models={"default": ModelConfig(provider="ollama", id="test-model")},
        router=RouterConfig(model="default"),
    )


@pytest.fixture
def assistant_user() -> AgentMatrixUser:
    """Create a mock assistant agent user in room mode."""
    return AgentMatrixUser(
        agent_name="assistant",
        password=TEST_PASSWORD,
        display_name="Assistant",
        user_id="@mindroom_assistant:localhost",
    )


@pytest.fixture
def coder_user() -> AgentMatrixUser:
    """Create a mock coder agent user in default thread mode."""
    return AgentMatrixUser(
        agent_name="coder",
        password=TEST_PASSWORD,
        display_name="Coder",
        user_id="@mindroom_coder:localhost",
    )


class TestThreadModeConfig:
    """Test thread_mode config parsing."""

    def test_default_thread_mode_is_thread(self) -> None:
        """Default thread_mode should be 'thread'."""
        agent = AgentConfig(display_name="Test")
        assert agent.thread_mode == "thread"

    def test_thread_mode_room(self) -> None:
        """Setting thread_mode to 'room' should work."""
        agent = AgentConfig(display_name="Test", thread_mode="room")
        assert agent.thread_mode == "room"

    def test_thread_mode_thread_explicit(self) -> None:
        """Explicitly setting thread_mode to 'thread' should work."""
        agent = AgentConfig(display_name="Test", thread_mode="thread")
        assert agent.thread_mode == "thread"

    def test_invalid_thread_mode_rejected(self) -> None:
        """Invalid thread_mode values should be rejected by Pydantic."""
        with pytest.raises(ValidationError):
            AgentConfig(display_name="Test", thread_mode="invalid")


class TestAgentBotThreadMode:
    """Test AgentBot.thread_mode property."""

    def test_thread_mode_room(
        self,
        room_mode_config: Config,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agent configured with thread_mode=room should report room mode."""
        bot = AgentBot(
            config=room_mode_config,
            agent_user=assistant_user,
            storage_path=tmp_path,
        )
        assert bot.thread_mode == "room"

    def test_thread_mode_default(
        self,
        room_mode_config: Config,
        coder_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Agent with default config should report thread mode."""
        bot = AgentBot(
            config=room_mode_config,
            agent_user=coder_user,
            storage_path=tmp_path,
        )
        assert bot.thread_mode == "thread"


class TestConfigThreadModeResolution:
    """Test thread-mode resolution for non-agent entities."""

    def test_router_inherits_uniform_room_mode(self) -> None:
        """Router should use room mode when all configured agents use room mode."""
        config = Config(
            agents={
                "assistant": AgentConfig(display_name="Assistant", thread_mode="room"),
                "coder": AgentConfig(display_name="Coder", thread_mode="room"),
            },
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )
        assert config.get_entity_thread_mode(ROUTER_AGENT_NAME) == "room"

    def test_team_uses_member_mode_when_uniform(self) -> None:
        """Team should inherit room mode when all member agents are room mode."""
        config = Config(
            agents={
                "assistant": AgentConfig(display_name="Assistant", thread_mode="room"),
                "coder": AgentConfig(display_name="Coder", thread_mode="room"),
            },
            teams={
                "ops": TeamConfig(
                    display_name="Ops Team",
                    role="Operations",
                    agents=["assistant", "coder"],
                ),
            },
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )
        assert config.get_entity_thread_mode("ops") == "room"

    def test_team_defaults_to_thread_when_members_mixed(self) -> None:
        """Team should default to thread mode when member modes differ."""
        config = Config(
            agents={
                "assistant": AgentConfig(display_name="Assistant", thread_mode="room"),
                "coder": AgentConfig(display_name="Coder", thread_mode="thread"),
            },
            teams={
                "ops": TeamConfig(
                    display_name="Ops Team",
                    role="Operations",
                    agents=["assistant", "coder"],
                ),
            },
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )
        assert config.get_entity_thread_mode("ops") == "thread"


class TestRouterHandoffThreadMode:
    """Test router handoff replies follow the suggested entity's thread mode."""

    @pytest.fixture
    def router_user(self) -> AgentMatrixUser:
        """Create a mock router user."""
        return AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            password=TEST_PASSWORD,
            display_name="Router",
            user_id="@mindroom_router:localhost",
        )

    @staticmethod
    def _routing_event() -> MagicMock:
        event = MagicMock(spec=nio.RoomMessageText)
        event.sender = "@user:localhost"
        event.body = "Help me"
        event.event_id = "$user_event"
        event.source = {
            "event_id": "$user_event",
            "sender": "@user:localhost",
            "type": "m.room.message",
            "content": {"body": "Help me", "msgtype": "m.text"},
        }
        return event

    @pytest.mark.asyncio
    async def test_router_handoff_uses_suggested_room_mode(
        self,
        room_mode_config: Config,
        router_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Router should send handoff in-room when the suggested agent is room-mode."""
        bot = AgentBot(config=room_mode_config, agent_user=router_user, storage_path=tmp_path)
        bot.response_tracker = MagicMock()
        bot._send_response = AsyncMock(return_value="$reply")

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"

        # Mixed agent modes keep the router itself in thread mode.
        assert bot.thread_mode == "thread"

        with patch("mindroom.bot.suggest_agent_for_message", AsyncMock(return_value="assistant")):
            await bot._handle_ai_routing(
                room,
                self._routing_event(),
                thread_history=[],
                thread_id="$thread_root",
            )

        assert bot._send_response.await_args.kwargs["thread_id"] is None

    @pytest.mark.asyncio
    async def test_router_handoff_uses_suggested_thread_mode(
        self,
        room_mode_config: Config,
        router_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Router should keep thread replies when the suggested agent is thread-mode."""
        bot = AgentBot(config=room_mode_config, agent_user=router_user, storage_path=tmp_path)
        bot.response_tracker = MagicMock()
        bot._send_response = AsyncMock(return_value="$reply")

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"

        with patch("mindroom.bot.suggest_agent_for_message", AsyncMock(return_value="coder")):
            await bot._handle_ai_routing(
                room,
                self._routing_event(),
                thread_history=[],
                thread_id="$thread_root",
            )

        assert bot._send_response.await_args.kwargs["thread_id"] == "$thread_root"


class TestCreateSessionIdWithNoneThread:
    """Verify create_session_id returns room-level ID when thread_id=None."""

    def test_room_level_session(self) -> None:
        """When thread_id is None, session_id should be just the room_id."""
        session_id = create_session_id("!room:localhost", None)
        assert session_id == "!room:localhost"

    def test_thread_level_session(self) -> None:
        """When thread_id is set, session_id should include it."""
        session_id = create_session_id("!room:localhost", "$thread123")
        assert session_id == "!room:localhost:$thread123"


class TestExtractMessageContextRoomMode:
    """Test _extract_message_context skips thread derivation in room mode."""

    @pytest.mark.asyncio
    async def test_room_mode_skips_derive(
        self,
        room_mode_config: Config,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """In room mode, _extract_message_context should return empty thread context."""
        bot = AgentBot(
            config=room_mode_config,
            agent_user=assistant_user,
            storage_path=tmp_path,
        )
        bot.client = MagicMock()

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.name = "Test Room"

        event = MagicMock(spec=nio.RoomMessageText)
        event.event_id = "$event123"
        event.sender = "@user:localhost"
        event.source = {
            "event_id": "$event123",
            "sender": "@user:localhost",
            "content": {"body": "hello", "msgtype": "m.text"},
            "type": "m.room.message",
        }

        with patch("mindroom.bot.check_agent_mentioned", return_value=([], False, False)):
            ctx = await bot._extract_message_context(room, event)

        assert ctx.is_thread is False
        assert ctx.thread_id is None
        assert ctx.thread_history == []


class TestSendResponseRoomMode:
    """Test _send_response skips thread relation in room mode."""

    @pytest.mark.asyncio
    async def test_room_mode_no_thread_metadata(
        self,
        room_mode_config: Config,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """In room mode, _send_response should not add thread relation metadata."""
        bot = AgentBot(
            config=room_mode_config,
            agent_user=assistant_user,
            storage_path=tmp_path,
        )
        bot.client = AsyncMock()

        captured_content: dict = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> str:
            captured_content.update(content)
            return "$response_event"

        with patch("mindroom.bot.send_message", side_effect=mock_send):
            event_id = await bot._send_response(
                room_id="!room:localhost",
                reply_to_event_id="$event123",
                response_text="Hello!",
                thread_id=None,
            )

        assert event_id == "$response_event"
        # Room mode should NOT have m.relates_to with thread relation
        relates_to = captured_content.get("m.relates_to")
        if relates_to:
            assert relates_to.get("rel_type") != "m.thread"


class TestStreamingResponseRoomMode:
    """Test StreamingResponse skips thread and reply relations when room_mode=True."""

    @pytest.fixture
    def streaming_config(self) -> Config:
        """Minimal config for streaming tests."""
        return Config(
            agents={},
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test")},
            router=RouterConfig(model="default"),
        )

    def test_room_mode_field_default(self, streaming_config: Config) -> None:
        """StreamingResponse should default room_mode to False."""
        sr = StreamingResponse(
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
            config=streaming_config,
        )
        assert sr.room_mode is False

    @pytest.mark.asyncio
    async def test_room_mode_no_relations(self, streaming_config: Config) -> None:
        """In room mode, _send_or_edit_message should emit no m.relates_to."""
        sr = StreamingResponse(
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
            config=streaming_config,
            room_mode=True,
            latest_thread_event_id="$latest",
        )
        sr.accumulated_text = "Hello!"

        captured: dict = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> str:
            captured.update(content)
            return "$sent"

        client = AsyncMock()
        with patch("mindroom.streaming.send_message", side_effect=mock_send):
            await sr._send_or_edit_message(client, is_final=True)

        assert "m.relates_to" not in captured

    @pytest.mark.asyncio
    async def test_thread_mode_has_relations(self, streaming_config: Config) -> None:
        """In default thread mode, _send_or_edit_message should emit m.relates_to."""
        sr = StreamingResponse(
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
            config=streaming_config,
            room_mode=False,
            latest_thread_event_id="$latest",
        )
        sr.accumulated_text = "Hello!"

        captured: dict = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> str:
            captured.update(content)
            return "$sent"

        client = AsyncMock()
        with patch("mindroom.streaming.send_message", side_effect=mock_send):
            await sr._send_or_edit_message(client, is_final=True)

        assert "m.relates_to" in captured
        assert captured["m.relates_to"]["rel_type"] == "m.thread"


class TestSendStreamingResponseRoomMode:
    """Test send_streaming_response skips thread lookup and relations in room mode."""

    @pytest.mark.asyncio
    async def test_room_mode_skips_latest_thread_lookup(self) -> None:
        """In room mode, send_streaming_response should not call get_latest_thread_event_id_if_needed."""
        config = Config(
            agents={},
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test")},
            router=RouterConfig(model="default"),
        )

        async def empty_stream() -> AsyncIterator[str]:
            yield "Hello!"

        client = AsyncMock()

        captured: dict = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> str:
            captured.update(content)
            return "$sent"

        with (
            patch("mindroom.streaming.send_message", side_effect=mock_send),
            patch("mindroom.streaming.get_latest_thread_event_id_if_needed") as mock_get_latest,
        ):
            await send_streaming_response(
                client,
                "!room:localhost",
                "$event123",
                "$thread123",
                "localhost",
                config,
                empty_stream(),
                room_mode=True,
            )

        mock_get_latest.assert_not_called()
        assert "m.relates_to" not in captured


class TestCommandThreadContextRoomMode:
    """Test command handling uses room context in room mode."""

    @pytest.mark.asyncio
    async def test_schedule_command_uses_no_thread_id_in_room_mode(
        self,
        tmp_path: Path,
    ) -> None:
        """Router command scheduling should persist room-level (not thread) context."""
        config = Config(
            agents={"assistant": AgentConfig(display_name="Assistant", thread_mode="room")},
            teams={},
            room_models={},
            models={"default": ModelConfig(provider="ollama", id="test-model")},
            router=RouterConfig(model="default"),
        )
        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            password=TEST_PASSWORD,
            display_name="Router",
            user_id="@mindroom_router:localhost",
        )
        bot = AgentBot(
            config=config,
            agent_user=router_user,
            storage_path=tmp_path,
        )
        bot.client = AsyncMock()
        bot.response_tracker = MagicMock()
        bot._send_response = AsyncMock(return_value="$reply")
        bot._derive_conversation_context = AsyncMock(return_value=(False, None, []))

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"

        event = nio.RoomMessageText.from_dict(
            {
                "event_id": "$event123",
                "sender": "@user:localhost",
                "origin_server_ts": 1234567890,
                "content": {"msgtype": "m.text", "body": "!schedule in 5 minutes ping"},
            },
        )
        command = Command(
            type=CommandType.SCHEDULE,
            args={"full_text": "in 5 minutes ping"},
            raw_text="!schedule in 5 minutes ping",
        )

        with (
            patch("mindroom.command_handler.check_agent_mentioned", return_value=([], False, False)),
            patch(
                "mindroom.command_handler.schedule_task",
                new_callable=AsyncMock,
                return_value=("task123", "scheduled"),
            ) as mock_schedule,
        ):
            await bot._handle_command(room, event, command)

        assert mock_schedule.await_args.kwargs["thread_id"] is None
        assert bot._send_response.await_args.args[3] is None
