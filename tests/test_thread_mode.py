"""Tests for thread_mode: room configuration and behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock, patch

import nio
import pytest
from pydantic import ValidationError

from mindroom.bot import AgentBot
from mindroom.commands.parsing import Command, CommandType
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

from tests.conftest import TEST_PASSWORD, bind_runtime_paths, runtime_paths_for


def _runtime_bound_config(config: Config, runtime_root: Path | None = None) -> Config:
    """Return a runtime-bound config for thread mode tests."""
    return bind_runtime_paths(config, runtime_root)


def _entity_thread_mode(config: Config, entity_name: str, *, room_id: str | None = None) -> str:
    """Resolve entity thread mode with the config's bound runtime context."""
    return config.get_entity_thread_mode(entity_name, runtime_paths_for(config), room_id=room_id)


def _agent_bot(
    *,
    config: Config,
    agent_user: AgentMatrixUser,
    storage_path: Path,
    rooms: list[str] | None = None,
) -> AgentBot:
    """Construct an agent bot with the test config's bound runtime context."""
    return AgentBot(
        config=config,
        agent_user=agent_user,
        storage_path=storage_path,
        runtime_paths=runtime_paths_for(config),
        rooms=[] if rooms is None else rooms,
    )


def _streaming_response(
    config: Config,
    *,
    room_id: str,
    reply_to_event_id: str | None,
    thread_id: str | None,
    sender_domain: str,
    room_mode: bool = False,
    latest_thread_event_id: str | None = None,
) -> StreamingResponse:
    """Construct a streaming response with the explicit runtime bound to the test config."""
    return StreamingResponse(
        room_id=room_id,
        reply_to_event_id=reply_to_event_id,
        thread_id=thread_id,
        sender_domain=sender_domain,
        config=config,
        runtime_paths=runtime_paths_for(config),
        room_mode=room_mode,
        latest_thread_event_id=latest_thread_event_id,
    )


@pytest.fixture
def room_mode_config() -> Config:
    """Config with one agent in room mode and one in default thread mode."""
    return _runtime_bound_config(
        Config(
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
        ),
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

    def test_room_thread_modes_override(self) -> None:
        """Per-room thread mode overrides should parse and persist."""
        agent = AgentConfig(
            display_name="Test",
            thread_mode="thread",
            room_thread_modes={"lobby": "room", "!room:localhost": "thread"},
        )
        assert agent.room_thread_modes == {"lobby": "room", "!room:localhost": "thread"}

    def test_invalid_room_thread_mode_rejected(self) -> None:
        """Invalid room_thread_modes values should be rejected by Pydantic."""
        with pytest.raises(ValidationError):
            AgentConfig(display_name="Test", room_thread_modes={"lobby": "invalid"})


class TestConfigThreadModeResolution:
    """Test thread-mode resolution for non-agent entities."""

    def test_agent_uses_room_override_for_matching_room(self) -> None:
        """Agent should honor room-specific thread mode overrides."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, "assistant", room_id="!room:localhost") == "room"
        assert _entity_thread_mode(config, "assistant", room_id="!other:localhost") == "thread"

    def test_router_inherits_uniform_room_mode(self) -> None:
        """Router should use room mode when all configured agents use room mode."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(display_name="Assistant", thread_mode="room"),
                    "coder": AgentConfig(display_name="Coder", thread_mode="room"),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, ROUTER_AGENT_NAME) == "room"

    def test_team_uses_member_mode_when_uniform(self) -> None:
        """Team should inherit room mode when all member agents are room mode."""
        config = _runtime_bound_config(
            Config(
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
            ),
        )
        assert _entity_thread_mode(config, "ops") == "room"

    def test_team_defaults_to_thread_when_members_mixed(self) -> None:
        """Team should default to thread mode when member modes differ."""
        config = _runtime_bound_config(
            Config(
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
            ),
        )
        assert _entity_thread_mode(config, "ops") == "thread"

    def test_team_uses_room_specific_member_modes(self) -> None:
        """Team should resolve member modes with room-specific overrides."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                    "coder": AgentConfig(
                        display_name="Coder",
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
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
            ),
        )
        assert _entity_thread_mode(config, "ops", room_id="!room:localhost") == "room"
        assert _entity_thread_mode(config, "ops", room_id="!other:localhost") == "thread"

    def test_router_uses_room_specific_modes_for_room_agents(self) -> None:
        """Router should resolve mode from agents configured for the active room."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        rooms=["!room:localhost"],
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                    "coder": AgentConfig(
                        display_name="Coder",
                        rooms=["!other:localhost"],
                        thread_mode="thread",
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, ROUTER_AGENT_NAME, room_id="!room:localhost") == "room"

    def test_router_uses_team_room_agents_for_room_mode_resolution(self) -> None:
        """Router should include agents brought into a room via team room mapping."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        thread_mode="thread",
                        room_thread_modes={"!team-room:localhost": "room"},
                    ),
                    "coder": AgentConfig(
                        display_name="Coder",
                        rooms=["!other:localhost"],
                        thread_mode="thread",
                    ),
                },
                teams={
                    "ops": TeamConfig(
                        display_name="Ops Team",
                        role="Operations",
                        agents=["assistant"],
                        rooms=["!team-room:localhost"],
                    ),
                },
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        assert _entity_thread_mode(config, ROUTER_AGENT_NAME, room_id="!team-room:localhost") == "room"


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
        bot = _agent_bot(config=room_mode_config, agent_user=router_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        bot.response_tracker = MagicMock()
        captured_content: dict[str, object] = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> str:
            captured_content.clear()
            captured_content.update(content)
            return "$reply"

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"

        # Mixed agent modes keep the router itself in thread mode.
        assert _entity_thread_mode(bot.config, ROUTER_AGENT_NAME, room_id=room.room_id) == "thread"

        with (
            patch("mindroom.bot.suggest_agent_for_message", AsyncMock(return_value="assistant")),
            patch("mindroom.bot.send_message", side_effect=mock_send),
            patch("mindroom.bot.get_latest_thread_event_id_if_needed", new_callable=AsyncMock) as mock_get_latest,
        ):
            await bot._handle_ai_routing(
                room,
                self._routing_event(),
                thread_history=[],
                thread_id="$thread_root",
            )
        mock_get_latest.assert_not_called()
        assert "m.relates_to" not in captured_content

    @pytest.mark.asyncio
    async def test_router_handoff_uses_suggested_thread_mode(
        self,
        room_mode_config: Config,
        router_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Router should keep thread replies when the suggested agent is thread-mode."""
        bot = _agent_bot(config=room_mode_config, agent_user=router_user, storage_path=tmp_path)
        bot.client = AsyncMock()
        bot.response_tracker = MagicMock()
        captured_content: dict[str, object] = {}

        async def mock_send(_client: object, _room_id: str, content: dict) -> str:
            captured_content.clear()
            captured_content.update(content)
            return "$reply"

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"

        with (
            patch("mindroom.bot.suggest_agent_for_message", AsyncMock(return_value="coder")),
            patch("mindroom.bot.send_message", side_effect=mock_send),
            patch(
                "mindroom.bot.get_latest_thread_event_id_if_needed",
                new_callable=AsyncMock,
                return_value="$latest",
            ) as mock_get_latest,
        ):
            await bot._handle_ai_routing(
                room,
                self._routing_event(),
                thread_history=[],
                thread_id="$thread_root",
            )
        mock_get_latest.assert_awaited_once()
        assert "m.relates_to" in captured_content
        assert isinstance(captured_content["m.relates_to"], dict)
        assert captured_content["m.relates_to"].get("rel_type") == "m.thread"


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
        bot = _agent_bot(config=room_mode_config, agent_user=assistant_user, storage_path=tmp_path)
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

    @pytest.mark.asyncio
    async def test_room_override_skips_derive_only_for_matching_room(
        self,
        assistant_user: AgentMatrixUser,
        tmp_path: Path,
    ) -> None:
        """Room-specific mode overrides should only affect matching rooms."""
        config = _runtime_bound_config(
            Config(
                agents={
                    "assistant": AgentConfig(
                        display_name="Assistant",
                        rooms=["!room:localhost", "!other:localhost"],
                        thread_mode="thread",
                        room_thread_modes={"!room:localhost": "room"},
                    ),
                },
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
        )
        bot = _agent_bot(config=config, agent_user=assistant_user, storage_path=tmp_path)
        bot.client = MagicMock()
        bot._derive_conversation_context = AsyncMock(return_value=(True, "$thread123", [{"event_id": "$thread123"}]))

        room = MagicMock(spec=nio.MatrixRoom)
        room.room_id = "!room:localhost"
        room.name = "Room Override"

        other_room = MagicMock(spec=nio.MatrixRoom)
        other_room.room_id = "!other:localhost"
        other_room.name = "No Override"

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
            room_mode_ctx = await bot._extract_message_context(room, event)
            thread_mode_ctx = await bot._extract_message_context(other_room, event)

        assert room_mode_ctx.is_thread is False
        assert room_mode_ctx.thread_id is None
        assert room_mode_ctx.thread_history == []

        assert thread_mode_ctx.is_thread is True
        assert thread_mode_ctx.thread_id == "$thread123"
        assert thread_mode_ctx.thread_history == [{"event_id": "$thread123"}]


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
        bot = _agent_bot(config=room_mode_config, agent_user=assistant_user, storage_path=tmp_path)
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
        return _runtime_bound_config(
            Config(
                agents={},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test")},
                router=RouterConfig(model="default"),
            ),
        )

    def test_room_mode_field_default(self, streaming_config: Config) -> None:
        """StreamingResponse should default room_mode to False."""
        sr = _streaming_response(
            streaming_config,
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
        )
        assert sr.room_mode is False

    @pytest.mark.asyncio
    async def test_room_mode_no_relations(self, streaming_config: Config) -> None:
        """In room mode, _send_or_edit_message should emit no m.relates_to."""
        sr = _streaming_response(
            streaming_config,
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
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
        sr = _streaming_response(
            streaming_config,
            room_id="!room:localhost",
            reply_to_event_id="$event123",
            thread_id="$thread123",
            sender_domain="localhost",
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
        config = _runtime_bound_config(
            Config(
                agents={},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test")},
                router=RouterConfig(model="default"),
            ),
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
                runtime_paths_for(config),
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
        config = _runtime_bound_config(
            Config(
                agents={"assistant": AgentConfig(display_name="Assistant", thread_mode="room")},
                teams={},
                room_models={},
                models={"default": ModelConfig(provider="ollama", id="test-model")},
                router=RouterConfig(model="default"),
            ),
            tmp_path,
        )
        router_user = AgentMatrixUser(
            agent_name=ROUTER_AGENT_NAME,
            password=TEST_PASSWORD,
            display_name="Router",
            user_id="@mindroom_router:localhost",
        )
        bot = _agent_bot(config=config, agent_user=router_user, storage_path=tmp_path)
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
            patch("mindroom.commands.handler.check_agent_mentioned", return_value=([], False, False)),
            patch(
                "mindroom.commands.handler.schedule_task",
                new_callable=AsyncMock,
                return_value=("task123", "scheduled"),
            ) as mock_schedule,
        ):
            await bot._handle_command(room, event, command)

        assert mock_schedule.await_args.kwargs["thread_id"] is None
        assert bot._send_response.await_args.args[3] is None
